"""Durable at-most-one dispatch receipts for first-party interactive chat.

The claim commits BEFORE entering the legacy send pipeline (which may itself
commit or execute a typed confirmation). Pending claims are never reclaimed or
expired: a crash after dispatch cannot prove that no effect happened. Do not
purge these rows without an equivalent durable tombstone.
"""

from __future__ import annotations

import hashlib
import json
import re

import frappe

from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_access

DOCTYPE = "Jarvis Chat Send Request"
RESULT_FIELDS = (
	"ok",
	"confirmed",
	"conversation_id",
	"message_id",
	"run_id",
	"reason",
	"message",
	"queued",
	"queued_position",
	"limit_period",
	"older_cards_waiting",
	"typed_rejection",
	"auto_mode",
)


def _key(request_id):
	refuse_in_tool_dispatch()
	if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
		frappe.throw("Invalid send request identifier")
	return hashlib.sha256(f"{frappe.session.user}:{request_id}".encode()).hexdigest()


def _read(key, *, for_update=False):
	return frappe.db.get_value(
		DOCTYPE,
		key,
		["fingerprint", "conversation", "state", "result_json"],
		as_dict=True,
		for_update=for_update,
	)


def _public(receipt):
	if not receipt or receipt.state != "settled":
		return {"delivery": "unknown"}
	result = json.loads(receipt.result_json)
	if conversation := result.get("conversation_id") or receipt.conversation:
		from jarvis.chat.api import _get_owned_conversation

		try:
			_get_owned_conversation(conversation)
		except frappe.DoesNotExistError:
			return {"delivery": "unknown"}
	return {"delivery": "settled", "result": result}


def _claim(key, fingerprint, conversation):
	# Do not gap-lock a missing key: concurrent new IDs must not deadlock.
	# The INSERT primary key is the claim arbiter; duplicate reads below lock
	# only an existing row to see the winner under repeatable-read.
	if _read(key):
		return False
	frappe.db.savepoint("chat_send_claim")
	try:
		frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"request_key": key,
				"fingerprint": fingerprint,
				"conversation": conversation,
				"state": "pending",
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Required on Postgres, where a duplicate otherwise aborts the transaction.
		frappe.db.rollback(save_point="chat_send_claim")
		return False
	# This dedicated endpoint has made no other writes. The durable claim must
	# survive an exception/rollback AFTER remote effects or internal send commits.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durable dispatch fence before side effects
	return True


@frappe.whitelist(methods=["POST"])
def check_delivery(request_id: str):
	require_jarvis_access()
	key = _key(request_id)
	try:
		receipt = _read(key)
	except Exception:
		return {"delivery": "unknown"}
	return _public(receipt)


@frappe.whitelist(methods=["POST"])
def send_message(
	request_id: str,
	conversation: str = "",
	message: str = "",
	model_override: str | None = None,
	attachments: str | None = None,
	context: str | None = None,
	thinking_override: str | None = None,
	approval_tokens: str | list | None = None,
	voice: bool = False,
	auto_mode: int = 0,
):
	from jarvis.chat import api

	require_jarvis_access()
	key = _key(request_id)
	args = dict(
		conversation=conversation,
		message=message,
		model_override=model_override,
		attachments=attachments,
		context=context,
		thinking_override=thinking_override,
		approval_tokens=approval_tokens,
		voice=voice,
		auto_mode=auto_mode,
	)
	fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
	if not _claim(key, fingerprint, conversation):
		existing = _read(key, for_update=True)
		if not existing or existing.fingerprint != fingerprint:
			return {"delivery": "unknown", "reason": "request_mismatch"}
		return _public(existing)
	# Never retry on an exception: pending is durable even if this transaction rolls back.
	result = api.send_message(**args)
	frappe.db.set_value(
		DOCTYPE,
		key,
		{
			"state": "settled",
			"result_json": json.dumps({k: result[k] for k in RESULT_FIELDS if k in result}),
		},
	)
	# Frappe commits this result with the request before returning the response.
	return {"delivery": "settled", "result": result}
