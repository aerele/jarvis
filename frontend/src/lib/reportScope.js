// Read durable ERP tool receipts, never assistant prose or requested filters.
export function reportScopes(tools) {
	const scopes = [];
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
		scopes.push({
			id: tool.name,
			report_name: scope.report_name,
			company: scope.company,
			report_date: scope.report_date,
			currency:
				scope.currency_mode === "account"
					? "Party/account currency"
					: (typeof scope.currency === "string" && scope.currency.trim()) ||
					  "Company currency",
		});
	}
	return scopes;
}
