// Starter-prompt helpers for the new-chat screen. Pure logic (no Vue, no DOM)
// so it is unit-testable under the PWA's `node --test src/lib/*.test.js` harness
// — the grid render itself is verified by the flow review.

// Shown until the personalized suggestions load, and as the fallback if that
// call fails or returns nothing. Wording mirrors the desktop DEFAULT_STARTERS.
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
// list; fall back to the defaults on anything empty or malformed so the empty
// chat is never a bare box.
export function normalizeStarters(rows) {
	if (!Array.isArray(rows) || rows.length === 0) return DEFAULT_STARTERS;
	const clean = rows
		.filter((r) => r && typeof r.title === "string" && typeof r.prompt === "string")
		.map((r) => ({ title: r.title, prompt: r.prompt }));
	return clean.length ? clean : DEFAULT_STARTERS;
}
