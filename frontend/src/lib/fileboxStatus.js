/**
 * File Box row status -> badge + the row's result link, in one place so the list,
 * its filters and the ?status= deep link agree. Mirrors the server ladder
 * (jarvis/chat/filebox.py): processing / needs_approval / applying (an approval
 * sheet being applied) / draft_created / failed / no_draft. The pre-ladder done /
 * error values still render for a stale page.
 */
import { escapeHtml } from "./errors";

export const STATUS_BADGE = {
	processing: { label: "Processing", theme: "blue" },
	needs_approval: { label: "Needs approval", theme: "orange" },
	applying: { label: "Applying", theme: "blue" },
	draft_created: { label: "Draft created", theme: "green" },
	no_draft: { label: "No draft", theme: "gray" },
	failed: { label: "Failed", theme: "red" },
	done: { label: "Done", theme: "green" },
	error: { label: "Failed", theme: "red" },
};

export const STATUSES = [
	"processing",
	"needs_approval",
	"applying",
	"draft_created",
	"no_draft",
	"failed",
];

// Rows whose status moves on its own: the list polls while one is on screen.
export function isLive(row) {
	return !!row && (row.status === "processing" || row.status === "applying");
}

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
 * Re-run (PR-5, AC8) is offered only on the viewer's own failed / no_draft rows -
 * never once a draft exists, never on a row merely shared with them.
 */
export function canRerun(row) {
	const status = row && row.status;
	return !!(row && row.is_owner) && (status === "failed" || status === "no_draft");
}

/**
 * The toast for a bulk Re-run response. Skip reasons are server text bound into
 * an HTML sink (frappe-ui Toast uses v-html), so they are escaped here.
 */
export function bulkRerunToast(res, requested) {
	const skipped = (res && res.skipped) || [];
	const sent = res && res.sent != null ? res.sent : requested - skipped.length;
	if (!skipped.length) {
		return { type: "success", message: `${sent} file${sent === 1 ? "" : "s"} re-running` };
	}
	const reasons = [...new Set(skipped.map((s) => s.reason || "skipped"))]
		.map(escapeHtml)
		.join(", ");
	return {
		type: "info",
		message: `${sent} re-running · ${skipped.length} skipped (${reasons})`,
	};
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
	// "/approvals?held=<name>" opens a held File Box write or a sheet on the board.
	if (href === "/approvals" || /^\/approvals[/?]/.test(href)) return { kind: "route", href };
	return null;
}
