// The phone's Submit next step on a created draft's receipt: the same call and the same reading of
// its answer as the desktop (chatCardActions.js nextStepRefusal / shouldHideNextStep). It parks the
// ordinary confirmation card for the record's submit (the amber tile); nothing runs from the
// button. Imports chatCardActions.js, which `node --test` cannot resolve, so this file is tested
// by vitest (frontend/src/components/PwaDraftReceipt.spec.js).
import {
	nextStepActedKeys,
	nextStepRefusal,
	shouldHideNextStep,
} from "../../../frontend/src/lib/chatCardActions.js";

export const STEP_FAILED_TEXT = "Couldn't open that step. Try again.";

// The steps whose Submit is not offered: a later receipt in the thread already submitted the
// record, or the server refused the step for good.
export function hiddenSteps(rows, goneKeys) {
	return nextStepActedKeys(rows || [], new Set(goneKeys || []));
}

// Resolves to "ignored" (one step is already in flight, or the chat was left), "refused" (a note is
// shown; the button stays only for a temporary refusal), "proposed" (the card was parked and the
// pending list reloaded, so it shows) or "failed" (the call threw: nothing ran, the button stays).
//   d = { ui (receiptUi), propose(step) -> reply, current() -> bool, reloadPending() }
export async function proposeSubmitStep(step, d) {
	if (!step || d.ui.busyKey() || !d.current()) return "ignored";
	d.ui.setBusy(step.key);
	d.ui.setNote(step.key, "");
	try {
		const r = await d.propose(step);
		if (!d.current()) return "ignored";
		if (r && r.ok === false) {
			const refusal = nextStepRefusal(r);
			d.ui.setNote(step.key, refusal.message);
			if (shouldHideNextStep(refusal)) d.ui.hide(step.key);
			return "refused";
		}
		await d.reloadPending();
		return "proposed";
	} catch (e) {
		if (!d.current()) return "ignored";
		d.ui.setNote(step.key, (e && e.message) || STEP_FAILED_TEXT);
		return "failed";
	} finally {
		if (d.current() && d.ui.busyKey() === step.key) d.ui.setBusy("");
	}
}
