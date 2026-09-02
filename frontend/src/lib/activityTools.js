// Customer-facing (jarvis__* platform) tools vs. the agent's internal built-ins
// (read/exec/bash/canvas/…). Both the live "N tools" counter and the settled activity
// accordion must count only the customer-facing tools, so the two never disagree (no 3→2
// jump when a built-in finishes). The catch: the tool_name is represented DIFFERENTLY in
// the two places, so each check uses the signal actually available to it:
//
//   • LIVE (streaming tool events, activeTools[].name): the event name KEEPS the "jarvis__"
//     prefix — the same signal the backend's own is_jarvis check uses — so a prefix test
//     is exact here.
//   • SETTLED (persisted Jarvis Chat Message rows, m.tool_name): call_tool STRIPS the
//     prefix before storing (rows read "find_skills", not "jarvis__find_skills"), so a
//     prefix test would wrongly hide every platform tool. Instead we use the reliable
//     structural signal: a jarvis__* tool always persists args+result (it round-trips
//     through call_tool); an internal built-in's pump-owned receipt carries only
//     name+status, so "no args AND no result" identifies exactly the built-ins to hide.

// Live check — event names carry the jarvis__ prefix.
export function isCustomerFacingTool(toolName) {
	return String(toolName || "").startsWith("jarvis__");
}

// Settled check — persisted rows have no prefix, so key off captured I/O.
export function shouldHideActivityTool(m) {
	if (!m || m.role !== "tool") return false;
	const hasArgs = m.tool_args != null && m.tool_args !== "";
	const hasResult = m.tool_result != null && m.tool_result !== "";
	return !hasArgs && !hasResult; // internal built-in receipt (name+status only) — nothing to show
}
