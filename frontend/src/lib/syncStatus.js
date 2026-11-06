// The one place that turns a Frappe `last_sync_status` into words a customer can read.
//
// Every apply pipeline in Jarvis (the LLM pool, the agent catalog, custom skills,
// learned skills) writes the SAME shape into its DocType: an internal audit string
// like "pending: provisioning container (pool)", "ok (restart via admin)" or
// "failed: fleet returned 502". Those strings exist so support can reconstruct what
// the backend did. They were never written for a customer, and four surfaces were
// putting them on screen verbatim.
//
// "ok (restart via admin)" is the clearest example of why that hurts: it records a
// restart that ALREADY happened, automatically, but it reads like an instruction to
// go and restart something in Desk. Nothing here ever tells the customer to act.
//
// Consumers keep their own presentation (a pill, a KvRow, a toast); they only take
// `kind` for styling and `text` for the sentence. `detail` is the operator-supplied
// failure reason, offered separately so a surface that has room can show it and a
// surface that does not can stay short without leaking the prefix.

/** Longest failure reason we will put in front of a customer. */
const MAX_DETAIL = 240;

// The exact marker BOTH teardown paths write into `last_sync_status`:
// jarvis/onboarding.py's _DISCONNECTED_LLM_FIELDS (Disconnect from AI) and
// jarvis/oauth/api.py's disconnect (Disconnect chat subscription).
//
// It is not an apply outcome, which is why it never matched a prefix above: there
// is no apply and no subject left to report on. It still has to be a NAMED state
// here rather than an unrecognised one. Falling through to "Status unavailable"
// described a deliberate teardown as a fault, and it left the AI models pane with
// nothing at all to show for a disconnect (its strip hides an "idle" kind), so a
// disconnect that fully succeeded was indistinguishable from one that never ran.
const DISCONNECTED = "disconnected";

/**
 * @param {string} raw - a `last_sync_status` value, possibly empty or unrecognised.
 * @returns {{kind: "pending"|"ok"|"failed"|"disconnected"|"unknown", text: string, detail: string}}
 */
export function humaniseSyncStatus(raw) {
	const s = typeof raw === "string" ? raw.trim() : "";
	if (!s) return { kind: "unknown", text: "Not synced yet", detail: "" };

	const head = s.toLowerCase();
	if (head === DISCONNECTED) {
		return { kind: "disconnected", text: "Disconnected", detail: "" };
	}
	if (head.startsWith("pending")) {
		return { kind: "pending", text: "Applying your changes", detail: "" };
	}
	if (head.startsWith("ok")) {
		// Deliberately says nothing about HOW it applied. The suffix ("restart via
		// admin", "pool_update via admin") is an audit trail, and every variant of it
		// means the same thing to the customer: the agent is running what they saved.
		return { kind: "ok", text: "Up to date", detail: "" };
	}
	if (head.startsWith("failed")) {
		return { kind: "failed", text: "Last sync failed", detail: failureDetail(s) };
	}
	// Unrecognised. A new backend status must never reach the screen as-is, so this
	// degrades to a label rather than passing the string through.
	return { kind: "unknown", text: "Status unavailable", detail: "" };
}

/** True when the pipeline is still working, for callers that only need the yes/no. */
export function isSyncPending(raw) {
	return humaniseSyncStatus(raw).kind === "pending";
}

/** True when the last apply ended badly. */
export function isSyncFailed(raw) {
	return humaniseSyncStatus(raw).kind === "failed";
}

/** True when the customer tore the AI connection down on purpose. */
export function isSyncDisconnected(raw) {
	return humaniseSyncStatus(raw).kind === "disconnected";
}

// admin-v2#645: when the control plane relays a fleet failure, the stored status is
// "failed: admin unreachable: admin returned a 502 error: <code>: <message>". The
// prefix is transport plumbing and the message is written for operators, so none of
// it goes on screen. The machine code is the stable part: it maps to plain copy here,
// and anything unmapped falls back to a generic line. The raw text stays in the
// Error Log, where support reads it.
const RELAY_PREFIX = /^(admin unreachable|admin returned)\b/i;
const RELAY_STATUS = /^admin unreachable:?\s*/i;
const RELAY_HTTP = /^admin returned(?: an? \d{3} error| \d{3})?:?\s*/i;
const RELAY_CODE = /^([a-z][a-z0-9_]*):\s/;
const RELAY_GENERIC = "Could not reach Jarvis just now. Try again in a minute.";
const RELAY_CODE_COPY = {
	reload_failed:
		"Could not apply the new AI key. Jarvis is still using the old one. Try saving again.",
	rotate_failed: "Could not save the new AI key. Try saving again.",
	restart_failed: "Could not restart Jarvis to apply your changes. Try again in a minute.",
	write_failed: "Could not save your changes. Try again in a minute.",
	chown_failed: "Could not save your changes. Try again in a minute.",
	clear_failed: "Could not clear your saved data. Try again in a minute.",
	doctor_failed: "Could not update Jarvis's saved sign-ins. Try again in a minute.",
};

/** Plain copy for a relayed admin/fleet failure body ("admin unreachable: ..."). */
function relayedFailureCopy(body) {
	const rest = body.replace(RELAY_STATUS, "").replace(RELAY_HTTP, "");
	const code = RELAY_CODE.exec(rest.toLowerCase() + " ");
	return (code && RELAY_CODE_COPY[code[1]]) || RELAY_GENERIC;
}

/**
 * The reason behind a "failed:" status, cleaned up for display: prefix stripped,
 * newlines collapsed (a traceback would otherwise blow out whatever it lands in)
 * and length-capped. A relayed admin/fleet failure becomes plain copy.
 */
function failureDetail(s) {
	const body = s
		.replace(/^failed:?/i, "")
		.replace(/\s+/g, " ")
		.trim();
	if (!body) return "";
	if (RELAY_PREFIX.test(body)) return relayedFailureCopy(body);
	return body.length > MAX_DETAIL ? body.slice(0, MAX_DETAIL - 1).trimEnd() + "…" : body;
}
