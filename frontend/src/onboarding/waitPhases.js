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
 */
export function inFlightPhase(label) {
	return {
		observed: false,
		state: PHASE_STATE.ACTIVE,
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
				label: "Applying your AI configuration",
				detail: say,
				stop: false,
			};
		case "container_provisioning":
			return {
				observed: true,
				state: PHASE_STATE.ACTIVE,
				label: "Your workspace is coming online",
				detail: say,
				stop: false,
			};
		case "readiness_unconfirmed":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
				label: "Nothing has confirmed your workspace yet",
				detail: "We'll keep checking.",
				stop: false,
			};
		case "authority_repair_required":
			return {
				observed: true,
				state: PHASE_STATE.UNKNOWN,
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
				label: "Your account is connected to a different site",
				detail: say,
				stop: true,
				paged: false,
				title: "This site no longer has your workspace",
			};
		default:
			// A reason this build does not know about gets the neutral line. It
			// must not be given a phase name invented here.
			return {
				observed: true,
				state: PHASE_STATE.ACTIVE,
				label: "Waiting for your workspace to come online",
				detail: say,
				stop: false,
			};
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
