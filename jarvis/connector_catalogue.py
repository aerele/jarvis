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

A second, independent policy rides the same pull: per-preset "Use a key instead"
overrides (marker ``connector_key_policy_supported``, key ``connector_key_overrides``, a
``{preset: bool}`` map). An override says whether the paste-a-key option is offered; absent
means the code default (``Provider.accepts_key``). It only applies to a switchable preset
(``dcr`` / ``static``): a key-only (``token``) or ``open`` preset always takes a key, and
Custom URL is never governed. Same KEEP-on-doubt table as above (marker absent, key absent
or malformed -> KEEP; a well-formed map, ``{}`` included, is authoritative). Consumers:
``accepts_key()`` (the ``add_connector`` and controller guards) and ``public_catalog()``
(the picker). A key row saved before the override keeps saving, as for a disabled preset.

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
_KEY_MARKER = "connector_key_policy_supported"
_KEY_OVERRIDES = "connector_key_overrides"
_KEY_SYNCED_AT = "connector_key_overrides_synced_at"
_SWITCHABLE_AUTHS = frozenset({catalog.AUTH_DCR, catalog.AUTH_STATIC})


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


def key_overrides() -> dict[str, bool]:
	"""The operator's ``{preset: offer a key}`` map as last applied, unknown names included.

	Cached read. ``{}`` on any read or parse error: that falls back to the code defaults,
	so the failure is logged and self-heals on the next good pull."""
	try:
		raw = frappe.get_cached_value(SETTINGS, SETTINGS, _KEY_OVERRIDES)
		data = frappe.parse_json(raw) if raw else {}
		if not isinstance(data, dict):
			return {}
		return {name: value for name, value in data.items() if isinstance(value, bool)}
	except Exception:
		frappe.log_error(
			title="connector_catalogue.key_overrides read failed", message=frappe.get_traceback()
		)
		return {}


def accepts_key(name: str) -> bool:
	"""Whether ``name`` may be connected with a pasted key, operator override applied.

	The override wins only on a switchable (``dcr`` / ``static``) preset; anything else
	(key-only, open, Custom URL, unknown) keeps the code default."""
	return _accepts_key(name, key_overrides())


def public_catalog() -> list[dict]:
	"""``catalog.to_public`` for the picker: operator-disabled presets dropped and each
	``accepts_key`` set to its effective value (the overlay seam never changes it)."""
	overrides = key_overrides()
	return [
		{**row, "accepts_key": _accepts_key(row["name"], overrides)}
		for row in catalog.to_public(providers=providers())
	]


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


def persist_key_overrides_from_connection(conn: dict) -> None:
	"""Store the "Use a key instead" overrides from a full ``get_connection`` payload.

	Same KEEP-on-doubt table as ``persist_from_connection``. Never raises."""
	try:
		if not conn.get(_KEY_MARKER):
			if key_overrides():
				frappe.logger("jarvis").info(
					"connector_catalogue: KEEP key overrides, connection lacks "
					"connector_key_policy_supported (old or rolled-back control plane)"
				)
			return
		if _KEY_OVERRIDES not in conn:
			return  # partial payload (the CP omits the key on a read error): KEEP
		_apply_key_overrides(conn.get(_KEY_OVERRIDES))
	except Exception:
		frappe.log_error(
			title="connector_catalogue.persist_key_overrides_from_connection failed",
			message=frappe.get_traceback(),
		)


def _apply_key_overrides(policy) -> None:
	normalized = _normalize_key_overrides(policy)
	if normalized is None:
		frappe.log_error(
			title="connector_catalogue apply skipped: malformed key overrides",
			message=f"phase=validate payload={repr(policy)[:2000]}",
		)
		return  # KEEP last-applied

	fresh = {_KEY_SYNCED_AT: frappe.utils.now()}
	stored = frappe.as_json(normalized)
	if frappe.get_cached_value(SETTINGS, SETTINGS, _KEY_OVERRIDES) != stored:
		fresh[_KEY_OVERRIDES] = stored
	frappe.db.set_single_value(SETTINGS, fresh, update_modified=False)


def _normalize_key_overrides(policy) -> dict[str, bool] | None:
	"""A ``{stripped name: bool}`` dict, or None when the payload is not a dict of
	non-empty string names to real booleans (one bad entry, or two names that collide
	after stripping with different values, rejects the whole payload)."""
	if not isinstance(policy, dict):
		return None
	out = {}
	for name, value in policy.items():
		if not isinstance(name, str) or not name.strip() or not isinstance(value, bool):
			return None
		if out.setdefault(name.strip(), value) != value:
			return None
	return out


def _accepts_key(name: str, overrides: dict[str, bool]) -> bool:
	if name in overrides and catalog.auth_of(name) in _SWITCHABLE_AUTHS:
		return overrides[name]
	return catalog.accepts_key_of(name)


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
