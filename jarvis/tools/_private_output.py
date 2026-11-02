"""Seal private delegate source bytes and retain only metadata receipts."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

from frappe.utils.response import json_handler

from jarvis.exceptions import InvalidArgumentError

MODE = "private_audit_v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
PRIVATE_SOURCE_TOOLS = frozenset({"get_payroll_deposit_inputs"})
TOOLS = PRIVATE_SOURCE_TOOLS
_active = ContextVar("jarvis_private_audit_output", default=False)


@contextmanager
def request(enabled: bool):
	token = _active.set(enabled)
	try:
		yield
	finally:
		_active.reset(token)


def is_private() -> bool:
	return _active.get()


def require_source_transport(tool: str) -> None:
	if tool in PRIVATE_SOURCE_TOOLS and not is_private():
		raise InvalidArgumentError("Private payroll evidence requires private audit output.")


def source_envelope(snapshot: dict) -> dict:
	if not isinstance(snapshot, dict):
		raise InvalidArgumentError("The private payroll source has an invalid snapshot.")
	return snapshot


def validate_args(tool: str, args: dict) -> None:
	if tool in PRIVATE_SOURCE_TOOLS and args != {}:
		raise InvalidArgumentError("Private payroll evidence takes no model-selected arguments.")


def allowed(mode: str | None, tool: str, cap) -> bool:
	return (
		mode == MODE
		and tool in PRIVATE_SOURCE_TOOLS
		and isinstance(cap, dict)
		and bool(cap.get("run"))
		and cap.get("status") == "running"
		and cap.get("agent") == "statutory-payroll-deposit-status"
		and not cap.get("legacy")
	)


def prepare(result: dict, *, tool: str) -> tuple[dict, dict]:
	"""Return private wire bytes and a raw-row-free receipt for persistence."""
	if not isinstance(result, dict) or not result.get("ok"):
		return result, result
	data = result.get("data")
	if tool not in PRIVATE_SOURCE_TOOLS or not isinstance(data, dict):
		raise InvalidArgumentError("The private payroll source has an invalid snapshot envelope.")
	from jarvis.chat import payroll_deposit_snapshot

	payload = payroll_deposit_snapshot.validate_snapshot(data)
	try:
		body = json.dumps(
			payload,
			default=json_handler,
			sort_keys=True,
			ensure_ascii=False,
			allow_nan=False,
			separators=(",", ":"),
		).encode("utf-8")
	except (TypeError, ValueError, OverflowError) as exc:
		raise InvalidArgumentError("The private payroll source cannot be serialized safely.") from exc
	if len(body) > MAX_BYTES:
		raise InvalidArgumentError("The private payroll source exceeds its byte cap.")
	summary = {
		"private_output": True,
		"count": 1,
		"bytes": len(body),
		"sha256": hashlib.sha256(body).hexdigest(),
	}
	return (
		{"ok": True, "data": body.decode("utf-8"), "result_mode": MODE, "private_output": summary},
		{"ok": True, "data": summary},
	)
