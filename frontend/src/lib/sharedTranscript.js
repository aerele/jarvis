import { BLOCK_TAGS } from "./chatBlocks";
import { __ } from "./i18n";

function xySplit(text) {
	return (String(text).match(/"(?:[^"\\]|\\.)*"|'[^']*'|[^,]+/g) || []).map((s) =>
		s.trim().replace(/^["']|["']$/g, "")
	);
}
function xychart(body) {
	if (!/^\s*xychart-beta\b/.test(body)) return null;
	const title = body.match(/title[ \t]+"([^"]*)"/)?.[1];
	const yLabel = body.match(/y-axis[ \t]+"([^"]*)"/)?.[1];
	const x = xySplit(body.match(/x-axis[ \t]+\[([^\]]*)\]/)?.[1] || "");
	const series = [];
	let bars = false;
	for (const match of body.matchAll(/\b(bar|line)[ \t]+(?:"([^"]*)"[ \t]+)?\[([^\]]*)\]/g)) {
		const data = xySplit(match[3]).map(Number).filter(Number.isFinite);
		if (!data.length) continue;
		bars ||= match[1] === "bar";
		series.push({ name: match[2] || yLabel || "Value", data });
	}
	return series.length
		? {
				type: bars ? "bar" : "line",
				x,
				series,
				...(title ? { title } : {}),
				...(/xychart-beta[ \t]+horizontal\b/.test(body)
					? { options: { horizontal: true } }
					: {}),
		  }
		: null;
}
function inertSummary(body) {
	try {
		const value = JSON.parse(body);
		if (value && typeof value === "object" && !Array.isArray(value))
			return Object.entries(value)
				.map(
					([key, item]) =>
						key.replaceAll("_", " ") +
						": " +
						(typeof item === "string" ? item : JSON.stringify(item, null, 2))
				)
				.join("\n");
	} catch {
		/* Plain-text markers remain plain text. */
	}
	return body;
}
// Presentation only. Structured action data is always escaped text, never an
// action component, document prefill, write handler or navigation instruction.
export function sharedTranscript(content, streaming = false) {
	const text = String(content || "");
	const parts = [];
	let at = 0;
	const fence = /^```([\w-]*)[^\n]*\n([\s\S]*?)^```[ \t]*$/gm;
	const addText = (value) => {
		if (value.trim()) parts.push({ type: "text", text: value.trim() });
	};
	for (const match of text.matchAll(fence)) {
		addText(text.slice(at, match.index));
		at = match.index + match[0].length;
		const tag = match[1],
			body = match[2].trim();
		let spec = null;
		if (tag === "jarvis-chart") {
			try {
				const value = JSON.parse(body);
				if (value && typeof value === "object" && !Array.isArray(value)) spec = value;
			} catch {
				/* Render malformed output as an inert summary. */
			}
		} else if (tag === "mermaid") spec = xychart(body);
		if (spec) parts.push({ type: "chart", spec });
		else if (BLOCK_TAGS.includes(tag))
			parts.push({
				type: "summary",
				label: tag.replace("jarvis-", ""),
				text: inertSummary(body),
			});
		else if (tag === "mermaid") parts.push({ type: "diagram", text: body });
		else addText(match[0]);
	}
	let tail = text.slice(at);
	if (streaming)
		tail = tail.replace(
			new RegExp(
				"\\n?```(?:" + [...BLOCK_TAGS, "mermaid"].join("|") + ")[ \\t]*(?:\\n[\\s\\S]*)?$"
			),
			""
		);
	addText(tail);
	return parts;
}
// The server rewrites attachment URLs. Reject arbitrary paths/remote URLs at
// the renderer too; a shared view never invokes the owner's preview endpoint.
export function sharedFileUrl(value, conversation) {
	const allowed = [
		"/api/method/jarvis.chat.sharing.get_shared_file",
		"/api/method/jarvis.chat.collaboration_api.get_file",
	];
	if (typeof value !== "string" || !allowed.some((path) => value.startsWith(path + "?")))
		return "";
	try {
		const url = new URL(value, "https://shared.invalid");
		return url.origin === "https://shared.invalid" &&
			allowed.includes(url.pathname) &&
			url.searchParams.getAll("conversation").length === 1 &&
			url.searchParams.get("conversation") === conversation &&
			url.searchParams.getAll("file_url").length === 1
			? value
			: "";
	} catch {
		return "";
	}
}
export function hiddenShareReason(reason) {
	return (
		{
			recipient_unavailable: __("Recipient is disabled or no longer has Jarvis access"),
			owner_unavailable: __("Owner is disabled or no longer has Jarvis access"),
			chat_unavailable: __("This chat is no longer shareable"),
			sharing_off: __("Sharing is turned off on this site"),
			recipient_disabled: __("Recipient is disabled"),
			recipient_no_access: __("Recipient no longer has Jarvis access"),
			owner_disabled: __("Owner is disabled"),
			owner_no_access: __("Owner no longer has Jarvis access"),
			not_shareable: __("This chat is no longer shareable"),
		}[reason] || __("Access is currently unavailable")
	);
}
