"""Bounded deferred decisions for one logical binding; never conversation-global."""

import json

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import txn
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.scrub import scrub_for_share

FIELD = "pending_agent_notes"
MAX_NOTES = 25
MAX_BYTES = 32 * 1024


def _parse(raw):
	if not raw:
		return []
	try:
		items = json.loads(raw)
	except (TypeError, ValueError, RecursionError):
		frappe.throw("This shared request's decision notes need reconciliation.")
	if (
		not isinstance(items, list)
		or len(items) > MAX_NOTES
		or len(raw.encode()) > MAX_BYTES
		or any(
			not isinstance(item, dict)
			or not isinstance(item.get("id"), str)
			or not item["id"]
			or len(item["id"]) > 100
			or not isinstance(item.get("text"), str)
			for item in items
		)
	):
		frappe.throw("This shared request's decision notes need reconciliation.")
	return [{"id": item["id"], "text": item["text"]} for item in items]


def _entries(binding):
	return _parse(frappe.db.get_value(c.BINDING, binding.name, FIELD, for_update=True))


def _store(binding, entries):
	frappe.db.set_value(
		c.BINDING,
		binding.name,
		FIELD,
		json.dumps(entries, ensure_ascii=False, separators=(",", ":")) if entries else None,
		update_modified=False,
	)


def read(binding):
	require_live(binding)
	return _entries(binding)


def append(binding, text: str, *, note_id: str):
	"""Join the caller's decision/settlement transaction; do not commit it early."""
	require_live(binding)
	if not isinstance(text, str) or not isinstance(note_id, str) or not note_id or len(note_id) > 100:
		frappe.throw("Invalid shared decision note.")
	text = scrub_for_share(text)
	entries = _entries(binding)
	existing = next((entry for entry in entries if entry["id"] == note_id), None)
	if existing:
		if existing["text"] != text:
			frappe.throw("This decision identifier already records a different note.")
		return
	entries.append({"id": note_id, "text": text})
	if len(entries) > MAX_NOTES or len(json.dumps(entries, ensure_ascii=False).encode()) > MAX_BYTES:
		frappe.throw("Finish the pending shared request before adding more decisions.")
	_store(binding, entries)


def clear_after_ack(binding, run_id: str, ids):
	"""Consume only proven-delivered IDs, retaining any decision appended later."""
	if not ids:
		return
	if not isinstance(ids, list) or len(ids) > MAX_NOTES or any(not isinstance(value, str) for value in ids):
		c._deny()

	def clear():
		if c.resolve_identity(binding.session_key) != binding:
			c._deny()
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		entries = _entries(binding)
		turn = frappe.db.get_value(
			"Jarvis Chat Turn",
			run_id,
			[
				"conversation",
				"actor",
				"collaboration_binding",
				"collaboration_send_started_at",
				"gateway_run_id",
				"state",
			],
			as_dict=True,
			for_update=True,
		)
		if (
			not turn
			or not turn.collaboration_send_started_at
			or not turn.gateway_run_id
			or turn.state not in {"streaming", "terminal_observed", "finalizing", "done"}
			or (turn.conversation, turn.actor, turn.collaboration_binding)
			!= (binding.conversation, binding.actor, binding.name)
		):
			c._deny()
		_store(binding, [entry for entry in entries if entry["id"] not in set(ids)])
		# Persist the acknowledged note IDs as one replayable actor-bound unit.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return txn.replay_on_conflict(clear, label="collaboration.notes_ack", fresh=True)
