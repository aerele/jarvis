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
# These tools have an explicit plugin-owned local-input contract. Adding a read
# here is not enough by itself: its tool implementation must reject the ordinary
# transport and its bundle must verify the returned receipt before it can assess.
PRIVATE_SOURCE_TOOLS = frozenset(
	{
		"get_cycle_count_inputs",
		"get_purchase_match_inputs",
		"get_receivables_review_inputs",
		"get_ticket_triage_inputs",
	}
)
TOOLS = frozenset({"query", "run_report"}) | PRIVATE_SOURCE_TOOLS
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


def require_source_transport(tool):
	"""Refuse a review snapshot unless the private delegate boundary is active."""
	if tool in PRIVATE_SOURCE_TOOLS and not is_private():
		raise InvalidArgumentError("Private review evidence requires private audit output.")


def source_envelope(snapshot):
	"""Put one server-captured review snapshot on the sealed source contract."""
	if not isinstance(snapshot, dict):
		raise InvalidArgumentError("The private review source has an invalid snapshot.")
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
	if not result.get("ok"):
		return result, result
	data = result["data"]
	rows = data.get("rows", data.get("result")) if isinstance(data, dict) else None
	if not isinstance(rows, list):
		raise InvalidArgumentError("The private source has no row array; coverage is incomplete.")
	if len(rows) > MAX_ROWS:
		raise InvalidArgumentError("The private source exceeds its row limit; coverage is incomplete.")
	if tool in PRIVATE_SOURCE_TOOLS:
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
	if tool in PRIVATE_SOURCE_TOOLS:
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
