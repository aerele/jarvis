import { messageText, serverSentMessage } from "./errors.js";

// Request identity survives uncertain delivery; only definite rejection gets a new ID.
export function newSendRequestId(rng = globalThis.crypto) {
	const bytes = rng.getRandomValues(new Uint8Array(16));
	return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function deliveryOutcome(envelope) {
	const r = envelope?.delivery === "settled" ? envelope.result : null;
	if (r?.confirmed === true)
		return typeof r.ok === "boolean" && r.conversation_id ? "confirmed" : "uncertain";
	if (r?.ok === true && r.conversation_id && r.message_id && r.run_id) return "accepted";
	return r?.ok === false ? "rejected" : "uncertain";
}

// The deadline exposes recovery; it does not discard a later authoritative result.
export async function observeDelivery(promise, onTimeout, timeout = 30000) {
	const timer = setTimeout(onTimeout, timeout);
	try {
		return await promise;
	} finally {
		clearTimeout(timer);
	}
}

// Fixed vocabulary: server/transport exception text never becomes recovery copy.
export function deliveryDiagnostic(status) {
	return (
		{
			missing: "No delivery receipt was found. Retry the same request to submit it safely.",
			pending:
				"The server received this request but has no final outcome. It may still be working or may have stopped unexpectedly. Check the conversation before sending a new request; retrying this same request will not dispatch it twice.",
			interrupted:
				"Processing was interrupted after work may have started. Some actions may have completed. Check the conversation and action receipts before sending a new request; retrying this same request will not repeat the work.",
			conversation_deleted:
				"This conversation was deleted. The original request will not be sent again. Review any action receipts before starting a new chat.",
			unavailable:
				"The delivery receipt is no longer available. Check the conversation before sending a new request.",
			// send_requests.send_message: the request ID was already claimed by a
			// different payload, so this one was never dispatched.
			// Recovery keeps the same ID (that is the whole point of the uncertain
			// state), so a same-request retry would mismatch again: the way out is
			// a new send from the composer.
			request_mismatch:
				"This request was not sent: its delivery ID already belongs to a different message. Dismiss this and send the message again from the composer.",
			// check_delivery's own read failed server-side ({delivery: "unknown"}
			// with no receipt_status): the receipt may exist, it just could not be read.
			unknown:
				"Delivery not confirmed. The server could not read the delivery receipt for this request just now. Check the conversation, or check delivery again in a moment.",
		}[status] || "Delivery not confirmed. Check delivery before sending again."
	);
}

export function settledSendResult(envelope) {
	if (deliveryOutcome(envelope) !== "uncertain") return envelope.result;
	// `reason` is only ever the server's request_mismatch; any other spelling
	// must not collide with a receipt_status key by accident.
	const status =
		envelope?.receipt_status ||
		(envelope?.reason === "request_mismatch" ? "request_mismatch" : undefined) ||
		(envelope?.delivery === "unknown" ? "unknown" : undefined);
	const error = new Error(deliveryDiagnostic(status));
	error.deliveryUncertain = true;
	throw error;
}

export async function boundedDelivery(promise, timeout = 30000) {
	let timer;
	try {
		return await Promise.race([
			promise,
			new Promise((_, reject) => {
				timer = setTimeout(() => reject(new Error("Delivery check timed out")), timeout);
			}),
		]);
	} finally {
		clearTimeout(timer);
	}
}

// A send whose answer never came within the deadline: not an exception, so the
// catch blocks never see it. observeDelivery's timeout callback builds this
// marker and hands it to deliveryFailureCopy like any other failure.
export function deliveryTimeoutError(timeoutMs) {
	return { deliveryTimeout: true, timeoutMs };
}

const SERVER_MESSAGE_CHARS = 240;
// The browser's own wording for a fetch that never reached a server (Chrome,
// Firefox, Safari, React Native). A TypeError with any OTHER message is a bug
// in this app, not the network, and is reported as such.
const NETWORK_ERROR_RE = /failed to fetch|networkerror|load failed|network request failed/i;

// WHAT stopped the answer, in a fixed vocabulary keyed on transport facts: the
// deadline, the browser's network state and the HTTP status. The server's own
// sentence is quoted only when Frappe sent one on purpose (_server_messages /
// frappe.throw); a raw traceback, an exception class name or the browser's
// TypeError text never reaches the customer (the Reference ID is what support
// uses to find those). Before this, every one of these read as "Delivery not
// confirmed." and support had nothing to go on.
export function deliveryFailureCause(error) {
	if (!error) return "The request failed before the server answered.";
	if (error.deliveryTimeout) {
		const seconds = Math.max(1, Math.round((error.timeoutMs || 30000) / 1000));
		return `The server did not answer within ${seconds} seconds. It may still be working on this message.`;
	}
	if (/^delivery check timed out$/i.test(error.message || ""))
		return "The delivery check did not get an answer from the server in time.";
	const status = Number(error.status);
	const said = serverSentence(error);
	// A definite server answer outranks the browser's network flag: the device
	// going offline AFTER a 500 arrived does not change what happened.
	if (status === 502 || status === 503 || status === 504)
		return `The server is not reachable right now (HTTP ${status}). It may be restarting.`;
	if (status === 429) return "The server is receiving too many requests right now (HTTP 429).";
	if (status === 413)
		return "The message or its attachments are too large for the server (HTTP 413).";
	if (status >= 500)
		return `The server hit an error while handling this message (HTTP ${status}).${said}`;
	if (status >= 400) return `The server refused this request (HTTP ${status}).${said}`;
	if (typeof navigator !== "undefined" && navigator.onLine === false)
		return "This device is offline, so the message could not reach the server.";
	if (NETWORK_ERROR_RE.test(error.message || ""))
		return "The network connection dropped before the server answered.";
	if (error instanceof TypeError)
		return "The app hit an error before the request reached the server.";
	return `The request failed before the server answered.${said}`;
}

function serverSentence(error) {
	const first = serverSentMessage(error);
	if (!first.trim()) return "";
	// frappe-ui fills this in when the server sent nothing; it adds no information.
	if (/^internal server error$/i.test(first.trim())) return "";
	// messageText drops markup via DOMParser in the browser and passes text
	// through untouched under node (errors.test pins that); the tags are cut
	// here first so the note reads the same in both. Plain text in both sinks
	// (the bubble's note and the chat toast interpolate text), so the decoded
	// entities are safe.
	const text = messageText(first.replace(/<[^>]*>/g, ""))
		.replace(/\s+/g, " ")
		.trim();
	if (!text) return "";
	const clipped =
		text.length > SERVER_MESSAGE_CHARS ? text.slice(0, SERVER_MESSAGE_CHARS - 1) + "…" : text;
	return ` The server said: ${clipped}`;
}

// The last 8 characters of the request ID: enough for support to find the
// server's own "Chat send interrupted" log line, which carries the full key.
function referenceClause(requestId) {
	return requestId ? ` Reference ${String(requestId).slice(-8)}.` : "";
}

// Transport gates need an actionable recovery step, not raw server exception text.
// Keep the same request ID: a failed check does not disprove an earlier dispatch.
export function deliveryFailureCopy(error, requestId) {
	return deliveryFailureLead(error) + referenceClause(requestId);
}

function deliveryFailureLead(error) {
	if (error?.exc_type === "CSRFTokenError")
		return "Your session security token expired. Keep a copy of this message before reloading, then check delivery before sending again.";
	if (
		error?.status === 401 ||
		["AuthenticationError", "SessionExpired"].includes(error?.exc_type)
	)
		return "Sign in again, then check delivery before sending again. Your message is preserved in this tab.";
	if (error?.status === 403 || error?.exc_type === "PermissionError")
		return "Access was refused. Sign in again or ask an administrator to restore your Jarvis access, then check delivery. Your message is preserved.";
	if (error?.deliveryUncertain) return error.message;
	return (
		deliveryFailureCause(error) +
		" Your message is preserved here. Check delivery before sending it again."
	);
}

// The toast beside the bubble lives 3 seconds: the title and where to look,
// not the whole note. The note under the message carries the detail.
export const DELIVERY_TOAST = "Delivery not confirmed. See the note under your message.";
