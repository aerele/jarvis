// Loads the golden files produced by PR 1's Python test (never hand-edit them).
// A new golden/<name>.json is picked up without any code change here.
// Two shapes exist: a status file `{scenario, status}` and an API reply file
// `{scenario, request, data}`.
const files = import.meta.glob("./golden/*.json", { eager: true, import: "default" });

const has = (json, key) => Object.hasOwn(json, key);

function kindOf(path, json) {
	if (!json || typeof json !== "object" || !json.scenario)
		throw new Error(`Golden file ${path} has no "scenario" key`);
	const isStatus = has(json, "status");
	const isReply = has(json, "request") || has(json, "data");
	if (isStatus && isReply)
		throw new Error(`Golden file ${path} mixes a "status" key with "request"/"data" keys`);
	if (isStatus) return "status";
	if (has(json, "request") && has(json, "data")) return "reply";
	throw new Error(`Golden file ${path} needs a "status" key or both "request" and "data" keys`);
}

// Exported for its own spec. A missing or repeated key would silently hide a scenario.
export function indexScenarios(byPath) {
	const statuses = {};
	const replies = {};
	const seen = new Set();
	for (const [path, json] of Object.entries(byPath)) {
		const kind = kindOf(path, json);
		if (seen.has(json.scenario))
			throw new Error(`Duplicate golden scenario "${json.scenario}" in ${path}`);
		seen.add(json.scenario);
		if (kind === "status") statuses[json.scenario] = json.status;
		else replies[json.scenario] = { request: json.request, data: json.data };
	}
	return { statuses, replies };
}

const { statuses, replies } = indexScenarios(files);

function lookup(index, kind, name) {
	if (!Object.hasOwn(index, name)) throw new Error(`Unknown golden ${kind}: ${name}`);
	return index[name];
}

export const scenarioNames = () => Object.keys(statuses);
export const replyNames = () => Object.keys(replies);

// Deep copies so a spec can mutate its payload without leaking into the next one.
export const scenario = (name) => structuredClone(lookup(statuses, "scenario", name));
export const statusEnvelope = (name) => ({ ok: true, data: scenario(name) });

export const reply = (name) => structuredClone(lookup(replies, "reply", name).data);
export const replyRequest = (name) => structuredClone(lookup(replies, "reply", name).request);
export const replyEnvelope = (name) => ({ ok: true, data: reply(name) });
