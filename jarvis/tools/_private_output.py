"""Bound source bytes for a private delegate file and keep receipts row-free."""

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe.utils.response import json_handler

from jarvis.exceptions import InvalidArgumentError

MODE = "private_audit_v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
# These tools have an explicit plugin-owned local-input contract. Adding a read
# here is not enough by itself: its tool implementation must reject the ordinary
# transport and its bundle must verify the returned receipt before it can assess.
REVIEW_SOURCE_TOOLS = frozenset(
	{
		"get_cycle_count_inputs",
		"get_purchase_match_inputs",
		"get_receivables_review_inputs",
		"get_ticket_triage_inputs",
	}
)
DEPOSIT_SOURCE_TOOL = "get_payroll_deposit_inputs"
PRIVATE_SOURCE_TOOLS = REVIEW_SOURCE_TOOLS | {DEPOSIT_SOURCE_TOOL}
TOOLS = frozenset({"query", "run_report"}) | PRIVATE_SOURCE_TOOLS
_active = ContextVar("jarvis_private_audit_output", default=False)
_redact_delegate_args = ContextVar("jarvis_private_delegate_args", default=False)

# This label is deliberately not a registered tool.  It is an observability
# marker, not an execution name: a Deposit delegate's caller-controlled tool
# name must not cross any durable or realtime boundary.
PRIVATE_DELEGATE_TOOL_LABEL = "private_delegate_tool"
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@contextmanager
def request(enabled):
	token = _active.set(enabled)
	try:
		yield
	finally:
		_active.reset(token)


def is_private():
	return _active.get()


def is_active_deposit_delegate(cap) -> bool:
	"""True only for the running, non-legacy Deposit delegate contract."""
	return is_deposit_delegate(cap) and cap.get("status") == "running" and not cap.get("legacy")


def is_deposit_delegate(cap) -> bool:
	"""True for any Deposit Run capability, including a terminal one.

	A run can move to a terminal state just before its session bearer is deleted.
	That short interval is still a Deposit boundary: caller-controlled tool data
	must remain redacted even though the run is no longer allowed to dispatch.
	"""
	return (
		isinstance(cap, dict)
		and bool(cap.get("run"))
		and cap.get("agent") == "statutory-payroll-deposit-status"
	)


def is_terminal_deposit_delegate(cap) -> bool:
	"""True when a Deposit capability exists but its Run may not dispatch."""
	return is_deposit_delegate(cap) and cap.get("status") != "running"


def is_legacy_deposit_delegate(cap) -> bool:
	"""True for an old Deposit bearer with no safe private-output contract.

	Generic legacy delegates retain their historical migration behavior.  Deposit
	does not: its old contract can reach new file-query tools which were never
	part of the sealed private-source protocol.
	"""
	return is_deposit_delegate(cap) and bool(cap.get("legacy"))


def has_private_source_capability(cap) -> bool:
	"""Require the exact snapshotted bench tool declaration before source capture."""
	if not is_active_deposit_delegate(cap) or not isinstance(cap.get("tools_allow"), list):
		return False
	from jarvis.tools import _delegate_capability

	return "get_payroll_deposit_inputs" in _delegate_capability.bench_tools(cap["tools_allow"])


def begin_delegate_redaction(cap):
	"""Enable fixed projections for one Deposit dispatch or terminal denial."""
	return _redact_delegate_args.set(is_deposit_delegate(cap))


def end_delegate_redaction(token) -> None:
	_redact_delegate_args.reset(token)


def mark_delegate_metadata(cap) -> None:
	"""Keep the request-level marker through call_tool's outer error capture."""
	if not is_deposit_delegate(cap):
		return
	try:
		frappe.local.jarvis_private_delegate_metadata = True
	except Exception:
		pass


def redact_delegate_args(args):
	"""Return a durable-safe argument projection for the active delegate.

	Audit lines, transcript receipts, and step labels may execute on denied or
	failed calls.  Their argument projection therefore cannot retain a caller
	value for Deposit runs, even when the tool itself never receives the value.
	"""
	if metadata_redacted():
		return {"private_delegate_args_redacted": True}
	return args


def metadata_redacted() -> bool:
	"""True when Deposit metadata must be replaced at every egress boundary."""
	if _redact_delegate_args.get():
		return True
	try:
		return bool(getattr(frappe.local, "jarvis_private_delegate_metadata", False))
	except Exception:
		return False


def redact_delegate_tool(tool):
	"""Return the fixed Deposit label, or the normal execution name."""
	return PRIVATE_DELEGATE_TOOL_LABEL if metadata_redacted() else tool


def safe_tool_call_id(value) -> str | None:
	"""Keep no caller id for Deposit; preserve generic receipt behavior."""
	if metadata_redacted():
		return None
	value = str(value or "").strip()
	return value or None


def is_opaque_tool_call_id(value) -> bool:
	"""Validate the bounded client id before an active Deposit call proceeds."""
	value = str(value or "").strip()
	return not value or bool(_CALL_ID.fullmatch(value))


def redact_delegate_result(result):
	"""Remove caller-controlled failure text from an active Deposit envelope."""
	if not metadata_redacted() or not isinstance(result, dict) or result.get("ok") is not False:
		return result
	error = result.get("error") if isinstance(result.get("error"), dict) else {}
	return {
		**result,
		"error": {
			"code": str(error.get("code") or "InvalidArgumentError"),
			"message": "Private payroll delegate call failed.",
		},
	}


def require_source_transport(tool):
	"""Refuse a review snapshot unless the private delegate boundary is active."""
	if tool in PRIVATE_SOURCE_TOOLS and not is_private():
		raise InvalidArgumentError("Private review evidence requires private audit output.")


def source_envelope(snapshot, *, tool: str | None = None):
	"""Put one server-captured review snapshot on the sealed source contract."""
	if not isinstance(snapshot, dict):
		raise InvalidArgumentError("The private review source has an invalid snapshot.")
	if tool == DEPOSIT_SOURCE_TOOL:
		return snapshot
	return {"rows": [snapshot]}


def validate_args(tool, args):
	if tool in PRIVATE_SOURCE_TOOLS and args != {}:
		raise InvalidArgumentError("Private review evidence takes no model-selected arguments.")
	if tool == "query":
		spec = args.get("spec")
		limit = spec.get("limit") if isinstance(spec, dict) else None
		if type(limit) is not int or limit <= 0:
			raise InvalidArgumentError("A private query needs an explicit positive integer limit.")


def allowed(mode, tool, cap):
	if is_deposit_delegate(cap) or tool == DEPOSIT_SOURCE_TOOL:
		return mode == MODE and tool == DEPOSIT_SOURCE_TOOL and has_private_source_capability(cap)
	return (
		mode == MODE
		and tool in TOOLS
		and isinstance(cap, dict)
		and bool(cap.get("run"))
		and cap.get("status") == "running"
		and not cap.get("legacy")
	)


def canonical_bytes(data) -> bytes:
	"""Return the exact canonical private-envelope bytes for ``data``.

	Callers that capture a private source use this before persistence. This keeps
	the source-size guard identical to the later transport guard in ``prepare``.
	"""
	try:
		return json.dumps(
			data,
			default=json_handler,
			sort_keys=True,
			ensure_ascii=False,
			allow_nan=False,
			separators=(",", ":"),
		).encode()
	except (TypeError, ValueError, OverflowError) as exc:
		raise InvalidArgumentError(
			"The private source cannot be serialized; coverage is incomplete."
		) from exc


def prepare(result, *, tool):
	"""Return the wire envelope and the safe receipt, or one bounded error."""
	if tool == DEPOSIT_SOURCE_TOOL:
		return _prepare_deposit(result, tool=tool)
	if not result.get("ok"):
		return result, result
	data = result["data"]
	rows = data.get("rows", data.get("result")) if isinstance(data, dict) else None
	if not isinstance(rows, list):
		raise InvalidArgumentError("The private source has no row array; coverage is incomplete.")
	if len(rows) > MAX_ROWS:
		raise InvalidArgumentError("The private source exceeds its row limit; coverage is incomplete.")
	if tool in REVIEW_SOURCE_TOOLS:
		# Each review tool carries exactly one server-captured snapshot in a row envelope.
		# Do not accept an arbitrary result-shaped object: the plugin and bundle use
		# this stable boundary to reject a normal transcript result or a stale spill.
		if set(data) != {"rows"} or len(rows) != 1 or not isinstance(rows[0], dict):
			raise InvalidArgumentError("The private review source has an invalid snapshot envelope.")
		snapshot = rows[0]
		digest = snapshot.get("source_digest")
		if (
			not isinstance(digest, str)
			or len(digest) != 64
			or any(char not in "0123456789abcdef" for char in digest)
		):
			raise InvalidArgumentError("The private review source has no valid evidence digest.")
	if tool == "run_report" and (
		data.get("prepared_report") or data.get("status") not in (None, "", "ready")
	):
		raise InvalidArgumentError("The private report is not a complete inline result.")
	if data.get("_truncated") or data.get("partial") or (tool == "run_report" and data.get("more_rows")):
		raise InvalidArgumentError("The private source is truncated; coverage is incomplete.")
	body = canonical_bytes(data)
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
	if tool in REVIEW_SOURCE_TOOLS:
		# Do not make the plugin parse and re-serialize source JSON. JavaScript
		# formatting can change numeric text or key order, which would make a good
		# server receipt fail spuriously. This canonical UTF-8 string is the exact
		# byte sequence that the bench hashed. The plugin verifies it, then saves a
		# mode-0600 envelope containing this string plus the server receipt.
		wire = {
			"ok": True,
			"data": body.decode("utf-8"),
			"result_mode": MODE,
			"private_output": summary,
		}
		return wire, {"ok": True, "data": summary}
	# Generic private query/report callers retain their existing object contract.
	return {**result, "result_mode": MODE, "private_output": summary}, {"ok": True, "data": summary}


def _prepare_deposit(result: dict, *, tool: str) -> tuple[dict, dict]:
	"""Return private wire bytes and a raw-row-free receipt for persistence."""
	if not isinstance(result, dict) or not result.get("ok"):
		return result, result
	data = result.get("data")
	if tool != DEPOSIT_SOURCE_TOOL or not isinstance(data, dict):
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
