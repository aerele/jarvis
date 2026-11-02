"""Test-only override.

The site config ``jarvis_holiday_touch_force`` (``"diwali:day"``, ``"diwali:leadup"``,
``"birthday"``) makes ``resolve.answer`` use a made-up occasion instead of the real one. It
bypasses the occasion cache and the cadence (``may_play`` is True), and applies ONLY when
``developer_mode`` is on: a production site ignores it. An invalid string is ignored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import frappe

from jarvis.holiday_touch import pick
from jarvis.holiday_touch.names import OCCASIONS

CONFIG_KEY = "jarvis_holiday_touch_force"
_KINDS = ("day", "leadup")


def forced_occasion(now: datetime | None = None) -> dict | None:
	"""The occasion dict (the shape of ``resolve.occasion``) the config asks for, or None."""
	try:
		if not frappe.conf.get("developer_mode"):
			return None
		spec = frappe.conf.get(CONFIG_KEY)
		if not isinstance(spec, str):
			return None
		return _build(spec.strip().lower(), now)
	except Exception:
		return None


def _build(spec: str, now: datetime | None) -> dict | None:
	if spec == "birthday":
		return pick.hit("birthday", "birthday", "birthday", None, True, "force")
	occasion, _, kind = spec.partition(":")
	kind = kind or "day"
	if occasion not in OCCASIONS or kind not in _KINDS:
		return None
	moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
	# A lead-up wishes the occasion that is a day away; that decides the year of a new year.
	on = moment.date() + timedelta(days=1 if kind == "leadup" else 0)
	year = on.year if occasion == "newyear" else None
	return pick.hit(kind, OCCASIONS[occasion], occasion, year, kind == "day", "force")
