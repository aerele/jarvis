"""Retire drained capabilities without deleting immutable actor/binding audit."""

import time

import frappe
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.chat import txn
from jarvis.chat.collaboration_access import _live_users, _lock_users
from jarvis.chat.collaboration_tool_claims import unsettled


def _pending():
	frappe.throw("Finish or reconcile pending shared requests and decisions before deleting this chat.")


def _retire_locked(conversation, name=None):
	doc = frappe.db.get_value(
		c.CONVERSATION,
		conversation,
		["name", "owner", "collaboration_active", "collaboration_epoch"],
		as_dict=True,
		for_update=True,
	)
	if not doc or not doc.collaboration_active:
		if frappe.db.exists(c.BINDING, {"conversation": conversation, "state": ["!=", "Retired"]}):
			_pending()  # Missing parents cannot prove active capabilities drained.
		return True
	filters = {"conversation": conversation, **({"name": name} if name else {})}
	# Hints only choose a conservative lock set. The current locked bindings
	# below must all belong to it; an older snapshot cannot retire a new actor.
	actors = set(frappe.get_all(c.BINDING, filters, pluck="actor")) | {doc.owner}
	users = _lock_users(actors)
	grants = frappe.db.get_values(
		c.SHARE,
		{"conversation": conversation},
		["name", "user", "access_mode", "access_revision"],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	live = _live_users(actors, users)
	by_grant = {row.name: row for row in grants}
	bindings = frappe.db.get_values(
		c.BINDING,
		filters,
		[
			"name",
			"actor",
			"conversation",
			"share",
			"grant_revision",
			"conversation_epoch",
			"state",
			"pending_agent_notes",
		],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	if not bindings:
		return True
	if any(row.actor not in actors for row in bindings):
		_pending()
	stale = set()
	for row in bindings:
		grant = by_grant.get(row.share)
		current = (
			doc.owner in live
			and row.actor in live
			and cint(row.conversation_epoch) == cint(doc.collaboration_epoch)
			and (
				(row.actor == doc.owner and not row.share and not cint(row.grant_revision))
				or (
					grant
					and grant.user == row.actor
					and grant.access_mode == "Chat"
					and cint(grant.access_revision) == cint(row.grant_revision)
				)
			)
		)
		if not current:
			stale.add(row.name)
	names = [row.name for row in bindings]
	frappe.db.get_values(
		c.SESSION, {"collaboration_binding": ["in", names]}, "name", order_by="name", for_update=True
	)
	turns = frappe.db.get_values(
		"Jarvis Chat Turn",
		{"conversation": conversation, **({"collaboration_binding": name} if name else {})},
		[
			"name",
			"collaboration_binding",
			"state",
			"collaboration_dispatches",
			"terminal_kind",
			"usage_recorded",
			"collaboration_usage_unavailable",
			"collaboration_send_started_at",
		],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	from jarvis.chat.collaboration_usage import mark_unavailable_locked

	by_binding = {binding.name: binding for binding in bindings}
	for row in turns:
		if (
			row.collaboration_binding in by_binding
			and not row.usage_recorded
			and not row.collaboration_usage_unavailable
		):
			if mark_unavailable_locked(by_binding[row.collaboration_binding], row.name):
				row.collaboration_usage_unavailable = 1
	# Every check uses current locking rows, not the caller's earlier snapshot.
	# Notes have a consumer only while a turn or human decision remains.
	# The checks below fence those consumers; drained notes remain immutable audit.
	if any(row.state == "Uncertain" for row in bindings):
		_pending()
	if any(
		row.state not in {"done", "errored", "cancelled"}
		or unsettled(row.collaboration_dispatches)
		or (
			row.collaboration_send_started_at
			and row.terminal_kind in {"relay:final", "relay:error"}
			and not row.usage_recorded
			and not row.collaboration_usage_unavailable
		)
		for row in turns
	):
		_pending()
	if turns and frappe.db.get_values(
		"Jarvis Turn Effect",
		{"turn": ["in", [row.name for row in turns]], "status": ["!=", "done"]},
		"name",
		limit=1,
		for_update=True,
	):
		_pending()
	actions = frappe.db.get_values(
		"Jarvis Pending Action",
		{"conversation": conversation, **({"collaboration_binding": name} if name else {})},
		["name", "status", "settled", "collaboration_binding"],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	# A withdrawn grant's unexecuted decisions have no lawful continuation.
	# Retire them as cancelled, preserving identity and deferred notes as audit.
	# Executing/unknown outcomes still block retirement above and below.
	from jarvis.chat.pending_actions._store import _terminal_update, claim_settled, null_sealed

	for row in actions:
		if row.collaboration_binding in stale and row.status == "Pending":
			if _terminal_update(row.name, ["Pending"], "Cancelled", reason_code="cancelled"):
				claim_settled([row.name])
				null_sealed([row.name])
				row.status, row.settled = "Cancelled", 1
	if any(
		row.status in {"Pending", "Executing"} or (row.collaboration_binding and not row.settled)
		for row in actions
	):
		_pending()
	questions = frappe.db.get_values(
		"Jarvis Approval Request",
		{
			"conversation": conversation,
			"status": "Pending",
			**({"collaboration_binding": name} if name else {}),
		},
		["name", "collaboration_binding"],
		as_dict=True,
		order_by="name",
		for_update=True,
	)
	for row in questions:
		if row.collaboration_binding not in stale:
			_pending()
		frappe.db.set_value(
			"Jarvis Approval Request",
			row.name,
			{"status": "Dismissed", "decision": "Shared access ended; no action was taken."},
		)
	frappe.db.delete(c.SESSION, {"collaboration_binding": ["in", names]})
	for row in bindings:
		if row.state != "Retired":
			frappe.db.set_value(c.BINDING, row.name, "state", "Retired")
	return True


def retire_conversation(conversation):
	"""Trusted delete hook; its caller owns commit/rollback of the entire delete."""
	return _retire_locked(conversation)


def on_conversation_trash(doc, method=None):
	if doc.get("collaboration_active"):
		retire_conversation(doc.name)


def sweep(limit=25):
	from jarvis.chat import session_lifecycle

	days = session_lifecycle._retention_days()
	if not days or not c.schema_ready() or not txn.owns_transaction():
		return {"retired": 0}
	rows = frappe.db.sql(
		f"""SELECT name, conversation FROM `tab{c.BINDING}`
		WHERE state='Active' AND creation < %(cut)s
		ORDER BY COALESCE(cleanup_checked_at,'1970-01-01'), creation, name LIMIT %(limit)s""",
		{"cut": frappe.utils.add_to_date(None, days=-days), "limit": max(1, min(50, cint(limit)))},
		as_dict=True,
	)
	retired = 0
	for row in rows:
		# Rotation survives a blocked retirement, so old human decisions cannot
		# starve collection of other completed logical bindings.
		frappe.db.set_value(
			c.BINDING, row.name, "cleanup_checked_at", frappe.utils.now_datetime(), update_modified=False
		)
		frappe.db.commit()
		try:

			def unit():
				result = _retire_locked(row.conversation, row.name)
				frappe.db.commit()
				return result

			retired += int(txn.replay_on_conflict(unit, label="collaboration.retire", fresh=True))
		except frappe.ValidationError:
			frappe.db.rollback()
			frappe.clear_messages()
	return {"retired": retired}


def reclaim_runtime(name, sess):
	"""Retired auth never licenses new work; delete only its exact idle UUID."""
	row = frappe.db.get_value(c.BINDING, name, ["*"], as_dict=True)
	if (
		not row
		or row.state != "Retired"
		or row.runtime_retired_at
		or c.NAMESPACE not in row.session_key
		or frappe.db.exists(c.SESSION, {"collaboration_binding": name})
	):
		return False
	if not row.runtime_session_id and frappe.db.exists(
		"Jarvis Chat Turn",
		{"collaboration_binding": name, "collaboration_send_started_at": ["is", "set"]},
	):
		# An absent UUID is reconcilable only for an abandoned provisioning
		# attempt. A possibly sent request must retain its original identity.
		return False
	# Retirement is irreversible. Release all database locks before either RPC;
	# the final CAS still requires that same retired key and physical UUID.
	if frappe.db.transaction_writes:
		raise RuntimeError("Native cleanup requires a clean transaction.")
	frappe.db.commit()
	result = sess._request(
		"sessions.list",
		{
			"label": "shared-" + row.logical_run_id,
			"limit": 2,
			"includeDerivedTitles": False,
			"includeLastMessage": False,
		},
		timeout_s=5,
	)
	entries = (result.get("payload") or result).get("sessions")
	if not isinstance(entries, list) or len(entries) > 1:
		return False
	if entries:
		entry = entries[0]
		if (
			not isinstance(entry, dict)
			or entry.get("key") != row.session_key
			or not isinstance(entry.get("sessionId"), str)
			or not entry["sessionId"]
			or (row.runtime_session_id and entry["sessionId"] != row.runtime_session_id)
			or entry.get("hasActiveRun") is not False
		):
			return False
		if not row.runtime_session_id:
			if not _remember_unsent_runtime(row, entry["sessionId"]):
				return False
			row.runtime_session_id = entry["sessionId"]
		# The deployed probe proves UUID refusal happens before drain.
		sess._request(
			"sessions.delete",
			{"key": row.session_key, "expectedSessionId": row.runtime_session_id},
			timeout_s=5,
		)
	else:
		# Lost delete acknowledgement: absence from a label search alone is
		# insufficient. Inspect the exact key without creating a new session.
		result = sess._request("sessions.describe", {"key": row.session_key}, timeout_s=5)
		payload = result.get("payload") or result
		if "session" not in payload or payload["session"] is not None:
			return False
	frappe.db.sql(
		f"""UPDATE `tab{c.BINDING}` SET runtime_retired_at=%(now)s
		WHERE name=%(name)s AND state='Retired' AND session_key=%(key)s
		AND COALESCE(runtime_session_id,'')=%(physical)s AND runtime_retired_at IS NULL""",
		{
			"name": name,
			"key": row.session_key,
			"physical": row.runtime_session_id or "",
			"now": frappe.utils.now_datetime(),
		},
	)
	frappe.db.commit()
	return True


def _remember_unsent_runtime(hint, physical_id):
	"""Persist exact idle metadata before deleting an abandoned provision.

	The opaque label/key is unique to this logical request. Retirement is
	irreversible, authentication is gone, and no send marker may exist. This
	also retains the UUID if the following delete succeeds but loses its ACK.
	"""
	frappe.db.get_value(c.CONVERSATION, hint.conversation, "name", for_update=True)
	row = frappe.db.get_value(
		c.BINDING, hint.name, ["state", "session_key", "runtime_session_id"], as_dict=True, for_update=True
	)
	if (
		not row
		or row.state != "Retired"
		or row.session_key != hint.session_key
		or row.runtime_session_id
		or frappe.db.exists(c.SESSION, {"collaboration_binding": hint.name})
		or frappe.db.exists(
			"Jarvis Chat Turn",
			{"collaboration_binding": hint.name, "collaboration_send_started_at": ["is", "set"]},
		)
	):
		frappe.db.rollback()
		return False
	frappe.db.set_value(c.BINDING, hint.name, "runtime_session_id", physical_id, update_modified=False)
	frappe.db.commit()
	return True


def reclaim_sessions(limit=25):
	"""Bounded gateway cleanup; immutable binding evidence remains on the bench."""
	if not c.schema_ready() or not txn.owns_transaction():
		return {"reclaimed": 0}
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{c.BINDING}` WHERE state='Retired'
		AND runtime_retired_at IS NULL
		ORDER BY COALESCE(cleanup_checked_at,'1970-01-01'), creation, name LIMIT %(limit)s""",
		{"limit": max(1, min(25, cint(limit)))},
		as_dict=True,
	)
	if not rows:
		return {"reclaimed": 0}
	from jarvis.chat.agent_client import AgentSession

	url = frappe.get_cached_doc("Jarvis Settings").agent_url
	frappe.db.commit()
	count, deadline = 0, time.monotonic() + 30
	sess = None
	try:
		sess = AgentSession.connect(url or "")
		for row in rows:
			if time.monotonic() >= deadline:
				break
			frappe.db.set_value(
				c.BINDING, row.name, "cleanup_checked_at", frappe.utils.now_datetime(), update_modified=False
			)
			frappe.db.commit()
			try:
				count += int(reclaim_runtime(row.name, sess))
			except Exception as exc:
				frappe.db.rollback()
				frappe.log_error(
					title="shared session cleanup", message=f"binding={row.name} cause={type(exc).__name__}"
				)
	finally:
		if sess:
			sess.close()
	return {"reclaimed": count}
