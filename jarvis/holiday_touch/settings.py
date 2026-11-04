"""Site and user settings the holiday touch reads.

Each read is guarded by ``get_meta(...).has_field``, so a field that a release has not
migrated yet is simply absent: nothing is thrown, and nothing is left in
``frappe.local.message_log`` for a JSON response to show as a red dialog. A switch whose
field does not exist yet reads as off.

The two feature switches (``holiday_touches_enabled``, ``birthday_greetings_enabled``) are
on by default. A Single stores a value only once something writes it, and a fresh install
writes other Jarvis Settings rows without these two, so a missing row means "never touched":
it reads as the field's default (on). An explicit ``"0"`` row is an admin's choice and reads
as off.
"""

from __future__ import annotations

import json
from datetime import datetime

import frappe

_SETTINGS = "Jarvis Settings"
_USER_SETTINGS = "Jarvis User Settings"
_SITE_FIELDS = (
	"holiday_touches_enabled",
	"birthday_greetings_enabled",
	"holiday_policy_disabled",
	"holiday_policy_disabled_designs",
	"holiday_policy_names",
	"brand_logo",
	"agent_name",
)
_DEFAULT_ON = ("holiday_touches_enabled", "birthday_greetings_enabled")
_USER_FIELDS = ("show_holiday_touches", "holiday_touch_shown")
_MAX_SHOWN = 10  # earlier showings kept; a day holds two, so more is junk
_DEFAULT_AGENT_NAME = "Jarvis"


def site() -> dict:
	"""The site fields above in one query (a Singles read); ``{}`` when it cannot be read.
	A default-on switch with no row reads as its field default (see the module docstring)."""
	try:
		meta = frappe.get_meta(_SETTINGS)
		raw = frappe.db.get_singles_dict(_SETTINGS)
		found = {f: raw.get(f) for f in _SITE_FIELDS if meta.has_field(f)}
		for field in _DEFAULT_ON:
			if field in found and field not in raw:
				found[field] = meta.get_field(field).default
		return found
	except Exception:
		return {}


def on(values: dict, field: str) -> bool:
	"""A Check read from a Singles string: ``"0"``, ``""`` and absent are off."""
	value = values.get(field)
	return bool(value) and str(value) != "0"


def white_label(values: dict) -> bool:
	"""A custom logo or agent name makes a white-label site: it gets the words only, no worn
	design and no scene."""
	name = (values.get("agent_name") or "").strip()
	return bool(values.get("brand_logo")) or bool(name and name != _DEFAULT_AGENT_NAME)


def blocked_designs(values: dict) -> frozenset:
	raw = values.get("holiday_policy_disabled_designs")
	try:
		data = json.loads(raw) if raw else []
	except Exception:  # ValueError, and RecursionError on a pathologically nested value
		return frozenset()
	return frozenset(d for d in data if isinstance(d, str)) if isinstance(data, list) else frozenset()


def names(values: dict) -> dict:
	"""The operator's extra holiday names as stored, ``{"aliases", "near_misses"}``; ``{}``
	when there are none or the text is unreadable. ``match.match_occasion`` takes it as
	``extra`` and ignores whatever is the wrong shape."""
	raw = values.get("holiday_policy_names")
	try:
		data = json.loads(raw) if raw else {}
	except Exception:  # ValueError, and RecursionError on a pathologically nested value
		return {}
	return data if isinstance(data, dict) else {}


def user_row(user: str):
	"""One read for the personal switch and the shown state; None when there is no row."""
	meta = frappe.get_meta(_USER_SETTINGS)
	fields = [f for f in _USER_FIELDS if meta.has_field(f)]
	return frappe.db.get_value(_USER_SETTINGS, user, fields, as_dict=True) if fields else None


def personal_off(row) -> bool:
	value = row.get("show_holiday_touches") if row else None
	return value is not None and not int(value)


def shown_times(row) -> list[datetime]:
	"""Earlier showings from the stored ``{"date", "times"}`` state; junk is dropped. A naive
	time is read as UTC by ``cadence.may_play``."""
	try:
		times = json.loads(row.get("holiday_touch_shown") or "null")["times"] if row else []
	except Exception:  # ValueError, TypeError, KeyError, and RecursionError on nested JSON
		return []
	found = []
	for raw in times[-_MAX_SHOWN:] if isinstance(times, list) else []:
		try:
			found.append(datetime.fromisoformat(raw))
		except (TypeError, ValueError):
			continue
	return found
