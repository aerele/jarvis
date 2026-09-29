// Read durable ERP tool receipts, never assistant prose or requested filters.
export function reportScopes(tools) {
	const scopes = [];
	const shown = new Set();
	for (const tool of tools) {
		if (
			tool.role !== "tool" ||
			tool.tool_name !== "run_report" ||
			tool.tool_status !== "completed"
		)
			continue;
		let result = tool.tool_result;
		if (typeof result === "string") {
			try {
				result = JSON.parse(result);
			} catch {
				continue;
			}
		}
		const data = result?.ok === true ? result.data : null;
		if (!Array.isArray(data?.result)) continue;
		if (data.prepared_report && data.status !== "ready") continue;
		if (data.status != null && data.status !== "ready") continue;
		const scope = data.report_scope;
		if (scope?.version !== 1 || !["company", "account"].includes(scope.currency_mode))
			continue;
		if (
			![scope.company, scope.report_name, scope.report_date].every(
				(v) => typeof v === "string" && v.trim()
			)
		)
			continue;
		const currencies =
			Array.isArray(scope.currencies) &&
			scope.currencies.length &&
			scope.currencies.every((code) => typeof code === "string" && code.trim())
				? [...new Set(scope.currencies)].join(", ")
				: null;
		const entry = {
			report_name: scope.report_name,
			company: scope.company,
			report_date: scope.report_date,
			generated_at: data.prepared_report
				? (typeof data.as_of === "string" && data.as_of.trim()) || "Unknown"
				: null,
			row_note: typeof data.row_note === "string" ? data.row_note : null,
			currency:
				currencies ||
				(scope.currency_mode === "account"
					? "Party/account currency"
					: (typeof scope.currency === "string" && scope.currency.trim()) ||
					  "Company currency"),
		};
		// One turn may run the same report twice; a repeated chip adds nothing.
		// Any visible difference (date, currency, freshness, note) stays separate.
		const key = JSON.stringify(Object.values(entry));
		if (shown.has(key)) continue;
		shown.add(key);
		scopes.push({ id: tool.name, ...entry });
	}
	return scopes;
}

// Only run events can bind a report to an answer. Legacy/unbound receipts stay
// in Activity; assigning them by transcript position could mislabel another turn.
export function reportToolsByAssistant(messages) {
	const owners = new Map();
	for (const message of messages) {
		if (message.role !== "assistant") continue;
		let ids = message.tool_call_ids;
		try {
			if (typeof ids === "string") ids = JSON.parse(ids);
		} catch {
			continue;
		}
		if (!Array.isArray(ids)) continue;
		for (const id of ids) {
			if (typeof id !== "string" || !id) continue;
			// Conflicting ownership is unknown, never last-writer-wins.
			if (!owners.has(id)) owners.set(id, message.name);
			else if (owners.get(id) !== message.name) owners.set(id, null);
		}
	}
	const grouped = Object.create(null);
	const seen = new Set();
	for (const message of messages) {
		const owner = owners.get(message.tool_call_id);
		if (message.role !== "tool" || !owner || seen.has(message.tool_call_id)) continue;
		seen.add(message.tool_call_id);
		(grouped[owner] ||= []).push(message);
	}
	return grouped;
}
