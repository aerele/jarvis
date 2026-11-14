/** Intake row status -> badge, and where its Draft cell links. Mirrors the server's
 * Jarvis Trigger Activity statuses (jarvis/chat/filebox.py list_intake_page). */
export const INTAKE_BADGE = {
	Queued: { label: "Queued", theme: "gray" },
	Running: { label: "Running", theme: "blue" },
	"Needs Review": { label: "Needs review", theme: "orange" },
	Success: { label: "Draft created", theme: "green" },
	Failed: { label: "Failed", theme: "red" },
	Skipped: { label: "Skipped", theme: "gray" },
};

export const INTAKE_STATUS_OPTIONS = [
	{ label: "All", value: "" },
	...Object.entries(INTAKE_BADGE).map(([value, b]) => ({ label: b.label, value })),
];

export function intakeBadge(row) {
	// The draft was deleted after the row finished: not "Draft created" any more.
	if (row && row.draft_missing) return { label: "Draft deleted", theme: "gray" };
	return INTAKE_BADGE[row && row.status] || { label: (row && row.status) || "", theme: "gray" };
}

// Rows whose status moves on its own: the list polls while one is on screen.
export function intakeIsLive(row) {
	return !!row && (row.status === "Queued" || row.status === "Running");
}

/** The draft's desk form (new tab), the approval on the board (in-app), or nothing. */
export function intakeDraftLink(row) {
	if (!row) return null;
	if (row.draft_doctype && row.draft_name) {
		const slug = row.draft_doctype.toLowerCase().replace(/\s+/g, "-");
		return {
			kind: "desk",
			label: `${row.draft_doctype} ${row.draft_name}`,
			href: `/app/${slug}/${encodeURIComponent(row.draft_name)}`,
		};
	}
	if (row.status === "Needs Review" && row.approval_request) {
		return {
			kind: "route",
			label: "Open review",
			href: `/approvals/${encodeURIComponent(row.approval_request)}`,
		};
	}
	return null;
}
