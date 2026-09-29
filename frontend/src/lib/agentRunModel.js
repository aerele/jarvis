/**
 * Pure read of a Jarvis Agent Run row's model fields (T6) for the runs rail
 * and the run detail header - the ONE place both surfaces ask "what should
 * this run's model line say", so they can't drift.
 *
 * A legacy run (recorded before per-agent models existed, or with the flag
 * off) carries neither `model_used` nor `model_rendered` - returns null, the
 * row's model line renders nothing.
 */

// model_used/model_rendered carry the full stored ref - "openai/gpt-5",
// "openai_compat/gpt-5.6-terra", "openrouter/anthropic/claude-x" - with the
// provider/door as the FIRST path segment. Only the model id itself belongs
// in the visible label (UX-1); the model id can itself contain '/' (an
// openrouter-style ref), so only the first segment is stripped.
function stripProviderPrefix(ref) {
	const s = String(ref || "");
	const i = s.indexOf("/");
	return i === -1 ? s : s.slice(i + 1);
}

export function runModelInfo(run) {
	const used = (run && run.model_used) || "";
	const rendered = (run && run.model_rendered) || "";
	const raw = used || rendered;
	if (!raw) return null;
	const label = stripProviderPrefix(raw);
	// `title` keeps the full raw ref (provider/door + model id) for a tooltip -
	// the label alone can be ambiguous across providers/doors.
	if (run.model_below_min) {
		return {
			label,
			title: raw,
			note: "Ran on a model below this agent's requirement.",
			warn: true,
		};
	}
	if (!used && rendered) {
		return { label, title: raw, note: "Model not verified", warn: false };
	}
	return { label, title: raw, note: "", warn: false };
}
