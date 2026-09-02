// Shared wiki-page metadata: the page-type catalog, the scope-to-badge-theme
// map, and the slug-sanitizing helper. WikiTab.vue (the list) and
// WikiDetail.vue (the routed create/view/edit page) each need the same three
// values and used to define them twice; kept here so they can't drift apart.

/** The 9 wiki page types, in display order. */
export const WIKI_TYPES = [
	"Customer",
	"Supplier",
	"Item",
	"Process",
	"Doctype",
	"Exception",
	"Integration",
	"People",
	"Org",
];

/** Badge theme for each page/row scope. */
export const SCOPE_THEME = { Org: "gray", Role: "blue", User: "green" };

/**
 * Lowercase, hyphenate and trim a string into the slug shape the server
 * derives page ids from (used both for the create-form preview and any
 * other slug-preview UI).
 */
export function scrub(s) {
	return String(s || "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "");
}
