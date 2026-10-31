"""How often the holiday scene plays. Pure: time is injected, nothing is stored here.

Each local day allows at most one showing before 13:00 and one from 13:00, and the
afternoon one waits 3 hours after the morning one. Showings are stored as UTC instants;
the slot is derived in the caller's zone. ``add_showing`` builds the stored showing state;
``api.record_shown`` is its caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_SPLIT_HOUR = 13
_MIN_GAP = timedelta(hours=3)


def may_play(now_utc: datetime, times_utc: list[datetime], tz: str) -> bool:
	"""May the scene play now? ``times_utc`` are earlier showings; only the ones on
	today's local date count, so a stale list or a zone change cannot block a new day.

	Raises ``ZoneInfoNotFoundError`` for an unknown zone and ``ValueError`` for a malformed
	key (``""``, ``"../x"``): the caller must validate the zone and catch both."""
	zone = ZoneInfo(tz)
	now = _utc(now_utc)
	local = now.astimezone(zone)
	today = sorted(t for t in (_utc(x) for x in times_utc) if t.astimezone(zone).date() == local.date())
	am = next((t for t in today if t.astimezone(zone).hour < _SPLIT_HOUR), None)
	pm = next((t for t in today if t.astimezone(zone).hour >= _SPLIT_HOUR), None)
	if local.hour < _SPLIT_HOUR:
		return am is None and pm is None
	if pm is not None:
		return False
	if am is None:
		return True
	return now - am >= _MIN_GAP


def add_showing(state: dict | None, now_utc: datetime, tz: str) -> dict:
	"""``{"date": local "YYYY-MM-DD", "times": [iso utc]}`` with ``now_utc`` appended.
	Showings from any other local date are dropped, and unreadable entries with them.

	Raises ``ZoneInfoNotFoundError`` for an unknown zone and ``ValueError`` for a malformed
	key: the caller must validate the zone and catch both."""
	zone = ZoneInfo(tz)
	now = _utc(now_utc)
	today = now.astimezone(zone).date()
	times = state.get("times") if isinstance(state, dict) else None
	kept = []
	for raw in times if isinstance(times, list) else []:
		when = _parse(raw)
		if when is not None and when.astimezone(zone).date() == today:
			kept.append(when.isoformat())
	kept.append(now.isoformat())
	return {"date": today.isoformat(), "times": kept}


def _utc(value: datetime) -> datetime:
	if value.tzinfo is None:
		return value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)


def _parse(raw) -> datetime | None:
	try:
		return _utc(datetime.fromisoformat(raw))
	except (TypeError, ValueError):
		return None
