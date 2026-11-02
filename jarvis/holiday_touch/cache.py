"""The holiday touch cache.

What is cached: the occasion (``resolve.occasion``) and the per-company set of placeholder
birth dates (dates only). The switches are applied on every read, outside the cache.

Every key starts with ``jarvis:holiday:`` and carries a random generation token, ``gen``:

* the prefix is in ``persistent_cache_keys`` (hooks.py), so ``frappe.clear_cache()`` (the
  pump runs one about every five minutes) and ERPNext's ``Employee.on_update`` leave it alone;
* a data change rotates ``gen`` instead of deleting keys. ``delete_keys`` is ``KEYS prefix*``
  and is never used here. The old keys expire on their own (TTL below);
* ``gen`` is a random token, never a counter, so a key written under an old ``gen`` can never
  collide with a later one.

Handlers that Frappe calls (``rotate_after_commit``, ``rotate``, ``on_after_migrate``) never
raise: ``CallbackManager.run`` does not catch, and ``after_migrate`` runs inside migrate's
atomic block, so a raise would abort the commit callbacks or the migrate.

This module does not import ``resolve``: the caller hands in the function that computes an
answer, so the dependency runs one way (``resolve`` -> ``cache``).
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe

from jarvis.holiday_touch import pick
from jarvis.holiday_touch.report import report

PREFIX = "jarvis:holiday:"
GEN_KEY = PREFIX + "gen"
# No answer outlives two hours: that bounds how stale a read inside a long REPEATABLE READ
# transaction can be, and how long a value written with db.set_value (which fires no doc
# event, so no rotation) stays visible.
MAX_TTL = 2 * 3600
ERROR_TTL = 10 * 60  # how long an error answer is remembered
QUEUED_FLAG = "jarvis_holiday_rotation_queued"  # frappe.flags: one rotation per transaction
# A cached occasion must carry all of these, or it is read as a miss (an entry written by an
# older release may have another shape).
_REQUIRED_KEYS = frozenset(("kind", "design", "occasion", "year", "scene_first_day", "reason"))
# The fields whose change can alter an occasion; any other save leaves the cache alone.
_RELEVANT_FIELDS = {
	"Employee": ("user_id", "status", "date_of_birth", "company"),
	"Company": ("country", "default_holiday_list"),
}


# -- the occasion ---------------------------------------------------------------------


def occasion_cached(user: str, zone: str, now: datetime | None, compute) -> dict:
	"""``compute()`` (the occasion dict for ``user``) through the cache, keyed by user, zone
	and local date. A compute error is remembered as a ``none`` answer (reason ``error``) for
	10 minutes and then re-raised, so the caller still logs it once (``resolve.safe_answer``)
	and the next reads in that window are quiet. A cache fault is never an error: the answer
	is just computed. ``zone`` must already be a valid IANA zone."""
	key = _occasion_key(user, zone, now)
	hit = _read(key) if key else None
	if _is_occasion(hit):
		return hit
	try:
		found = compute()
	except Exception:
		if key:
			_write(key, pick.none("error"), ERROR_TTL)
		raise
	if key:
		# A holiday-list failure inside the computation comes back as a none, not a raise:
		# keep it for the short error window so the fault is retried soon.
		failed = found.get("reason") == "holiday_error"
		_write(key, found, ERROR_TTL if failed else positive_ttl(now, zone))
	return found


def _is_occasion(value) -> bool:
	"""A dict of the current shape (and a ``fallback`` of it, when present)."""
	if not isinstance(value, dict) or not _REQUIRED_KEYS.issubset(value):
		return False
	fallback = value.get("fallback")
	return fallback is None or _is_occasion(fallback)


def _occasion_key(user: str, zone: str, now: datetime | None) -> str | None:
	try:
		return f"{PREFIX}{current_gen()}:{user}:{zone}:{_local_now(now, zone).date().isoformat()}"
	except Exception:
		return None


def positive_ttl(now: datetime | None, zone: str) -> int:
	"""Seconds to the next local midnight, at most two hours, at least one."""
	local = _local_now(now, zone)
	midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=ZoneInfo(zone))
	left = (midnight.astimezone(timezone.utc) - local.astimezone(timezone.utc)).total_seconds()
	return max(1, min(MAX_TTL, int(left)))


def _local_now(now: datetime | None, zone: str) -> datetime:
	if now is None:
		now = datetime.now(timezone.utc)
	elif now.tzinfo is None:
		now = now.replace(tzinfo=timezone.utc)
	return now.astimezone(ZoneInfo(zone))


# -- the placeholder set --------------------------------------------------------------


def placeholder_dates(company: str, today: date, compute) -> set[date]:
	"""``compute()`` (a set of dates) cached per company, ``gen`` and day. Only a sorted list
	of ISO dates is stored: no counts, no names, no employee."""
	try:
		key = f"{PREFIX}{current_gen()}:ph:{company}:{today.isoformat()}"
	except Exception:
		return compute()
	hit = _read(key)
	if isinstance(hit, list):
		try:
			return {date.fromisoformat(d) for d in hit}
		except (TypeError, ValueError):
			pass
	found = compute()
	_write(key, sorted(d.isoformat() for d in found), MAX_TTL)
	return found


# -- gen and rotation -----------------------------------------------------------------


def current_gen() -> str:
	"""The live generation token; created (random) on first use. Read with ``expires=True``
	so another process's rotation is seen at once, not after this request's local copy."""
	cache = frappe.cache()
	gen = cache.get_value(GEN_KEY, expires=True)
	if not gen or not isinstance(gen, str):
		gen = _new_token()
		cache.set_value(GEN_KEY, gen)
	return gen


def rotate() -> None:
	"""Make every cached answer unreachable. Never raises."""
	try:
		frappe.cache().set_value(GEN_KEY, _new_token())
	except Exception:
		report("rotate", "Holiday touch: cache rotate failed")


def rotate_after_commit(doc=None, method=None) -> None:
	"""doc_events handler: rotate once the data change is committed, never on a rollback
	(a rollback resets ``frappe.db.after_commit``). Only a change that can alter an occasion
	counts (``_relevant``), and at most one rotation is queued per transaction: a bulk import
	of employees would otherwise queue one per row. Never raises."""
	try:
		if not _relevant(doc, method) or frappe.flags.get(QUEUED_FLAG):
			return
		frappe.db.after_commit.add(_rotate_queued)
		frappe.db.after_rollback.add(_clear_queued)
		frappe.flags[QUEUED_FLAG] = True
	except Exception:
		report("rotate", "Holiday touch: cache rotate failed")


def _relevant(doc, method) -> bool:
	"""Deletes, Holiday Lists and Holiday List Assignments always count. An Employee or a
	Company save counts only when a field an occasion depends on changed (a new document
	has no earlier value, so it counts)."""
	fields = _RELEVANT_FIELDS.get(getattr(doc, "doctype", None))
	if fields is None or method == "on_trash":
		return True
	return any(doc.has_value_changed(f) for f in fields)


def _rotate_queued() -> None:
	_clear_queued()
	rotate()


def _clear_queued() -> None:
	frappe.flags.pop(QUEUED_FLAG, None)


def on_after_migrate() -> None:
	"""A release can change the matcher, and ``bench migrate`` clears the cache but not the
	persistent prefix, so rotate. Runs inside migrate's atomic block: swallow everything."""
	try:
		rotate()
	except Exception:
		pass


def _new_token() -> str:
	return secrets.token_hex(8)


# -- plumbing -------------------------------------------------------------------------


def _read(key: str):
	try:
		return frappe.cache().get_value(key, expires=True)
	except Exception:
		return None


def _write(key: str, value, ttl: int) -> None:
	try:
		frappe.cache().set_value(key, value, expires_in_sec=ttl)
	except Exception:
		pass
