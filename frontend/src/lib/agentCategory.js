/**
 * Display title for an agent listing's `category` field.
 *
 * `sync_agent_listings` (agent_catalog.py) already maps the registry's
 * `domain` code to a real label ("Accounts Payable", not "ap") before it ever
 * reaches the SPA, so in the common case this is a passthrough. It still
 * earns its keep for two edge cases:
 *   - no category yet -> "Other"
 *   - a raw slug a pre-migrate row might still carry (hyphen-split,
 *     title-cased) until the next catalog sync overwrites it.
 *
 * Split out of AgentDetail.vue and AgentsList.vue (jarvis#1062 polish),
 * where it was duplicated byte for byte.
 *
 * @param {unknown} value - `agent.category` (or any category-shaped string).
 * @returns {string} a display-ready title.
 */
export function categoryTitle(value) {
	return String(value || "Other")
		.split("-")
		.map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
		.join(" ");
}
