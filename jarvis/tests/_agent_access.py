"""Shared fixture helper for the DENY-BY-DEFAULT agent access gate (jarvis#1062).

Before this gate landed, a ``Jarvis Agent Listing`` with no ``allowed_roles``
rows was reachable by everyone, so a test could install and run an agent as a
plain user with no setup at all. It is now CLOSED until an admin allows it, and
that flipped the default for every fixture in the suite: an install/run/sweep/
push test acting as a non-admin has to say who is allowed FIRST.

Kept here rather than in one test module because a dozen modules need it and the
alternative is a dozen slightly different copies (the exact drift ``_role_guard``
and ``_transport_helpers`` already exist to prevent). Writes the child rows
directly rather than through ``set_agent_access``: the fixture is describing a
precondition, not exercising the endpoint, and going through the endpoint would
make every one of those modules depend on the admin gate's behaviour too.
"""

import frappe

LISTING = "Jarvis Agent Listing"
ALLOWED_ROLE = "Jarvis Agent Allowed Role"
ALLOWED_USER = "Jarvis Agent Allowed User"


def allow_listing_for(listing: str, user: str | None = None, roles=None) -> None:
	"""Grant ``user`` and/or ``roles`` access to ``listing`` (additive, idempotent).

	``listing`` is the Jarvis Agent Listing docname (== its agent_slug). Silently
	does nothing when the listing does not exist on this site, so a module can
	grant for every slug it MIGHT touch without asserting the catalog's contents.
	"""
	if not frappe.db.exists(LISTING, listing):
		return
	for role in roles or []:
		if not frappe.db.exists(
			ALLOWED_ROLE,
			{"parenttype": LISTING, "parentfield": "allowed_roles", "parent": listing, "role": role},
		):
			frappe.get_doc(
				{
					"doctype": ALLOWED_ROLE,
					"parenttype": LISTING,
					"parentfield": "allowed_roles",
					"parent": listing,
					"role": role,
				}
			).insert(ignore_permissions=True)
	if user:
		if not frappe.db.exists(
			ALLOWED_USER,
			{"parenttype": LISTING, "parentfield": "allowed_users", "parent": listing, "user": user},
		):
			frappe.get_doc(
				{
					"doctype": ALLOWED_USER,
					"parenttype": LISTING,
					"parentfield": "allowed_users",
					"parent": listing,
					"user": user,
				}
			).insert(ignore_permissions=True)
	frappe.db.commit()


def allow_all_listings_for(*users: str) -> None:
	"""Open EVERY listing on the site to each named user.

	The blunt instrument, for modules whose subject is not access control at all
	(dispatch idempotency, run lifecycle, provenance) and which would otherwise
	need the catalog's slug list hard-coded into their setUp — a list that goes
	stale the next time registry.json grows an agent.
	"""
	for name in frappe.get_all(LISTING, pluck="name"):
		for u in users:
			allow_listing_for(name, user=u)


def clear_listing_access(listing: str | None = None) -> None:
	"""Drop every allow row (all listings, or just ``listing``) — the CLOSED state.

	The deny-by-default precondition: a test asserting that a plain user cannot
	see / install / run an agent starts from here.
	"""
	for doctype, parentfield in ((ALLOWED_ROLE, "allowed_roles"), (ALLOWED_USER, "allowed_users")):
		filters = {"parenttype": LISTING, "parentfield": parentfield}
		if listing:
			filters["parent"] = listing
		frappe.db.delete(doctype, filters)
	frappe.db.commit()
