// The File Box row's result line on the phone. A row waiting on an approval is
// decided on the desktop Approval Board (there is no board on the PWA), so say so.
export function resultLine(row) {
	if (!row) return "";
	if (row.status === "needs_approval")
		return row.missing
			? `Needs approval on desktop — missing: ${row.missing}`
			: `${row.result || "Needs approval"} — resolve on desktop`;
	return row.result || "";
}
