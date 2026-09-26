"""Fernet seal for ``Jarvis Pending Action`` payloads (§4.2).

The key is HKDF-SHA256 over the site's ``encryption_key``, so a sealed call is
unreadable off-site and a key rotation or restore reads as ``unverifiable``. The
sealed call binds the row's identity columns and a hash of what the human saw
(``card_sha256``), so a transplanted envelope or an edited card never dispatches."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from frappe.utils.password import get_encryption_key

VERSION = 1
_INFO = b"jarvis-pending-action-v1"
_OPEN_KEY_INFO = b"jarvis-pending-action-open-key-v1"
# Row columns the sealed call must match. ``conversation`` is left out on purpose:
# a held row's primary conversation moves when a waiter is promoted.
BOUND_FIELDS = (
	"name",
	"kind",
	"tool",
	"exec_user",
	"owner_user",
	"origin_conversation",
	"skill_docname",
	"run_id",
)


class SealError(Exception):
	"""An envelope that must not execute. ``reason_code`` is ``tampered`` or
	``unverifiable``; ``binding`` names the failed check (never a value)."""

	def __init__(self, reason_code: str, binding: str = ""):
		super().__init__(f"{reason_code}:{binding}")
		self.reason_code = reason_code
		self.binding = binding


def canonical(obj) -> str:
	return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def json_text(value) -> str | None:
	"""The canonical text a JSON column stores (``None`` for empty)."""
	if value in (None, ""):
		return None
	if isinstance(value, str):
		value = json.loads(value)
	return canonical(value)


def _parsed(value):
	text = json_text(value)
	return json.loads(text) if text else None


def card_sha256(row) -> str:
	"""SHA-256 over the canonical ``{tool, card, preview, summary}`` of ``row``."""
	payload = {
		"tool": row.get("tool") or "",
		"card": _parsed(row.get("card")),
		"preview": _parsed(row.get("preview")),
		"summary": row.get("summary") or "",
	}
	return hashlib.sha256(canonical(payload).encode()).hexdigest()


def _derive(info: bytes) -> bytes:
	return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
		get_encryption_key().encode()
	)


def _fernet() -> Fernet:
	return Fernet(base64.urlsafe_b64encode(_derive(_INFO)))


def _json_default(obj):
	from frappe.utils.response import json_handler

	try:
		return json_handler(obj)
	except TypeError:
		return str(obj)


def _encrypt(payload: dict) -> str:
	text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
	return _fernet().encrypt(text.encode()).decode()


def _open(token: str | None) -> dict:
	if not token:
		raise SealError("unverifiable", "missing")
	try:
		plain = _fernet().decrypt(token.encode())
	except InvalidToken:
		raise SealError("unverifiable", "key") from None
	try:
		payload = json.loads(plain)
	except ValueError:
		raise SealError("tampered", "payload") from None
	if not isinstance(payload, dict) or payload.get("v") != VERSION:
		raise SealError("tampered", "version")
	return payload


def seal_call(row, args: dict, targets: list | None) -> str:
	"""Seal the exact call. ``row`` must already carry its name and stored card."""
	payload = {f: row.get(f) or "" for f in BOUND_FIELDS}
	payload.update(v=VERSION, args=args, targets=targets or [], card_sha256=card_sha256(row))
	return _encrypt(payload)


def unseal_call(row) -> dict:
	"""The sealed call, verified against ``row``; raises :class:`SealError`."""
	payload = _open(row.get("sealed_call"))
	for field in BOUND_FIELDS:
		if (payload.get(field) or "") != (row.get(field) or ""):
			raise SealError("tampered", field)
	if payload.get("card_sha256") != card_sha256(row):
		raise SealError("tampered", "card")
	return payload


def seal_settlement(name: str, settlement: dict) -> str:
	return _encrypt({**settlement, "v": VERSION, "name": name})


def unseal_settlement(row) -> dict | None:
	if not row.get("sealed_settlement"):
		return None
	payload = _open(row.get("sealed_settlement"))
	if payload.get("name") != row.get("name"):
		raise SealError("tampered", "name")
	return payload


def open_key(owner: str, dedup_key: str) -> str:
	"""Keyed dedup fingerprint: equal for the same owner + key, opaque otherwise."""
	return hmac.new(_derive(_OPEN_KEY_INFO), f"{owner}\x00{dedup_key}".encode(), hashlib.sha256).hexdigest()
