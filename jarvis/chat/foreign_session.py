"""Record and report plugin sessions this site never started.

A restored copy of a site can keep using the real site's Jarvis agent. The agent's
tool calls then reach the real site, which has no row for the session. Each such
session key is recorded once per 24 h, counted, and reported to admin as a count
and a time, never the key. Event-driven only; recording never raises.
"""

import hashlib

import frappe

from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

_ONE_DAY = 24 * 3600
_KEY_PREFIX = "jarvis:foreign_session:"
_NOTICE_KEY = "jarvis:foreign_session:notice"

NOTICE_SUBJECT = "Another site is using this site's Jarvis connection"
NOTICE_MESSAGE = (
	"Jarvis received requests for chats this site never started. This usually means a copy or "
	"backup restore of this site is still running Jarvis. Turn off Jarvis on that copy, or contact support."
)


def record(session_key: str) -> bool:
	"""True only the first time this session key is seen in 24 h. Never raises."""
	try:
		key = _KEY_PREFIX + hashlib.sha256(str(session_key).encode()).hexdigest()[:12]
		if frappe.cache().get_value(key):
			return False
		frappe.cache().set_value(key, 1, expires_in_sec=_ONE_DAY)
		seen = int(frappe.db.get_single_value("Jarvis Settings", "foreign_sessions_seen") or 0)
		frappe.db.set_single_value(
			"Jarvis Settings", "foreign_sessions_seen", seen + 1, update_modified=False
		)
		frappe.db.set_single_value(
			"Jarvis Settings", "foreign_session_last_at", frappe.utils.now(), update_modified=False
		)
		frappe.enqueue("jarvis.chat.foreign_session.report", queue="short", enqueue_after_commit=True)
		return True
	except Exception:
		frappe.logger("jarvis.foreign_session").warning("could not record foreign session", exc_info=True)
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
	if frappe.cache().get_value(_NOTICE_KEY):
		return
	frappe.cache().set_value(_NOTICE_KEY, 1, expires_in_sec=_ONE_DAY)
	# System context (background job): get_all is intended.
	managers = frappe.get_all(
		"Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"
	)
	enabled = frappe.get_all("User", filters={"name": ["in", managers or [""]], "enabled": 1}, pluck="name")
	for user in enabled:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"from_user": user,
				"type": "Alert",
				"subject": NOTICE_SUBJECT,
				"email_content": NOTICE_MESSAGE,
			}
		).insert(ignore_permissions=True)
