"""Actor-bound questions; answering never grants an ERP confirmation bypass."""

import hashlib
import json

import frappe

from jarvis._session import impersonate
from jarvis.chat import collaboration as c
from jarvis.chat import txn
from jarvis.chat.collaboration_bindings import require_live, require_settlement
from jarvis.chat.collaboration_context import validate_request
from jarvis.chat.collaboration_turn import binding_for_run
from jarvis.chat.scrub import scrub_for_share

APPROVAL = "Jarvis Approval Request"
IDENTITY = (
	"actor",
	"collaboration_binding",
	"collaboration_run_id",
	"collaboration_grant_revision",
	"collaboration_epoch",
	"collaboration_question_key",
)


def _key(binding, run_id):
	return hashlib.sha256((binding.name + "\0" + run_id).encode()).hexdigest()


def guard_record(doc):
	c.guard_server_fields(doc, IDENTITY)
	if not (doc.get("collaboration_binding") or c.is_collaborative(doc.get("conversation"))):
		return
	if not doc.flags.jarvis_collaboration_write or not doc.is_new():
		c._deny()
	binding = binding_for_run(doc.get("collaboration_run_id"))
	if not binding or (
		doc.actor,
		doc.conversation,
		doc.collaboration_binding,
		doc.collaboration_question_key,
		doc.collaboration_grant_revision,
		doc.collaboration_epoch,
		doc.source,
		doc.status,
	) != (
		binding.actor,
		binding.conversation,
		binding.name,
		_key(binding, doc.collaboration_run_id),
		binding.grant_revision,
		binding.conversation_epoch,
		"Chat",
		"Pending",
	):
		c._deny()
	require_live(binding)
	require_settlement(binding, doc.collaboration_run_id, "assistant")


def materialize(run_id: str):
	from jarvis.chat.chat_asks import _BOARD_MAX_CHIPS, ask_label, parse_ask

	binding = binding_for_run(run_id)
	if not binding:
		c._deny()

	def unit():
		require_live(binding)
		require_settlement(binding, run_id, "assistant")
		turn = frappe.db.get_value(
			"Jarvis Chat Turn", run_id, ["state", "assistant_message"], as_dict=True, for_update=True
		)
		if not turn or turn.state not in {"finalizing", "done"} or not turn.assistant_message:
			return None
		from jarvis.chat.collaboration_output import lock_reply

		lock_reply(binding, run_id, turn.assistant_message)
		content = frappe.db.get_value("Jarvis Chat Message", turn.assistant_message, "content") or ""
		questions = parse_ask(scrub_for_share(content))
		if not questions:
			return None
		question = "\n".join(ask_label(item) for item in questions)
		if len(question.encode()) > 8 * 1024:
			# The complete question remains visible in the shared reply. A
			# bounded control must not silently alter what the human answers.
			return None
		key = _key(binding, run_id)
		existing = frappe.db.get_value(APPROVAL, {"collaboration_question_key": key}, "name", for_update=True)
		if existing:
			frappe.db.commit()
			return existing
		options = questions[0]["options"] if len(questions) == 1 else []
		if len(options) > _BOARD_MAX_CHIPS:
			options = []
		doc = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": ask_label(questions[0])[:100],
				"status": "Pending",
				"source": "Chat",
				"conversation": binding.conversation,
				"question": question,
				"options": json.dumps(options) if options else "",
				"actor": binding.actor,
				"collaboration_binding": binding.name,
				"collaboration_run_id": run_id,
				"collaboration_grant_revision": binding.grant_revision,
				"collaboration_epoch": binding.conversation_epoch,
				"collaboration_question_key": key,
			}
		)
		doc.flags.jarvis_collaboration_write = True
		with impersonate(binding.actor):
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	try:
		return txn.replay_on_conflict(unit, label="collaboration.question", fresh=True)
	except frappe.PermissionError:
		# Actual text may settle after revocation; no fresh usable question is
		# created for an identity whose grant no longer permits a continuation.
		frappe.db.rollback()
		return None


def _binding(row, conversation, actor):
	if not row or (row.conversation, row.actor, row.owner, row.source) != (
		conversation,
		actor,
		actor,
		"Chat",
	):
		c._deny()
	binding = binding_for_run(row.collaboration_run_id)
	if not binding or (
		row.collaboration_binding,
		row.collaboration_grant_revision,
		row.collaboration_epoch,
		row.collaboration_question_key,
	) != (
		binding.name,
		binding.grant_revision,
		binding.conversation_epoch,
		_key(binding, row.collaboration_run_id),
	):
		c._deny()
	return binding


def answer(conversation: str, name: str, decision: str):
	actor = frappe.session.user
	if not isinstance(name, str) or not name or len(name) > 140:
		c._deny()
	decision = scrub_for_share(validate_request(decision).strip())
	row = frappe.db.get_value(
		APPROVAL,
		name,
		["name", "owner", "conversation", "source", "status", "decision", "question", *IDENTITY],
		as_dict=True,
	)
	binding = _binding(row, conversation, actor)
	require_live(binding)
	if row.status != "Pending":
		if row.status == "Answered" and row.decision == decision:
			return {"ok": True, "duplicate": True, "status": "Answered"}
		frappe.throw("This question has already been answered. Reload the shared chat.")
	# Admission must acquire Shard before Conversation. Its adapter rechecks
	# this grant under those locks; do not carry this read-only preflight into it.
	frappe.db.commit()
	from jarvis.chat import policy

	ok, reason = policy.validate_can_send(actor)
	if not ok:
		return {"ok": False, "reason": reason}
	prompt = validate_request(
		f"Your answer to the question:\n{row.question}\n\n{decision}\n\n"
		"Continue using your own permissions. This answer is not an ERP action confirmation; "
		"every proposed write still needs its own explicit human confirmation."
	)

	def claim():
		# Admission holds the canonical shard/participant/binding locks. The
		# question decision and visible user seed commit with the child Turn.
		current = frappe.db.get_value(
			APPROVAL,
			name,
			["name", "owner", "conversation", "source", "status", *IDENTITY],
			as_dict=True,
			for_update=True,
		)
		if _binding(current, conversation, actor) != binding:
			c._deny()
		if current.status != "Pending":
			frappe.throw("This question has already been answered. Reload the shared chat.")
		from jarvis.chat import turn_state as ts

		return bool(
			ts._run_cas(
				f"""UPDATE `tab{APPROVAL}` SET status='Answered', decision=%(d)s,
			decided_by=%(a)s, decided_at=%(now)s WHERE name=%(n)s AND status='Pending'""",
				{"d": decision, "a": actor, "now": frappe.utils.now_datetime(), "n": name},
			)
		)

	from jarvis.chat import collaboration_continuations

	previous = getattr(frappe.local, "jarvis_shared_continuation_ids", None)
	frappe.local.jarvis_shared_continuation_ids = ["question:" + name]
	try:
		result = collaboration_continuations.enqueue(
			binding,
			prompt,
			claim=claim,
			seed_hidden=False,
			seed_origin="human",
			exempt_overload=False,
		)
		if result.get("ok") and result.get("duplicate"):
			# A competing click may have committed after the preflight and before
			# admission's stable-child duplicate check (which skips on_seed).
			current = frappe.db.get_value(APPROVAL, name, ["status", "decision"], as_dict=True)
			if not current or current.status != "Answered" or current.decision != decision:
				frappe.throw("This question has already been answered. Reload the shared chat.")
		return result
	finally:
		frappe.local.jarvis_shared_continuation_ids = previous


def resolve_on_user_message(conversation: str, actor: str):
	"""Server-only admission hook. Another actor's ordinary message answers none."""
	frappe.db.get_value(c.CONVERSATION, conversation, "name", for_update=True)
	frappe.db.sql(
		f"""UPDATE `tab{APPROVAL}` SET status='Answered', decision='(answered in chat)',
		decided_by=%(actor)s, decided_at=%(now)s
		WHERE conversation=%(conversation)s AND actor=%(actor)s AND status='Pending'
		AND source='Chat' AND COALESCE(collaboration_binding,'')<>''""",
		{"actor": actor, "conversation": conversation, "now": frappe.utils.now_datetime()},
	)
