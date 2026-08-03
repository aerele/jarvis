// The ONE canonical list-page request encoder for every filters_v2 surface
// (plan 08 P0-01). Before this, three copies of `_page()` existed — api.js
// (Skills/Macros/File Box/Approvals), api/dashboards.js and api/triggers.js —
// and the last two rebuilt the request WITHOUT `filters_v2`, so a canonical
// clause silently never reached the endpoint. Every migrated wrapper now routes
// through here so the drift cannot come back:
//   - Skills / Macros / File Box / Approvals -> `listPageArgs` (via api.js)
//   - Saved Dashboards / Triggers            -> `listPageArgs` (feature wrappers)
//   - Wiki                                    -> `encodeFiltersV2` (bespoke shape)

// Add `filters_v2` to an already-built args object. Plan §6.2: ADDITIVE, and
// only when there are clauses, so an unmigrated endpoint never sees an argument
// it does not declare. JSON-encoded so the server `frappe.parse_json`s it. The
// one place the conditional serialization lives.
export function encodeFiltersV2(args, p = {}) {
	if (Array.isArray(p.filters_v2) && p.filters_v2.length) {
		args.filters_v2 = JSON.stringify(p.filters_v2);
	}
	return args;
}

// The standard {search, filters, sort, paging} request shape shared by the
// offset-paginated list endpoints, with `filters_v2` folded in. `filters` is
// JSON-encoded here so callers pass a plain object.
export function listPageArgs(p = {}) {
	return encodeFiltersV2(
		{
			search: p.search || "",
			filters: JSON.stringify(p.filters || {}),
			sort_field: p.sort_field || "",
			sort_dir: p.sort_dir || "",
			start: p.start || 0,
			page_length: p.page_length || 20,
		},
		p
	);
}
