import rules from "../../../jarvis/public/js/turn_error_rules.mjs";

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

// Deliberately accepts diagnostic metadata only. Never copy an error string,
// caller-supplied headline, chat transcript, URL, tool data or attachment here.
// Fixed English labels help support staff; the existing composer remains editable.
export function supportErrorDraft({ code, upstream, conversation, message, at } = {}) {
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
	return {
		subject: [...`Chat error: ${headline}`].slice(0, 140).join(""),
		body: [
			"Please describe what you expected and what happened:",
			"",
			"",
			"Error context",
			`Summary: ${headline}`,
			`Error type: ${rule.code}`,
			`Conversation: ${reference(conversation)}`,
			`Message: ${reference(message)}`,
			`Saved (server time): ${savedTime(at)}`,
		].join("\n"),
	};
}
