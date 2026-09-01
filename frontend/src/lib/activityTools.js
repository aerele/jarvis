// Which role=tool receipts to drop from the customer-facing "N tool calls" activity
// accordion. openclaw built-in tools (read/exec/bash/canvas/image/edit/…) get a
// pump-owned receipt that records only tool_name + tool_status — never args/result
// (jarvis/chat/pump.py) — so their card expands to nothing. jarvis__* platform tools go
// through call_tool WITH args+result, so they always carry content.
export function shouldHideActivityTool(m) {
	if (!m || m.role !== "tool") return false;
	if (String(m.tool_name || "").startsWith("jarvis__")) return false; // platform tool: keep
	// Keep any failure so it surfaces — mirrors the status-dot classing (only
	// "completed"/"running" are non-failure states).
	if (m.tool_status !== "completed" && m.tool_status !== "running") return false;
	const hasArgs = m.tool_args != null && m.tool_args !== "";
	const hasResult = m.tool_result != null && m.tool_result !== "";
	return !hasArgs && !hasResult; // built-in receipt with nothing to render
}
