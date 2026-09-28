/**
 * Pure read of a Jarvis Agent Run row's model fields (T6) for the runs rail
 * and the run detail header - the ONE place both surfaces ask "what should
 * this run's model line say", so they can't drift.
 *
 * A legacy run (recorded before per-agent models existed, or with the flag
 * off) carries neither `model_used` nor `model_rendered` - returns null, the
 * row's model line renders nothing.
 */
export function runModelInfo(run) {
	const used = (run && run.model_used) || "";
	const rendered = (run && run.model_rendered) || "";
	const label = used || rendered;
	if (!label) return null;
	if (run.model_below_min) {
		return { label, note: "Ran on a lower model after a provider failure", warn: true };
	}
	if (!used && rendered) {
		return { label, note: "Model not verified", warn: false };
	}
	return { label, note: "", warn: false };
}
