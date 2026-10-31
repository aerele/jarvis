"""Committed shared updates carry invalidation only, never a producer's content."""

import time

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import sharing
from jarvis.permissions import JARVIS_ACCESS_ROLES

_last_failure_log = 0.0


def is_shared(conversation):
	if not isinstance(conversation, str) or not frappe.db.has_column(c.CONVERSATION, "collaboration_active"):
		return False
	# No negative cache: first activation must immediately fence owner events,
	# even on long-lived workers that previously handled this private chat.
	return bool(
		# Only the fixed conversation DocType is formatted; its name stays bound.
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection
			f"SELECT 1 FROM `tab{c.CONVERSATION}` WHERE name=%s AND collaboration_active=1",
			(conversation,),
		)
	)


def _recipients(conversation):
	# An invalidation contains no content or controls. Plain reads on a fresh
	# connection are enough: every subsequent content read authorizes again.
	# Never wait for a participant's row lock on the shard's producer thread.
	doc = frappe.db.get_value(c.CONVERSATION, conversation, ["*"], as_dict=True)
	if not doc or not doc.collaboration_active:
		return []
	grants = set(frappe.get_all(c.SHARE, {"conversation": conversation}, pluck="user"))
	candidates = grants | {doc.owner}
	live = set(
		row[0]
		for row in frappe.db.sql(
			"""SELECT u.name FROM `tabUser` u WHERE u.name IN %s
			AND u.enabled=1 AND u.user_type='System User'
			AND u.name NOT IN ('Guest','Administrator')
			AND EXISTS (SELECT 1 FROM `tabHas Role` r WHERE r.parent=u.name
			AND r.parenttype='User' AND r.role IN %s)""",
			(sorted(candidates), JARVIS_ACCESS_ROLES),
		)
	)
	if doc.owner not in live:
		return []
	result = {doc.owner}
	if sharing.sharing_enabled() and sharing._shareable(doc):
		result.update(grants & live)
	return sorted(result)


def _read_recipients(conversation):
	"""Read current membership without inheriting the producer's read snapshot."""
	from jarvis.chat.collaboration_database import read_snapshot

	with read_snapshot(timeout=1):
		return _recipients(conversation)


def notify(user, conversation, kind):
	from jarvis.chat.events import CHANNEL

	if kind == "share:changed":
		# Revoked users still need this inert notice so they discard their view.
		frappe.publish_realtime(CHANNEL, {"kind": kind, "conversation_id": conversation}, user=user)
		return

	def committed():
		try:
			# No database connection/fan-out on the shard's producer thread.
			# Only an opaque conversation id enters the existing short queue.
			frappe.enqueue(
				"jarvis.chat.collaboration_realtime.publish_notice",
				queue="short",
				timeout=30,
				enqueue_after_commit=False,
				conversation=conversation,
			)
		except Exception as exc:
			_log_failure(exc)

	# Most producers have already committed before publishing. Waiting for an
	# unrelated next transaction loses their notice if that transaction rolls back.
	if frappe.db.transaction_writes == 0:
		committed()
	else:
		# Coalesce only on this database's current transaction callback list.
		# Rollback/reset naturally removes the marker; no process-local state.
		if any(
			getattr(fn, "_shared_notice", None) == conversation for fn in frappe.db.after_commit._functions
		):
			return
		committed._shared_notice = conversation
		frappe.db.after_commit.add(committed)


def publish_notice(conversation):
	from jarvis.chat.events import CHANNEL

	try:
		for recipient in _read_recipients(conversation):
			frappe.publish_realtime(
				CHANNEL, {"kind": "collaboration:changed", "conversation_id": conversation}, user=recipient
			)
	except Exception as exc:
		# Poll/reconnect re-authorize too. There is never a raw fallback.
		_log_failure(exc)


def _log_failure(exc):
	global _last_failure_log
	now = time.monotonic()
	if now - _last_failure_log >= 60:
		_last_failure_log = now
		frappe.logger("jarvis.collaboration").warning("notification failed: %s", type(exc).__name__)
