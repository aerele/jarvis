"""Code-owned hard closure for agents that have no safe runtime path.

The marketplace listing is tenant data. It is useful for display, but it is not
authority to re-open an agent after a server safety close. This module keeps the
closed-agent roster in reviewed code and gives every shared runtime path one
answer.

Deployment runbook: put the site in maintenance or drain state; stop web and
background workers that can hold older Jarvis imports; run ``bench migrate``
from the new checkout; restart all workers; then Apply and verify that the fleet
roster omits every closed agent. A restart only after migrate is too late: an old
worker can hold a confirmation or bearer that new code must revoke.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from typing import Any

import frappe
from frappe import _

CLOSED_STATUS = "Coming Soon"
FENCE_DOCTYPE = "Jarvis Agent Close Fence"
FENCE_NAME_PREFIX = "agent-close:"
PENDING_ACTION_FENCE_FIELD = "agent_close_fences"

_UNAVAILABLE = "This agent is not available yet. It cannot be installed, enabled, scheduled, or run."
_LISTING_UNAVAILABLE = "This agent is not available yet. Its status must stay Coming Soon."
_LEGACY_UNAVAILABLE = "This agent is not available yet. Disable its old installation before you edit it."
_RUN_UNAVAILABLE = "This agent is not available yet. The old run was stopped. No ERP record was changed."
CONFIRMATION_REVOKED = (
	"This confirmation was cancelled during an agent safety update. "
	"Submit the action again if it is still needed."
)

_PENDING_ACTION = "Jarvis Pending Action"
_INSTALLATION = "Jarvis Agent Installation"
_LISTING = "Jarvis Agent Listing"
_RUN = "Jarvis Agent Run"
_SESSION = "Jarvis Chat Session"
_MESSAGE = "Jarvis Chat Message"
_CLOSE_SNAPSHOT_LIMIT = 2_000
_REDIS_SCAN_LIMIT = 2_000

# A legacy confirmation can contain the model's proposed arguments. A close
# settlement must not preserve them in a terminal receipt. This is the only
# result we write for a close-revoked legacy card.
_SAFETY_UPDATE_RECEIPT = {
	"ok": False,
	"reason_code": "safety_update",
	"error": {
		"type": "InvalidConfirmation",
		"message": (
			"This confirmation was cancelled during an agent safety update. "
			"Submit the action again if it is still needed."
		),
	},
}


@dataclass(frozen=True)
class ClosedAgentSpec:
	"""One immutable hard-close contract.

	``slug`` and ``generation`` are protocol values. Do not derive either from a
	listing, installation, or customer setting. The two historic session forms are
	kept with the spec because a deleted Run row must not re-open its old bearer.
	"""

	slug: str
	title: str
	generation: str
	patch: str

	@property
	def fence_name(self) -> str:
		return f"{FENCE_NAME_PREFIX}{self.slug}"

	@property
	def session_prefixes(self) -> tuple[str, str]:
		return (f"agent:agent-{self.slug}:", f"agent:{self.slug}:")


CLOSED_AGENT_SPECS = (
	ClosedAgentSpec(
		slug="reorder-replenishment-operator",
		title="Reorder & Replenishment Operator",
		generation="rro-close-v1",
		patch="jarvis.patches.v2_31_disable_reorder_replenishment_installations",
	),
	ClosedAgentSpec(
		slug="ticket-triage-operator",
		title="Ticket-Triage & Assignment Operator",
		generation="ticket-triage-close-v1",
		patch="jarvis.patches.v2_31_disable_ticket_triage_installations",
	),
)
_CLOSED_BY_SLUG = {spec.slug: spec for spec in CLOSED_AGENT_SPECS}


def closed_agent_spec(agent: str | None) -> ClosedAgentSpec | None:
	"""Return the reviewed close contract for ``agent``, if one exists."""
	return _CLOSED_BY_SLUG.get((agent or "").strip())


def is_closed_agent(agent: str | None) -> bool:
	"""Return whether ``agent`` is closed by reviewed server code."""
	return closed_agent_spec(agent) is not None


def closed_agent_for_session(session_key: str | None) -> ClosedAgentSpec | None:
	"""Find a closed agent from either historic opaque bearer form."""
	key = (session_key or "").strip()
	for spec in CLOSED_AGENT_SPECS:
		if any(key.startswith(prefix) for prefix in spec.session_prefixes):
			return spec
	return None


def refuse() -> None:
	"""Raise the stable public refusal for a closed agent opening action."""
	frappe.throw(_(_UNAVAILABLE), frappe.PermissionError)


def assert_new_install_allowed(agent: str | None) -> None:
	"""Refuse before an installation row can be created."""
	if is_closed_agent(agent):
		refuse()


def assert_listing_status(agent: str | None, status: str | None) -> None:
	"""Keep a closed agent's catalog row in the only safe display state."""
	if is_closed_agent(agent) and (status or "").strip() != CLOSED_STATUS:
		frappe.throw(_(_LISTING_UNAVAILABLE), frappe.PermissionError)


def assert_enabled(agent: str | None, enabled: int | bool) -> None:
	"""Refuse an attempt to enable a closed agent."""
	if is_closed_agent(agent) and frappe.utils.cint(enabled):
		refuse()


def assert_scheduled(agent: str | None, schedule_enabled: int | bool) -> None:
	"""Refuse an attempt to schedule a closed agent."""
	if is_closed_agent(agent) and frappe.utils.cint(schedule_enabled):
		refuse()


def assert_launch_allowed(agent: str | None) -> None:
	"""Refuse before a launch can create a run, session, or conversation."""
	if is_closed_agent(agent):
		refuse()


def assert_session_dispatch_allowed(session_key: str | None) -> None:
	"""Refuse an old closed-agent bearer immediately before generic dispatch."""
	if closed_agent_for_session(session_key):
		frappe.throw(_("This agent is not available yet. No action was taken."), frappe.PermissionError)


def retire_document(doc) -> None:
	"""Make a historic installation inert before a supported retirement save."""
	if not is_closed_agent(doc.get("agent")):
		return
	doc.enabled = 0
	doc.schedule_enabled = 0
	doc.next_run_at = None


def validate_installation(doc) -> None:
	"""Guard every installation ORM save, including Desk and import paths."""
	if not is_closed_agent(doc.get("agent")):
		return
	if doc.is_new():
		refuse()
	if frappe.utils.cint(doc.get("enabled")) or frappe.utils.cint(doc.get("schedule_enabled")):
		frappe.throw(_(_LEGACY_UNAVAILABLE), frappe.PermissionError)
	# A raw legacy edit can otherwise retain a future scheduler wake-up after both
	# active switches were cleared.
	doc.next_run_at = None


def retire_legacy_installation(name: str | None, agent: str | None = None) -> None:
	"""Disable a stale scheduler row without creating a run.

	``agent`` is optional for callers that already have the row. The raw update is
	strictly scoped to a reviewed closed slug, so no normal installation can be
	retired by this helper.
	"""
	spec = closed_agent_spec(agent)
	if not name or not spec:
		return
	frappe.db.sql(
		f"""UPDATE `tab{_INSTALLATION}`
		SET enabled = 0, schedule_enabled = 0, next_run_at = NULL
		WHERE name = %s AND agent = %s""",
		(name, spec.slug),
	)


def delegate_tool_denial(cap: dict, tool: str) -> dict | None:
	"""Return a fatal refusal for every tool call of a closed delegate run."""
	spec = closed_agent_spec(cap.get("agent"))
	if not spec:
		return None
	name = str(tool or "").strip()
	return {
		"run": cap.get("run"),
		"agent": spec.slug,
		"tool": name,
		"fatal": True,
		"message": (
			f"the {spec.title} is unavailable while its server-side closure is active; "
			"no tool call is allowed and no ERP record was changed"
		),
		"chat_message": "This agent is not available yet. No action was taken.",
		"run_error": _RUN_UNAVAILABLE,
	}


def direct_write_refusal(agent: str | None, operation: str, doctype: str) -> str | None:
	"""Return a generic-write refusal for a closed delegate, else ``None``."""
	spec = closed_agent_spec(agent)
	if not spec:
		return None
	return (
		f"agent '{spec.slug}' may not {operation} '{doctype}': {spec.title} is unavailable "
		"and has no generic ERP write authority"
	)


def closed_catalog_values(slug: str | None, values: dict) -> dict:
	"""Return catalog fields forced to the reviewed no-runtime contract.

	This is intentionally applied during registry sync too. A later registry edit
	cannot publish a closed slug or make its tenant listing advertise tools/writes.
	The raw registry remains the source for customer-facing title and description.
	"""
	if not is_closed_agent(slug):
		return values
	return {
		**values,
		"status": CLOSED_STATUS,
		"tools_required": frappe.as_json([]),
		"doctypes_required": frappe.as_json([]),
		"config_keys": frappe.as_json([]),
		"rule_tokens": frappe.as_json([]),
		"advisory_tokens": frappe.as_json([]),
		"writes": frappe.as_json([]),
		"default_schedule": frappe.as_json({"schedule_enabled": 0, "schedule_frequency": "Daily"}),
	}


# --------------------------------------------------------------------------- #
# Combined confirmation close fence
# --------------------------------------------------------------------------- #
def _fence_table_ready() -> bool:
	"""Return whether model sync created the normal close-fence table."""
	try:
		return bool(frappe.db.table_exists(FENCE_DOCTYPE, cached=False))
	except Exception:
		return False


def _patch_logged(spec: ClosedAgentSpec) -> bool:
	"""Return immutable Patch Log proof for one close, failing closed on errors."""
	try:
		return bool(frappe.db.exists("Patch Log", {"patch": spec.patch, "skipped": 0}))
	except Exception:
		return False


def _fence_generation(spec: ClosedAgentSpec, *, lock: bool) -> str | None:
	"""Read one active fence and optionally retain its primary-key lock."""
	if not _fence_table_ready():
		return None
	params = {"name": spec.fence_name, "slug": spec.slug, "user": "Administrator"}
	if lock:
		# The normal DocType's primary key serializes a confirmation and an
		# activation even when this request is the first code path to touch it.
		frappe.db.sql(
			f"""INSERT IGNORE INTO `tab{FENCE_DOCTYPE}`
			(`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
			`agent_slug`, `generation`)
			VALUES (%(name)s, NOW(6), NOW(6), %(user)s, %(user)s, 0, 0, %(slug)s, '')""",
			params,
		)
	rows = frappe.db.sql(
		f"SELECT `generation` FROM `tab{FENCE_DOCTYPE}` WHERE `name`=%(name)s"
		+ (" FOR UPDATE" if lock else ""),
		params,
	)
	if lock and not rows:
		frappe.throw("The agent close fence could not be locked.", frappe.ValidationError)
	stored = str(rows[0][0] or "") if rows else ""
	# Patch Log is the durable authority after a migration. The row is still kept
	# for serialized runtime checks and audit visibility, but a bad editable value
	# must never re-open a close whose non-skippable patch already completed.
	if stored == spec.generation or _patch_logged(spec):
		return spec.generation
	return None


def expected_confirmation_fences(*, lock: bool = False) -> dict[str, str] | None:
	"""Return the exact full map that a generic confirmation must carry.

	There is no per-agent inference for historic generic calls. Until every
	registered close fence is active, no map is safe to stamp and all generic
	confirmations fail closed. This also protects the code-before-migration window.
	"""
	if not _fence_table_ready():
		return None
	result: dict[str, str] = {}
	for spec in CLOSED_AGENT_SPECS:
		generation = _fence_generation(spec, lock=lock)
		if generation != spec.generation:
			return None
		result[spec.slug] = generation
	return {slug: result[slug] for slug in sorted(result)}


def normalize_confirmation_fences(value: Any) -> dict[str, str] | None:
	"""Parse a card's fence map without accepting partial or loose values."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			return None
	if not isinstance(value, dict):
		return None
	result: dict[str, str] = {}
	for key, generation in value.items():
		if not isinstance(key, str) or not isinstance(generation, str):
			return None
		if not key or not generation:
			return None
		result[key] = generation
	return {slug: result[slug] for slug in sorted(result)}


def _required_confirmation_fences() -> dict[str, str]:
	"""Return the immutable full map a post-close generic record must carry."""
	return {spec.slug: spec.generation for spec in sorted(CLOSED_AGENT_SPECS, key=lambda item: item.slug)}


def legacy_confirmation_is_current(record) -> bool:
	"""Check a Redis record's immutable map without opening a DB connection.

	The Redis claim primitive is also used by concurrent low-level callers. It
	must not share a Frappe database connection between those callers. A full map
	could only be minted by :func:`stamp_confirmation_fences` after every fence
	was active; historic records have no map and fail here. The request's final
	dispatch boundary still calls :func:`confirmation_is_current` under the
	durable fence locks.
	"""
	if not record or (record.get("kind") and record.get("kind") != "chat"):
		return False
	return normalize_confirmation_fences(record.get(PENDING_ACTION_FENCE_FIELD)) == _required_confirmation_fences()


def stamp_confirmation_fences() -> dict[str, str] | None:
	"""Return the full current map for a newly parked generic confirmation."""
	return expected_confirmation_fences(lock=True)


def pending_action_fences_ready() -> bool:
	"""Return whether a new Pending Action can persist the shared JSON map."""
	try:
		return bool(
			frappe.db.table_exists(_PENDING_ACTION, cached=False)
			and frappe.get_meta(_PENDING_ACTION).has_field(PENDING_ACTION_FENCE_FIELD)
		)
	except Exception:
		return False


def confirmation_is_current(record, *, lock: bool = False) -> bool:
	"""Return whether a generic confirmation exactly matches all active fences.

	Non-chat durable pending actions are not generic chat confirmations and retain
	their own sealed lifecycle. Every legacy Redis record is generic, so it has no
	``kind`` and must carry the map too.
	"""
	if not record:
		return False
	if record.get("kind") and record.get("kind") != "chat":
		return True
	expected = expected_confirmation_fences(lock=lock)
	if expected is None:
		return False
	return legacy_confirmation_is_current(record) and expected == _required_confirmation_fences()


def _close_snapshot_names() -> list[str]:
	"""Return a bounded snapshot of pending chat cards for migration cleanup."""
	if not frappe.db.table_exists(_PENDING_ACTION, cached=False):
		return []
	names = frappe.db.sql_list(
		f"""SELECT name FROM `tab{_PENDING_ACTION}`
		WHERE kind='chat' AND status='Pending'
		ORDER BY creation, name LIMIT %(limit)s""",
		{"limit": _CLOSE_SNAPSHOT_LIMIT + 1},
	)
	if len(names) > _CLOSE_SNAPSHOT_LIMIT:
		frappe.log_error(
			title="jarvis.pending_action.agent_close_snapshot_capped",
			message=f"cancelled_at_most={_CLOSE_SNAPSHOT_LIMIT}",
		)
		return names[:_CLOSE_SNAPSHOT_LIMIT]
	return names


def cancel_unfenced_chat_cards() -> int:
	"""Cancel a bounded migration snapshot of stale or malformed chat cards."""
	if not frappe.db.table_exists(_PENDING_ACTION, cached=False):
		return 0
	from jarvis.chat.pending_actions._settle import settle
	from jarvis.chat.pending_actions._store import CANCELLED, PENDING, _transition, get_row

	done = 0
	for name in _close_snapshot_names():
		row = get_row(name)
		if row and not confirmation_is_current(row):
			if _transition(name, [PENDING], CANCELLED, reason_code="safety_update") == "ok":
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before settle
				settle(name)
				done += 1
	return done


def settle_revoked_legacy_chat_card(token: str, *, strict: bool = False) -> bool:
	"""Make a close-revoked legacy card terminal in its durable chat row.

	Legacy Redis credentials predate ``Jarvis Pending Action``. Their display row
	is therefore the only reload source for the card. Flip an existing pending
	row in place, never insert a new receipt for a token that was not displayed,
	and erase its pre-action arguments and card payload. ``strict`` is used by the
	migration so a database failure leaves the Redis token for a safe retry;
	runtime callers keep the execution refusal even if a receipt write fails.
	"""
	token = (token or "").strip()
	if not token or not frappe.db.table_exists(_MESSAGE, cached=False):
		return True

	rows = frappe.get_all(
		_MESSAGE,
		filters={"role": "tool", "tool_call_id": token, "tool_status": "pending"},
		fields=["name", "conversation", "tool_name"],
	)
	if not rows:
		return True

	try:
		from jarvis import api

		for row in rows:
			api.persist_tool_receipt(
				row.conversation,
				row.tool_name or "",
				{},
				dict(_SAFETY_UPDATE_RECEIPT),
				action_outcome="cancelled",
				flip_token=token,
				require_pending_flip=True,
			)
	except Exception:
		if strict:
			raise
		frappe.log_error(
			title="jarvis.agent_close.legacy_card_settle_failed",
			message=frappe.get_traceback(),
		)
		return False

	# A concurrent terminal writer is safe too. The only unsafe state is a row
	# that remains pending after the flip-only receipt attempt.
	return not frappe.db.exists(
		_MESSAGE,
		{"name": ["in", [row.name for row in rows]], "tool_status": "pending"},
	)


def revoke_unfenced_legacy_tokens() -> int:
	"""Remove a bounded Redis snapshot of generic tokens that lack the full map."""
	from jarvis.chat import pending_confirm_legacy

	cache = frappe.cache()
	prefix = cache.make_key(pending_confirm_legacy._PREFIX)
	prefix = prefix if isinstance(prefix, bytes) else str(prefix).encode()
	done = 0
	scanned = 0
	for raw_key in cache.scan_iter(match=prefix + b"*", count=200):
		scanned += 1
		if scanned > _REDIS_SCAN_LIMIT:
			frappe.log_error(
				title="jarvis.pending_confirm.agent_close_scan_capped",
				message=f"scanned={scanned - 1} deleted={done}",
			)
			break
		key = raw_key if isinstance(raw_key, bytes) else str(raw_key).encode()
		if not key.startswith(prefix):
			continue
		token = key[len(prefix) :]
		if not token or token == b"open" or b":" in token:
			continue
		try:
			raw = cache.get(raw_key)
			record = pickle.loads(raw) if raw is not None else None
		except Exception:
			record = None
		if confirmation_is_current(record):
			continue
		try:
			decoded = token.decode()
		except UnicodeDecodeError:
			decoded = ""
		if decoded and not settle_revoked_legacy_chat_card(decoded, strict=True):
			# Do not burn this credential while its displayed card could remain
			# confirmable. The next migration run can retry the durable settlement.
			frappe.throw("Could not settle a revoked legacy confirmation card.", frappe.ValidationError)
		cache.delete(raw_key)
		frappe.local.cache.pop(key, None)
		if decoded:
			pending_confirm_legacy._gauge_remove(decoded)
		done += 1
	return done


def activate_close_fence(agent: str | ClosedAgentSpec) -> str:
	"""Durably activate one close fence. The migration commits it first."""
	spec = agent if isinstance(agent, ClosedAgentSpec) else closed_agent_spec(agent)
	if spec is None:
		raise ValueError("unknown closed agent")
	if not _fence_table_ready():
		frappe.throw("The agent close fence table is not ready.", frappe.ValidationError)
	_fence_generation(spec, lock=True)
	frappe.db.sql(
		f"""UPDATE `tab{FENCE_DOCTYPE}`
		SET `agent_slug`=%(slug)s, `generation`=%(generation)s,
			`modified`=NOW(6), `modified_by`=%(user)s
		WHERE `name`=%(name)s""",
		{
			"name": spec.fence_name,
			"slug": spec.slug,
			"generation": spec.generation,
			"user": "Administrator",
		},
	)
	return spec.generation


def run_close_migration(agent: str | ClosedAgentSpec) -> None:
	"""Run one idempotent, non-skippable per-agent retirement migration.

	The fence commit is deliberately the first durable change. If later cleanup
	fails, generic confirmations remain closed and a retry resumes safe cleanup.
	"""
	spec = agent if isinstance(agent, ClosedAgentSpec) else closed_agent_spec(agent)
	if spec is None:
		raise ValueError("unknown closed agent")

	activate_close_fence(spec)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist close fence before cleanup

	# Keep installations, runs, findings, and activity as audit history. Only the
	# active route is removed.
	frappe.db.sql(
		f"""UPDATE `tab{_INSTALLATION}`
		SET enabled = 0, schedule_enabled = 0, next_run_at = NULL
		WHERE agent = %s""",
		(spec.slug,),
	)
	frappe.db.sql(
		f"UPDATE `tab{_LISTING}` SET status = %s WHERE name = %s",
		(CLOSED_STATUS, spec.slug),
	)
	for pattern in spec.session_prefixes:
		frappe.db.sql(f"DELETE FROM `tab{_SESSION}` WHERE session_key LIKE %s", (f"{pattern}%",))

	# The raw retirement changes the desired fleet payload. A non-skippable close
	# must not get a Patch Log row if the durable Apply marker cannot be stored.
	from jarvis.chat.agents_api import _mark_catalog_dirty

	_mark_catalog_dirty(strict=True)

	from jarvis.chat import agent_scheduler

	runs = frappe.get_all(
		_RUN,
		filters={"agent": spec.slug},
		fields=["name", "status", "session_key"],
	)
	for run in runs:
		if run.session_key:
			frappe.db.sql(f"DELETE FROM `tab{_SESSION}` WHERE session_key = %s", (run.session_key,))
	for run in runs:
		if run.status != "running":
			continue
		agent_scheduler.fail_run(run.name, _RUN_UNAVAILABLE)
		terminal = frappe.db.get_value(_RUN, run.name, "status", for_update=True)
		if terminal == "running":
			frappe.throw(
				f"Could not terminalize legacy {spec.title} run {run.name}; retry migration after repair.",
				frappe.ValidationError,
			)

	# Cleanup is bounded and retryable. Runtime checks remain the authority for a
	# token or card that arrives after this finite scan.
	cancel_unfenced_chat_cards()
	revoke_unfenced_legacy_tokens()
