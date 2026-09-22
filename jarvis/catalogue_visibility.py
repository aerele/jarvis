"""Mirror the operator's fleet-global agent-catalogue visibility policy locally.

The control plane owns a per-agent visibility policy (``available`` / ``teaser`` /
``hidden``); the bench pulls it on ``get_connection`` (beside the release/announcement/
maintenance mirrors) and applies it onto ``Jarvis Agent Listing.operator_visibility``.
The pull is the ONLY authoritative writer of a governed agent's visibility — it writes via
``frappe.db.set_value`` (bypassing the listing ``validate()`` guard), and the guard in turn
makes a governed agent's visibility read-only to the tenant (Desk / endpoint), so operator
and tenant can never fight over a governed row.

Marker discipline (mirrors ``maintenance_notice`` but with the safe direction INVERTED):
the CP stamps an ``agent_visibility_supported`` capability marker on every payload it can
serve. Here **reverting a withdrawn agent back to ``available`` (un-hiding it) is the harmful
outcome**, so we KEEP on any doubt — the opposite of maintenance (which clears on doubt
because a stranded hold is its harm). Cases:

- marker ABSENT (old / rolled-back CP) -> KEEP last-applied (never un-hide fleet-wide on a
  rollback).
- marker present, ``agent_visibility`` key ABSENT (transient / partial payload) -> KEEP.
- marker present, key present but MALFORMED (not a dict, or a bad slug/visibility value)
  -> reject the WHOLE payload, KEEP (all-or-nothing).
- marker present, key present + well-formed dict (incl. explicit ``{}``) -> AUTHORITATIVE:
  set each governed slug, revert any slug dropped from the policy to ``available``.

The apply is UNCONDITIONAL (no version gate): it is ~a few dozen local field writes, and
re-asserting every poll is what keeps a tenant-SM Desk edit or a newly-synced listing from
drifting a governed row. Never raises — it runs from the daily scheduler and the "Sync now"
button; every failure degrades and is logged once, phase-tagged.
"""

import frappe

SETTINGS = "Jarvis Settings"
LISTING = "Jarvis Agent Listing"
_MARKER = "agent_visibility_supported"
_KEY = "agent_visibility"
_VISIBILITY = ("available", "teaser", "hidden")
_DEFAULT = "available"


def governed_map() -> dict:
	"""The last-applied ``{slug: visibility}`` policy map, or ``{}`` on any read/parse error.

	Cached read (called from the install + listing guards on every save). Never raises —
	but it FAILS OPEN (returns ``{}`` -> nothing governed), so a corrupt blob would silently
	un-gate; log the read failure so that lapse is never silent (it self-heals on the next
	successful apply)."""
	try:
		raw = frappe.get_cached_value(SETTINGS, SETTINGS, "catalogue_visibility_governed")
		if not raw:
			return {}
		data = frappe.parse_json(raw)
		return data if isinstance(data, dict) else {}
	except Exception:
		frappe.log_error(
			title="catalogue_visibility.governed_map read failed", message=frappe.get_traceback()
		)
		return {}


def is_operator_governed(slug: str) -> bool:
	"""True iff ``slug`` is under the operator's fleet-global policy (any visibility).

	A governed agent's visibility is operator-owned: read-only to the tenant and — when
	teaser/hidden — un-installable by anyone (the two guards consume this)."""
	return bool(slug) and slug in governed_map()


def persist_from_connection(conn: dict) -> None:
	"""Apply the visibility mirror from a full ``get_connection`` payload, marker-aware.

	See the module docstring for the KEEP-on-doubt case table. Never raises."""
	try:
		if not conn.get(_MARKER):
			# Old / rolled-back CP: KEEP (un-hiding on rollback is the harm). If we currently
			# hold a governed policy, breadcrumb it — a bench stuck polling an old CP would
			# otherwise silently never pick up a newly-governed policy (deploy-order diagnostic,
			# mirrors maintenance_notice).
			if governed_map():
				frappe.logger("jarvis").info(
					"catalogue_visibility: KEEP -- connection lacks agent_visibility_supported "
					"(old/rolled-back control plane); confirm CP-before-app deploy order"
				)
			return
		if _KEY not in conn:
			return  # transient / partial payload: KEEP last-applied
		_apply(conn.get(_KEY))
	except Exception:
		frappe.log_error(
			title="catalogue_visibility.persist_from_connection failed", message=frappe.get_traceback()
		)


def _apply(policy) -> None:
	"""Authoritative apply of a well-formed policy map. Validates all-or-nothing, then
	reverts dropped slugs and sets governed ones, and records the new map + synced_at."""
	valid, normalized = _validate(policy)
	if not valid:
		# repr, not frappe.as_json: a non-JSON-native payload would make as_json raise, which
		# would escape to the outer handler and mislabel this as the generic failure.
		payload_repr = repr(policy)[:2000]
		frappe.log_error(
			title="catalogue_visibility apply skipped: malformed policy",
			message=f"phase=validate payload={payload_repr}",
		)
		return  # KEEP last-applied — a malformed payload must never revert a withdrawal

	old = governed_map()
	# Revert slugs the operator dropped from the policy back to the ungoverned default.
	for slug in old:
		if slug not in normalized:
			_set_listing_visibility(slug, _DEFAULT)
	# Assert every governed slug's value (unknown slugs — no local listing — are skipped
	# here but stay in the stored map, so they attach when their listing later syncs in).
	for slug, vis in normalized.items():
		_set_listing_visibility(slug, vis)

	fresh = {"catalogue_visibility_synced_at": frappe.utils.now()}
	if normalized != old:
		fresh["catalogue_visibility_governed"] = frappe.as_json(normalized)
	frappe.db.set_value(SETTINGS, SETTINGS, fresh, update_modified=False)


def _validate(policy) -> tuple[bool, dict]:
	"""A policy is a dict of ``{str slug: visibility-in-_VISIBILITY}``. Returns
	``(ok, normalized)``; ``ok`` False rejects the WHOLE payload (all-or-nothing)."""
	if not isinstance(policy, dict):
		return False, {}
	normalized = {}
	for slug, vis in policy.items():
		if not isinstance(slug, str) or not slug.strip():
			return False, {}
		if vis not in _VISIBILITY:
			return False, {}
		normalized[slug.strip()] = vis
	return True, normalized


def _set_listing_visibility(slug: str, value: str) -> None:
	"""Set one listing's ``operator_visibility`` (no-op if the slug has no local listing or
	already holds ``value``). Writes via ``db.set_value`` — bypasses the listing guard (the
	pull is the authoritative writer) and avoids ``modified`` churn."""
	current = frappe.db.get_value(LISTING, slug, "operator_visibility")
	if current is None:
		return  # no such listing on this tenant (version skew) — governance attaches later
	if (current or _DEFAULT) == value:
		return
	frappe.db.set_value(LISTING, slug, "operator_visibility", value, update_modified=False)
