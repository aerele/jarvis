import { receiptView } from "@/lib/actionSummary";

function parseJson(v) {
	if (v == null || typeof v === "object") return v;
	try {
		return JSON.parse(v);
	} catch (e) {
		return null;
	}
}

// A confirmed card writes a receipt chip plus an assistant row repeating its
// sentence ("Updated ToDo X."), so the thread said it twice. True when `msg` is
// only that echo of the chip `prev` right before it: render-time, row kept. A
// longer reply (e.g. the Trigger "It is on." note) is real text and stays.
export function isReceiptEcho(msg, prev) {
	if (!msg || msg.role !== "assistant" || msg.error || msg.streaming) return false;
	if (!prev || prev.role !== "tool" || prev.action_outcome !== "confirmed") return false;
	const { title } = receiptView(
		prev.tool_name,
		parseJson(prev.tool_args) || {},
		parseJson(prev.tool_result),
		"confirmed"
	);
	const text = (msg.content || "").replace(/\s+/g, " ").trim().replace(/\.$/, "");
	return text === title;
}
