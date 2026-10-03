// How a macro and its runs are worded and coloured, wherever they are listed: the
// owner's Macros list and Runs tab, and the admin's pane in Settings. One copy, so
// an admin and an owner talking about the same macro use the same words for it.

// Colour of a "Last run" cell, by the tone lib/macroRunOutcome's lastRunCell gives.
export const LAST_RUN_TONE = {
	bad: "text-ink-red-4",
	warn: "text-ink-amber-3",
	muted: "text-ink-gray-5",
	info: "text-ink-blue-3",
};
// Colour of a "Next run" cell, by the tone lib/macroSchedule's nextRunCell gives.
export const NEXT_RUN_TONE = { warn: "text-ink-amber-3", muted: "text-ink-gray-5" };

// A run's status badge.
export const RUN_THEMES = {
	queued: "gray",
	running: "blue",
	waiting_capacity: "orange", // parked, not failed - matches the merge_status "pending" badge elsewhere in Macros
	completed: "green",
	failed: "red",
	stopped: "gray",
};
// Statuses whose display label isn't just a capitalized value (e.g. waiting_capacity).
export const STATUS_LABELS = {
	waiting_capacity: "Waiting for capacity",
};
export function statusLabel(s) {
	if (!s) return "";
	return STATUS_LABELS[s] || s.charAt(0).toUpperCase() + s.slice(1);
}

// A macro's on/off switch, in the words of the Macros list's Status filter.
export function enabledLabel(enabled) {
	return enabled ? "Enabled" : "Draft";
}

// What "Armed" means, for whoever reads the badge without having armed a macro.
export const ARMED_HELP = "Armed: this macro's runs write without asking for confirmation.";
