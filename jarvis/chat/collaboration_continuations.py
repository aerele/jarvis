"""Continue only the initiator's logical chain, behind the settlement CAS."""

import hashlib
import json

import frappe

from jarvis.chat import admission, txn
from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_admission import SharedAdmission
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_turn import new_admission_context, turn_context


def deliver(item):
	from jarvis import api
	from jarvis.chat.pending_actions._settle import with_reference

	key = frappe.db.get_value(c.BINDING, item["collaboration_binding"], "session_key")
	binding = c.resolve_identity(key)
	if binding.conversation != item["conversation"] or binding.actor != item["actor"]:
		c._deny()
	with turn_context(binding, item["run_id"]):
		api.persist_tool_receipt(
			item["conversation"],
			item["tool"],
			item["args"],
			with_reference(item["result"], item["name"]) if item["status"] == "Failed" else item["result"],
			action_outcome=item["outcome"],
			flip_token=item["name"],
			overwrite_outcomes=("cancelled", "superseded"),
		)


def enqueue(
	binding,
	prompt,
	*,
	claim=None,
	on_seed=None,
	seed_hidden=True,
	seed_origin="continuation",
	exempt_overload=True,
):
	"""Admission commits the settlement claim with its hidden seed and Turn."""
	# SharedAdmission.authorize rechecks the live grant under Shard -> Conv.
	# Taking require_live's Conv lock here would invert that order.
	# The settlement context supplies stable pending-action IDs, never a browser.
	identity = getattr(frappe.local, "jarvis_shared_continuation_ids", None)
	if not identity:
		c._deny()
	run_id = hashlib.sha256(
		json.dumps([binding.name, sorted(identity)], separators=(",", ":")).encode()
	).hexdigest()[:32]
	context = SharedAdmission(binding.actor, binding=binding)

	def seeded(message):
		if claim and not claim():
			raise frappe.DuplicateEntryError("This shared action already has a continuation.")
		if on_seed:
			on_seed(message)

	def dispatch():
		from jarvis.chat.api import _dispatch_turn

		return _dispatch_turn(
			{"conversation_id": binding.conversation, "message_id": context.seed_message, "run_id": run_id}
		)

	with new_admission_context():
		result = txn.replay_on_conflict(
			lambda: admission.accept_or_queue(
				conversation=binding.conversation,
				run_id=run_id,
				seed_message=None,
				seed_content=prompt,
				seed_origin=seed_origin,
				seed_hidden=seed_hidden,
				exempt_overload=exempt_overload,
				on_seed=seeded,
				dispatch=dispatch,
				collaboration=context,
			),
			label="collaboration.continue",
		)
	return {**result, "message_id": context.seed_message}


def on_settled(conversation, items):
	from jarvis.chat import actions_api
	from jarvis.chat.api import continuation_prompt
	from jarvis.chat.pending_actions import claim_settled

	for item in items:
		if not item.get("collaboration_binding"):
			c._deny()
		key = frappe.db.get_value(c.BINDING, item["collaboration_binding"], "session_key")
		binding = c.resolve_identity(key)
		if binding.actor != item["actor"] or binding.conversation != conversation:
			c._deny()
		names = [item["name"]]
		try:
			require_live(binding)
		except frappe.PermissionError:
			# A real completed effect is still disclosed/settled after revocation;
			# its old grant may never dispatch a continuation or borrow the owner.
			claim_settled(names)
			frappe.db.commit()
			continue
		if item["status"] not in ("Executed", "Failed"):
			if claim_settled(names):
				from jarvis.chat.collaboration_notes import append

				append(
					binding,
					f"Your proposed {item['tool']} action is {item['outcome']}. Do not treat it as approved; a new action always needs its own human confirmation.",
					note_id="decision-" + hashlib.sha256(item["name"].encode()).hexdigest(),
				)
			# Persist the settled decision claim and note before processing the next receipt.
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			continue
		plan = actions_api._settled_plan([item])
		previous = getattr(frappe.local, "jarvis_shared_continuation_ids", None)
		frappe.local.jarvis_shared_continuation_ids = names
		try:
			with turn_context(binding, item["run_id"]):
				continuation = enqueue(
					binding,
					continuation_prompt(plan["receipt"], outcome=plan["outcome"], spans=plan["spans"]),
					claim=lambda: bool(claim_settled(names)),
				)
			if not continuation.get("ok"):
				raise frappe.ValidationError("The shared action continuation could not be queued.")
		finally:
			frappe.local.jarvis_shared_continuation_ids = previous
