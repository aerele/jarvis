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
	# This endpoint owns its transaction. A losing INSERT must release ALL
	# locks/snapshots before reading the winner; never upgrade a duplicate lock.
	for attempt in range(3):
		if _read(key):
			frappe.db.rollback()
			return False
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
		except (frappe.DuplicateEntryError, frappe.QueryDeadlockError):
			# 1020/1213 can abort the whole transaction, including savepoints.
			frappe.db.rollback()
			if _read(key):
				return False
			frappe.db.rollback()
			if attempt == 2:
				raise
			continue
		# Never retry an ambiguous commit acknowledgement.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durable claim before dispatch
		return True


def _safe_result(result):
	out = {k: result[k] for k in RESULT_FIELDS if k in result}
	if result.get("reason") == "send_refused":
		out["message"] = "The request was not sent. Review the message and try again."
	if isinstance(result.get("typed_rejection"), dict):
		out["typed_rejection"] = {
			kind: [
				{k: item[k] for k in ("token", "reason_code") if k in item}
				for item in result["typed_rejection"].get(kind, [])
				if isinstance(item, dict)
			]
			for kind in ("discarded", "skipped")
		}
	if result.get("confirmed"):
		out.pop("message", None)
		out.pop("reason", None)
	if result.get("confirmed") and result.get("ok") is False:
		kind = (result.get("error") or {}).get("type")
		messages = {
			"InvalidConfirmation": "That confirmation is no longer valid. Check the action before trying again.",
			"ConfirmationUnavailableError": "The confirmation could not be processed. Check the action before trying again.",
		}
		out["error"] = {
			"type": kind if kind in messages else "ConfirmationFailed",
			"message": messages.get(
				kind,
				"One or more actions could not be completed. Check the action receipts before continuing.",
			),
		}
	return out


def _settle(key, result):
	# A workspace wipe may have erased this claim while dispatch was in flight.
	# Never put conversation references or outcomes back into that tombstone.
	table = frappe.qb.DocType(DOCTYPE)
	(
		frappe.qb.update(table)
		.set(table.state, "settled")
		.set(table.result_json, json.dumps(_safe_result(result)))
		.where((table.name == key) & (table.state == "pending"))
	).run()


def erase_receipts():
	"""Remove workspace metadata but retain opaque keys to fence stale retries."""
	if not frappe.db.table_exists(DOCTYPE):
		return
	table = frappe.qb.DocType(DOCTYPE)
	(
		frappe.qb.update(table)
		.set(table.state, "erased")
		.set(table.fingerprint, "")
		.set(table.conversation, "")
		.set(table.result_json, None)
		.set(table.owner, "Administrator")
		.set(table.modified_by, "Administrator")
	).run()


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
		existing = _read(key)
		if not existing or existing.fingerprint != fingerprint:
			return {"delivery": "unknown", "reason": "request_mismatch"}
		return _public(existing)
	# Only the instrumented pipeline can prove it is still before all effects.
	previous = frappe.flags.get("jarvis_send_receipt_guard")
	guard = {"safe": False}
	frappe.flags.jarvis_send_receipt_guard = guard
	try:
		try:
			result = api.send_message(**args)
		except Exception as exc:
			if not guard["safe"]:
				raise
			frappe.db.rollback()
			result = {
				"ok": False,
				"reason": guard.get("reason") or "send_refused",
				"message": str(exc)
				if isinstance(exc, frappe.ValidationError)
				else "The request was not sent. Please try again.",
			}
		_settle(key, result)
		return {"delivery": "settled", "result": result}
	finally:
		frappe.flags.jarvis_send_receipt_guard = previous
