"""What a failed connector call tells us about its outcome. NO frappe import.

The broker answers ``{ok: False, error: {code, message}}``. Some codes are raised
BEFORE anything leaves the bench; for those the chip may say "nothing changed". Every
other code may have reached the app (a half-applied write, an answer lost on the way
back), so it stays "unknown". Keep this list to codes proven pre-send."""

from __future__ import annotations

# Raised before ``broker._do_call`` (see ``broker.call`` / ``broker.precheck``).
# ``ssrf_blocked`` and ``egress_denied`` are NOT here: they are raised inside
# ``mcp_client.run_tool``, and a refused redirect answers a request already sent.
NOT_SENT_CODES = frozenset(
	{
		"connector_not_found",
		"connector_disabled",
		"connector_not_ready",
		"action_denied",
		"action_unknown",
		"invalid_arguments",
		"circuit_open",
		"at_capacity",
	}
)

# Of those, the ones a corrected call can fix, and the ones that clear on their own.
FIXABLE_CODES = frozenset({"invalid_arguments"})
RETRY_LATER_CODES = frozenset({"circuit_open", "at_capacity"})

NOT_SENT = "not_sent"
FAILED = "failed"


def inner_error_code(result) -> str:
	"""The connector's own error code from a tool envelope ``{ok: True, data: {ok:
	False, error: {code}}}``; empty for anything else (a success, an outer failure)."""
	data = result.get("data") if isinstance(result, dict) and result.get("ok") else None
	if not isinstance(data, dict) or data.get("ok") is not False:
		return ""
	error = data.get("error")
	return str(error.get("code") or "") if isinstance(error, dict) else ""


def connector_reason(result) -> str:
	"""The Pending Action ``reason_code`` of a failed ``call_connector`` card:
	``not_sent`` when the app was provably never called, else ``failed``."""
	return NOT_SENT if inner_error_code(result) in NOT_SENT_CODES else FAILED
