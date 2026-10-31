"""Bound source bytes for a private delegate file and keep receipts row-free."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

from frappe.utils.response import json_handler

from jarvis.exceptions import InvalidArgumentError

MODE = "private_audit_v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
TOOLS = frozenset({"query", "run_report"})
_active = ContextVar("jarvis_private_audit_output", default=False)


@contextmanager
def request(enabled):
	token = _active.set(enabled)
	try:
		yield
	finally:
		_active.reset(token)


def is_private():
	return _active.get()


def validate_args(tool, args):
	if tool == "query":
		spec = args.get("spec")
		limit = spec.get("limit") if isinstance(spec, dict) else None
		if type(limit) is not int or limit <= 0:
			raise InvalidArgumentError("A private query needs an explicit positive integer limit.")


def allowed(mode, tool, cap):
	return (
		mode == MODE
		and tool in TOOLS
		and isinstance(cap, dict)
		and bool(cap.get("run"))
		and cap.get("status") == "running"
		and not cap.get("legacy")
	)


def prepare(result, *, tool):
	"""Return the wire envelope and the safe receipt, or one bounded error."""
	if not result.get("ok"):
		return result, result
	data = result["data"]
	rows = data.get("rows", data.get("result")) if isinstance(data, dict) else None
	if not isinstance(rows, list):
		raise InvalidArgumentError("The private source has no row array; coverage is incomplete.")
	if len(rows) > MAX_ROWS:
		raise InvalidArgumentError("The private source exceeds its row limit; coverage is incomplete.")
	if tool == "run_report" and (
		data.get("prepared_report") or data.get("status") not in (None, "", "ready")
	):
		raise InvalidArgumentError("The private report is not a complete inline result.")
	if data.get("_truncated") or data.get("partial") or (tool == "run_report" and data.get("more_rows")):
		raise InvalidArgumentError("The private source is truncated; coverage is incomplete.")
	try:
		body = json.dumps(
			data, default=json_handler, ensure_ascii=False, allow_nan=False, separators=(",", ":")
		).encode()
	except (TypeError, ValueError, OverflowError) as exc:
		raise InvalidArgumentError(
			"The private source cannot be serialized; coverage is incomplete."
		) from exc
	if len(body) > MAX_BYTES:
		raise InvalidArgumentError(
			"The private source exceeds its row or byte limit; coverage is incomplete."
		)
	summary = {
		"private_output": True,
		"count": len(rows),
		"bytes": len(body),
		"sha256": hashlib.sha256(body).hexdigest(),
	}
	return {**result, "result_mode": MODE}, {"ok": True, "data": summary}
