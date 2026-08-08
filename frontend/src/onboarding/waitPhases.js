/**
 * Copy for the two long onboarding waits, derived ONLY from what the wait loop
 * actually observed.
 *
 * Both waits already receive a real phase signal on every poll and throw it
 * away:
 *
 *   - the provisioning poll (usePaymentFlow.runProvisioningPoll) gets
 *     `tenant_status` back from jarvis.onboarding.sync_connection on every one
 *     of its 45 ticks, and only ever tests it for "running";
 *   - the readiness wait (OnboardingView.waitForChatReadiness) gets a `reason`
 *     from jarvis.account.is_ready_for_chat on every one of its 40 polls, and
 *     only ever keeps the `detail` for the exhaustion message.
 *
 * So both screens showed ONE fixed sentence for their whole two-minute wait and
 * a customer could not tell a workspace being built from one that was stuck.
 * This module turns those observations into staged copy.
 *
 * The rule this module exists to enforce, and the reason it is pure and
 * separately tested: **a phase is only ever named because something reported
 * it.** There is no elapsed-time branch anywhere here. If nothing answered, the
 * caller gets `observed: false` and copy that says exactly that, never a phase.
 * This is the same discipline readinessWait.js applies at the poll ceiling
 * (jarvis#708/#709), extended to the middle of the wait rather than only its
 * end.
 *
 * Pure so `node --test` runs it without a bundler.
 */

/**
 * The three states a phase row can be in. `unknown` is deliberately distinct
 * from `waiting`: "we asked and got nothing" is not the same as "not started",
 * and rendering the first as the second would imply progress.
 */
export const PHASE_STATE = {
	DONE: "done",
	ACTIVE: "active",
	UNKNOWN: "unknown",
	WAITING: "waiting",
};

/**
 * WHAT a connect-wait phase is waiting on, as opposed to `state`, which is how
 * that wait is going. Only the connect wait carries a kind: it is the one screen
 * whose headline is derived from the live phase (`setupHeadline`, jarvis#727),
 * and a headline needs to know the subject, not the progress.
 *
 * `NONE` is a real member of the vocabulary, not an omission. Every phase this
 * module refuses to name - a poll that never answered, `readiness_unconfirmed`,
 * an unrecognised reason, and all three stopping verdicts - is explicitly NONE,
 * so `setupHeadline` falls back to the generic headline instead of borrowing a
 * neighbouring phase's words. Naming a subject we did not observe is the same
 * false claim the rest of this module exists to prevent.
 *
 * `provisioningPhase` deliberately carries no kind: its screen's headline is a
 * settled fact ("Payment confirmed"), never a phase, so there is nothing there
 * to derive.
 */
export const PHASE_KIND = {
	NONE: "",
	// The customer's chosen AI connection is being wired into the container.
	LLM_APPLY: "llm_apply",
	// The serving container itself is still coming up.
	CONTAINER: "container",
};

/**
 * The row to show while a wait is genuinely UNDER WAY but nothing has reported
 * back yet - the very first poll is still in flight, or (on the Connect step)
 * the apply operation is running and the readiness wait has not started.
 *
 * This exists because the alternative was worse in exactly the way this module
 * is supposed to prevent. Seeding the "last observation" as `answered: false`
 * made the row render "We couldn't reach your workspace to check" on the first
 * frame - announcing a FAILED check before any check had been attempted. That
 * is a false claim about the system's own state, just inverted from the ones
 * jarvis#708/#709 were about, and it is a lie the old fixed-sentence screen
 * never told.
 *
 * `observed` is false: nothing has reported. But the STATE is active, not
 * unknown, because "we are asking right now" is itself true and directly
 * known - unlike "we asked and got nothing", which is what UNKNOWN means.
 *
 * @param {string} label - what is actually being done, in the caller's words.
 * @param {string} [kind] - PHASE_KIND, when the caller can name the SUBJECT of
 *   this wait from something it already established rather than guessed. The
 *   connect wait passes LLM_APPLY because reaching it means a save returned an
 *   apply-operation descriptor and we are polling that operation - the AI
 *   connection is demonstrably being wired in, which is exactly what the phase
 *   claims. Defaults to NONE so a caller that cannot ground a subject gets no
 *   headline derived from one.
 */
export function inFlightPhase(label, kind = PHASE_KIND.NONE) {
	return {
		observed: false,
		state: PHASE_STATE.ACTIVE,
		kind,
		label,
		detail: "",
		stop: false,
	};
}

/**
 * Post-payment provisioning wait.
 *
 * @param {{answered?: boolean, tenantStatus?: string}} [last] - what the most
 *   recent poll returned. `answered` is true iff sync_connection returned a
 *   real payload (it resolves to null on throw or deadline). `tenantStatus` is
 *   admin's own value, e.g. "pending".
 * @returns {{observed: boolean, state: string, label: string, detail: string}}
 */
export function provisioningPhase({ answered = false, tenantStatus = "" } = {}) {
	if (!answered) {
		// A poll that threw or timed out is not a verdict. Say only that, and
		// restate the one thing that IS settled, because a customer who has just
		// been charged is asking about their money, not about a container.
		return {
			observed: false,
			state: PHASE_STATE.UNKNOWN,
			label: "We couldn't reach your workspace to check",
			detail: "Your payment is complete and nothing more is owed. We'll keep trying.",
		};
	}
	const status = String(tenantStatus || "")
		.trim()
		.toLowerCase();
	if (status === "running") {
		return {
			observed: true,
			state: PHASE_STATE.DONE,
			label: "Your workspace is ready",
			detail: "",
		};
	}
	// Admin answered and did not say running. "pending" is the value the bench
	// defaults to and the only other one it is known to send, but any answered
	// non-running status means the same thing for the customer: admin has this
	// and it is not finished. An unrecognised value must not invent a phase of
	// its own, so it shares this one.
	return {
		observed: true,
		state: PHASE_STATE.ACTIVE,
		label: "Jarvis is getting ready for you",
		detail: "Your workspace is being prepared.",
	};
}

/**
 * Connect-step readiness wait. Keyed on is_ready_for_chat's `reason` enum
 * (jarvis/account.py), which is a fixed server-side vocabulary.
 *
 * Two reasons must never render as a phase in progress:
 *   - `readiness_unconfirmed` is BY DEFINITION the absence of a verdict.
 *   - `authority_repair_required` means admin found the serving container
 *     ambiguous and paged a human. readiness.js is explicit that the only safe
 *     thing this surface can do is show admin's own reassurance, never an
 *     action, because retrying payment or reconnecting could make it worse.
 *
 * @param {{answered?: boolean, reason?: string, detail?: string}} [last]
 * @returns {{observed: boolean, state: string, label: string, detail: string,
 *   stop: boolean, paged?: boolean, title?: string}} `stop` means waiting cannot
 *   resolve this, so the loop must end rather than run to a ceiling whose copy
 *   invites a retry that cannot help. `paged` distinguishes "support has already
 *   been notified, do nothing" from "nobody was notified, you must act".
 */
export function readinessPhase({ answered = false, reason = "", detail = "" } = {}) {
	const say = String(detail || "").trim();
	if (!answered) {
		return {
			observed: false,
			state: PHASE_STATE.UNKNOWN,
			kind: PHASE_KIND.NONE,
			label: "We couldn't reach your workspace to check",
			detail: "We'll keep trying.",
			stop: false,
		};
	}
	switch (String(reason || "").trim()) {
		case "llm_pool_provisioning":
		case "llm_provisioning":
			return {
				observed: true,
				state: PHASE_STATE.ACTIVE,
				kind: PHASE_KIND.LLM_APPLY,
				label: "Applying your AI configuration",
				detail: say,
				stop: false,
			};
		case "container_provisioning":
			return {
				observed: true,
				state: PHASE_STATE.ACTIVE,
				kind: PHASE_KIND.CONTAINER,
				label: "Your workspace is coming online",
				detail: say,
				stop: false,
			};
		case "readiness_unconfirmed":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
				// The absence of a verdict names no subject: admin could not be
				// asked, so nothing here knows WHAT is outstanding.
				kind: PHASE_KIND.NONE,
				label: "Nothing has confirmed your workspace yet",
				detail: "We'll keep checking.",
				stop: false,
			};
		case "authority_repair_required":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
				kind: PHASE_KIND.NONE,
				label: "This needs a person to check it",
				// Admin's own reassurance, quoted. Never paraphrased, and never
				// accompanied by an action.
				detail: say,
				stop: true,
				paged: true,
				title: "We're looking into your workspace",
			};
		// The two reasons that no amount of waiting can resolve. Both used to fall
		// through to the default below and render as active progress, which is the
		// same false claim this module exists to prevent - and jarvis/account.py is
		// explicit about why the first one must stay distinct: "kept distinct so a
		// suspended customer isn't told to wait for a container that won't come
		// back". Neither is `paged`: nobody was notified, the customer has to act.
		case "subscription_suspended":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
				kind: PHASE_KIND.NONE,
				label: "Your subscription is paused",
				detail: say,
				stop: true,
				paged: false,
				title: "Your subscription is paused",
			};
		case "site_replaced":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
				kind: PHASE_KIND.NONE,
				label: "Your account is connected to a different site",
				detail: say,
				stop: true,
				paged: false,
				title: "This site no longer has your workspace",
			};
		default:
			// A reason this build does not know about gets the neutral line. It
			// must not be given a phase name invented here - and, for the same
			// reason, no kind: a headline derived from a subject this build cannot
			// identify would be pure invention.
			return {
				observed: true,
				state: PHASE_STATE.ACTIVE,
				kind: PHASE_KIND.NONE,
				label: "Waiting for your workspace to come online",
				detail: say,
				stop: false,
			};
	}
}

/**
 * The connect wait's HEADLINE, derived from the live phase (jarvis#727).
 *
 * The screen used to render a fixed "Setting up {agentName}" over a phase list
 * that had already become real (jarvis#722), so the largest text on screen was
 * the only part of it saying nothing. This maps the phase's SUBJECT - its
 * `kind`, never its `state` and never its label text - onto a headline that
 * describes what is actually being done.
 *
 * The honesty rule is the same one the rest of this module follows, applied to
 * a bigger font: **a headline may only name a phase that was observed.** Every
 * phase whose kind is NONE - a poll that never answered, an absent verdict, an
 * unrecognised reason, a stopping verdict - falls back to the generic setup
 * headline. "Giving {agentName} a brain" is reachable only from LLM_APPLY,
 * which requires either a live apply operation we are polling or admin naming
 * an apply as the outstanding work.
 *
 * `navigating` is the one branch that does not come from a phase, and it is the
 * only honest source for "Opening your chat": readinessPhase has no DONE branch
 * (jarvis#726 documents why - every reason it knows maps to ACTIVE or UNKNOWN),
 * so a chat-opening phase can never be OBSERVED on this screen. What can be
 * observed is that a ready verdict arrived and navigateToChat() ran; the router
 * guard then re-checks readiness over the network while this screen is still
 * painted. During that window "Opening your chat" is a statement about what we
 * are doing, made from the verdict we just received - not a guess about a phase
 * nobody reported. The DONE mapping is kept too, so the function stays total
 * over the phase vocabulary for any caller whose phases can reach it.
 *
 * @param {{state?: string, kind?: string}} [phase]
 * @param {string} [agentName] - the white-label brand name; blank falls back to
 *   Jarvis, exactly as @/branding does.
 * @param {{navigating?: boolean}} [opts]
 * @returns {string} sentence case, per design.md.
 */
export function setupHeadline(phase, agentName, { navigating = false } = {}) {
	const name = String(agentName || "").trim() || "Jarvis";
	if (navigating || (phase && phase.state === PHASE_STATE.DONE)) return "Opening your chat";
	switch ((phase && phase.kind) || PHASE_KIND.NONE) {
		case PHASE_KIND.LLM_APPLY:
			// The literal act of giving the assistant its model. This is the phase
			// the whole connect step exists for, and the one a customer waits on
			// longest.
			return `Giving ${name} a brain`;
		case PHASE_KIND.CONTAINER:
			// The model is settled; what is outstanding is the container. Not
			// "workspace": the customer does not have one to speak of yet.
			return "Bringing your setup online";
		default:
			// Nothing was observed, or nothing this build can name. The generic
			// headline is the honest one, and it is what this screen said before.
			return `Setting up ${name}`;
	}
}

/**
 * Headline for a wait that ended without a Ready verdict.
 *
 * Split out because the Connect step used to render every one of these under
 * "Still finishing setup" - which asserts the workspace IS still finishing,
 * the exact claim readinessWaitExhaustedMessage was written to stop making in
 * the body copy directly underneath it (jarvis#709). A headline is not exempt
 * from that just because it is short.
 *
 * @param {string} phase - the connect phase this headline sits above.
 * @param {{fromReadinessCeiling?: boolean}} [opts]
 */
export function connectHeadline(phase, { fromReadinessCeiling = false } = {}) {
	if (phase === "superseded") return "Your workspace changed";
	if (phase === "support") return "We couldn't finish setting up your AI";
	if (fromReadinessCeiling) return "We couldn't confirm your setup";
	// An operation-level retry: something actually failed and said so, which is
	// a different (and honestly reportable) thing from never getting an answer.
	return "Setup hit a snag";
}

/**
 * Progress-bar reading of a phase (jarvis#726), for the fixed 3-step shape
 * already drawn as the phase list next to it: a settled prior fact (the save
 * persisted / the payment cleared), the CURRENT phase (this function's
 * argument), and a step that resolves only by leaving this screen entirely -
 * `navigateToChat()` fires the instant `ready` is true, so that step is never
 * rendered done in place. Reads the SAME phase object the row already
 * renders (provisioningPhase / readinessPhase / inFlightPhase) - no separate
 * source of truth, and in particular no ordering inferred across `reason`
 * codes, which waitPhases.js does not encode and never has: a phase counts
 * as done ONLY when the model itself says PHASE_STATE.DONE.
 *
 * That means the two waits behave differently here, honestly:
 * provisioningPhase has a DONE branch (tenant_status === "running"), so the
 * provisioning wait's bar can and does advance to 2 of 3 mid-screen.
 * readinessPhase has NO done branch at all - every reason it knows about
 * maps to ACTIVE or UNKNOWN - so the connect wait's bar stays at 1 of 3
 * until the moment is_ready_for_chat says ready and the screen navigates
 * away; it never fills to 2 in place. Both waits still get the bar: the
 * provisioning wait makes the determinate case real and demonstrable, and
 * the connect wait's honestly-stuck-at-1/3 bar is exactly the indeterminate
 * case this function has to tell apart from real progress.
 *
 * `indeterminate` is true whenever the phase carries nothing to act on -
 * PHASE_STATE.UNKNOWN, or no phase read yet. It must render differently from
 * the ACTIVE case: ACTIVE means the last poll told us specifically what's
 * running (or, for inFlightPhase, that we are genuinely asking right now -
 * itself known, not guessed, which is why inFlightPhase's state is ACTIVE
 * and this counts it as determinate, matching the rest of this module's
 * philosophy). UNKNOWN means the last poll told us nothing. A bar that looks
 * identical for both would fake the confidence the honest phase label next
 * to it already refuses to claim.
 *
 * The bar's own visible label is deliberately just "Step {current} of
 * {total}" (built by the caller from `current`/`total`, not from `label`
 * below) - never the phase's own sentence. The phase row right next to the
 * bar already renders that sentence (waitPhases.js's whole reason for
 * existing); repeating it as the bar's label too read as an unintentional
 * duplicate rather than a deliberate echo. `label` is kept on the return
 * value for callers that want it (tests, a future non-duplicating layout),
 * not because OnboardingView.vue's bar renders it directly.
 *
 * @param {{state?: string, label?: string}} [phase]
 * @returns {{done: number, current: number, total: number, percent: number,
 *   label: string, indeterminate: boolean}} `done` is how many of `total`
 *   segments fill. `current` is the 1-indexed step the customer is ON
 *   (done + 1, capped at total) - never `done` itself, because a customer
 *   watching this is on the step that ISN'T done yet. `percent` is `done` /
 *   `total` rounded to an integer, for the Progress component's numeric
 *   `value` prop (frappe-ui's own `filledIntervalCount` re-derives the
 *   segment count from this by rounding it back against `total`, so the
 *   rounding here never changes which segments fill) - it exists only so
 *   the rendered `aria-valuenow` is a clean integer instead of a repeating
 *   fraction; it is never displayed to the customer as a percentage.
 */
export function phaseProgress(phase) {
	const total = 3;
	const state = phase && phase.state;
	const done = state === PHASE_STATE.DONE ? 2 : 1;
	return {
		done,
		current: Math.min(done + 1, total),
		total,
		percent: Math.round((done / total) * 100),
		label: (phase && phase.label) || "",
		indeterminate: !phase || state === PHASE_STATE.UNKNOWN,
	};
}
