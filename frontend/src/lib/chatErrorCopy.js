// Plain-words copy for the failures a chat surface shows outside the failed-turn card
// (turnErrorInfo owns that one). Each helper returns a sentence that says what went
// wrong and, when the user or their workspace admin can fix it, the next step. None of
// them quotes an exception message: only what the server said on purpose (frappe.throw),
// the expired-session sentence, or the offline sentence.
import {
	OFFLINE_MESSAGE,
	SESSION_EXPIRED_MESSAGE,
	isNetworkFailure,
	isSessionExpired,
	messageText,
	serverSentMessage,
} from "./errors.js";

export function isPermissionError(e) {
	return !!e && (e.exc_type === "PermissionError" || e.status === 403) && !isSessionExpired(e);
}

// The cause as a sentence, or "" when the error carries nothing a user can read.
export function causeOf(e) {
	if (isSessionExpired(e) || (e && e.status === 401)) return SESSION_EXPIRED_MESSAGE;
	if (isNetworkFailure(e)) return OFFLINE_MESSAGE;
	return messageText(serverSentMessage(e));
}

const trimDot = (s) => String(s).replace(/[.\s]+$/, "");

// The step that follows a cause: nothing when the cause already carries one (expired
// session, offline), the admin when it is a permission refusal, otherwise retry.
export function nextStepOf(e) {
	const cause = causeOf(e);
	if (cause === SESSION_EXPIRED_MESSAGE || cause === OFFLINE_MESSAGE) return "";
	if (isPermissionError(e)) return "Ask your workspace admin if you need access.";
	return "Try again.";
}

// "<what failed>. <cause and next step>" for a failed action (toast text, plain).
// `what` is the per-action sentence start ("Couldn't clear your history").
// `said` is a cause the caller already has (a refused envelope's sentence).
export function actionErrorCopy(what, e, said = "") {
	const head = trimDot(what);
	const cause = said || causeOf(e);
	if (!cause) return `${head}. Try again in a moment.`;
	if (cause === SESSION_EXPIRED_MESSAGE || cause === OFFLINE_MESSAGE) return `${head}. ${cause}`;
	return `${head}: ${trimDot(cause)}. ${nextStepOf(e)}`;
}

// An action card's error object for a request that threw. What the server said, an
// expired session and a refused permission are for the user or admin to fix, so they stay
// plain messages. Anything else is internal: it carries InternalError so the card offers
// Report error.
export function thrownActionError(e, fallback) {
	const cause = causeOf(e);
	const status = Number(e && e.status) || 0;
	if (cause && status < 500) return { message: cause };
	return { code: "InternalError", message: fallback, hint: "Try again in a moment." };
}

// Why an attachment failed. The upload helper throws "upload failed (413)" or
// "larger than the 25 MB limit"; frappe-ui's uploader throws the server's message.
export function uploadErrorCopy(e) {
	if (isNetworkFailure(e)) return OFFLINE_MESSAGE;
	const text = String((e && e.message) || "");
	const code = Number((e && e.status) || (/\((\d{3})\)/.exec(text) || [])[1]) || 0;
	if (code === 401 || isSessionExpired(e)) return SESSION_EXPIRED_MESSAGE;
	const limit = /larger than the (\d+) MB limit/i.exec(text);
	if (limit) return `That file is larger than ${limit[1]} MB. Choose a smaller file.`;
	if (code === 413 || /too large|exceeds|file size|size limit/i.test(text)) {
		return "That file is too large. Choose a smaller file and try again.";
	}
	if (code === 415 || /not allowed|file type|extension|unsupported/i.test(text)) {
		return "That file type isn't allowed. Choose a different file, or ask your workspace admin which types are allowed.";
	}
	if (code === 403) {
		return "You don't have permission to attach files. Ask your workspace admin for access.";
	}
	const said = messageText(serverSentMessage(e));
	if (said) return said;
	return "Couldn't attach that file. Try again.";
}

export const MIC_BLOCKED_MESSAGE =
	"Allow microphone access in your browser settings, then try again.";
export const RECORDING_FAILED_MESSAGE = "Recording failed. Try again.";

// The recorders' own sentences are already plain; only the blocked-microphone one needs
// the browser-settings step, and anything empty gets the generic recording line.
export function micErrorCopy(msg, fallback = RECORDING_FAILED_MESSAGE) {
	const text = String(msg || "").trim();
	if (!text) return fallback;
	if (/microphone access is blocked|permission denied|not ?allowed/i.test(text)) {
		return MIC_BLOCKED_MESSAGE;
	}
	return text;
}
