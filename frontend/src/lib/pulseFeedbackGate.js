// Periodic business-pulse survey. Unlike sessionFeedbackGate, this one owns
// its own trigger check (pulse_context is cheap and self-gating -- see spec
// Performance review), so callers just invoke maybeOpenPulseFeedback() once
// per chat open rather than passing a precomputed condition.
import { ref } from "vue";
import { pulseContext } from "@/api";

export const pulseFeedbackOpen = ref(false);
export const pulseFeedbackContext = ref(null); // {period_label, period_key, period_is_previous, features_offered}

// Client-side "at most one offer per page load" rule. The server enforces its
// own 3-per-month cap (see pulse_context's docstring), but that cap is keyed
// only to the calendar period, not to a single browsing session: without this
// flag, dismissing the survey ("Maybe later") in one conversation and then
// switching to another conversation re-triggers _checkPulseOnce, which calls
// pulse_context() again, which is still due -- reopening the survey and
// burning another one of the three monthly offers. A page reload resets this
// (module state, not persisted), which is the intended boundary.
let _dismissedThisLoad = false;

export async function maybeOpenPulseFeedback() {
	if (_dismissedThisLoad) return;
	try {
		const ctx = await pulseContext();
		// Re-check after the await: two chat opens in quick succession both pass
		// the gate above before either request returns. If the first one's
		// survey was dismissed while the second request was still in flight, the
		// second must not reopen it on top of the dismissal.
		if (_dismissedThisLoad) return;
		if (ctx && ctx.due) {
			pulseFeedbackContext.value = ctx;
			pulseFeedbackOpen.value = true;
		}
	} catch (e) {
		/* offline or admin unreachable -- skip silently, try again next open */
	}
}

export function closePulseFeedback() {
	pulseFeedbackOpen.value = false;
	_dismissedThisLoad = true;
}
