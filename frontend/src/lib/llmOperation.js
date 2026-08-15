// The ONE client-side follower of a durable LLM-apply operation (plan-05 D2,
// review §8.4/§8.5). This is the integration seam the "one awaited controller"
// (saveConnect) is built on: it owns the single poll loop for exactly one
// operation, projects each admin status into a UI phase, and enforces the
// exactly-once terminal handoff. Kept framework-agnostic (timers, visibility,
// storage and the poll call are all injected) so it is unit-testable without
// mounting a component, and so the OnboardingView wiring stays a thin shell over
// it - see the header of createOperationController.
//
// The admin half (jarvis_admin_v2.fleet.apply_operation) owns the operation's
// truth; this module never invents state. The frozen 12-key status shape it reads
// is documented in ./__fixtures__/llmOperation.fixtures.js.

// -- contract vocabulary (verbatim from the admin half) -----------------------

export const OP_STATE = Object.freeze({
	PENDING: "pending",
	APPLYING: "applying",
	APPLIED_WAITING_READINESS: "applied_waiting_readiness",
	READY: "ready",
	FAILED: "failed",
	SUPERSEDED: "superseded",
});

const TERMINAL = new Set([OP_STATE.READY, OP_STATE.FAILED, OP_STATE.SUPERSEDED]);

export const OP_CODE = Object.freeze({
	PENDING: "LLM_APPLY_PENDING",
	APPLYING: "LLM_APPLYING",
	APPLIED_READINESS_PENDING: "LLM_APPLIED_READINESS_PENDING",
	READY: "LLM_READY",
	REJECTED: "LLM_APPLY_REJECTED", // permanent
	FAILED: "LLM_APPLY_FAILED", // transient
	BLOCKED_OAUTH: "LLM_APPLY_BLOCKED_OAUTH",
	SUPERSEDED_NEWER: "SUPERSEDED_NEWER_SAVE",
	SUPERSEDED_AUTHORITY: "SUPERSEDED_AUTHORITY_CHANGED",
});

/** True once the operation has reached a durable terminal state. */
export function isTerminal(state) {
	return TERMINAL.has(state);
}

// -- UI projection ------------------------------------------------------------
//
// The phases the CTA/copy render around. Distinct from the raw admin state so the
// view never branches on wire strings and `applied_waiting_readiness` is a
// first-class honest state ("saved, finishing setup"), not a generic spinner
// (review P1-03). "failed" splits by retryability: a transient failure or a
// rate-limit offers a bounded retry OF THE SAME OPERATION; a permanent rejection
// (LLM_APPLY_REJECTED) sends the customer back to fix their input. `superseded`
// means a newer save / an authority change replaced this operation, so the answer
// is to re-follow the current one, never to keep polling this dead id.

export const OP_PHASE = Object.freeze({
	WORKING: "working", // pending / applying — keep polling, show progress
	FINISHING: "finishing", // applied_waiting_readiness — saved, readiness converging
	READY: "ready", // terminal success — navigate exactly once
	RETRY: "retry", // transient failure / rate-limit — bounded retry of this op
	REJECTED: "rejected", // permanent — the input is the problem, edit & re-save
	SUPERSEDED: "superseded", // a newer save / authority change owns the truth now
});

/**
 * Pure projection of an admin status into a UI descriptor. Never navigates, never
 * mutates. The controller and the view both read this so they cannot disagree.
 *
 * @param {object} op - a §8.4 status object (12 keys), or a falsy value.
 * @returns {{phase:string, terminal:boolean, canNavigate:boolean, retryable:boolean,
 *            retryAfterSeconds:number, message:string, chatReadiness:(string|null),
 *            chatReadinessReason:string, code:string, state:string}}
 */
export function classifyOperation(op) {
	const state = (op && op.state) || "";
	const code = (op && op.code) || "";
	const message = (op && op.message) || "";
	const chatReadiness = op ? op.chat_readiness ?? null : null;
	const chatReadinessReason = (op && op.chat_readiness_reason) || "";
	const retryAfterSeconds = Math.max(0, Number((op && op.retry_after_seconds) || 0));

	let phase = OP_PHASE.WORKING;
	if (state === OP_STATE.READY) phase = OP_PHASE.READY;
	else if (state === OP_STATE.APPLIED_WAITING_READINESS) phase = OP_PHASE.FINISHING;
	else if (state === OP_STATE.SUPERSEDED) phase = OP_PHASE.SUPERSEDED;
	else if (state === OP_STATE.FAILED) {
		// The admin marks a permanent rejection non-retryable (code === REJECTED);
		// everything else failed transiently and may be retried as the SAME op.
		phase = code === OP_CODE.REJECTED ? OP_PHASE.REJECTED : OP_PHASE.RETRY;
	}

	// The operation being READY means "your AI connection applied". It does NOT mean
	// "chat works": admin reports the workspace's own verdict separately, on
	// `chat_readiness`, and this projection was reading that field and then ignoring
	// it. So a customer whose model saved cleanly while their container was still
	// coming up was navigated into a chat that could not answer, with no error and
	// no way to tell what had gone wrong. That is the "it went to chat but chat
	// didn't work" report, and it closes here.
	//
	// `false` is the ONLY blocking value. `null`/undefined means admin did not say
	// (an older control plane, or a path that does not compute it), and refusing to
	// navigate on silence would strand every customer on that build. Absent
	// information keeps the old behaviour; a definite "not ready" is now believed.
	const chatBlocked = chatReadiness === false;

	return {
		phase,
		state,
		code,
		terminal: isTerminal(state),
		// Exactly-once navigation is only ever offered on a genuine READY that admin
		// has not explicitly told us is un-chattable yet.
		canNavigate: state === OP_STATE.READY && !chatBlocked,
		// READY, but chat is not up yet. The caller must stay on the setup step and
		// keep waiting rather than treat this as a failure: nothing is wrong, the
		// workspace is simply still coming online.
		awaitingChatReadiness: state === OP_STATE.READY && chatBlocked,
		// Whether the customer can recover by retrying THIS operation (no new desired
		// version). A permanent rejection and a superseded op are not retryable here.
		retryable:
			phase === OP_PHASE.RETRY || phase === OP_PHASE.FINISHING || phase === OP_PHASE.WORKING,
		retryAfterSeconds,
		message,
		chatReadiness,
		chatReadinessReason,
	};
}

// -- active-operation persistence (reload resume) -----------------------------
//
// So a reload mid-apply resumes the SAME operation instead of showing an editable
// form over a running apply (review P1-05) or starting a second poller. Only the
// opaque operation id is persisted - never a credential, never a token.

const STORE_KEY = "jarvis.llm_apply.operation_id";

/** Wrap a Storage (sessionStorage by default; injectable for tests/SSR-safety). */
export function operationStore(storage) {
	const s = storage || (typeof sessionStorage !== "undefined" ? sessionStorage : null);
	return {
		remember(operationId) {
			try {
				if (operationId) s && s.setItem(STORE_KEY, String(operationId));
			} catch {
				/* private-mode / quota: resume is best-effort, never fatal */
			}
		},
		recall() {
			try {
				return (s && s.getItem(STORE_KEY)) || "";
			} catch {
				return "";
			}
		},
		forget() {
			try {
				s && s.removeItem(STORE_KEY);
			} catch {
				/* ignore */
			}
		},
	};
}

// -- the single operation controller ------------------------------------------

const DEFAULTS = {
	baseDelayMs: 1500,
	maxDelayMs: 8000,
	jitterMs: 400,
	// Hard ceiling so a stuck operation cannot poll forever. On expiry the last
	// status is returned with a synthetic `timedOut` flag; the caller renders a
	// support state and never silently declares ready.
	deadlineMs: 5 * 60 * 1000,
};

/**
 * Create the ONE observer for LLM-apply operations. A second follow() supersedes
 * the first (its timers are cancelled and its promise rejects with {aborted:true})
 * so there is never more than one live poll loop or more than one terminal handler
 * - the C05-4 three-poller problem and the duplicate-navigation problem both close
 * here rather than in the view.
 *
 * Injected seams (all optional except poll):
 *   poll(operationId)  -> Promise<status>   (get_llm_apply_operation)
 *   onUpdate(uiDescriptor, rawStatus)       side-effect-free render hook
 *   now()              -> ms                 (Date.now)
 *   setTimer/clearTimer                       (setTimeout/clearTimeout)
 *   isVisible()        -> bool               document visibility (pause when hidden)
 *   onVisible(cb)      -> unsubscribe         visibility-change subscription
 *   store                                     operationStore() (reload resume)
 *   random()           -> [0,1)              jitter source (seedable for tests)
 */
export function createOperationController(opts) {
	const {
		poll,
		onUpdate = () => {},
		now = () => Date.now(),
		setTimer = (fn, ms) => setTimeout(fn, ms),
		clearTimer = (h) => clearTimeout(h),
		isVisible = () => true,
		onVisible = () => () => {},
		store = operationStore(),
		random = Math.random,
		config = {},
	} = opts || {};
	if (typeof poll !== "function") throw new Error("createOperationController requires a poll()");
	const cfg = { ...DEFAULTS, ...config };

	// Monotonic generation: every follow()/abort() bumps it, so a timer or an
	// in-flight poll that resolves after supersession can detect it is stale and
	// refuse to touch the newer run's state (review R5 unmount-abort requirement).
	let generation = 0;
	let timer = null;
	let unsubscribeVisible = null;
	let deadlineAt = 0;
	// The active run's reject, so a supersede/abort ends its promise with
	// {aborted:true} rather than leaving the caller's finishing state hanging.
	let activeReject = null;

	function _clear() {
		if (timer) {
			clearTimer(timer);
			timer = null;
		}
		if (unsubscribeVisible) {
			unsubscribeVisible();
			unsubscribeVisible = null;
		}
	}

	function abort() {
		generation += 1; // invalidate any in-flight poll/timer
		_clear();
		if (activeReject) {
			const rej = activeReject;
			activeReject = null;
			rej({ aborted: true });
		}
	}

	/**
	 * Follow exactly one operation to a terminal state.
	 * @param {object|string} descriptorOrId - the save descriptor (§8.4) or a bare
	 *   operation id (used on reload resume). A descriptor is rendered immediately
	 *   so the UI never flashes empty before the first poll.
	 * @returns {Promise<object>} resolves with the terminal status (a `timedOut`
	 *   flag is set if the deadline was hit, plus `neverConfirmed` - see below);
	 *   rejects {aborted:true} if superseded.
	 */
	function follow(descriptorOrId) {
		abort(); // supersede any prior run - ONE observer
		const myGen = generation;
		const descriptor = typeof descriptorOrId === "string" ? null : descriptorOrId || null;
		const operationId = descriptor ? descriptor.operation_id : String(descriptorOrId || "");
		if (!operationId) return Promise.reject(new Error("follow() needs an operation id"));

		store.remember(operationId);
		deadlineAt = now() + cfg.deadlineMs;
		let attempt = 0;
		// Whether ANY poll of THIS run has ever come back with a real admin answer
		// (jarvis#690). A transport error on the read is correctly absorbed and
		// retried below - but absorbing it silently for the FULL deadline and then
		// telling the customer "it's still finishing on its own" is only honest if
		// we ever actually heard from admin. If every single poll failed, nothing
		// is known to be finishing; the deadline timeout says so via
		// `neverConfirmed` rather than implying progress that was never observed.
		let sawLiveStatus = false;

		// Seed the UI from the save descriptor before the first network poll.
		if (descriptor) onUpdate(classifyOperation(descriptor), descriptor);

		return new Promise((resolve, reject) => {
			activeReject = reject;
			const stale = () => myGen !== generation;

			const settleTerminal = (status) => {
				_clear();
				store.forget();
				if (myGen === generation) activeReject = null;
				resolve(status);
			};

			const scheduleNext = (delayMs) => {
				if (stale()) return;
				timer = setTimer(tick, Math.max(0, delayMs));
			};

			const backoff = () => {
				const capped = Math.min(cfg.baseDelayMs * 2 ** attempt, cfg.maxDelayMs);
				return capped + Math.floor(random() * cfg.jitterMs);
			};

			const tick = async () => {
				if (stale()) return;
				if (!isVisible()) {
					// Paused: resume on the next visibility change rather than burning polls
					// against a backgrounded tab. Exactly one resume subscription at a time.
					if (!unsubscribeVisible) {
						unsubscribeVisible = onVisible(() => {
							if (stale()) return;
							if (unsubscribeVisible) {
								unsubscribeVisible();
								unsubscribeVisible = null;
							}
							scheduleNext(0);
						});
					}
					return;
				}
				if (now() >= deadlineAt) {
					if (stale()) return;
					const timedOut = {
						operation_id: operationId,
						state: "",
						code: "",
						timedOut: true,
						// jarvis#690: never having heard from admin is a different, more
						// honest story than "still finishing" - see sawLiveStatus above.
						neverConfirmed: !sawLiveStatus,
					};
					onUpdate(
						{
							...classifyOperation(null),
							phase: OP_PHASE.RETRY,
							timedOut: true,
							neverConfirmed: !sawLiveStatus,
						},
						timedOut
					);
					settleTerminal(timedOut);
					return;
				}

				let status;
				try {
					status = await poll(operationId);
				} catch (err) {
					if (stale()) return;
					// A transport error on the READ is not a verdict: keep polling with
					// backoff until the deadline. The status read never spends the mutation
					// bucket, so re-reading is safe.
					attempt += 1;
					scheduleNext(backoff());
					onUpdate(
						{ ...classifyOperation(null), phase: OP_PHASE.WORKING, pollError: true },
						null
					);
					void err;
					return;
				}
				if (stale()) return;
				sawLiveStatus = true;

				const ui = classifyOperation(status);
				onUpdate(ui, status);

				if (ui.terminal) {
					settleTerminal(status);
					return;
				}
				// Honour a per-operation cooldown (rate-limit bound to THIS op): wait the
				// server's retry_after before the next read, never resubmit, never mint a
				// new desired version.
				attempt += 1;
				const wait = ui.retryAfterSeconds > 0 ? ui.retryAfterSeconds * 1000 : backoff();
				scheduleNext(wait);
			};

			// First poll immediately. Supersession/abort ends this promise via
			// abort()'s stored-reject path ({aborted:true}); the stale() guard keeps a
			// late timer or in-flight poll from touching the newer run's state.
			scheduleNext(0);
		});
	}

	return {
		follow,
		abort,
		get generation() {
			return generation;
		},
	};
}
