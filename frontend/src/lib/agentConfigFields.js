/**
 * The engagement-config keys ConfigForm.vue renders as real, purpose-built
 * form fields (link pickers / dates / numbers / a select), in display order.
 *
 * Verified against the run path that actually reads them - not a schema:
 *   - agent_scope.py's `_resolve`: company, fiscal_year, from_date, to_date -
 *     the UNIVERSAL scope every run receives regardless of which agent it
 *     is (`scope: true` below) - always rendered, flat (`path` === `key`).
 *   - each agent's OWN bundle (jarvis-agents/agents/<slug>/evaluate.py) for
 *     everything else - close-auditor's `_materiality_pl_balance` reads
 *     benchmark_value/percentage/engagement_risk_level/rounding_step NESTED
 *     under a top-level `materiality` object
 *     (`config["materiality"]["benchmark_value"]`, etc - jarvis#1063
 *     CRITICAL fix, they are NOT flat top-level keys), the only agent that
 *     reads any agent-specific config today. Rendered only when the
 *     listing's own `config_keys` (agents_api.py get_agent, jarvis#1063)
 *     names the field's storage `path` (a dot path for a nested key) -
 *     ConfigForm.vue filters this array by that list at render time.
 *
 * `key` is the LOCAL form-state / label / validation identity (unchanged by
 * nesting); `path` is WHERE the value actually lives in the saved config
 * JSON, as dot notation - `"company"` (flat, same as `key`) or
 * `"materiality.benchmark_value"` (nested). ConfigForm.vue's seed()/save()
 * read and write through `path`, with getPath/setPath/deletePath below;
 * `key` alone would silently save to the wrong (flat, unread) location.
 *
 * A single exported constant on purpose (jarvis#1062 owner feedback): #1063
 * (a per-agent DECLARED config schema) can swap this for a schema-generated
 * list without touching ConfigForm's rendering logic, which only branches on
 * `type` and filters agent-specific entries by `config_keys`.
 *
 * Shapes:
 *   - {key, path, label, type: "link", linkDoctype, help, scope?}
 *   - {keys: [from, to], paths: [from, to], labels: [from, to], type: "date-range", help, scope?}
 *   - {key, path, label, type: "number", help, suffix?}
 *   - {key, path, label, type: "select", options: [{label, value}, ...]}
 */
export const CONFIG_FIELD_SET = [
	{
		key: "company",
		path: "company",
		label: "Company",
		type: "link",
		linkDoctype: "Company",
		help: "Defaults to your default company",
		scope: true,
	},
	{
		key: "fiscal_year",
		path: "fiscal_year",
		label: "Fiscal year",
		type: "link",
		linkDoctype: "Fiscal Year",
		help: "Defaults to the current fiscal year",
		scope: true,
	},
	{
		type: "date-range",
		keys: ["from_date", "to_date"],
		paths: ["from_date", "to_date"],
		labels: ["Period from", "Period to"],
		help: "Optional; overrides the fiscal year",
		scope: true,
	},
	{
		key: "benchmark_value",
		path: "materiality.benchmark_value",
		label: "Materiality benchmark amount",
		type: "number",
		help: "Used to judge which differences matter",
	},
	{
		key: "percentage",
		path: "materiality.percentage",
		label: "Materiality percentage",
		type: "number",
		suffix: "%",
		help: "Auditors report checks as not evaluable when materiality is unset",
	},
	{
		key: "engagement_risk_level",
		path: "materiality.engagement_risk_level",
		label: "Engagement risk level",
		type: "select",
		options: [
			{ label: "-", value: "" },
			{ label: "Low", value: "low" },
			{ label: "Medium", value: "medium" },
			{ label: "High", value: "high" },
		],
	},
	{
		key: "rounding_step",
		path: "materiality.rounding_step",
		label: "Rounding step",
		type: "number",
		help: "Step used to detect round-number plugs",
	},
];

/** The always-rendered universal-scope subset (company/fiscal_year/period). */
export const SCOPE_CONFIG_FIELDS = CONFIG_FIELD_SET.filter((f) => f.scope);

/** The agent-specific subset - each rendered only when the listing's
 * `config_keys` (get_agent) names its key. */
export const AGENT_SPECIFIC_CONFIG_FIELDS = CONFIG_FIELD_SET.filter((f) => !f.scope);

/** Every config key CONFIG_FIELD_SET renders a dedicated field for - anything
 * else is an "unknown" key that ConfigForm.vue leaves untouched in its
 * Advanced (JSON) editor. */
export const KNOWN_CONFIG_KEYS = new Set(CONFIG_FIELD_SET.flatMap((f) => f.keys || [f.key]));

/** key -> its field's human label, for save-time validation messages
 * ("\"Materiality percentage\" must be a number."). */
export const CONFIG_FIELD_LABELS = Object.fromEntries(
	CONFIG_FIELD_SET.flatMap((f) =>
		f.keys ? f.keys.map((k, i) => [k, f.labels[i]]) : [[f.key, f.label]]
	)
);

/** The subset of known keys that are numeric on save (everyone else is a
 * plain string, cleared to absent when blank). */
export const NUMBER_CONFIG_KEYS = CONFIG_FIELD_SET.filter((f) => f.type === "number").map(
	(f) => f.key
);

/** local form key -> its storage dot path ("company" flat, or
 * "materiality.benchmark_value" nested). ConfigForm.vue's seed()/save() read
 * and write through this, not the bare key - jarvis#1063 CRITICAL fix: a
 * flat save of a key the bundle reads nested never reaches the evaluator. */
export const KEY_TO_PATH = Object.fromEntries(
	CONFIG_FIELD_SET.flatMap((f) =>
		f.keys ? f.keys.map((k, i) => [k, (f.paths || f.keys)[i]]) : [[f.key, f.path || f.key]]
	)
);

// ── dot-path get/set/delete over a plain JSON-shaped object ─────────────────
// Depth is whatever a `path` names (1 for a flat key, 2 for "materiality.x");
// these do not special-case depth. Exported so ConfigForm.vue's seed()/save()
// and this module's own tests share one implementation.

/** Reads `path` ("a.b.c") off `obj`; undefined if any segment is missing or
 * not an object to descend into. */
export function getPath(obj, path) {
	let cur = obj;
	for (const seg of path.split(".")) {
		if (cur == null || typeof cur !== "object") return undefined;
		cur = cur[seg];
	}
	return cur;
}

/** Sets `path` on `obj` to `value`, creating/reusing intermediate objects
 * (an existing `materiality` object is MERGED into, not replaced). Mutates
 * `obj`. */
export function setPath(obj, path, value) {
	const segs = path.split(".");
	const last = segs.pop();
	let cur = obj;
	for (const seg of segs) {
		if (cur[seg] === null || typeof cur[seg] !== "object" || Array.isArray(cur[seg])) {
			cur[seg] = {};
		}
		cur = cur[seg];
	}
	cur[last] = value;
}

/** Deletes `path` off `obj`, then prunes any intermediate object left empty
 * by the deletion (innermost first) - clearing the last `materiality.*`
 * field drops the empty `materiality` object entirely, rather than leaving
 * `{"materiality": {}}` behind. Mutates `obj`; no-ops if `path` does not
 * resolve (already absent). */
export function deletePath(obj, path) {
	const segs = path.split(".");
	const last = segs.pop();
	const chain = [obj];
	let cur = obj;
	for (const seg of segs) {
		if (cur == null || typeof cur !== "object" || !(seg in cur)) return;
		cur = cur[seg];
		chain.push(cur);
	}
	if (cur && typeof cur === "object") delete cur[last];
	for (let i = chain.length - 1; i > 0; i--) {
		const node = chain[i];
		if (node && typeof node === "object" && Object.keys(node).length === 0) {
			delete chain[i - 1][segs[i - 1]];
		} else {
			break;
		}
	}
}
