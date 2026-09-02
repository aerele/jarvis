// Count only customer-facing (jarvis__*) tools, not the agent's internal built-ins
// (read/exec/…), in both the live counter and the settled accordion so they never disagree.
// tool_name differs by source: live events keep the jarvis__ prefix; persisted rows strip it
// (call_tool), so there we key off I/O — a jarvis tool has args+result, a built-in doesn't.

export function isCustomerFacingTool(toolName) {
	return String(toolName || "").startsWith("jarvis__");
}

export function shouldHideActivityTool(m) {
	if (!m || m.role !== "tool") return false;
	const hasArgs = m.tool_args != null && m.tool_args !== "";
	const hasResult = m.tool_result != null && m.tool_result !== "";
	return !hasArgs && !hasResult;
}
