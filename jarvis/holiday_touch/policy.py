"""Mirror the operator's holiday-touch policy locally.

The control plane owns two switches, ``disabled`` and ``disabled_designs``. The bench pulls
them with ``get_connection`` (daily in ``onboarding.sync_connection``, and on the chat gate)
and stores them on Jarvis Settings. ``resolve.answer`` reads the mirror fields per request,
so a change takes effect at once and nothing here rotates the cache.

Rules:

* The two subkeys are independent: a malformed ``disabled_designs`` never stops ``disabled``
  from applying, and the other way round.
* A missing marker, a missing ``holiday_policy`` or a missing subkey KEEPS the stored value.
  A malformed subkey keeps the stored value and logs (rate limited, no payload content).
  Unknown design names inside a valid ``disabled_designs`` are dropped and logged the same way.
* Writes happen only on change: the gate path runs about every 30 s and Jarvis Settings, a
  Single, keeps a version history on every save, so an unchanged payload writes nothing.
* ``holiday_policy_synced_at`` is stamped by the daily sync only, never by the chat gate, and
  only when the stored stamp is empty or over an hour old (the onboarding wizard polls the
  same sync).
* The names subkeys (``aliases``, ``near_misses``) are ignored here: the matcher does not use
  an admin overlay yet.
* Never raises.
"""

from __future__ import annotations

import json

import frappe

from jarvis.holiday_touch import settings
from jarvis.holiday_touch.names import DESIGNS
from jarvis.holiday_touch.report import report

_SETTINGS = "Jarvis Settings"
_MARKER = "holiday_policy_supported"
_KEY = "holiday_policy"
_DISABLED = "holiday_policy_disabled"
_DESIGNS = "holiday_policy_disabled_designs"
_SYNCED_AT = "holiday_policy_synced_at"
_STAMP_GAP_SECONDS = 3600
_MALFORMED = "Holiday touch: admin policy malformed"


def persist(payload: dict, *, daily: bool) -> None:
	"""Store the policy from a ``get_connection`` payload. ``daily`` is True only from the
	daily sync, the one path that stamps ``holiday_policy_synced_at``. Never raises."""
	try:
		_persist(payload, daily)
	except Exception:
		report("policy_persist", "Holiday touch: policy mirror failed")


def _persist(payload, daily: bool) -> None:
	if not isinstance(payload, dict) or not payload.get(_MARKER):
		return  # old or rolled-back control plane, or admin could not read its settings: KEEP
	if _KEY not in payload:
		return  # partial payload: KEEP
	policy = payload[_KEY]
	if not isinstance(policy, dict):
		report(
			"policy_malformed",
			_MALFORMED,
			f"subkeys=[{_KEY}] type={type(policy).__name__}",
		)
		return
	meta = frappe.get_meta(_SETTINGS)
	if not all(meta.has_field(f) for f in (_DISABLED, _DESIGNS, _SYNCED_AT)):
		return  # the release is not migrated yet
	stored = frappe.db.get_singles_dict(_SETTINGS)
	changes, bad = {}, []
	if "disabled" in policy:
		flag = _as_flag(policy["disabled"])
		if flag is None:
			bad.append("disabled")
		elif flag != settings.on(stored, _DISABLED):
			changes[_DISABLED] = int(flag)
	if "disabled_designs" in policy:
		designs = _as_designs(policy["disabled_designs"])
		if designs is None:
			bad.append("disabled_designs")
		else:
			dropped = _unknown_count(policy["disabled_designs"])
			if dropped:
				report(
					"policy_designs",
					"Holiday touch: admin policy names unknown designs",
					f"{dropped} unknown entries in disabled_designs were ignored",
				)
			if designs != _designs_json(settings.blocked_designs(stored)):
				changes[_DESIGNS] = designs
	if bad:
		report("policy_malformed", _MALFORMED, f"subkeys=[{', '.join(bad)}] kept the stored values")
	if daily and _stamp_due(stored):
		changes[_SYNCED_AT] = frappe.utils.now()
	if changes:
		frappe.db.set_single_value(_SETTINGS, changes, update_modified=False)


def _stamp_due(stored: dict) -> bool:
	"""True when the stored stamp is empty, unreadable or older than ``_STAMP_GAP_SECONDS``.
	``sync_connection`` also runs on every onboarding-wizard poll; "daily" must not mean
	"per poll"."""
	raw = stored.get(_SYNCED_AT)
	if not raw:
		return True
	try:
		age = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), raw)
	except Exception:
		return True
	return age >= _STAMP_GAP_SECONDS


def _as_flag(value) -> bool | None:
	"""A bool or 0/1; anything else (a string, a list, None) is malformed."""
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	return None


def _as_designs(value) -> str | None:
	"""The normalized JSON list (canonical design order) with unknown entries dropped; None
	when ``value`` is not a list."""
	if not isinstance(value, list):
		return None
	return _designs_json({d for d in value if isinstance(d, str)})


def _unknown_count(value: list) -> int:
	"""How many entries of a ``disabled_designs`` list are not a known design name."""
	return sum(1 for d in value if not isinstance(d, str) or d not in DESIGNS)


def _designs_json(found) -> str:
	return json.dumps([d for d in DESIGNS if d in found])
