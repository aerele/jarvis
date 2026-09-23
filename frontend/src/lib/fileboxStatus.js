/**
 * File Box row status -> badge + the row's result link, in one place so the list,
 * its filters and the ?status= deep link agree. Mirrors the server ladder
 * (jarvis/chat/filebox.py): processing / needs_approval / draft_created / failed /
 * no_draft. The pre-ladder done / error values still render for a stale page.
 */
export const STATUS_BADGE = {
	processing: { label: "Processing", theme: "blue" },
	needs_approval: { label: "Needs approval", theme: "orange" },
	draft_created: { label: "Draft created", theme: "green" },
	no_draft: { label: "No draft", theme: "gray" },
	failed: { label: "Failed", theme: "red" },
	done: { label: "Done", theme: "green" },
	error: { label: "Failed", theme: "red" },
};

export const STATUSES = ["processing", "needs_approval", "draft_created", "no_draft", "failed"];

export const STATUS_OPTIONS = [
	{ label: "All", value: "" },
	...STATUSES.map((value) => ({ label: STATUS_BADGE[value].label, value })),
];

/** The badge for a row; a processing row still queued behind chat says so. */
export function statusBadge(row) {
	if (row && row.behind_chat && row.status === "processing") {
		return { label: "Waiting (behind chat)", theme: "gray" };
	}
	const status = (row && row.status) || "";
	return STATUS_BADGE[status] || { label: status, theme: "gray" };
}

/**
 * Where a row's result line links: an in-app route (the approval), the draft's
 * Desk form (opened in a new tab), or nothing. Only these two server-built
 * shapes are honoured - any other value renders as plain text.
 */
export function resultLink(row) {
	const href = row && row.result_link;
	if (typeof href !== "string") return null;
	if (href.startsWith("/app/")) return { kind: "desk", href };
	if (href === "/approvals" || href.startsWith("/approvals/")) return { kind: "route", href };
	return null;
}
