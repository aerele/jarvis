"""Typed, private Ticket Triage controls.

Ticket Triage handles Helpdesk tickets that can contain customer text.  It does
not use ``operator_review`` or the generic approval decision route.  The model
can see a ticket only in the plugin-owned private input file.  This module
keeps the server-side state intentionally small and safe.

State contract (``Jarvis Agent Run.assessment_output_json``)::

    {
        "kind": "ticket_triage_capture_v1",
        "capture_id": "opaque server id",
        "policy_version": "ticket-triage-v1",
        "config_hmac": "64 lower-case hex chars",
        "source_hmac": "64 lower-case hex chars",
        "captured_at": "UTC timestamp",
        "reviewer": "named user",
        "scope": {"safe immutable controls only"},
        "ticket_count": 1,
        "tickets": [
            {
                "ticket": "HD-TCK-...",
                "modified": "UTC timestamp",
                "evidence_hmac": "64 lower-case hex chars",
                "status": "safe control",
                "ticket_type": "safe control or null",
                "priority": "safe control or null",
            }
        ],
        "stage": "captured|staged",
        "proposals": [{"ticket": "HD-TCK-...", "proposal_hmac": "...", "approval": "..."}],
    }

The state never contains subject, description, email, similarity text, model
text, or a raw snapshot.  One deterministic ``Jarvis Approval Request`` shell
is made per proposed ticket.  Its ``assessment_key`` is an HMAC of the run,
capture, ticket evidence and typed proposal.  Its ``assessment_evidence`` has
only the safe fields in ``_proposal_evidence``.  We lock Run, then Ticket; a
duplicate-key insert is re-read and accepted only when its safe evidence is
byte-identical.  That gives a deterministic sidecar without a new DocType or a
migration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.utils import get_datetime
from frappe.utils.password import get_encryption_key

from jarvis._session import impersonate
from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

AGENT = "ticket-triage-operator"
SOURCE_TOOL = "get_ticket_triage_inputs"
STAGE_TOOL = "stage_ticket_triage_proposals"
POLICY_VERSION = "ticket-triage-v1"
STATE_KIND = "ticket_triage_capture_v1"
PROPOSAL_KIND = "ticket_triage_proposal_v1"
APPROVAL_SOURCE = "Ticket Triage"
RUN = "Jarvis Agent Run"
APPROVAL = "Jarvis Approval Request"
TICKET = "HD Ticket"

MAX_TICKETS = 250
MAX_CONTROL_VALUES = 25
MAX_STATE_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024
MAX_NAME_LENGTH = 140
MAX_CAPTURE_ID_LENGTH = 64
HEX_LENGTH = 64
UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

CONFIG_KEYS = frozenset(
	{
		"policy_version",
		"from_at",
		"to_at",
		"eligible_team_names",
		"eligible_status_names",
		"allowed_ticket_types",
		"allowed_priorities",
		"max_tickets",
		"assignment_mode",
	}
)
SAFE_TICKET_FIELDS = (
	"name",
	"modified",
	"creation",
	"status",
	"status_category",
	"ticket_type",
	"priority",
	"agent_group",
	"_assign",
	"is_merged",
)
# These fields can enter the sealed local input only.  None can enter state,
# an approval, a timeline, chat, realtime, dashboard, or an error message.
PRIVATE_TICKET_FIELDS = ("subject", "description", "raised_by")
RAW_TICKET_FIELDS = (*SAFE_TICKET_FIELDS, *PRIVATE_TICKET_FIELDS)
PROHIBITED_WRITE_FIELDS = (
	"status",
	"agent_group",
	"_assign",
	"subject",
	"description",
	"raised_by",
	"contact",
	"is_merged",
	"ticket_split_from",
)

_STATE_KEYS = frozenset(
	{
		"kind",
		"capture_id",
		"policy_version",
		"config_hmac",
		"source_hmac",
		"captured_at",
		"reviewer",
		"scope",
		"ticket_count",
		"tickets",
		"stage",
		"proposals",
	}
)
_STATE_TICKET_KEYS = frozenset({"ticket", "modified", "evidence_hmac", "status", "ticket_type", "priority"})
_STATE_PROPOSAL_KEYS = frozenset({"ticket", "proposal_hmac", "approval", "disposition"})
_SAFE_DISPOSITIONS = frozenset({"propose", "manual_review"})

_PRIVATE_REQUIRED = "Ticket triage evidence requires private local input."
_RUN_REQUIRED = "Ticket triage requires an active manual delegate run."
_CONFIG_INVALID = "Ticket triage configuration is invalid or no longer available."
_EVIDENCE_UNAVAILABLE = "Ticket evidence is unavailable. Start a new review after checking scope and access."
_EVIDENCE_CHANGED = "Ticket evidence changed after capture. Start a new review before proposing changes."
_STATE_INVALID = "Ticket triage state is invalid or no longer safe to use."
_PROPOSAL_INVALID = "Ticket triage proposals must be complete, typed, and within the captured scope."
_REVIEWER_REQUIRED = "Only the named Ticket Triage reviewer can use this control."
_WRITE_BLOCKED = "The ticket change was not applied because a safety check blocked it."


def _refuse(message: str, exc=frappe.ValidationError):
	"""Raise a fixed public error.  Never add a ticket value to this message."""
	frappe.throw(message, exc)


def _canonical(value) -> bytes:
	return json.dumps(
		value,
		sort_keys=True,
		ensure_ascii=False,
		allow_nan=False,
		separators=(",", ":"),
	).encode("utf-8")


def _canonical_text(value) -> str:
	return _canonical(value).decode("utf-8")


def _hmac(label: str, value) -> str:
	"""Return a domain-separated opaque evidence value.

	The value can include private text during capture, but the digest itself is the
	only value that can leave the private path.
	"""
	key = get_encryption_key().encode("utf-8")
	subkey = hmac.new(key, b"jarvis-ticket-triage-v1:" + label.encode("ascii"), hashlib.sha256).digest()
	return hmac.new(subkey, _canonical(value), hashlib.sha256).hexdigest()


def _is_digest(value) -> bool:
	return (
		isinstance(value, str)
		and len(value) == HEX_LENGTH
		and all(char in "0123456789abcdef" for char in value)
	)


def _safe_string(value, *, nullable: bool = False, limit: int = MAX_NAME_LENGTH):
	if value is None and nullable:
		return None
	if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
		_refuse(_STATE_INVALID)
	return value.strip()


def _utc_timestamp(value, *, error: str) -> datetime:
	"""Parse one strict, timezone-aware UTC instant.

	The database stores Helpdesk timestamps in the site timezone, but configured
	bounds and sealed state always use this RFC 3339 UTC form.  A naive value has
	no safe as-of meaning, so it is refused rather than interpreted in a browser
	or container timezone.
	"""
	if not isinstance(value, str) or not value.strip() or len(value) > 64:
		_refuse(error)
	try:
		parsed = datetime.strptime(value, UTC_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
	except (TypeError, ValueError, OverflowError):
		_refuse(error)
	if parsed.strftime(UTC_TIMESTAMP_FORMAT) != value:
		_refuse(error)
	return parsed


def _utc_text(value: datetime) -> str:
	return value.astimezone(timezone.utc).replace(microsecond=0).strftime(UTC_TIMESTAMP_FORMAT)


def _site_zone(*, error: str) -> ZoneInfo:
	try:
		return ZoneInfo(frappe.utils.get_system_timezone())
	except (TypeError, ZoneInfoNotFoundError):
		_refuse(error)


def _server_now_utc(*, error: str) -> datetime:
	"""Use the site clock, not a model or container clock, for the as-of cap."""
	try:
		now = frappe.utils.now_datetime().replace(microsecond=0)
		if now.tzinfo is None:
			now = now.replace(tzinfo=_site_zone(error=error))
		return now.astimezone(timezone.utc)
	except (AttributeError, TypeError, ValueError, OverflowError):
		_refuse(error)


def _db_time(value: str, *, error: str) -> str:
	"""Convert a canonical UTC bound to the site-local database timestamp."""
	return (
		_utc_timestamp(value, error=error)
		.astimezone(_site_zone(error=error))
		.replace(tzinfo=None)
		.strftime("%Y-%m-%d %H:%M:%S")
	)


def _source_timestamp(value, *, error: str) -> datetime:
	"""Normalize one HD Ticket ``modified`` timestamp to a UTC instant."""
	try:
		parsed = get_datetime(value)
		if not isinstance(parsed, datetime):
			raise TypeError
		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=_site_zone(error=error))
		return parsed.astimezone(timezone.utc).replace(microsecond=0)
	except (TypeError, ValueError, OverflowError):
		_refuse(error)


def _normalise_datetime(value, field: str) -> str:
	# ``field`` makes call sites self-documenting in configuration validation.
	return _utc_text(_utc_timestamp(value, error=_CONFIG_INVALID))


def _normalise_names(value, field: str, *, error: str = _CONFIG_INVALID) -> list[str]:
	if not isinstance(value, list) or not value or len(value) > MAX_CONTROL_VALUES:
		_refuse(error)
	if any(
		not isinstance(item, str) or not item.strip() or len(item.strip()) > MAX_NAME_LENGTH for item in value
	):
		_refuse(error)
	result = sorted({item.strip() for item in value})
	if len(result) != len(value):
		_refuse(error)
	return result


def _load_raw_config(raw) -> dict:
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		_refuse(_CONFIG_INVALID)
	if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
		# Exact keys deliberately reject Company and any future implicit scope.
		_refuse(_CONFIG_INVALID)
	return config


def _control(doctype: str, name: str, fields: Iterable[str]) -> dict:
	"""Read a configured control without accepting a redacted value."""
	try:
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		before = {field: doc.get(field) for field in fields}
		doc.apply_fieldlevel_read_permissions()
		after = {field: doc.get(field) for field in fields}
	except Exception:
		_refuse(_CONFIG_INVALID)
	if before != after:
		_refuse(_CONFIG_INVALID)
	return before


def _validate_controls(config: dict) -> None:
	for name in config["eligible_team_names"]:
		if _control("HD Team", name, ("disabled",)).get("disabled"):
			_refuse(_CONFIG_INVALID)
	for name in config["eligible_status_names"]:
		state = _control("HD Ticket Status", name, ("enabled", "category"))
		if not state.get("enabled") or state.get("category") != "Open":
			_refuse(_CONFIG_INVALID)
	for name in config["allowed_ticket_types"]:
		if _control("HD Ticket Type", name, ("disabled",)).get("disabled"):
			_refuse(_CONFIG_INVALID)
	for name in config["allowed_priorities"]:
		if _control("HD Ticket Priority", name, ("disabled",)).get("disabled"):
			_refuse(_CONFIG_INVALID)


def configuration_issues(raw) -> dict[str, str]:
	"""Return a safe, field-level setup result without exposing record values."""
	try:
		configuration(frappe._dict(config=raw))
		return {}
	except Exception:
		return {"config": _CONFIG_INVALID}


def configuration(inst) -> dict:
	"""Validate and normalize the complete immutable no-Company policy."""
	raw = _load_raw_config(inst.get("config") if hasattr(inst, "get") else getattr(inst, "config", None))
	if raw.get("policy_version") != POLICY_VERSION or raw.get("assignment_mode") != "none":
		_refuse(_CONFIG_INVALID)
	from_at = _normalise_datetime(raw.get("from_at"), "from_at")
	to_at = _normalise_datetime(raw.get("to_at"), "to_at")
	try:
		if _utc_timestamp(from_at, error=_CONFIG_INVALID) >= _utc_timestamp(
			to_at, error=_CONFIG_INVALID
		) or _utc_timestamp(to_at, error=_CONFIG_INVALID) > _server_now_utc(error=_CONFIG_INVALID):
			_refuse(_CONFIG_INVALID)
	except (TypeError, ValueError, OverflowError):
		_refuse(_CONFIG_INVALID)
	if type(raw.get("max_tickets")) is not int or not 1 <= raw["max_tickets"] <= MAX_TICKETS:
		_refuse(_CONFIG_INVALID)
	config = {
		"policy_version": POLICY_VERSION,
		"from_at": from_at,
		"to_at": to_at,
		"eligible_team_names": _normalise_names(raw.get("eligible_team_names"), "eligible_team_names"),
		"eligible_status_names": _normalise_names(raw.get("eligible_status_names"), "eligible_status_names"),
		"allowed_ticket_types": _normalise_names(raw.get("allowed_ticket_types"), "allowed_ticket_types"),
		"allowed_priorities": _normalise_names(raw.get("allowed_priorities"), "allowed_priorities"),
		"max_tickets": raw["max_tickets"],
		"assignment_mode": "none",
	}
	_validate_controls(config)
	return config


def scope_from_config(config: dict) -> dict:
	"""The safe immutable scope.  HD Ticket has no Company field by design."""
	return {
		"policy_version": config["policy_version"],
		"from_at": config["from_at"],
		"to_at": config["to_at"],
		"eligible_team_names": list(config["eligible_team_names"]),
		"eligible_status_names": list(config["eligible_status_names"]),
		"allowed_ticket_types": list(config["allowed_ticket_types"]),
		"allowed_priorities": list(config["allowed_priorities"]),
		"max_tickets": config["max_tickets"],
		"assignment_mode": "none",
	}


def _assert_safe_scope(scope) -> None:
	"""Validate a stored scope without reading any Helpdesk record.

	This protects the durable state parser from becoming a second free-text lane.
	The controls themselves are checked before capture; this function checks the
	sealed value shape and its canonical UTC bounds again on every later action.
	"""
	if not isinstance(scope, dict) or set(scope) != CONFIG_KEYS:
		_refuse(_STATE_INVALID)
	if scope.get("policy_version") != POLICY_VERSION or scope.get("assignment_mode") != "none":
		_refuse(_STATE_INVALID)
	from_at = _utc_timestamp(scope.get("from_at"), error=_STATE_INVALID)
	to_at = _utc_timestamp(scope.get("to_at"), error=_STATE_INVALID)
	if from_at >= to_at or to_at > _server_now_utc(error=_STATE_INVALID):
		_refuse(_STATE_INVALID)
	if type(scope.get("max_tickets")) is not int or not 1 <= scope["max_tickets"] <= MAX_TICKETS:
		_refuse(_STATE_INVALID)
	normal = {
		"policy_version": POLICY_VERSION,
		"from_at": _utc_text(from_at),
		"to_at": _utc_text(to_at),
		"eligible_team_names": _normalise_names(
			scope.get("eligible_team_names"), "eligible_team_names", error=_STATE_INVALID
		),
		"eligible_status_names": _normalise_names(
			scope.get("eligible_status_names"), "eligible_status_names", error=_STATE_INVALID
		),
		"allowed_ticket_types": _normalise_names(
			scope.get("allowed_ticket_types"), "allowed_ticket_types", error=_STATE_INVALID
		),
		"allowed_priorities": _normalise_names(
			scope.get("allowed_priorities"), "allowed_priorities", error=_STATE_INVALID
		),
		"max_tickets": scope["max_tickets"],
		"assignment_mode": "none",
	}
	if scope != normal:
		_refuse(_STATE_INVALID)


def _configuration_from_run(run) -> dict:
	if (run.get("input_snapshot_json") or "").strip():
		# This field is forbidden for this agent because it is a raw-evidence lane.
		_refuse(_STATE_INVALID)
	config = configuration(frappe._dict(config=run.get("assessment_config_json") or ""))
	if _canonical_text(config) != _canonical_text(frappe.parse_json(run.assessment_config_json or "{}")):
		_refuse(_STATE_INVALID)
	return config


def _run_reviewer(run) -> str:
	installation = (run.get("installation") or "").strip()
	if not installation:
		_refuse(_RUN_REQUIRED)
	try:
		reviewer = (frappe.db.get_value("Jarvis Agent Installation", installation, "reviewer") or "").strip()
	except Exception:
		_refuse(_RUN_REQUIRED)
	if reviewer in ("", "Guest") or not frappe.db.get_value("User", reviewer, "enabled"):
		_refuse(_RUN_REQUIRED)
	return reviewer


def _require_ticket_run(run, *, statuses: set[str]) -> None:
	if (
		not run
		or run.get("agent") != AGENT
		or run.get("status") not in statuses
		or run.get("trigger") != "manual"
		or not (run.get("session_key") or "").strip()
	):
		_refuse(_RUN_REQUIRED, InvalidArgumentError)
	try:
		from jarvis.tools import _delegate_capability

		allowed = _delegate_capability.bench_tools(frappe.parse_json(run.get("tools_allow_json") or "[]"))
	except Exception:
		_refuse(_RUN_REQUIRED, InvalidArgumentError)
	if SOURCE_TOOL not in allowed or STAGE_TOOL not in allowed:
		_refuse(_RUN_REQUIRED, InvalidArgumentError)


def _require_active_run(run) -> None:
	_require_ticket_run(run, statuses={"running"})


def _require_stage_run(run) -> None:
	# A retransmitted stage receipt can see a completed Run before the plugin
	# receives its first response.  It may only return the already-safe summary.
	_require_ticket_run(run, statuses={"running", "completed"})


def run_from_session(session_key: str, *, allow_staged_retry: bool = False):
	if not isinstance(session_key, str) or not session_key.strip():
		_refuse(_RUN_REQUIRED, InvalidArgumentError)
	try:
		run = frappe.get_doc(RUN, {"session_key": session_key.strip()})
	except Exception:
		_refuse(_RUN_REQUIRED, InvalidArgumentError)
	(_require_stage_run if allow_staged_retry else _require_active_run)(run)
	return run


def _raw_ticket_projection(doc) -> dict:
	"""Get the model's private row and fail if any selected field is redacted."""
	try:
		doc.check_permission("read")
		before = {field: doc.get(field) for field in RAW_TICKET_FIELDS}
		doc.apply_fieldlevel_read_permissions()
		after = {field: doc.get(field) for field in RAW_TICKET_FIELDS}
	except Exception:
		_refuse(_EVIDENCE_UNAVAILABLE)
	if before != after:
		_refuse(_EVIDENCE_UNAVAILABLE)
	try:
		return json.loads(frappe.as_json(before))
	except Exception:
		_refuse(_EVIDENCE_UNAVAILABLE)


def _ticket_filters(config: dict) -> dict:
	# ``modified`` is the relevant source timestamp.  It makes ``to_at`` an
	# actual as-of cap: a ticket changed after the sealed cutoff cannot enter the
	# source, even if it was created earlier in the window.
	return {
		"modified": [
			"between",
			[
				_db_time(config["from_at"], error=_CONFIG_INVALID),
				_db_time(config["to_at"], error=_CONFIG_INVALID),
			],
		],
		"agent_group": ["in", config["eligible_team_names"]],
		"status": ["in", config["eligible_status_names"]],
	}


def _candidate_names(config: dict) -> list[str]:
	"""Return all readable names only if readable and physical scope agree."""
	if not frappe.has_permission(TICKET, "read"):
		_refuse(_EVIDENCE_UNAVAILABLE)
	filters = _ticket_filters(config)
	try:
		names = frappe.get_list(
			TICKET,
			filters=filters,
			pluck="name",
			order_by="modified asc, name asc",
			limit_page_length=config["max_tickets"] + 1,
		)
		physical_count = frappe.db.count(TICKET, filters=filters)
	except Exception:
		_refuse(_EVIDENCE_UNAVAILABLE)
	if (
		not isinstance(names, list)
		or any(not isinstance(name, str) or not name for name in names)
		or len(names) > config["max_tickets"]
		or len(set(names)) != len(names)
		or physical_count != len(names)
	):
		# A difference means the run-as user cannot prove complete scoped coverage.
		_refuse(_EVIDENCE_UNAVAILABLE)
	return names


def _assert_ticket_scope(row: dict, config: dict) -> None:
	try:
		modified = _source_timestamp(row.get("modified"), error=_EVIDENCE_CHANGED)
		in_time = (
			_utc_timestamp(config["from_at"], error=_EVIDENCE_CHANGED)
			<= modified
			<= _utc_timestamp(config["to_at"], error=_EVIDENCE_CHANGED)
		)
	except (TypeError, ValueError, OverflowError):
		in_time = False
	if (
		not in_time
		or row.get("agent_group") not in config["eligible_team_names"]
		or row.get("status") not in config["eligible_status_names"]
		or row.get("status_category") != "Open"
	):
		_refuse(_EVIDENCE_CHANGED)


def _record_hmac(config_hmac: str, capture_id: str, row: dict) -> str:
	return _hmac("ticket", {"config_hmac": config_hmac, "capture_id": capture_id, "row": row})


def _collect(config: dict, capture_id: str, config_hmac: str) -> dict:
	"""Collect an all-or-nothing private input snapshot without durable raw data."""
	names = _candidate_names(config)
	rows = []
	for name in names:
		try:
			doc = frappe.get_doc(TICKET, name)
		except Exception:
			_refuse(_EVIDENCE_UNAVAILABLE)
		row = _raw_ticket_projection(doc)
		_assert_ticket_scope(row, config)
		rows.append(row)
	# A second identical scoped name query catches collection-time source drift.
	if _candidate_names(config) != names:
		_refuse(_EVIDENCE_CHANGED)
	snapshot = {
		"kind": "ticket_triage_private_input_v1",
		"capture_id": capture_id,
		"scope": scope_from_config(config),
		"captured_at": _utc_text(_server_now_utc(error=_EVIDENCE_UNAVAILABLE)),
		"tickets": rows,
	}
	snapshot["source_digest"] = _hmac(
		"source", {"config_hmac": config_hmac, "capture_id": capture_id, "snapshot": snapshot}
	)
	return snapshot


def _safe_ticket_state(row: dict, config_hmac: str, capture_id: str) -> dict:
	return {
		"ticket": _safe_string(row.get("name")),
		"modified": _utc_text(_source_timestamp(row.get("modified"), error=_EVIDENCE_UNAVAILABLE)),
		"evidence_hmac": _record_hmac(config_hmac, capture_id, row),
		"status": _safe_string(row.get("status")),
		"ticket_type": _safe_string(row.get("ticket_type"), nullable=True),
		"priority": _safe_string(row.get("priority"), nullable=True),
	}


def _capture_state(*, capture_id: str, config: dict, config_hmac: str, snapshot: dict, reviewer: str) -> dict:
	state = {
		"kind": STATE_KIND,
		"capture_id": capture_id,
		"policy_version": POLICY_VERSION,
		"config_hmac": config_hmac,
		"source_hmac": snapshot["source_digest"],
		"captured_at": snapshot["captured_at"],
		"reviewer": reviewer,
		"scope": scope_from_config(config),
		"ticket_count": len(snapshot["tickets"]),
		"tickets": [_safe_ticket_state(row, config_hmac, capture_id) for row in snapshot["tickets"]],
		"stage": "captured",
		"proposals": [],
	}
	_assert_safe_state(state)
	return state


def _parse_state(raw) -> dict:
	try:
		state = json.loads(raw or "{}")
	except (TypeError, ValueError):
		_refuse(_STATE_INVALID)
	if not isinstance(state, dict):
		_refuse(_STATE_INVALID)
	_assert_safe_state(state)
	return state


def _assert_safe_state(state: dict) -> None:
	"""Shape-check state so injected free text cannot become a durable lane."""
	if set(state) != _STATE_KEYS:
		_refuse(_STATE_INVALID)
	if (
		state.get("kind") != STATE_KIND
		or state.get("policy_version") != POLICY_VERSION
		or state.get("stage") not in {"captured", "staged"}
		or not isinstance(state.get("capture_id"), str)
		or not 1 <= len(state["capture_id"]) <= MAX_CAPTURE_ID_LENGTH
		or not _is_digest(state.get("config_hmac"))
		or not _is_digest(state.get("source_hmac"))
		or not isinstance(state.get("scope"), dict)
		or not isinstance(state.get("ticket_count"), int)
		or not 0 <= state["ticket_count"] <= MAX_TICKETS
		or not isinstance(state.get("tickets"), list)
		or len(state["tickets"]) != state["ticket_count"]
		or not isinstance(state.get("proposals"), list)
	):
		_refuse(_STATE_INVALID)
	_assert_safe_scope(state["scope"])
	_safe_string(state.get("reviewer"))
	_utc_timestamp(state.get("captured_at"), error=_STATE_INVALID)
	tickets = set()
	for row in state["tickets"]:
		if (
			not isinstance(row, dict)
			or set(row) != _STATE_TICKET_KEYS
			or not _is_digest(row.get("evidence_hmac"))
		):
			_refuse(_STATE_INVALID)
		name = _safe_string(row.get("ticket"))
		_utc_timestamp(row.get("modified"), error=_STATE_INVALID)
		_safe_string(row.get("status"))
		_safe_string(row.get("ticket_type"), nullable=True)
		_safe_string(row.get("priority"), nullable=True)
		if name in tickets:
			_refuse(_STATE_INVALID)
		tickets.add(name)
	proposal_tickets = set()
	for proposal in state["proposals"]:
		if (
			not isinstance(proposal, dict)
			or set(proposal) != _STATE_PROPOSAL_KEYS
			or proposal.get("disposition") not in _SAFE_DISPOSITIONS
			or not _is_digest(proposal.get("proposal_hmac"))
		):
			_refuse(_STATE_INVALID)
		name = _safe_string(proposal.get("ticket"))
		_safe_string(proposal.get("approval"))
		if name not in tickets or name in proposal_tickets:
			_refuse(_STATE_INVALID)
		proposal_tickets.add(name)
	if state["stage"] == "captured" and state["proposals"]:
		_refuse(_STATE_INVALID)
	if len(_canonical(state)) > MAX_STATE_BYTES:
		_refuse(_STATE_INVALID)


def _lock_run(run_name: str):
	try:
		frappe.db.sql(f"select name from `tab{RUN}` where name=%s for update", (run_name,))
		return frappe.get_doc(RUN, run_name)
	except Exception:
		_refuse(_STATE_INVALID)


def _store_state(run_name: str, state: dict) -> None:
	_assert_safe_state(state)
	frappe.db.set_value(
		RUN, run_name, "assessment_output_json", _canonical_text(state), update_modified=False
	)


def capture_private_inputs(run) -> dict:
	"""Return one raw snapshot only through the active private-output request."""
	from jarvis.tools import _private_output

	# This check is deliberately first.  An ordinary call cannot collect one row.
	if not _private_output.is_private():
		_refuse(_PRIVATE_REQUIRED, InvalidArgumentError)
	_require_active_run(run)
	locked = _lock_run(run.name)
	_require_active_run(locked)
	config = _configuration_from_run(locked)
	config_hmac = _hmac("config", config)
	state_raw = (locked.get("assessment_output_json") or "").strip()
	if state_raw:
		state = _parse_state(state_raw)
		if state["config_hmac"] != config_hmac or state["stage"] != "captured":
			_refuse(_EVIDENCE_CHANGED)
		capture_id = state["capture_id"]
		reviewer = state["reviewer"]
	else:
		capture_id = frappe.generate_hash(length=32)
		reviewer = _run_reviewer(locked)
	snapshot = _collect(config, capture_id, config_hmac)
	if state_raw:
		if snapshot["source_digest"] != state["source_hmac"]:
			_refuse(_EVIDENCE_CHANGED)
	else:
		state = _capture_state(
			capture_id=capture_id,
			config=config,
			config_hmac=config_hmac,
			snapshot=snapshot,
			reviewer=reviewer,
		)
		_store_state(locked.name, state)
	return {"rows": [snapshot]}


def _locked_ticket(name: str):
	try:
		frappe.db.sql("select name from `tabHD Ticket` where name=%s for update", (name,))
		return frappe.get_doc(TICKET, name)
	except Exception:
		_refuse(_EVIDENCE_CHANGED)


def _proposal_row(row: dict, state_row: dict, config: dict) -> dict:
	"""Validate one model row.  It is an enum plus controlled link names only."""
	if not isinstance(row, dict) or set(row) - {"ticket", "disposition", "ticket_type", "priority"}:
		_refuse(_PROPOSAL_INVALID)
	ticket = _safe_string(row.get("ticket"))
	disposition = row.get("disposition")
	if disposition not in _SAFE_DISPOSITIONS:
		_refuse(_PROPOSAL_INVALID)
	ticket_type = row.get("ticket_type")
	priority = row.get("priority")
	if ticket_type is not None and (
		not isinstance(ticket_type, str) or ticket_type not in config["allowed_ticket_types"]
	):
		_refuse(_PROPOSAL_INVALID)
	if priority is not None and (
		not isinstance(priority, str) or priority not in config["allowed_priorities"]
	):
		_refuse(_PROPOSAL_INVALID)
	if disposition == "manual_review":
		if ticket_type is not None or priority is not None:
			_refuse(_PROPOSAL_INVALID)
		return {"ticket": ticket, "disposition": disposition, "ticket_type": None, "priority": None}
	# A proposed update must alter at least one controlled value.  A model cannot
	# turn this endpoint into a generic note, assignment, status, or free-text path.
	if ticket_type is None and priority is None:
		_refuse(_PROPOSAL_INVALID)
	if ticket_type == state_row["ticket_type"] and priority == state_row["priority"]:
		_refuse(_PROPOSAL_INVALID)
	return {
		"ticket": ticket,
		"disposition": disposition,
		"ticket_type": ticket_type,
		"priority": priority,
	}


def _proposal_hmac(state: dict, proposal: dict) -> str:
	return _hmac(
		"proposal",
		{
			"capture_id": state["capture_id"],
			"config_hmac": state["config_hmac"],
			"ticket": proposal["ticket"],
			"disposition": proposal["disposition"],
			"ticket_type": proposal["ticket_type"],
			"priority": proposal["priority"],
		},
	)


def _proposal_evidence(state: dict, state_row: dict, proposal: dict, proposal_hmac: str) -> dict:
	"""The approval shell's safe immutable evidence.  No raw ticket field enters."""
	evidence = {
		"kind": PROPOSAL_KIND,
		"capture_id": state["capture_id"],
		"config_hmac": state["config_hmac"],
		"ticket": state_row["ticket"],
		"evidence_hmac": state_row["evidence_hmac"],
		"reviewer": state["reviewer"],
		"current": {
			"status": state_row["status"],
			"ticket_type": state_row["ticket_type"],
			"priority": state_row["priority"],
		},
		"proposed": {
			"ticket_type": proposal["ticket_type"],
			"priority": proposal["priority"],
		},
		"proposal_hmac": proposal_hmac,
	}
	if len(_canonical(evidence)) > MAX_EVIDENCE_BYTES:
		_refuse(_STATE_INVALID)
	return evidence


def _approval_key(run_name: str, evidence: dict) -> str:
	return _hmac("approval", {"run": run_name, "evidence": evidence})


def _parse_evidence(raw) -> dict:
	try:
		evidence = json.loads(raw or "{}")
	except (TypeError, ValueError):
		_refuse(_STATE_INVALID)
	if not isinstance(evidence, dict) or set(evidence) != {
		"kind",
		"capture_id",
		"config_hmac",
		"ticket",
		"evidence_hmac",
		"reviewer",
		"current",
		"proposed",
		"proposal_hmac",
	}:
		_refuse(_STATE_INVALID)
	if (
		evidence.get("kind") != PROPOSAL_KIND
		or not _is_digest(evidence.get("config_hmac"))
		or not _is_digest(evidence.get("evidence_hmac"))
		or not _is_digest(evidence.get("proposal_hmac"))
		or not isinstance(evidence.get("current"), dict)
		or not isinstance(evidence.get("proposed"), dict)
	):
		_refuse(_STATE_INVALID)
	_safe_string(evidence.get("capture_id"), limit=MAX_CAPTURE_ID_LENGTH)
	_safe_string(evidence.get("ticket"))
	_safe_string(evidence.get("reviewer"))
	if set(evidence["current"]) != {"status", "ticket_type", "priority"} or set(evidence["proposed"]) != {
		"ticket_type",
		"priority",
	}:
		_refuse(_STATE_INVALID)
	for key in ("status", "ticket_type", "priority"):
		_safe_string(evidence["current"].get(key), nullable=key != "status")
	for key in ("ticket_type", "priority"):
		_safe_string(evidence["proposed"].get(key), nullable=True)
	return evidence


def _same_evidence(existing, evidence: dict, key: str, run_name: str) -> bool:
	return bool(
		existing
		and existing.get("agent") == AGENT
		and existing.get("source") == APPROVAL_SOURCE
		and existing.get("run") == run_name
		and existing.get("assessment_key") == key
		and hmac.compare_digest(existing.get("assessment_evidence") or "", _canonical_text(evidence))
	)


def _approval_values(run, evidence: dict, key: str) -> dict:
	"""Build the one safe Approval shell payload for a typed proposal."""
	return {
		"doctype": APPROVAL,
		"title": f"Ticket Triage: {evidence['ticket']}"[:140],
		"status": "Pending",
		"source": APPROVAL_SOURCE,
		"agent": AGENT,
		"run": run.name,
		"preparation_mode": run.get("preparation_mode") or "shadow",
		"result_class": "derived_candidate",
		"assessment_key": key,
		"assessment_evidence": _canonical_text(evidence),
		"document_type": TICKET,
		"conversation": "",
		"question": "Confirm the controlled Ticket Triage classification change.",
		"context_md": "",
		"options": json.dumps(["Confirm controlled update", "Reject"], separators=(",", ":")),
		# Keep ref fields blank.  The generic controller must never write a ticket
		# Comment or resume a conversation for this typed sidecar.
		"ref_doctype": "",
		"ref_name": "",
		"apply_status": "Pending",
	}


def _approval_shell(run, evidence: dict) -> str:
	"""Get or create the exact one safe shell for a proposed ticket."""
	key = _approval_key(run.name, evidence)
	existing_name = frappe.db.get_value(APPROVAL, {"assessment_key": key}, "name")
	if existing_name:
		existing = frappe.db.get_value(
			APPROVAL,
			existing_name,
			["name", "agent", "source", "run", "assessment_key", "assessment_evidence"],
			as_dict=True,
		)
		if not _same_evidence(existing, evidence, key, run.name):
			_refuse(_STATE_INVALID)
		return existing_name
	approval = frappe.get_doc(_approval_values(run, evidence, key))
	approval.flags.jarvis_server_write = True
	try:
		approval.insert(ignore_permissions=True)
	except Exception:
		# The unique assessment_key arbitrates concurrent identical stage attempts.
		existing_name = frappe.db.get_value(APPROVAL, {"assessment_key": key}, "name")
		existing = (
			frappe.db.get_value(
				APPROVAL,
				existing_name,
				["name", "agent", "source", "run", "assessment_key", "assessment_evidence"],
				as_dict=True,
			)
			if existing_name
			else None
		)
		if not _same_evidence(existing, evidence, key, run.name):
			_refuse(_STATE_INVALID)
		return existing_name
	# Owner is the named reviewer, never the run-as identity.  The typed endpoint
	# still rechecks the immutable evidence and real HD Ticket permissions.
	frappe.db.set_value(APPROVAL, approval.name, "owner", evidence["reviewer"], update_modified=False)
	return approval.name


def _fresh_capture_evidence(state: dict, config: dict) -> None:
	"""Re-read every captured raw row locally and compare only opaque HMACs."""
	captured_names = [state_row["ticket"] for state_row in state["tickets"]]
	if _candidate_names(config) != captured_names:
		# New, deleted, hidden, or newly out-of-scope tickets invalidate the
		# all-ticket stage.  Individual HMAC checks alone would miss a new row.
		_refuse(_EVIDENCE_CHANGED)
	for state_row in state["tickets"]:
		doc = _locked_ticket(state_row["ticket"])
		row = _raw_ticket_projection(doc)
		_assert_ticket_scope(row, config)
		if not hmac.compare_digest(
			_record_hmac(state["config_hmac"], state["capture_id"], row), state_row["evidence_hmac"]
		):
			_refuse(_EVIDENCE_CHANGED)
	if _candidate_names(config) != captured_names:
		_refuse(_EVIDENCE_CHANGED)


def _validate_stage_rows(state: dict, config: dict, rows) -> list[dict]:
	if not isinstance(rows, list) or len(rows) != state["ticket_count"]:
		_refuse(_PROPOSAL_INVALID)
	known = {record["ticket"]: record for record in state["tickets"]}
	seen = set()
	proposals = []
	for raw_row in rows:
		if (
			not isinstance(raw_row, dict)
			or raw_row.get("ticket") not in known
			or raw_row.get("ticket") in seen
		):
			_refuse(_PROPOSAL_INVALID)
		proposal = _proposal_row(raw_row, known[raw_row["ticket"]], config)
		seen.add(proposal["ticket"])
		proposals.append(proposal)
	if seen != set(known):
		_refuse(_PROPOSAL_INVALID)
	return sorted(proposals, key=lambda value: value["ticket"])


def _stage_result(state: dict) -> dict:
	proposed = [row for row in state["proposals"] if row["disposition"] == "propose"]
	manual = [row for row in state["proposals"] if row["disposition"] == "manual_review"]
	return {
		"status": "staged",
		"proposed_count": len(proposed),
		"manual_review_count": len(manual),
		"approvals": [row["approval"] for row in proposed],
	}


def _finish_staged_run(run, state: dict) -> None:
	"""Finish without generic record_agent_run and retire the bearer after commit."""
	frappe.db.set_value(
		RUN,
		run.name,
		{
			"assessment_output_json": _canonical_text(state),
			"status": "completed",
			"finished_at": frappe.utils.now_datetime(),
			"findings_count": len([row for row in state["proposals"] if row["disposition"] == "propose"]),
			"coverage_note": "Ticket Triage staged typed reviewer proposals.",
		},
		update_modified=False,
	)
	from jarvis.chat import agent_runs

	# This runs after the dispatch has persisted its safe tool receipt.  It never
	# carries raw source bytes and prevents a completed run bearer from being reused.
	frappe.db.after_commit.add(lambda: agent_runs.teardown_run_session(run.get("session_key")))


def stage_proposals(run, capture_id: str, rows) -> dict:
	"""Server-authoritative typed staging.  The model cannot choose a write field."""
	_require_stage_run(run)
	if not isinstance(capture_id, str) or not capture_id or len(capture_id) > MAX_CAPTURE_ID_LENGTH:
		_refuse(_PROPOSAL_INVALID, InvalidArgumentError)
	locked = _lock_run(run.name)
	_require_stage_run(locked)
	state = _parse_state(locked.get("assessment_output_json"))
	if state["capture_id"] != capture_id:
		_refuse(_PROPOSAL_INVALID)
	if state["stage"] == "staged":
		proposals = _validate_stage_rows(state, state["scope"], rows)
		stored = {row["ticket"]: row for row in state["proposals"]}
		if len(stored) != len(proposals) or any(
			stored.get(row["ticket"], {}).get("proposal_hmac") != _proposal_hmac(state, row)
			for row in proposals
		):
			_refuse(_PROPOSAL_INVALID)
		return _stage_result(state)
	if locked.get("status") != "running":
		_refuse(_RUN_REQUIRED, InvalidArgumentError)
	config = _configuration_from_run(locked)
	if state["config_hmac"] != _hmac("config", config) or state["scope"] != scope_from_config(config):
		_refuse(_EVIDENCE_CHANGED)
	proposals = _validate_stage_rows(state, config, rows)
	_fresh_capture_evidence(state, config)
	staged = []
	by_ticket = {row["ticket"]: row for row in state["tickets"]}
	for proposal in proposals:
		proposal_hmac = _proposal_hmac(state, proposal)
		if proposal["disposition"] == "manual_review":
			# An explicit manual row has no side effect and no approval shell.
			staged.append(
				{
					"ticket": proposal["ticket"],
					"proposal_hmac": proposal_hmac,
					"approval": f"manual:{proposal_hmac[:24]}",
					"disposition": "manual_review",
				}
			)
			continue
		evidence = _proposal_evidence(state, by_ticket[proposal["ticket"]], proposal, proposal_hmac)
		staged.append(
			{
				"ticket": proposal["ticket"],
				"proposal_hmac": proposal_hmac,
				"approval": _approval_shell(locked, evidence),
				"disposition": "propose",
			}
		)
	state["stage"] = "staged"
	state["proposals"] = staged
	_assert_safe_state(state)
	_finish_staged_run(locked, state)
	return _stage_result(state)


def is_ticket_triage_approval(doc) -> bool:
	"""Exact typed-sidecar discriminator for generic approval refusal.

	This is intentionally narrower than ``assessment_key`` and narrower than an
	agent-name heuristic.  A generic agent review remains on its existing route;
	only a Ticket Triage sidecar with its server-only source is refused there.
	"""
	return bool(
		doc
		and doc.get("agent") == AGENT
		and doc.get("source") == APPROVAL_SOURCE
		and (doc.get("assessment_key") or "").strip()
	)


def _lock_approval(name: str):
	try:
		frappe.db.sql("select name from `tabJarvis Approval Request` where name=%s for update", (name,))
		return frappe.get_doc(APPROVAL, name)
	except Exception:
		_refuse(_STATE_INVALID)


def _proposal_context(name: str, *, lock: bool) -> tuple[object, object, dict, dict, dict]:
	"""Return the locked Run/Approval/state/evidence/proposal, or fail closed."""
	try:
		run_name = frappe.db.get_value(APPROVAL, name, "run")
	except Exception:
		_refuse(_STATE_INVALID)
	if not run_name:
		_refuse(_STATE_INVALID)
	run = _lock_run(run_name) if lock else frappe.get_doc(RUN, run_name)
	doc = _lock_approval(name) if lock else frappe.get_doc(APPROVAL, name)
	return _validate_proposal_context(run, doc)


def _validate_proposal_context(run, doc) -> tuple[object, object, dict, dict, dict]:
	"""Validate a Run/Approval pair that is already locked when mutation needs it."""
	if not is_ticket_triage_approval(doc) or doc.get("run") != run.name:
		_refuse(_STATE_INVALID)
	state = _parse_state(run.get("assessment_output_json"))
	if state["stage"] != "staged":
		_refuse(_STATE_INVALID)
	evidence = _parse_evidence(doc.get("assessment_evidence"))
	if (
		evidence["capture_id"] != state["capture_id"]
		or evidence["config_hmac"] != state["config_hmac"]
		or evidence["reviewer"] != state["reviewer"]
	):
		_refuse(_STATE_INVALID)
	state_rows = {row["ticket"]: row for row in state["tickets"]}
	state_row = state_rows.get(evidence["ticket"])
	proposal_row = next((row for row in state["proposals"] if row["approval"] == doc.name), None)
	if (
		not state_row
		or not proposal_row
		or proposal_row["ticket"] != evidence["ticket"]
		or proposal_row["disposition"] != "propose"
		or state_row["evidence_hmac"] != evidence["evidence_hmac"]
	):
		_refuse(_STATE_INVALID)
	proposal = {
		"ticket": evidence["ticket"],
		"disposition": "propose",
		"ticket_type": evidence["proposed"]["ticket_type"],
		"priority": evidence["proposed"]["priority"],
	}
	proposal_hmac = _proposal_hmac(state, proposal)
	if (
		proposal_hmac != evidence["proposal_hmac"]
		or proposal_hmac != proposal_row["proposal_hmac"]
		or doc.get("assessment_key") != _approval_key(run.name, evidence)
		or not hmac.compare_digest(doc.get("assessment_evidence") or "", _canonical_text(evidence))
	):
		_refuse(_STATE_INVALID)
	return run, doc, state, evidence, proposal


def _confirm_context(name: str) -> tuple[object, object, object, dict, dict, dict]:
	"""Lock order is Run -> Ticket -> Approval for stage/confirm deadlock safety."""
	try:
		preview = frappe.get_doc(APPROVAL, name)
	except Exception:
		_refuse(_STATE_INVALID)
	if not is_ticket_triage_approval(preview):
		_refuse(_STATE_INVALID)
	preview_evidence = _parse_evidence(preview.get("assessment_evidence"))
	run_name = preview.get("run")
	if not run_name:
		_refuse(_STATE_INVALID)
	run = _lock_run(run_name)
	ticket = _locked_ticket(preview_evidence["ticket"])
	doc = _lock_approval(name)
	run, doc, state, evidence, proposal = _validate_proposal_context(run, doc)
	if ticket.name != evidence["ticket"]:
		_refuse(_STATE_INVALID)
	return run, doc, ticket, state, evidence, proposal


def _require_reviewer(run, evidence: dict, *, write: bool) -> None:
	from jarvis.chat.collaboration import require_legacy_conversation

	reviewer = evidence["reviewer"]
	if frappe.session.user != reviewer:
		_refuse(_REVIEWER_REQUIRED, frappe.PermissionError)
	require_legacy_conversation(run.get("conversation"))
	if not frappe.has_permission(TICKET, "read", evidence["ticket"], user=reviewer):
		_refuse(_REVIEWER_REQUIRED, frappe.PermissionError)
	if write and not frappe.has_permission(TICKET, "write", evidence["ticket"], user=reviewer):
		_refuse(_REVIEWER_REQUIRED, frappe.PermissionError)


def _current_state_record(state: dict, evidence: dict) -> dict:
	row = next((row for row in state["tickets"] if row["ticket"] == evidence["ticket"]), None)
	if not row:
		_refuse(_STATE_INVALID)
	return row


def _verify_fresh_proposal(run, state: dict, evidence: dict, proposal: dict, *, ticket=None):
	"""Validate controls and the actual raw ticket locally before a human write."""
	config = _configuration_from_run(run)
	if state["config_hmac"] != _hmac("config", config) or state["scope"] != scope_from_config(config):
		_refuse(_EVIDENCE_CHANGED)
	if proposal["ticket_type"] not in (None, *config["allowed_ticket_types"]) or proposal["priority"] not in (
		None,
		*config["allowed_priorities"],
	):
		_refuse(_EVIDENCE_CHANGED)
	state_row = _current_state_record(state, evidence)
	doc = ticket or _locked_ticket(evidence["ticket"])
	row = _raw_ticket_projection(doc)
	_assert_ticket_scope(row, config)
	if not hmac.compare_digest(
		_record_hmac(state["config_hmac"], state["capture_id"], row), state_row["evidence_hmac"]
	):
		_refuse(_EVIDENCE_CHANGED)
	if (
		row.get("status") != evidence["current"]["status"]
		or row.get("ticket_type") != evidence["current"]["ticket_type"]
		or row.get("priority") != evidence["current"]["priority"]
	):
		_refuse(_EVIDENCE_CHANGED)
	return doc


def _related_count(doctype: str, filters: dict) -> int:
	try:
		if not frappe.db.table_exists(doctype):
			return 0
		return int(frappe.db.count(doctype, filters=filters))
	except Exception:
		# Unknown side-effect state is unsafe; callers treat this as a blocked write.
		raise RuntimeError("side effect check unavailable")


def _write_guard(ticket) -> dict:
	"""Capture only local comparison values.  Do not persist this structure."""
	return {
		"fields": {field: ticket.get(field) for field in PROHIBITED_WRITE_FIELDS},
		"todos": _related_count("ToDo", {"reference_type": TICKET, "reference_name": ticket.name}),
		"communications": _related_count(
			"Communication", {"reference_doctype": TICKET, "reference_name": ticket.name}
		),
		"comments": _related_count("Comment", {"reference_doctype": TICKET, "reference_name": ticket.name}),
		"ticket_comments": _related_count("HD Ticket Comment", {"reference_ticket": ticket.name}),
		"notifications": _related_count("HD Notification", {"reference_ticket": ticket.name}),
	}


def _assert_write_guard(before: dict, after: dict, ticket, proposal: dict) -> None:
	if before["fields"] != after["fields"]:
		raise RuntimeError("prohibited ticket field changed")
	if before["todos"] != after["todos"] or before["communications"] != after["communications"]:
		raise RuntimeError("assignment or communication side effect")
	if (
		before["comments"] != after["comments"]
		or before["ticket_comments"] != after["ticket_comments"]
		or before["notifications"] != after["notifications"]
	):
		raise RuntimeError("timeline or notification side effect")
	if proposal["ticket_type"] is not None and ticket.get("ticket_type") != proposal["ticket_type"]:
		raise RuntimeError("ticket type not applied")
	if proposal["priority"] is not None and ticket.get("priority") != proposal["priority"]:
		raise RuntimeError("priority not applied")


def _mark_apply_failed(name: str, reason: str) -> None:
	# Raw SQL avoids the generic Approval controller's comment/resume hooks.  The
	# reason is an enum, never an exception string or a ticket value.
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set apply_status='Failed', apply_reason=%s, modified=%s, modified_by=%s
		where name=%s and status='Pending'""",
		(reason, frappe.utils.now_datetime(), frappe.session.user, name),
	)


def _safe_proposal_view(doc, evidence: dict, can_confirm: bool) -> dict:
	return {
		"name": doc.name,
		"ticket": evidence["ticket"],
		"status": doc.status,
		"apply_status": doc.get("apply_status") or "Pending",
		"current": dict(evidence["current"]),
		"proposed": dict(evidence["proposed"]),
		"can_confirm": bool(can_confirm and doc.status == "Pending" and doc.get("apply_status") == "Pending"),
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def get_ticket_triage_proposal(name: str) -> dict:
	"""Return safe controlled values to the named reviewer only."""
	refuse_in_tool_dispatch()
	run, doc, _state, evidence, _proposal = _proposal_context(name, lock=False)
	_require_reviewer(run, evidence, write=False)
	return _safe_proposal_view(
		doc, evidence, can_confirm=frappe.has_permission(TICKET, "write", evidence["ticket"])
	)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def reject_ticket_triage_proposal(name: str) -> dict:
	"""Reject one sidecar proposal.  It never resumes chat or changes a ticket."""
	refuse_in_tool_dispatch()
	run, doc, _state, evidence, _proposal = _proposal_context(name, lock=True)
	_require_reviewer(run, evidence, write=False)
	if doc.status == "Rejected" and doc.decided_by == frappe.session.user:
		return {"ok": True, "status": "Rejected", "idempotent": True}
	if doc.status != "Pending":
		_refuse(_STATE_INVALID)
	changed = frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Rejected', decision=%s, decided_by=%s, decided_at=%s,
			modified=%s, modified_by=%s
		where name=%s and status='Pending'""",
		(
			"Rejected by named reviewer; no ticket change.",
			frappe.session.user,
			frappe.utils.now_datetime(),
			frappe.utils.now_datetime(),
			frappe.session.user,
			name,
		),
	)
	if not changed:
		_refuse(_STATE_INVALID)
	return {"ok": True, "status": "Rejected", "idempotent": False}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def confirm_ticket_triage_proposal(name: str) -> dict:
	"""Apply only type/priority after fresh evidence and side-effect checks."""
	refuse_in_tool_dispatch()
	run, doc, ticket, state, evidence, proposal = _confirm_context(name)
	_require_reviewer(run, evidence, write=True)
	if doc.status == "Approved" and doc.get("apply_status") == "Applied":
		return {"ok": True, "status": "Approved", "idempotent": True}
	if doc.status != "Pending" or doc.get("apply_status") != "Pending":
		_refuse(_STATE_INVALID)
	with impersonate(evidence["reviewer"]):
		ticket = _verify_fresh_proposal(run, state, evidence, proposal, ticket=ticket)
		savepoint = "ticket_triage_apply_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(savepoint)
		try:
			before = _write_guard(ticket)
			if proposal["ticket_type"] is not None:
				ticket.ticket_type = proposal["ticket_type"]
			if proposal["priority"] is not None:
				ticket.priority = proposal["priority"]
			ticket.save()
			ticket.reload()
			after = _write_guard(ticket)
			_assert_write_guard(before, after, ticket, proposal)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			_mark_apply_failed(doc.name, "safety_check_blocked")
			return {"ok": False, "status": "blocked", "reason": "safety_check_blocked"}
	changed = frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Approved', decision=%s, decided_by=%s, decided_at=%s,
			apply_status='Applied', apply_reason='', modified=%s, modified_by=%s
		where name=%s and status='Pending' and apply_status='Pending'""",
		(
			"Confirmed controlled type/priority update by named reviewer.",
			frappe.session.user,
			frappe.utils.now_datetime(),
			frappe.utils.now_datetime(),
			frappe.session.user,
			name,
		),
	)
	if not changed:
		# The ticket write remains inside this transaction.  A loser must roll it
		# back instead of leaving a duplicate or unacknowledged change behind.
		frappe.db.rollback(save_point=savepoint)
		_refuse(_STATE_INVALID)
	return {"ok": True, "status": "Approved", "idempotent": False}

