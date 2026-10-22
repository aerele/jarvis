"""Actor-safe adapters for the existing bounded, idempotent effect ledger."""

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.chat import txn, usage
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_output import lock_reply
from jarvis.chat.collaboration_permissions import actor_permissions
from jarvis.chat.scrub import scrub_for_share


def _cards(ctx):
	if ctx.errored or not ctx.turn.get("assistant_message"):
		return
	binding = ctx.shared_binding

	def unit():
		require_live(binding)
		with actor_permissions(binding.actor):
			am = ctx.turn["assistant_message"]
			lock_reply(binding, ctx.run_id, am)
			content = frappe.db.get_value(ts.MSG, am, "content") or ""
			from jarvis.chat.cards_enrich import enrich_text

			new = scrub_for_share(enrich_text(content, owner=binding.actor))
			if new != content:
				frappe.db.set_value(ts.MSG, am, "content", new, update_modified=False)
		frappe.db.commit()

	try:
		txn.replay_on_conflict(unit, label="collaboration.enrich", fresh=True)
	except frappe.PermissionError:
		# Settling already-dispatched text remains valid after revoke. Enriching
		# it with a new ERP read does not: leave the original disclosed text.
		frappe.db.rollback()


def _terminal(ctx):
	row = frappe.db.get_value(
		ts.TURN,
		ctx.run_id,
		["state", "terminal_kind", "error", "last_event_seq", "pump_epoch", "was_recovered"],
		as_dict=True,
	)
	if not row or row.state not in {"finalizing", "done", "errored", "cancelled"}:
		c._deny()
	kind = "run:error" if row.state == "errored" else "run:end"
	extra = {"message_id": ctx.turn.get("assistant_message")}
	if row.state == "errored":
		extra["error"] = scrub_for_share(row.error or "This shared request ended with an error.")
	elif row.state == "cancelled":
		extra["stopped"] = True
	else:
		extra.update(enrichment_pending=True, was_recovered=bool(row.was_recovered))
	frappe.db.commit()
	ts.publish_fenced(
		ctx.shared_binding.actor,
		kind,
		conversation_id=ctx.conversation,
		run_id=ctx.run_id,
		event_seq=int(row.last_event_seq or 0),
		pump_epoch=row.pump_epoch,
		**extra,
	)


def run_effect(name, ctx):
	# This closed dispatcher never invokes legacy media, title, macro, memory,
	# File Box, owner-session usage or conversation-owned question helpers.
	if name == "terminal_publish":
		_terminal(ctx)
	elif name == "enrich_cards":
		_cards(ctx)
	elif name == "chat_asks":
		if not ctx.errored:
			from jarvis.chat.collaboration_asks import materialize

			materialize(ctx.run_id)
	elif name == "usage":
		from jarvis.chat.collaboration_usage import capture

		if capture(ctx.shared_binding, ctx.run_id) == usage.USAGE_RETRY:
			raise RuntimeError("Shared usage is not fresh yet")
	elif name == "telemetry_flush":
		from jarvis.chat.finalize import _effect_telemetry

		_effect_telemetry(ctx)
	else:
		c._deny()
