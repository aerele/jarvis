"""Validate the durable output pointer before any collaborative row mutation."""

import frappe

from jarvis.chat import collaboration as c


def lock_reply(binding, run_id: str, assistant: str):
	# Caller holds Conv before this Turn -> Message pair. This is identity-only:
	# already-dispatched results retain attribution after grant revocation.
	row = frappe.db.get_value(
		"Jarvis Chat Turn",
		run_id,
		[
			"conversation",
			"actor",
			"collaboration_binding",
			"assistant_message",
			"collaboration_send_started_at",
		],
		as_dict=True,
		for_update=True,
	)
	if (
		not row
		or not row.collaboration_send_started_at
		or not assistant
		or (row.conversation, row.actor, row.collaboration_binding, row.assistant_message)
		!= (binding.conversation, binding.actor, binding.name, assistant)
	):
		c._deny()
	reply = frappe.db.get_value(
		"Jarvis Chat Message",
		assistant,
		["conversation", "actor", "collaboration_binding", "collaboration_run_id", "role"],
		as_dict=True,
		for_update=True,
	)
	if not reply or tuple(
		reply.get(field)
		for field in ("conversation", "actor", "collaboration_binding", "collaboration_run_id", "role")
	) != (binding.conversation, binding.actor, binding.name, run_id, "assistant"):
		c._deny()
	return reply
