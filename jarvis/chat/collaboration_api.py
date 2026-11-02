"""Explicit shared APIs. Browser input never chooses an actor or agent session."""

import hashlib
import hmac
import json
import uuid

import frappe
from frappe.utils.password import get_encryption_key

from jarvis.chat import admission, policy, txn
from jarvis.chat.collaboration_admission import SharedAdmission
from jarvis.chat.collaboration_context import request_fingerprint, validate_request
from jarvis.chat.collaboration_permissions import actor_permissions
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_access


def _attachments(value):
	if value is None:
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (ValueError, RecursionError):
			frappe.throw("Attachments must be a list.")
	if not isinstance(value, list) or len(value) > 10:
		frappe.throw("Choose at most ten attachments.")
	result = []
	for item in value:
		url = item.get("file_url") if isinstance(item, dict) else None
		if not isinstance(url, str) or len(url) > 2048 or not url.startswith(("/private/files/", "/files/")):
			frappe.throw("Choose an uploaded attachment.")
		result.append({"file_url": url})
	return result


def _request_id(actor: str, conversation: str, nonce: str):
	try:
		nonce = uuid.UUID(nonce).hex
	except (ValueError, TypeError, AttributeError):
		frappe.throw("Invalid request identifier.")
	message = json.dumps([frappe.local.site, actor, conversation, nonce], separators=(",", ":"))
	return hmac.new(get_encryption_key().encode(), message.encode(), hashlib.sha256).hexdigest()[:32]


@frappe.whitelist(methods=["POST"])
def stop_run(conversation: str, run_id: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_control import stop

	return stop(conversation, run_id)


@frappe.whitelist(methods=["POST"])
def retry_run(conversation: str, run_id: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_retry import retry

	return retry(conversation, run_id)


@frappe.whitelist(methods=["POST"])
def answer_question(conversation: str, question: str, decision: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_asks import answer

	return answer(conversation, question, decision)


@frappe.whitelist(methods=["POST"])
def send_message(
	conversation: str, message: str, request_id: str, attachments: str | list | None = None
) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	actor = frappe.session.user
	atts = _attachments(attachments)
	message = validate_request(message, allow_empty=bool(atts)).strip()
	run_id = _request_id(actor, conversation, request_id)
	ok, reason = policy.validate_can_send(actor)
	if not ok:
		return {"ok": False, "reason": reason}
	context = SharedAdmission(actor, request_hash=request_fingerprint(message, atts))

	def validate_files():
		from jarvis.chat.api import _att_canvas_item

		with actor_permissions(actor):
			validated = []
			for item in atts:
				file = frappe.get_doc("File", {"file_url": item["file_url"]})
				file.check_permission("read")
				validated.append({"file_url": file.file_url, "file_name": file.file_name})
			context.seed_fields["canvas"] = (
				frappe.as_json([_att_canvas_item(item) for item in validated]) if validated else None
			)

	context.validate_inputs = validate_files
	payload = {"attachments": atts} if atts else None

	def on_seed(_message):
		from jarvis.chat.collaboration_asks import resolve_on_user_message

		resolve_on_user_message(conversation, actor)

	def dispatch():
		from jarvis.chat.api import _dispatch_turn

		return _dispatch_turn(
			{
				"conversation_id": conversation,
				"message_id": context.seed_message,
				"run_id": run_id,
				**({"attachments": atts} if atts else {}),
			}
		)

	result = txn.replay_on_conflict(
		lambda: admission.accept_or_queue(
			conversation=conversation,
			run_id=run_id,
			seed_message=None,
			seed_content=message,
			seed_origin="human",
			dispatch=dispatch,
			dispatch_payload=payload,
			collaboration=context,
			on_seed=on_seed,
		),
		label="collaboration.send",
	)
	return {**result, "conversation_id": conversation, "message_id": context.seed_message}


@frappe.whitelist(methods=["POST"])
def get_conversation(
	conversation: str, limit: int = 80, before: int | None = None, watch_run: str | None = None
) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_read import read_conversation

	if watch_run is not None:
		try:
			watch_run = uuid.UUID(watch_run).hex
		except (ValueError, TypeError, AttributeError):
			frappe.throw("Invalid request identifier.")
	return txn.replay_on_conflict(
		lambda: read_conversation(conversation, limit=limit, before=before, watch_run=watch_run),
		label="collaboration.read",
		fresh=True,
	)


@frappe.whitelist(methods=["GET"])
def get_file(conversation: str, file_url: str) -> None:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_files import get_file as download

	txn.replay_on_conflict(lambda: download(conversation, file_url), label="collaboration.file", fresh=True)


@frappe.whitelist(methods=["POST"])
def get_canvas(message: str, name: str, dark: int = 0) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from frappe.utils import cint

	from jarvis.chat.collaboration_files import get_canvas as read

	return txn.replay_on_conflict(
		lambda: read(message, name, dark=bool(cint(dark))), label="collaboration.canvas", fresh=True
	)


@frappe.whitelist(methods=["POST"])
def get_tool_detail(message: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_read import read_tool_detail

	return txn.replay_on_conflict(
		lambda: read_tool_detail(message), label="collaboration.tool_detail", fresh=True
	)


@frappe.whitelist(methods=["POST"])
def upload_file(conversation: str) -> dict:
	refuse_in_tool_dispatch()
	require_jarvis_access()
	from jarvis.chat.collaboration_files import upload_file as upload

	return upload(conversation)
