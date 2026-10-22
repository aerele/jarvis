// Ambiguous or partial projected history stays visible rather than losing tools.
const ids = (value) => {
	try {
		const list = typeof value === "string" ? JSON.parse(value) : value;
		return Array.isArray(list) ? list.filter((id) => typeof id === "string" && id) : [];
	} catch {
		return [];
	}
};
const decision = (row) =>
	row.awaiting_actor ||
	row.awaiting_owner ||
	row.control ||
	row.action_outcome ||
	row.tool_status === "pending";
export function sharedActivity(rows = []) {
	const tools = new Map(),
		grouped = new Set();
	let segment = [],
		actor,
		run;
	function flush() {
		const owners = new Map();
		for (const row of segment)
			if (row.role === "assistant") {
				for (const id of ids(row.tool_call_ids))
					owners.set(id, [...(owners.get(id) || []), row]);
			}
		let previous = null;
		for (const row of segment) {
			if (row.role === "assistant") previous = row;
			if (row.role !== "tool") continue;
			const matches = row.tool_call_id ? owners.get(row.tool_call_id) || [] : [];
			const parent = row.tool_call_id
				? matches.length === 1
					? matches[0]
					: null
				: previous &&
				  (!previous.tool_call_ids ||
						(Array.isArray(previous.tool_call_ids) &&
							!previous.tool_call_ids.length) ||
						previous.tool_call_ids === "[]")
				? previous
				: null;
			if (!parent || (parent.run_id && row.run_id && parent.run_id !== row.run_id)) continue;
			tools.set(parent.name, [...(tools.get(parent.name) || []), row]);
			grouped.add(row.name);
		}
		segment = [];
	}
	for (const row of rows) {
		const nextActor = row.actor?.id || "";
		if (row.role === "user" || decision(row)) {
			flush();
			actor = undefined;
			run = undefined;
			continue;
		}
		if (
			(actor !== undefined && actor !== nextActor) ||
			(run && row.run_id && run !== row.run_id)
		) {
			flush();
			run = undefined;
		}
		actor = nextActor;
		if (row.run_id) run = row.run_id;
		segment.push(row);
	}
	flush();
	return { messages: rows.filter((row) => !grouped.has(row.name)), tools };
}
export function clearSharedToolDetails(cache) {
	for (const entry of cache.values()) {
		entry.valid = false;
		entry.version++;
		entry.detail = null;
	}
	cache.clear();
}
