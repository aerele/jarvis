"""Conservative delivery receipts for PWA recovery, not a durable send ledger.

A missing/expired receipt is ALWAYS unknown. No endpoint here retries an unknown
send. Redis claims suppress concurrent reuse only during RETENTION_SECONDS.
"""

from __future__ import annotations

import hashlib
import json
import re

import frappe

from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_access

RETENTION_SECONDS = 7 * 24 * 60 * 60


def _key(request_id):
	refuse_in_tool_dispatch()
	if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
		frappe.throw("Invalid send request identifier")
	owner_key = hashlib.sha256(f"{frappe.session.user}:{request_id}".encode()).hexdigest()
	cache = frappe.cache()
	return cache, cache.make_key(f"jarvis:pwa-send:{owner_key}")


def _read(cache, key):
	value = cache.get(key)
	return json.loads(value) if value else None


def _public(receipt):
	if not receipt or receipt.get("state") != "settled":
		return {"delivery": "unknown"}
	# Recheck ownership on every lookup; a cached result never confers access.
	result = receipt["result"]
	if conversation := result.get("conversation_id") or receipt.get("conversation"):
		from jarvis.chat.api import _get_owned_conversation

		try:
			_get_owned_conversation(conversation)
		except frappe.DoesNotExistError:
			return {"delivery": "unknown"}
	return {"delivery": "settled", "result": result}


@frappe.whitelist(methods=["POST"])
def check_delivery(request_id: str):
	require_jarvis_access()
	cache, key = _key(request_id)
	try:
		receipt = _read(cache, key)
	except Exception:
		return {"delivery": "unknown"}
	return _public(receipt)


@frappe.whitelist(methods=["POST"])
def send_message(
	request_id: str,
	conversation: str = "",
	message: str = "",
	attachments: str | None = None,
	approval_tokens: str | None = None,
):
	from jarvis.chat import api

	require_jarvis_access()
	cache, key = _key(request_id)
	payload = json.dumps([conversation, message, attachments, approval_tokens], separators=(",", ":"))
	fingerprint = hashlib.sha256(payload.encode()).hexdigest()
	receipt = {"state": "pending", "fingerprint": fingerprint, "conversation": conversation}
	try:
		claimed = cache.set(key, json.dumps(receipt), ex=RETENTION_SECONDS, nx=True)
	except Exception:
		# A lost Redis acknowledgement might itself have claimed the key. Nothing
		# was sent by THIS call, but another same-ID call may already be running.
		return {"delivery": "unknown"}
	if not claimed:
		try:
			existing = _read(cache, key)
		except Exception:
			return {"delivery": "unknown"}
		if not existing or existing.get("fingerprint") != fingerprint:
			return {"delivery": "unknown"}
		return _public(existing)

	# Exceptions can occur after a commit or a typed approval. Leave pending,
	# propagate the error, and never convert it into a retryable rejection.
	result = api.send_message(
		conversation=conversation,
		message=message,
		attachments=attachments,
		approval_tokens=approval_tokens,
	)
	# No request content, file references, tool results or business records in Redis.
	fields = (
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
	)
	receipt["state"] = "settled"
	receipt["result"] = {k: result[k] for k in fields if k in result}

	def publish_receipt():
		try:
			cache.set(key, json.dumps(receipt), ex=RETENTION_SECONDS)
		except Exception:
			# Committed work must not fail because its optional receipt was lost.
			# A later lookup stays unknown; it never authorizes replay.
			pass

	# Let Frappe own the request transaction. A rollback/failed commit never
	# publishes success, and the response is sent only after request commit.
	frappe.db.after_commit.add(publish_receipt)
	return {"delivery": "settled", "result": result}
