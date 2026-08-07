/**
 * Copy for the moment a bounded chat-readiness poll (waitForChatReadiness /
 * followLegacyReadiness in OnboardingView.vue) runs out with no Ready verdict
 * (jarvis#708).
 *
 * The poll's job is only ever to ask admin "is chat_readiness Ready" - it is
 * never proof that a stalled workspace is quietly correcting itself. The copy
 * that used to render here ("It's still finishing on its own, so you can keep
 * waiting or retry") asserted exactly that, unconditionally, even when every
 * poll came back with the SAME not-ready verdict for the whole wait, or with
 * chat_readiness reasons that never advance if nobody ever acts on them (a
 * stuck provisioning row is the case this was written against). A customer in
 * that state had no way to tell the two apart and no way out but to keep
 * clicking Retry forever.
 *
 * This module only ever says what the wait loop actually observed: whether
 * admin answered at all (`sawVerdict` - the readiness-poll analogue of
 * `neverConfirmed` on the LLM-apply-operation timeout a few lines up in
 * OnboardingView.vue, jarvis#690), and admin's own last-known explanation
 * (`detail` - jarvis.account.is_ready_for_chat's `detail`, e.g. "applying your
 * LLM configuration"), if it ever sent one. It never says "still finishing on
 * its own" - that is a claim about what admin is DOING, which the wait loop
 * cannot see.
 *
 * Pure so `node --test` runs it without a bundler.
 *
 * @param {{sawVerdict?: boolean, detail?: string}} [last] - what the wait loop
 *   observed. sawVerdict is true iff at least one poll returned a real
 *   (non-thrown) answer, regardless of what that answer was. detail is the
 *   most recent non-empty `detail` string across every poll of this wait.
 * @returns {string}
 */
export function readinessWaitExhaustedMessage({ sawVerdict = false, detail = "" } = {}) {
	if (!sawVerdict) {
		return "We couldn't reach your workspace to check, so we don't know whether it's finished coming online. You're welcome to keep waiting and retry, or get a person to look into it.";
	}
	const trimmed = (detail || "").trim();
	if (trimmed) {
		return `Your workspace's last status: ${trimmed} We haven't been able to confirm that's finished, and we can't promise it's still progressing on its own. You're welcome to keep waiting and retry, or get a person to look into it.`;
	}
	return "Your AI connection saved, but we haven't been able to confirm your workspace has finished coming online. We can't promise it's still progressing on its own. You're welcome to keep waiting and retry, or get a person to look into it.";
}
