// Tool rows seen live, for the reply that is running.
//
// The bench publishes `tool:result` the moment one of its tools returns, with
// the id of the tool row it just saved. The steps box count, its details and
// the "Reports consulted" card all read tool rows, which used to arrive only
// with the enrichment reload a few seconds after the answer, so the count
// changed ("1 tool" -> "2 tools", the plugin's memory recall has no tool
// start) and the card popped in above a finished answer. Merging the live rows
// in makes all three right before the answer lands; the reload then brings
// rows with the same names, so nothing moves.
//
// A report binds to its answer through the reply's `tool_call_ids`
// (reportScope.reportToolsByAssistant), which the server fills from the run's
// own tool events. The live reply row has none yet, so the merge lists the
// live rows' call ids on it: a `tool:result` is only kept while this chat's
// run is live, and a chat runs one turn at a time, so it is that run's tool.
//
// Pure functions only, pinned by liveToolRows.spec.js.

/**
 * A `tool:result` event as the saved tool row it stands for. `ord` is the
 * order this tab saw it in among the turn's steps and results
 * (liveTurn.activityItems); a saved row orders by its `creation` instead.
 */
export function toolRowFromResult(p, ord = undefined) {
	const row = {
		name: p.tool_message_id,
		role: "tool",
		tool_call_id: p.tool_call_id || null,
		tool_name: p.tool_name,
		tool_status: p.status || "completed",
		tool_args: p.args ?? null,
		tool_result: p.result ?? null,
		action_outcome: p.action_outcome || null,
	};
	if (Number.isFinite(ord)) row.ord = ord;
	return row;
}

/** `rows` with `row` added, or replacing the row of the same name (a new list). */
export function upsertToolRow(rows, row) {
	const list = Array.isArray(rows) ? rows : [];
	if (!row || !row.name) return list;
	const at = list.findIndex((r) => r.name === row.name);
	if (at === -1) return [...list, row];
	return list.map((r, i) => (i === at ? row : r));
}

function callIds(ids) {
	let list = ids;
	if (typeof list === "string") {
		try {
			list = JSON.parse(list);
		} catch {
			return [];
		}
	}
	return Array.isArray(list) ? list : [];
}

/**
 * `messages` with the live rows placed after their assistant row (and any tool
 * rows already loaded for it), skipping rows the transcript already has, and
 * that assistant row standing in as `{name, role, tool_call_ids}` with the live
 * rows' call ids added. Only those fields: spreading the whole row would read
 * the streaming `content` and rerun every consumer on each reveal tick (the
 * two consumers, activity and report grouping, read nothing else of a reply).
 * Returns `messages` itself when there is nothing to add.
 *
 * @param {Array<{name: string, role: string}>} messages
 * @param {{msgId: string|null, rows: Array<{name: string}>}} live
 */
export function withLiveToolRows(messages, live) {
	const list = Array.isArray(messages) ? messages : [];
	if (!live || !live.msgId || !Array.isArray(live.rows) || !live.rows.length) return list;
	const at = list.findIndex((m) => m.name === live.msgId);
	if (at === -1) return list;
	const have = new Set(list.map((m) => m.name));
	const extra = live.rows.filter((r) => !have.has(r.name));
	if (!extra.length) return list;
	let end = at + 1;
	while (end < list.length && list[end].role === "tool") end++;
	const owned = callIds(list[at].tool_call_ids);
	const added = extra.map((r) => r.tool_call_id).filter((id) => id && !owned.includes(id));
	const reply = added.length
		? { name: list[at].name, role: list[at].role, tool_call_ids: [...owned, ...added] }
		: list[at];
	return [...list.slice(0, at), reply, ...list.slice(at + 1, end), ...extra, ...list.slice(end)];
}
