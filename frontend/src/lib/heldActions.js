// Copy for the Approval Board's held File Box writes (Jarvis Pending Action, kind
// file_box_held). Pure, so the specs pin every reason_code's words. Everything
// returned is plain text: callers escape it for an HTML sink (frappe-ui toasts).

export function filesWaiting(n) {
	const count = Number(n) || 0;
	return count === 1 ? "1 file waiting" : `${count} files waiting`;
}

export function skipLabel(n) {
	const count = Math.max(1, Number(n) || 1);
	return `Don't create — skip ${count === 1 ? "1 file" : `${count} files`}`;
}

// Refusals the board words itself; the rest carry a server message naming the record.
const REFUSALS = {
	not_found: "This approval is no longer available.",
	busy: "Someone is acting on this approval right now. Try again in a moment.",
	executing: "This is being created right now.",
	already_handled: "This approval was already handled.",
	identity_refused:
		"This can no longer run as the person who dropped the file. Skip it, or ask them to drop the file again.",
	stale: "The record changed after this was proposed. Nothing ran.",
	target_missing: "The record this targets no longer exists. Nothing ran.",
	tampered: "This approval failed an integrity check. Nothing ran.",
	unverifiable: "This approval can no longer be verified on this site. Nothing ran.",
	record_required: "Pick the existing record to use.",
	use_existing_unavailable: "Use existing works for a single new record only.",
	interrupted: "The create was interrupted. Check whether the record exists before retrying.",
};
const SERVER_WORDED = new Set(["exists", "record_not_found", "failed", "partial"]);

export function refusalMessage(res) {
	const code = (res && res.reason_code) || "";
	const server = res && res.error && res.error.message;
	if (SERVER_WORDED.has(code) && server) {
		return code === "exists" || code === "record_not_found"
			? String(server)
			: `Couldn't create: ${server}`;
	}
	return REFUSALS[code] || (server ? String(server) : "This approval could not be completed.");
}

function continues(n) {
	const count = Number(n) || 0;
	if (!count) return "";
	return count === 1 ? " The waiting file continues." : ` The ${count} waiting files continue.`;
}

export function outcomeMessage(res) {
	const code = res && res.reason_code;
	// ok:true from a Skip that lost the race: someone else decided it first.
	if (code === "already_handled") return REFUSALS.already_handled;
	const tail = continues(res && res.waiters_count);
	if (code === "created") return "Created." + tail;
	if (code === "use_existing") {
		const name = res.data && res.data.name;
		return (name ? `Using ${name}.` : "Using the existing record.") + tail;
	}
	if (code === "discarded") return "Skipped. Nothing was created." + tail;
	return "Done." + tail;
}

// The row left Pending (decided, failed, or gone): the lane drops it.
export function isSettled(res) {
	if (!res) return false;
	if (res.ok) return true;
	return (
		["Executed", "Failed", "Discarded", "Cancelled", "Superseded"].includes(res.pa_status) ||
		["not_found", "already_handled"].includes(res.reason_code)
	);
}

// Read-only status line for a row that can no longer be acted on.
export function statusLine(rec) {
	if (!rec) return "";
	if (rec.status === "Executing") return "Creating this record now…";
	if (rec.status === "Executed" && rec.reason_code === "use_existing")
		return `Resolved with the existing ${rec.result_doctype} ${rec.result_name}.`;
	if (rec.status === "Executed") return "Created.";
	if (rec.status === "Discarded") return "Skipped. Nothing was created.";
	return rec.reason || "This approval was already handled.";
}
