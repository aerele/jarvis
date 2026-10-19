"""Mirror the operator's fleet-global connector catalogue policy locally.

The control plane owns a list of connector presets the operator has switched off for
every tenant. The bench pulls it on ``get_connection`` (beside the agent-catalogue
visibility mirror) and stores it on Jarvis Settings. Two readers consume it:

- ``providers()``: the shipped catalog with those presets disabled through
  ``catalog.apply_overlay``, so the Connectors picker stops offering them.
- ``is_disabled()``: the create guard in ``connectors_api.add_connector`` and the
  ``Jarvis Connector`` controller, so a new row for a disabled preset is refused.

Rows already saved on a disabled preset keep working: ``catalog.by_name`` resolves
disabled entries by design, so sign-in, tests and tool calls are untouched.

Marker discipline (same direction as ``catalogue_visibility``): the CP stamps a
``connector_catalogue_supported`` capability marker on every payload it can serve.
Silently re-enabling a preset the operator switched off is the harmful outcome, so we
KEEP on any doubt. Cases:

- marker ABSENT (old / rolled-back CP) -> KEEP last-applied.
- marker present, ``connector_catalogue_disabled`` key ABSENT (partial payload) -> KEEP.
- marker present, key present but MALFORMED (not a list of non-empty strings) -> reject
  the WHOLE payload, KEEP, log once.
- marker present, key present and well-formed (incl. explicit ``[]``) -> AUTHORITATIVE:
  store the list as is. Names this bench does not ship are stored but ignored by the
  readers, so they take effect if a later release ships that preset.

``Custom URL`` is a flow, not a shipped preset, so this policy can never turn it off.
Never raises: it runs from the daily scheduler and the "Sync now" button; every failure
degrades and is logged.
"""

from functools import lru_cache

import frappe

from jarvis.connectors import catalog

SETTINGS = "Jarvis Settings"
_MARKER = "connector_catalogue_supported"
_KEY = "connector_catalogue_disabled"
_FIELD = "connector_catalogue_disabled"
_SYNCED_AT = "connector_catalogue_synced_at"


def disabled_names() -> set[str]:
	"""Every preset name the last applied policy switched off, unknown names included.

	Cached read. Empty set on any read or parse error: that fails open (the picker shows
	the shipped catalog), so the failure is logged and self-heals on the next good pull."""
	try:
		raw = frappe.get_cached_value(SETTINGS, SETTINGS, _FIELD)
		if not raw:
			return set()
		data = frappe.parse_json(raw)
		if not isinstance(data, list):
			return set()
		return {name for name in data if isinstance(name, str)}
	except Exception:
		frappe.log_error(
			title="connector_catalogue.disabled_names read failed", message=frappe.get_traceback()
		)
		return set()


def is_disabled(name: str) -> bool:
	"""True iff ``name`` is a shipped preset the operator switched off.

	Limited to shipped names so a stray ``Custom URL`` (or any unknown name) in the
	policy can never block a create the catalog does not govern."""
	return bool(name) and name in _shipped_names() and name in disabled_names()


def providers() -> tuple[catalog.Provider, ...]:
	"""The shipped catalog with the operator's disabled presets switched off.

	Falls back to the shipped catalog on an overlay error (logged), so a bad policy can
	never take the Connectors pane down; the create guard does not depend on this."""
	disabled = frozenset(disabled_names() & _shipped_names())
	try:
		return _overlaid(disabled)
	except Exception:
		frappe.log_error(title="connector_catalogue.providers overlay failed", message=frappe.get_traceback())
		return catalog.PROVIDERS


def persist_from_connection(conn: dict) -> None:
	"""Store the disabled-preset policy from a full ``get_connection`` payload.

	See the module docstring for the KEEP-on-doubt case table. Never raises."""
	try:
		if not conn.get(_MARKER):
			# Old / rolled-back CP: KEEP. Breadcrumb it while a policy is held, so a bench
			# stuck on an old CP is diagnosable (same as catalogue_visibility).
			if disabled_names():
				frappe.logger("jarvis").info(
					"connector_catalogue: KEEP, connection lacks connector_catalogue_supported "
					"(old or rolled-back control plane); confirm CP-before-app deploy order"
				)
			return
		if _KEY not in conn:
			return  # partial payload: KEEP last-applied
		_apply(conn.get(_KEY))
	except Exception:
		frappe.log_error(
			title="connector_catalogue.persist_from_connection failed", message=frappe.get_traceback()
		)


def _apply(policy) -> None:
	"""Validate all-or-nothing, then store the normalized list and the sync time."""
	normalized = _normalize(policy)
	if normalized is None:
		# repr, not as_json: a non-JSON-native payload would make as_json raise and
		# mislabel this as the generic failure.
		frappe.log_error(
			title="connector_catalogue apply skipped: malformed policy",
			message=f"phase=validate payload={repr(policy)[:2000]}",
		)
		return  # KEEP last-applied: a malformed payload must never re-enable a preset

	fresh = {_SYNCED_AT: frappe.utils.now()}
	stored = frappe.as_json(normalized)
	if frappe.get_cached_value(SETTINGS, SETTINGS, _FIELD) != stored:
		fresh[_FIELD] = stored
	frappe.db.set_single_value(SETTINGS, fresh, update_modified=False)


def _normalize(policy) -> list[str] | None:
	"""A sorted, deduped list of stripped names, or None when the payload is not a list
	of non-empty strings (one bad entry rejects the whole payload)."""
	if not isinstance(policy, list):
		return None
	names = set()
	for name in policy:
		if not isinstance(name, str) or not name.strip():
			return None
		names.add(name.strip())
	return sorted(names)


def _shipped_names() -> frozenset[str]:
	return frozenset(catalog.all_names())


@lru_cache(maxsize=16)
def _overlaid(disabled: frozenset[str]) -> tuple[catalog.Provider, ...]:
	"""Keyed on the disabled set, so a policy change is never served stale."""
	return catalog.apply_overlay([{"name": name, "enabled": False} for name in sorted(disabled)])
