// The `?settings=<pane>` deep link desk surfaces and the reconnect email use. Pure so the
// AppShell handler is testable. `?reconnect=<account_ref>` (I8) only means something on the AI
// models pane: it becomes the intent that pane reads on mount (`direct:openai` keys a lone
// direct-mode OpenAI subscription). `rest` is null when there is no `settings` param (a plain
// load is left alone), else the remaining query string to put back in the URL.
// Panes that moved out of Settings, by their old key: where a saved link goes now.
const MOVED = { macroadmin: "MacroAdmin" };

// The route a `?settings=<key>` link should open in place of a pane, or null: the route
// name, with whatever else the address carried (`rest` from parseSettingsDeepLink, and
// the hash) kept.
export function movedSettingsRoute(section, rest = "", hash = "") {
	const name = MOVED[section];
	if (!name) return null;
	return { name, query: queryOf(rest), hash: hash || "" };
}

// A query string as the router takes it: a key given more than once keeps every
// value, as an array (Object.fromEntries would keep only the last). No prototype,
// so a key named like an inherited property (`toString`) is a key like any other.
function queryOf(search) {
	const query = Object.create(null);
	for (const [key, value] of new URLSearchParams(search || "")) {
		query[key] = key in query ? [].concat(query[key], value) : value;
	}
	return query;
}

export function parseSettingsDeepLink(search, keys) {
	const params = new URLSearchParams(search || "");
	if (!params.has("settings")) return { section: "", intent: null, rest: null };
	const key = params.get("settings");
	const reconnect = (params.get("reconnect") || "").trim();
	params.delete("settings");
	params.delete("reconnect");
	return {
		section: key && keys.has(key) ? key : "",
		intent: key === "aimodels" && reconnect ? { reconnect } : null,
		rest: params.toString(),
	};
}
