// Page → feature-wrapper adapters, extracted so the transport chain is actually
// TESTED (S6). Every migrated list page turns useListPage's request `p` into its
// wrapper's argument shape here; the round-2 defect was one layer up (a wrapper
// that rebuilt the request without filters_v2), and its twin lives here: a page
// adapter that maps parameters EXPLICITLY can silently forget `filters_v2` and no
// wrapper test would notice. These pure functions make that a unit a spec can
// pin, so `filters_v2: p.filters_v2` cannot be quietly deleted.

/**
 * The search/filter split the four spread-shaped wave-1 pages (Skills, Macros,
 * Saved dashboards, Triggers) apply before calling their wrapper. The backend
 * whitelists filter KEYS and throws on "search", so search moves out of `filters`
 * into the envelope's own param; everything else — crucially `filters_v2` — rides
 * the spread through untouched. Centralised so the spread cannot be "helpfully"
 * rewritten into an explicit list that forgets filters_v2.
 */
export function searchSplitArgs(p) {
	const { search: q, ...rest } = p.filters || {};
	return { ...p, search: q || p.search || "", filters: rest };
}

/**
 * WikiTab's argument builder — the one EXPLICIT-parameter adapter among the
 * migrated pages, so the one that must name `filters_v2` itself. The wiki endpoint
 * takes a page NUMBER and its own named view controls rather than the generic
 * `filters` object, so this cannot use {@link searchSplitArgs}; it must forward
 * `filters_v2` by hand, and the spec guards exactly that line.
 */
export function buildWikiListArgs(p) {
	const f = p.filters || {};
	const pl = p.page_length || 20;
	return {
		search: f.search || p.search || "",
		page_type: f.page_type || "",
		scope_filter: f.scope || "all",
		attention: f.attention === "1" ? 1 : 0,
		archived: f.attention === "archived" ? 1 : 0,
		// the kit sends a start offset; the endpoint takes a page number
		page: Math.floor((p.start || 0) / pl) + 1,
		page_length: pl,
		filters_v2: p.filters_v2,
	};
}
