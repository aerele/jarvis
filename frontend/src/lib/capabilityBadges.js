// Presentation for the capability-catalog safety badge. The tier ENUM is set by
// the server (jarvis.api._gating_badge, derived from the frozensets that enforce
// the gate); this maps it to a frappe-ui Badge theme + a human, NON-ABSOLUTE
// label. The legend spells out that auto-approve or a saved macro can skip the
// card, so the catalog never over-claims a guarantee the runtime can bypass.
export const BADGE_META = {
	reads_only: { theme: "green", label: "Reads only" },
	asks_to_approve: { theme: "orange", label: "Asks you first" },
	always_asks: { theme: "red", label: "Always asks you" },
};

export const CATALOG_LEGEND =
	"Jarvis always works within your own permissions. Each label shows what happens by " +
	"default when you ask — if you've turned on auto-approve or a saved macro, some steps " +
	"run without asking first.";
