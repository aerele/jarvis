/**
 * The engagement-config keys ConfigForm.vue renders as real, purpose-built
 * form fields (link pickers / dates / numbers / a select), in display order.
 *
 * Verified against the run path that actually reads them - not a schema:
 *   - agent_scope.py's `_resolve`: company, fiscal_year, from_date, to_date -
 *     the UNIVERSAL scope every run receives regardless of which agent it
 *     is (`scope: true` below) - always rendered.
 *   - each agent's OWN bundle (jarvis-agents/agents/<slug>/evaluate.py) for
 *     everything else - benchmark_value/percentage/engagement_risk_level/
 *     rounding_step, verified against close-auditor, the only agent that
 *     reads any of them today. Rendered only when the listing's own
 *     `config_keys` (agents_api.py get_agent, jarvis#1063) names the key -
 *     ConfigForm.vue filters this array by that list at render time.
 *
 * A single exported constant on purpose (jarvis#1062 owner feedback): #1063
 * (a per-agent DECLARED config schema) can swap this for a schema-generated
 * list without touching ConfigForm's rendering logic, which only branches on
 * `type` and filters agent-specific entries by `config_keys`.
 *
 * Shapes:
 *   - {key, label, type: "link", linkDoctype, help, scope?}
 *   - {keys: [from, to], labels: [from, to], type: "date-range", help, scope?}
 *   - {key, label, type: "number", help, suffix?}
 *   - {key, label, type: "select", options: [{label, value}, ...]}
 */
export const CONFIG_FIELD_SET = [
	{
		key: "company",
		label: "Company",
		type: "link",
		linkDoctype: "Company",
		help: "Defaults to your default company",
		scope: true,
	},
	{
		key: "fiscal_year",
		label: "Fiscal year",
		type: "link",
		linkDoctype: "Fiscal Year",
		help: "Defaults to the current fiscal year",
		scope: true,
	},
	{
		type: "date-range",
		keys: ["from_date", "to_date"],
		labels: ["Period from", "Period to"],
		help: "Optional; overrides the fiscal year",
		scope: true,
	},
	{
		key: "benchmark_value",
		label: "Materiality benchmark amount",
		type: "number",
		help: "Used to judge which differences matter",
	},
	{
		key: "percentage",
		label: "Materiality percentage",
		type: "number",
		suffix: "%",
		help: "Auditors report checks as not evaluable when materiality is unset",
	},
	{
		key: "engagement_risk_level",
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
