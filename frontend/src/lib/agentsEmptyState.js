/**
 * Which empty state the agents catalog shows, as a pure function of what we know.
 *
 * Split out of AgentsList.vue so it can be tested directly: mounting that view
 * needs a router, useListPage, and half of frappe-ui, none of which have
 * anything to do with the decision being made here.
 *
 * The decision that matters (jarvis#1062): agent access is DENY BY DEFAULT, so
 * for a non-admin an empty list usually is not "the catalog is empty" but
 * "nothing has been granted to you". Saying the former sends someone off to look
 * for a bug that is not there, and the per-TAB copy is worse still - the
 * Featured tab told them to go and browse the Available tab, which for that same
 * user is equally empty. So a non-admin whose WHOLE visible catalog is empty
 * gets one honest message on every tab, and no call to action, because there is
 * no tab they could go to that would help.
 *
 * The per-tab states survive for everyone else: an admin (for whom an empty
 * catalog really is an empty catalog), and a user who has some agents but none
 * on the tab they are looking at.
 */

/** Copy for a non-admin who has been granted nothing at all. */
export const NO_ACCESS_EMPTY_STATE = Object.freeze({
	title: "No agents available to you",
	description: "No agents have been made available to you yet. Ask your administrator.",
	cta: false,
});

/**
 * jarvis#1062 P2-9 (production-readiness audit): the session user
 * "Administrator" (the Frappe superuser account, not a named person) is
 * refused Install server-side (S3 owner-gate needs a real user) and the
 * Install button is already disabled for it elsewhere (AgentDetail.vue) -
 * but the empty Installed tab still said "install one to get started" with
 * a working-looking CTA. Told straight instead: sign in as a named user.
 */
export const ADMINISTRATOR_CANNOT_INSTALL = Object.freeze({
	title: "You haven't installed any agents yet",
	description: "Administrator cannot install agents. Sign in as a named user.",
	cta: false,
});

/**
 * @param {object} state
 * @param {string} state.tab              featured | available | installed
 * @param {boolean} state.filtersActive   a search term or category is set
 * @param {boolean} state.canAdminister   caps.admin - a tenant admin
 * @param {boolean} [state.isAdministrator]
 *        session.user === "Administrator" - the superuser account, refused
 *        Install server-side regardless of caps.admin.
 * @param {boolean|null} state.wholeCatalogEmpty
 *        true/false once probed; null while unknown. Only meaningful for a
 *        non-admin, and only probed when it is about to be needed.
 * @returns {{title: string, description: string, cta: boolean}}
 */
export function agentsEmptyState({
	tab,
	filtersActive,
	canAdminister,
	isAdministrator,
	wholeCatalogEmpty,
}) {
	// A filtered view that came back empty is about the FILTER, whoever is
	// looking - checked first so it never gets misread as an access problem.
	if (filtersActive) {
		return {
			title: "No agents match",
			description: "Try clearing the search or category filter.",
			cta: false,
		};
	}
	// The access case wins over every per-tab message, on every tab.
	if (!canAdminister && wholeCatalogEmpty === true) {
		return { ...NO_ACCESS_EMPTY_STATE };
	}
	if (tab === "featured") {
		return {
			title: "No featured agents yet",
			description: "Browse the Available tab for the full catalog.",
			cta: true,
		};
	}
	if (tab === "installed") {
		if (isAdministrator) return { ...ADMINISTRATOR_CANNOT_INSTALL };
		return {
			title: "You haven't installed any agents yet",
			description: "Browse the catalog and install one to get started.",
			cta: true,
		};
	}
	return {
		title: "No agents available",
		description: "The catalog is empty right now.",
		cta: false,
	};
}

/**
 * Should the catalog be probed for "is this caller's whole catalog empty"?
 *
 * Deliberately LAZY - it costs a round trip, and the answer only changes what is
 * rendered in the one situation where a tab has come back empty for a non-admin
 * with no filters on. Probed once per mount: within one visit the set of agents
 * granted to you does not change.
 */
export function shouldProbeWholeCatalog({
	tab,
	loading,
	rowCount,
	filtersActive,
	canAdminister,
	wholeCatalogEmpty,
	probing,
}) {
	return (
		["featured", "available", "installed"].includes(tab) &&
		!loading &&
		rowCount === 0 &&
		!filtersActive &&
		!canAdminister &&
		wholeCatalogEmpty === null &&
		!probing
	);
}
