// Pure, dependency-free support presentation helpers, split out of the support
// store so they're unit-testable: importing the store itself drags in frappe-ui
// and @/api, which the vitest harness can't resolve. Re-exported from
// stores/support.js so existing `from "@/stores/support"` imports keep working.

// Helpdesk ticket priorities are Urgent / High / Medium / Low. Restricted palette
// (no blue/purple, matching the status badges): only the two that warrant
// attention carry colour - Urgent (red) and High (orange) - so they draw the eye,
// while Medium and Low stay quiet gray. A blank/unknown priority returns null so
// the cell renders nothing: the Priority column is self-activating (it only
// appears once a row actually carries the field), so an empty value is the normal
// transitional state, not an error.
const PRIORITY_THEME = { Urgent: "red", High: "orange", Medium: "gray", Low: "gray" };
export function priorityBadge(priority) {
	if (!priority) return null;
	return { label: priority, theme: PRIORITY_THEME[priority] || "gray", variant: "subtle" };
}
