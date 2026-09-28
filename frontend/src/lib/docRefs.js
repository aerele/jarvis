/**
 * {document name -> DocType} harvested from a conversation's tool calls
 * (get_doc / create_doc / get_list / update_doc / ...). The chat only ever
 * linkifies IDs that actually came back from a tool, so it always knows the
 * DocType for the Desk URL and never false-positives on arbitrary prose.
 *
 * Records of the `DocType` doctype are skipped (jarvis-admin-v2#620): once the
 * agent read a DocType's definition, every later mention of "Sales Order" in the
 * chat linked to the core DocType form instead of reading as plain text.
 *
 * @param {Array<{role: string, tool_args?: string, tool_result?: string}>} messages
 * @returns {Record<string, string>} document name -> DocType
 */
export function collectDocRefs(messages) {
	const map = {};
	const add = (dt, name) => {
		if (dt === "DocType") return;
		if (dt && typeof name === "string" && name.length >= 4) map[name] = dt;
	};
	for (const m of messages) {
		if (m.role !== "tool") continue;
		const args = parseJson(m.tool_args);
		const res = parseJson(m.tool_result);
		const dt = args.doctype;
		if (args.name) add(dt, args.name);
		const data = res && res.data;
		if (Array.isArray(data)) {
			for (const row of data) if (row && row.name) add(row.doctype || dt, row.name);
		} else if (data && typeof data === "object") {
			add(data.doctype || dt, data.name);
		}
	}
	return map;
}

// A missing, unparsable or null payload reads as an empty object.
function parseJson(text) {
	try {
		return (text && JSON.parse(text)) || {};
	} catch (e) {
		return {};
	}
}
