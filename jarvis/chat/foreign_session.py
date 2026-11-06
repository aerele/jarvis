"""Record and report plugin sessions this site never started.

A restored copy of a site can keep using the real site's Jarvis agent. The agent's
tool calls then reach the real site, which has no row for the session. Each such
session key is recorded once per 24 h, counted, and reported to admin as a count
and a time, never the key. Event-driven only; recording never raises.
"""

import hashlib

import frappe

from jarvis._cache_latch import claim_once
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

_ONE_DAY = 24 * 3600
_KEY_PREFIX = "jarvis:foreign_session:"
_NOTICE_KEY = "jarvis:foreign_session:notice"
_INSTANCE_CHANGED = "instance_changed"

NOTICE_SUBJECT = "Another site is using this site's Jarvis connection"
NOTICE_MESSAGE = (
	"Jarvis received requests for chats this site never started. This usually means a copy or "
	"backup restore of this site is still running Jarvis. Turn off Jarvis on that copy, or contact support."
)


_KNOWN_KEY_DOCTYPES = ("Jarvis Agent Run", "Jarvis Conversation", "Jarvis Chat Session")


def has_row_for_key(session_key: str) -> bool:
	"""Strict exact-key lookup: True only on a positive match, and a lookup error
	RAISES so a caller that must not fail open (``vouch_session``) can refuse.

	One query for all the tables that exist (``table_exists`` answers from a cached
	table list, so it costs no query)."""
	from functools import reduce

	from jarvis.chat.collaboration import BINDING

	tables = [dt for dt in (*_KNOWN_KEY_DOCTYPES, BINDING) if frappe.db.table_exists(dt)]
	if not tables:
		return False
	lookups = []
	for doctype in tables:
		table = frappe.qb.DocType(doctype)
		lookups.append(frappe.qb.from_(table).select(1).where(table.session_key == session_key).limit(1))
	return bool(reduce(lambda a, b: a.union_all(b), lookups).run())


def is_known_key(session_key: str) -> bool:
	"""True when this site knows the session key anywhere (so it is not foreign).

	A run's chat-session row is deleted when the run finishes, and a shared session can
	fail to resolve (retired binding, bumped epoch, revoked share) while still being ours,
	so the audit trails count too. Throwaway sessions (prewarm, chat titles, polish,
	preflight) never get a row, only a site claim from ``create_session``, so a claim
	counts as known as well. Fails SAFE: a row or claim lookup error answers True, which
	means "do not alert".
	"""
	from jarvis.chat import session_claims

	try:
		return has_row_for_key(session_key) or session_claims.is_claimed_strict(session_key)
	except Exception:
		frappe.logger("jarvis.foreign_session").warning("known-session lookup failed", exc_info=True)
		return True


def _claim(key: str) -> bool:
	"""Atomic set-if-absent with a 24 h expiry, so parallel first calls record once."""
	return claim_once(key, _ONE_DAY)


def record(session_key: str) -> bool:
	"""True only the first time an unknown session key is seen in 24 h. Never raises."""
	return _record(_KEY_PREFIX + _digest(session_key), session_key, skip_known=True)


def record_instance_mismatch(session_key: str) -> bool:
	"""Like ``record`` for a key this site DOES know but whose plugin is pinned to another
	instance id (a database restored elsewhere). Own 24 h throttle; no known-key guard.
	Also marks the site "instance changed" (every time, not once a day) so chat readiness
	says so until a Re-verify passes."""
	mark_instance_changed()
	return _record(_KEY_PREFIX + "mismatch:" + _digest(session_key), session_key, skip_known=False)


def instance_changed() -> bool:
	"""True while a plugin pinned to another instance id has been seen and no Re-verify
	has passed since. Read straight off ``tabSingles``, not ``get_single_value``: that
	validates the field against the DocType meta and pops a "Field does not exist"
	message on a site whose Jarvis Settings is not migrated yet. A missing row is False.
	Fails OPEN (False) on a read error."""
	try:
		singles = frappe.qb.DocType("Singles")
		rows = (
			frappe.qb.from_(singles)
			.select(singles.value)
			.where(singles.doctype == "Jarvis Settings")
			.where(singles.field == _INSTANCE_CHANGED)
			.run()
		)
		return bool(rows) and str(rows[0][0] or "0") not in ("", "0")
	except Exception:
		return False


def _field_migrated() -> bool:
	"""``set_single_value`` throws (and queues a user message) for a field the DocType
	meta does not have yet; check first so an unmigrated site never raises."""
	return bool(frappe.get_meta("Jarvis Settings").has_field(_INSTANCE_CHANGED))


def _set_instance_changed(value: int) -> None:
	if _field_migrated():
		frappe.db.set_single_value("Jarvis Settings", _INSTANCE_CHANGED, value, update_modified=False)


def mark_instance_changed() -> None:
	"""Never raises."""
	try:
		if not instance_changed():
			_set_instance_changed(1)
	except Exception:
		frappe.logger("jarvis.foreign_session").warning("could not mark instance changed", exc_info=True)


def clear_instance_changed() -> None:
	"""Never raises."""
	try:
		if instance_changed():
			_set_instance_changed(0)
	except Exception:
		frappe.logger("jarvis.foreign_session").warning("could not clear instance changed", exc_info=True)


def _digest(session_key: str) -> str:
	return hashlib.sha256(str(session_key).encode()).hexdigest()[:12]


def _record(key: str, session_key: str, *, skip_known: bool) -> bool:
	try:
		if (skip_known and is_known_key(session_key)) or not _claim(key):
			return False
		seen = int(frappe.db.get_single_value("Jarvis Settings", "foreign_sessions_seen") or 0)
		frappe.db.set_single_value(
			"Jarvis Settings", "foreign_sessions_seen", seen + 1, update_modified=False
		)
		frappe.db.set_single_value(
			"Jarvis Settings", "foreign_session_last_at", frappe.utils.now(), update_modified=False
		)
		frappe.enqueue(
			"jarvis.chat.foreign_session.report",
			queue="short",
			job_id="jarvis-foreign-session-report",
			deduplicate=True,
			enqueue_after_commit=True,
		)
		return True
	except Exception:
		frappe.logger("jarvis.foreign_session").warning("could not record foreign session", exc_info=True)
		try:
			frappe.cache().delete_value(key)
		except Exception:
			pass
		return False


def report() -> None:
	"""Send the count and last time to admin, and notify System Managers once a day."""
	from jarvis import admin_client

	count = int(frappe.db.get_single_value("Jarvis Settings", "foreign_sessions_seen") or 0)
	last_seen_at = str(frappe.db.get_single_value("Jarvis Settings", "foreign_session_last_at") or "")
	try:
		admin_client.report_foreign_session(count, last_seen_at)
	except (AdminUnreachableError, AdminAuthError, AdminRateLimitedError, AdminValidationError):
		frappe.logger("jarvis.foreign_session").warning("admin did not accept the foreign session report")
	_notify_system_managers_once_a_day()


def _notify_system_managers_once_a_day() -> None:
	from jarvis.learning.lifecycle import notify_system_managers

	if _claim(_NOTICE_KEY):
		notify_system_managers(NOTICE_SUBJECT, NOTICE_MESSAGE)
