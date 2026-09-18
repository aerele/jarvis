// Starter-prompt helpers for the new-chat screen. Pure logic (no Vue, no DOM)
// so it is unit-testable under the PWA's `node --test src/lib/*.test.js` harness
// — the grid render itself is verified by the flow review.

// Shown immediately (and as the fallback if the suggestions call fails or
// returns nothing), so the empty chat is never a bare box. A PWA-tailored set
// in the spirit of the desktop starters, not a verbatim copy.
export const DEFAULT_STARTERS = [
	{ title: "Analyse", prompt: "Which sales orders are overdue this month?" },
	{ title: "Look up", prompt: "What does this customer owe us right now?" },
	{ title: "Draft", prompt: "Write a follow-up email to a late payer" },
	{ title: "Take action", prompt: "Raise a sales order for a customer" },
];

// Positional tint, cycled: the backend rows are {title, prompt} only (no
// category), so the tile colour is chosen by index, not meaning. Maps to
// .jv-tint-a/b/c (green / amber / accent tokens) in NewChatView's scoped CSS.
const TINTS = ["a", "b", "c"];

export function starterTint(i) {
	return TINTS[i % TINTS.length];
}

// Coerce whatever get_prompt_suggestions returns into a safe {title, prompt}
// list: drop rows with a blank/non-string title or prompt, cap the count
// (client-side defence — the backend also caps at 4), and fall back to the
// defaults on anything empty or malformed so the empty chat is never bare.
const MAX_STARTERS = 8;

export function normalizeStarters(rows) {
	if (!Array.isArray(rows) || rows.length === 0) return DEFAULT_STARTERS;
	const clean = rows
		.filter(
			(r) =>
				r &&
				typeof r.title === "string" &&
				r.title.trim() &&
				typeof r.prompt === "string" &&
				r.prompt.trim()
		)
		.map((r) => ({ title: r.title, prompt: r.prompt }))
		.slice(0, MAX_STARTERS);
	return clean.length ? clean : DEFAULT_STARTERS;
}
