"""Choosing among matched holiday dates. Pure: no database, no cache.

``found`` is ``{date: {occasion: via}}`` as built by ``resolve._matches``. The result has the
shape documented in ``resolve``: ``{kind, design, occasion, year, scene_first_day, reason}``.
"""

from __future__ import annotations

from datetime import date, timedelta

from jarvis.holiday_touch.names import OCCASIONS

SPAN = 3  # days looked at on each side of today
PAYLOAD_VERSION = 1  # the ``v`` of the payload the client reads


def hit(kind, design, occ, year, first_day, reason) -> dict:
	return {
		"kind": kind,
		"design": design,
		"occasion": occ,
		"year": year,
		"scene_first_day": first_day,
		"reason": reason,
	}


def none(reason: str) -> dict:
	return hit("none", None, None, None, False, reason)


def none_payload(zone: str, today: date) -> dict:
	"""The payload that shows nothing: the four keys every answer carries. Used for every
	"no touch" answer, and as the page boot's last-resort fallback."""
	return {"v": PAYLOAD_VERSION, "date": today.isoformat(), "tz": zone, "kind": "none"}


def pick(found: dict, today: date) -> dict:
	"""Day, else the nearest lead-up, else none."""
	if today in found:
		return _day(found, today)
	for ahead in range(1, SPAN + 1):
		on = today + timedelta(days=ahead)
		if on in found:
			return _leadup(found, today, on)
	return none("no_match")


def _single(occs: dict):
	"""The one occasion on a date, or None when two different designs tie."""
	if len({OCCASIONS[o] for o in occs}) != 1:
		return None
	return sorted(occs)[0]


def _day(found: dict, today: date) -> dict:
	occ = _single(found[today])
	if occ is None:
		return none("tie")
	design = OCCASIONS[occ]
	first = not _matched_before(found, design, today)
	year = today.year if occ == "newyear" else None
	return hit("day", design, occ, year, first, found[today][occ])


def _leadup(found: dict, today: date, on: date) -> dict:
	occ = _single(found[on])
	if occ is None:
		return none("tie")
	design = OCCASIONS[occ]
	if _matched_before(found, design, today):
		return none("lookback")  # the festival just happened; do not wish it in advance
	year = on.year if occ == "newyear" else None
	return hit("leadup", design, occ, year, False, found[on][occ])


def _matched_before(found: dict, design: str, today: date) -> bool:
	days = (today - timedelta(days=n) for n in range(1, SPAN + 1))
	return any(OCCASIONS[o] == design for d in days for o in found.get(d, ()))
