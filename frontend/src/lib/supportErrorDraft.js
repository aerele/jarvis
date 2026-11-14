import rules from "../../../jarvis/public/js/turn_error_rules.mjs";
import { turnErrorInfo } from "../../../jarvis/public/js/turn_errors.mjs";

const byCode = new Map(rules.map((rule) => [rule.code, rule]));
const subscriptionLabels = byCode.get("subscription-expired").upstreams.labels;

function reference(value) {
	return typeof value === "string" && /^[A-Za-z0-9_-]{1,140}$/.test(value)
		? value
		: "Unavailable";
}

function savedTime(value) {
	// Frappe timestamps have no timezone. Keep server time; never infer browser time.
	return typeof value === "string" &&
		value.length <= 32 &&
		/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$/.test(value)
		? value
		: "Unavailable";
}

// The stored error is capped at 1000 chars on the server; the draft never carries more.
const DETAIL_MAX = 1000;

function modelId(value) {
	return typeof value === "string" && /^[\w.:/+-]{1,140}$/.test(value) ? value : "";
}

// `details` is the server's reading of the failed reply (jarvis.support.api.error_report_details):
// the error with secrets removed, the model and the run id. Support cannot open the customer's site,
// so the ticket must carry them. Never pass the browser's own copy of the error here: only the
// server-scrubbed text. The chat transcript, tool data, files and URLs never go in; the composer
// stays editable so the customer sees and can change everything before sending.
function detailLines(details) {
	if (!details || typeof details !== "object" || typeof details.error !== "string") return null;
	const text = details.error.slice(0, DETAIL_MAX).trim();
	if (!text) return null;
	const info = turnErrorInfo(text);
	const lines = [];
	if (Array.isArray(info.models) && info.models.length) {
		lines.push("Models:");
		for (const m of info.models) lines.push(`- ${m.model || "unknown"}: ${m.reason}`);
	} else if (modelId(details.model)) {
		lines.push(`Model: ${details.model}`);
	}
	lines.push(`Run: ${reference(details.run)}`);
	return { headline: info.headline, lines, text };
}

// Accepts diagnostic metadata, plus the server-scrubbed `details` above. Never a caller-supplied
// headline, chat transcript, URL, tool data or attachment. Fixed English labels help support staff.
export function supportErrorDraft({ code, upstream, conversation, message, at, details } = {}) {
	let rule =
		(typeof code === "string" && code.length < 64 && byCode.get(code)) ||
		byCode.get("internal");
	if (rule.code === "cancelled") return null;
	let headline = rule.headline;
	if (rule.code === "subscription-expired") {
		if (typeof upstream === "string" && Object.hasOwn(subscriptionLabels, upstream)) {
			headline = headline.replace("{provider}", subscriptionLabels[upstream]);
		} else {
			rule = byCode.get("authentication");
			headline = rule.headline;
		}
	}
	const detail = detailLines(details);
	if (detail && detail.headline) headline = detail.headline;
	return {
		subject: [...`Chat error: ${headline}`].slice(0, 140).join(""),
		body: [
			"Please describe what you expected and what happened:",
			"",
			"",
			"Error context",
			`Summary: ${headline}`,
			`Error type: ${rule.code}`,
			...(detail ? detail.lines : []),
			`Conversation: ${reference(conversation)}`,
			`Message: ${reference(message)}`,
			`Saved (server time): ${savedTime(at)}`,
			...(detail ? ["", "Error text (secrets removed):", detail.text] : []),
		].join("\n"),
	};
}
