import json
import time
from collections import deque
from contextlib import nullcontext

import frappe
from frappe.utils import strip_html
from frappe.utils.caching import request_cache

from jarvis import audit, telemetry
from jarvis._http import validate_bearer as _validate_bearer
from jarvis._plugin_auth import PluginAuthError, validate_plugin_request
from jarvis._session import impersonate
from jarvis.chat import txn
from jarvis.exceptions import (
	CapabilityDeniedError,
	FeatureDisabledError,
	InvalidArgumentError,
	JarvisError,
	PreviewSandboxLost,
	RunDisarmedError,
	RunHaltedError,
	SensitiveWriteRefusedError,
	WriteRefusedError,
)
from jarvis.permissions import (
	ARMED_MACRO_WRITE_FLAG,
	ARMED_SKILL_OWNER_FLAG,
	has_jarvis_access,
	refuse_in_tool_dispatch,
)
from jarvis.tools._result_guard import enforce_result_budget
from jarvis.tools.registry import _ACCEPTED_PARAMS, dispatch


@frappe.whitelist(allow_guest=True, methods=["POST"])
def call_tool(tool: str, args: dict | str | None = None) -> dict:
	"""Whitelisted entry point for Jarvis tool dispatch.

	Two authentication modes are supported:

	1. **Standard Frappe auth** (external callers): session cookie or
	   ``Authorization: token <api_key>:<api_secret>``. The calling user is
	   whoever Frappe's session resolves to; their permissions are what the
	   tool sees. Guest is rejected.

	2. **Plugin auth** (Jarvis agent plugin, Path A): two custom headers
	   are presented together:

	   - ``X-Jarvis-Token`` - the shared ``agent_token`` secret
	     (proves the request originated inside the agent container)
	   - ``X-Jarvis-Session`` - the agent sessionKey for this conversation

	   The token is validated, then the user is resolved from
	   ``Jarvis Chat Session`` (the row inserted at session-create time maps
	   sessionKey → Frappe user). Dispatch runs under that user via
	   ``frappe.set_user``. The original session user is restored after.

	Returns ``{ok: True, data: ...}`` on success or
	``{ok: False, error: {code, message}}`` on tool-level error. Auth
	failures are reported with the corresponding HTTP status code.
	"""
	# Plugin auth mode - detected by presence of X-Jarvis-Token header.
	# Goes through the C2 hardening pipeline (bearer → session →
	# optional HMAC signature → rate limit) implemented in
	# jarvis._plugin_auth. PluginAuthError carries the correct status
	# code; any other exception bubbles as a 500 (logged by Frappe).
	if _get_header("X-Jarvis-Token"):
		try:
			session_key = validate_plugin_request(_request_body_bytes())
		except PluginAuthError as e:
			frappe.local.response.http_status_code = e.http_status
			return _error(e.code, e.message)

		plugin_user = frappe.db.get_value(
			"Jarvis Chat Session",
			{"session_key": session_key},
			"user",
		)
		if not plugin_user:
			frappe.local.response.http_status_code = 400
			return _error(
				"InvalidArgumentError",
				f"unknown session: {session_key}",
			)
		enabled = frappe.db.get_value("User", plugin_user, "enabled")
		if enabled is None:
			frappe.local.response.http_status_code = 400
			return _error(
				"InvalidArgumentError",
				f"session references unknown user: {plugin_user}",
			)
		# Disabling a user is how access is revoked, but Frappe only ends their
		# browser sessions, not this chat session row: refuse it here.
		if not enabled:
			frappe.local.response.http_status_code = 403
			return _error("PermissionError", f"session user is disabled: {plugin_user}")

		# NOTE: no Jarvis-access role gate on the plugin path. It is
		# machine-authenticated (token/HMAC proves the call came from agent),
		# and `plugin_user` is the real chat user — role-gated when they sent
		# the message (chat/api.py send_message). Delegated runs (agent
		# run-as, approvals resume, File Box) legitimately act as an owner
		# without the role, so a role check here would break them. Per-DocType
		# perms still apply under _dispatch_from_session.

		# C2 stretch (2026-06-16 review): bind session_key -> bench's
		# device_id at session-create time, verify on every call. If the
		# bench has re-paired since this session was created (operator
		# rotation, incident response, etc.) the snapshot won't match
		# the current device_id and we reject. Bounds leaked-session
		# replay to "until the next re-pair."
		#
		# Backwards-compat (two-fold):
		#   1. A row without ``chat_device_id`` (pre-migration row from
		#      before this column was added) passes through.
		#   2. A bench whose Jarvis Chat Session DocType hasn't picked up
		#      the new column yet (pre-bench-migrate state) also passes
		#      through - we tolerate the AttributeError-ish read failure
		#      so call_tool doesn't 500 on the first call after a deploy.
		# Once everyone is migrated the strict check applies uniformly.
		try:
			row_device = (
				frappe.db.get_value(
					"Jarvis Chat Session",
					{"session_key": session_key},
					"chat_device_id",
				)
				or ""
			).strip()
		except Exception:
			row_device = ""
		current_device = (frappe.db.get_single_value("Jarvis Settings", "chat_device_id") or "").strip()
		from jarvis.chat.device import session_device_is_stale

		if session_device_is_stale(row_device, current_device):
			frappe.local.response.http_status_code = 401
			# jarvis #712: this rejection is deliberate (it bounds a leaked
			# session key's replay window to the next re-pair) and MUST NOT
			# self-heal here - that would defeat the check. But left as a bare
			# 401 it was a SILENT permanent outage: no transcript receipt, no
			# realtime push, nothing telling the customer to start over. The
			# turn_handler side (handle_chat_send) mints a fresh session for
			# the NEXT turn on this conversation, so this call_tool rejection
			# is expected to be transient in practice - but the in-flight turn
			# that hit it still needs an honest, visible outcome now.
			tool_call_id = (_get_header("X-Jarvis-Tool-Call-Id") or "").strip() or None
			_notify_stale_pairing(session_key=session_key, tool=tool, tool_call_id=tool_call_id)
			return _error(
				"AuthenticationError",
				"session is bound to a previous device pairing; "
				"the bench has re-paired since this session was issued. "
				"Do not retry this call - it will keep failing. Tell the "
				"user in your reply: this conversation needs a restart, "
				"start a new chat to continue. Then stop.",
			)

		return _dispatch_from_session(plugin_user, session_key, tool, args)

	# Standard Frappe auth path - Guest is rejected; everything else dispatches
	# under the current Frappe session user.
	if frappe.session.user == "Guest":
		frappe.local.response.http_status_code = 401
		return _error("AuthenticationError", "authentication required")

	# Same app-access gate as the plugin path (defense in depth): a logged-in
	# user without Jarvis access can't drive Jarvis tools even by POSTing
	# call_tool directly. Per-DocType perms still apply beneath this.
	if not has_jarvis_access():
		frappe.local.response.http_status_code = 403
		return _error("PermissionError", "you do not have access to Jarvis")

	return _dispatch_current_user(tool, args)


def _dispatch_current_user(tool: str, args: dict | str | None) -> dict:
	return _run_tool(tool, args)


def _dispatch_from_session(
	user: str,
	session_key: str,
	tool: str,
	args: dict | str | None,
) -> dict:
	"""Run the dispatch under ``user``, then attribute the tool call to the
	chat session so the UI sees it.
	"""
	# impersonate is session-safe: a bare frappe.set_user in this HTTP path
	# would gut the caller's cookie session sid + data and log them out.
	from jarvis.chat import agent_run_steps
	from jarvis.tools import _agent_run_ctx, _delegate_capability

	# Expose the caller's session_key to the tool for this dispatch (the LLM
	# never authors it — it is the delegate's opaque bearer, delivered over the
	# HTTPS header). record_agent_run resolves its Jarvis Agent Run from it.
	# R-6: the agent tool-call id, when the plugin sends it (forward-compatible
	# header), keys the out-of-band receipt idempotency. Absent today => no dedupe,
	# but the seq race is still fixed under the conversation lock.
	tool_call_id = (_get_header("X-Jarvis-Tool-Call-Id") or "").strip() or None
	_agent_run_ctx.set_session_key(session_key)
	try:
		with impersonate(user):
			# JF-017 — the delegate capability gate, BEFORE anything is dispatched.
			# When this session resolves to a marketplace-agent run, the only tools it
			# may call are the ones its manifest declared and the bench snapshotted
			# onto the run at launch. The container's tools.allow is configuration
			# (fleet renders it into agent configuration); it is not authorization, so a
			# compromised container/plugin or a leaked run bearer would otherwise
			# reach ANY registered tool the run-as user's Frappe roles permit. Fails
			# closed: a run with no snapshot and no legacy marker is refused outright.
			# Non-delegate sessions (ordinary chat) resolve to None and are untouched.
			# ONE run-row read per plugin tool call: the capability contract is
			# resolved here and handed to both consumers - the gate below and the
			# #1062 step timeline further down, which needs the run's name, owner and
			# status. A non-delegate session resolves to None and both are no-ops.
			cap = _delegate_capability.resolve(session_key)
			denial = _delegate_capability.tool_denial(session_key, tool, cap=cap)
			if denial:
				code = CapabilityDeniedError.__name__
				hint = _hint_for(code, "")
				# The DELEGATE reads this one: the contract vocabulary, and (inside the
				# message, the only field the plugin relays to the model) the fact that
				# retrying can never help because the snapshot is fixed for the run.
				result = _error(code, denial["message"], hint=hint)
				# What was ATTEMPTED is the forensic value of a refusal, so record the
				# args too — scrubbed + truncated by audit.record. Parsed only here,
				# inside the deny branch, so the gate itself stays the first thing that
				# runs and a malformed-args call is still refused on the contract.
				try:
					attempted = _parse_args(args, tool)
				except JarvisError:
					attempted = {}
				audit.record(
					tool=tool,
					args=attempted,
					ok=False,
					error_code=code,
					error_message=denial["message"],
				)
				# The CUSTOMER reads this one. The transcript receipt renders verbatim
				# in chat, so it carries plain language plus the remedy — the contract
				# wording stays in the audit trail and the delegate's envelope above.
				_persist_and_publish_tool_call(
					session_key=session_key,
					tool=tool,
					args=attempted,
					result=_error(code, denial["chat_message"], hint=hint),
					tool_call_id=tool_call_id,
				)
				if denial["fatal"]:
					# This run's contract authorises NOTHING — record_agent_run included
					# — so it can never finish itself. Terminalize it NOW with the real
					# reason instead of leaving it "running" for the 3h stale-run sweep
					# to mislabel as a timeout.
					from jarvis.chat import agent_scheduler

					agent_scheduler.fail_run(denial["run"], denial["run_error"])
				return result
			# Parse args up front so persist_and_publish gets the same
			# dict shape the tool ran against (or the empty dict on a
			# malformed-args rejection).
			try:
				parsed_args = _parse_args(args, tool)
			except InvalidArgumentError as e:
				result = _error("InvalidArgumentError", str(e))
				_persist_and_publish_tool_call(
					session_key=session_key,
					tool=tool,
					args={},
					result=result,
					tool_call_id=tool_call_id,
				)
				return result
			# Pass the already-parsed dict back through _run_tool. _run_tool's
			# _parse_args call is idempotent on dicts (no JSON parse path,
			# legacy-marker strip is also idempotent), so no double-work.
			# Resolve the conversation for this session up front so the
			# confirmation gate can bind a parked write to it.
			conv = frappe.db.get_value("Jarvis Conversation", {"session_key": session_key}, "name")
			# STEP TIMELINE (#1062): the run to file this call's step against, taken
			# from the contract resolved above - BEFORE the tool executes.
			# record_agent_run flips the run off ``running`` from inside the tool, so
			# reading its status afterwards would find nothing for the very call that
			# finishes the run. An ordinary chat session yields None and nothing below
			# does anything.
			step_run = agent_run_steps.step_target(cap)
			started_ms = time.monotonic()
			try:
				result = _run_tool(tool, parsed_args, conversation=conv)
			except Exception as exc:
				# A tool that raised past _run_tool's envelope translation is a real
				# fault; try to leave an honest step saying the attempt happened and
				# failed. Strictly best-effort: on a true 500 Frappe's request handler
				# rolls the whole transaction back and takes this row with it, and
				# committing here to save it would also flush whatever partial writes
				# the raising tool made. The step that MATTERS is the ok=False envelope
				# path below, which is how a tool error normally arrives. The re-raise
				# is untouched either way.
				_record_tool_step(
					step_run,
					tool,
					parsed_args,
					None,
					ok=False,
					started_ms=started_ms,
					error_message=str(exc),
				)
				raise
			# Recorded BEFORE the result budget trims the payload, so a step's row
			# count is the count the tool actually returned, not the truncated one.
			_ok = bool(isinstance(result, dict) and result.get("ok"))
			_record_tool_step(
				step_run,
				tool,
				parsed_args,
				result.get("data") if isinstance(result, dict) else None,
				ok=_ok,
				started_ms=started_ms,
				error_message=(
					((result.get("error") or {}).get("message") or "")
					if (not _ok and isinstance(result, dict))
					else ""
				),
			)
			# Agent-boundary model-facing size cap. ONLY on this (agent session)
			# path - the dashboard builder/desk/external call_tool callers go through
			# _dispatch_current_user and are deliberately uncapped.
			if isinstance(result, dict) and result.get("ok"):
				guarded, event = enforce_result_budget(result["data"], tool=tool)
				if event:
					result["data"] = guarded
					telemetry.record_budget_event(
						tool=tool,
						outcome=event["kind"],
						original_chars=event["original_chars"],
						shown=event["shown"],
						total=event["total"],
					)
			_persist_and_publish_tool_call(
				session_key=session_key,
				tool=tool,
				args=parsed_args,
				result=result,
				tool_call_id=tool_call_id,
			)
			return result
	finally:
		_agent_run_ctx.clear_session_key()
		_agent_run_ctx.take_armed_by_macro()  # belt: never leak an armed marker across dispatches
		_agent_run_ctx.take_armed_by_skill()  # belt: same for the approved-skill-run marker
		_agent_run_ctx.take_request_autorun_applied()  # belt: same for the request-scoped marker


#: The findings writeback narrates ITSELF (``record_agent_run`` records a
#: ``writeback`` step naming the finding count it actually persisted), so the
#: generic per-tool hook below skips it rather than writing a second row for the
#: same act.
_SELF_NARRATING_TOOLS = frozenset({"record_agent_run"})


def _record_tool_step(
	step_run,
	tool: str,
	args,
	data,
	*,
	ok: bool,
	started_ms: float,
	error_message: str = "",
) -> None:
	"""Append one ``tool`` step to a delegate run's timeline (#1062).

	``step_run`` is the ``{name, owner}`` resolved before dispatch, or None when
	the caller is not a delegate - in which case this is a no-op, so ordinary
	chat is untouched. Best-effort throughout: narrating a tool call must never
	be able to fail it.

	On a failure the label stays the humanized ACTION and ``error_message``
	becomes the detail, so the timeline says both what was attempted and why it
	did not work instead of leaving a bare red row the customer cannot act on.
	"""
	if not step_run:
		return
	try:
		from jarvis.chat import agent_run_steps

		if agent_run_steps.bare_tool(tool) in _SELF_NARRATING_TOOLS:
			return
		label, detail = agent_run_steps.humanize_tool_call(tool, args, data if ok else None)
		if not ok:
			detail = agent_run_steps.error_detail(error_message) or detail
		elif agent_run_steps.is_pending_confirmation(data):
			# A gated write returns ok but was only PARKED - the confirmation card is
			# still sitting in front of a human. Narrating it as done would claim a
			# write that may never happen, which is the one lie a timeline must not
			# tell.
			label, detail = agent_run_steps.pending_confirmation_step(label)
		agent_run_steps.record_step(
			step_run.get("name"),
			kind="tool",
			tool=tool,
			label=label,
			detail=detail,
			status="ok" if ok else "error",
			duration_ms=int((time.monotonic() - started_ms) * 1000),
			owner=step_run.get("owner"),
		)
	except Exception:
		try:
			frappe.logger("jarvis.agents").warning(
				f"run-step hook failed for tool {tool}: {frappe.get_traceback()}"
			)
		except Exception:
			pass


_STALE_PAIRING_DEDUPE_TTL_S = 3600


def _notify_stale_pairing(*, session_key: str, tool: str, tool_call_id: str | None = None) -> None:
	"""Make a stale-device-pairing 401 (jarvis #712) visible to the customer
	instead of a silent permanent outage: persist a normal tool-error receipt
	into the conversation transcript and push it over the realtime channel,
	the same way ``_persist_and_publish_tool_call`` does for an ordinary
	dispatched call. Best-effort and NEVER raises - this runs on the reject
	path, so a persistence hiccup here must not turn a clean 401 into a 500.

	Deduped per session_key (cache, ``_STALE_PAIRING_DEDUPE_TTL_S``): every
	tool call the model makes in the SAME broken turn hits this same
	rejection, and without a guard each one would write another identical
	row, burying the transcript. One notice per stale session is enough -
	the model is told in the wire message not to retry, and the next turn on
	this conversation gets a freshly re-paired session from turn_handler.
	"""
	dedupe_key = f"jarvis:stale_pairing_notified:{session_key}"
	try:
		if frappe.cache().get_value(dedupe_key, expires=True):
			return
	except Exception:
		pass  # cache unavailable - fall through and notify anyway
	message = (
		"This conversation's connection is out of date: the assistant was "
		"re-paired after this chat started, so it can no longer run tools "
		"here. Please start a new chat to continue."
	)
	try:
		_persist_and_publish_tool_call(
			session_key=session_key,
			tool=tool,
			args={},
			result=_error("AuthenticationError", message),
			tool_call_id=tool_call_id,
		)
	except Exception:
		frappe.log_error(
			title="call_tool: stale-pairing notice failed",
			message=frappe.get_traceback(),
		)
	try:
		frappe.cache().set_value(dedupe_key, 1, expires_in_sec=_STALE_PAIRING_DEDUPE_TTL_S)
	except Exception:
		pass


def _persist_and_publish_tool_call(
	*,
	session_key: str,
	tool: str,
	args: dict,
	result: dict,
	tool_call_id: str | None = None,
) -> None:
	"""Persist a Jarvis Chat Message (role=tool) and publish a realtime event.

	Best-effort: if no conversation owns this session_key, return silently.
	"""
	conv_name = frappe.db.get_value("Jarvis Conversation", {"session_key": session_key}, "name")
	if not conv_name:
		return
	# T8: an armed-skip write set a request-local provenance marker (consume-once).
	# Label its receipt auto_applied + armed_by_macro so the SPA renders a distinct
	# "ran without confirmation - armed macro X" chip and the row is queryable, instead
	# of a silent Activity-accordion tool row.
	from jarvis.tools import _agent_run_ctx

	# An uncarded auto-run write carries EXACTLY ONE provenance: a macro (armed macro's
	# skip_confirmation), a skill (approved run's skill_autorun), or a request-scoped
	# "confirm all" (request_autorun) - the gate branches are mutually exclusive
	# (macro-first, then skill, then request). Consume all three markers (consume-once) so
	# none leaks onto the next, unrelated call in a reused worker.
	armed_by_macro = _agent_run_ctx.take_armed_by_macro()
	armed_by_skill = _agent_run_ctx.take_armed_by_skill()
	request_applied = _agent_run_ctx.take_request_autorun_applied()
	# A FAILED armed write must still be visible (it ran uncarded and broke - exactly what
	# T8 surfaces): label it "failed" (a red chip) rather than letting it fall into the
	# collapsed Activity row. A request-scoped write has no armer NAME (the user approved
	# it), so it only drives the outcome label, not an armed_by_* field.
	if armed_by_macro or armed_by_skill or request_applied:
		armed_outcome = "auto_applied" if (isinstance(result, dict) and result.get("ok")) else "failed"
	else:
		armed_outcome = None
	persist_tool_receipt(
		conv_name,
		tool,
		args,
		result,
		action_outcome=armed_outcome,
		armed_by_macro=armed_by_macro if armed_outcome else None,
		armed_by_skill=armed_by_skill if armed_outcome else None,
		tool_call_id=tool_call_id,
	)


def _locked_insert_chat_message(
	conv_name: str, fields: dict, *, dedupe_filters: dict | None = None
) -> str | None:
	"""Allocate the next ``seq`` for ``conv_name`` UNDER a ``Jarvis Conversation``
	``FOR UPDATE`` lock and insert one ``Jarvis Chat Message`` with ``fields`` merged
	onto ``{conversation, seq}``. Returns the message name, or ``None`` when
	``dedupe_filters`` already matches a row (nothing inserted).

	MUST be called inside an ``impersonate(conv_owner)`` block that has already
	``commit``ed, so this ``FOR UPDATE`` is the first statement of the transaction
	(REPEATABLE-READ discipline). The caller commits AFTER (releasing the lock) and
	publishes post-commit. This is the ONE ``FOR UPDATE`` + ``MAX(seq)+1`` critical
	section in the codebase - shared by ``persist_tool_receipt`` (inline + confirmed
	receipts) and ``import_announce._post_completion`` (the import auto-tell) so the
	seq-collision guard is never forked (PC-1)."""
	frappe.db.sql(
		"SELECT name FROM `tabJarvis Conversation` WHERE name=%(c)s FOR UPDATE",
		{"c": conv_name},
	)
	if dedupe_filters and frappe.db.exists("Jarvis Chat Message", dedupe_filters):
		return None
	seq = (
		frappe.db.sql(
			"SELECT MAX(seq) FROM `tabJarvis Chat Message` WHERE conversation=%(c)s",
			{"c": conv_name},
		)[0][0]
		or 0
	) + 1
	doc = frappe.get_doc({"doctype": "Jarvis Chat Message", "conversation": conv_name, "seq": seq, **fields})
	doc.flags.jarvis_server_write = True
	doc.insert(ignore_permissions=True)
	return doc.name


def persist_tool_receipt(
	conv_name: str,
	tool: str,
	args: dict,
	result: dict | None,
	*,
	action_outcome: str | None = None,
	armed_by_macro: str | None = None,
	armed_by_skill: str | None = None,
	tool_call_id: str | None = None,
	flip_token: str | None = None,
	overwrite_outcomes: tuple = (),
) -> None:
	"""Write a role=tool Jarvis Chat Message receipt into ``conv_name`` and
	publish the realtime tool:result event, running as the conversation owner so
	DocType perms allow the insert. Shared by the inline model-write path
	(``_persist_and_publish_tool_call``) and the confirmed-write path
	(``confirm_tool`` -> ``dispatch_confirmed``) so a confirmed delete/submit/
	email leaves the same transcript trace the SPA already renders (fixes #7).

	``action_outcome`` marks a row that came from a confirmation card so the SPA
	renders it as an inline receipt chip instead of an Activity-accordion tool
	row: "confirmed" (ran ok), "failed" (confirmed but errored), or "discarded"
	(the user declined - nothing ran, so ``result`` may be None/empty). Ordinary
	inline tool calls pass None and render unchanged.

	R-6 (WP-1d) tool-receipt hardening — the receipt is a PRECIOUS out-of-band fact
	(it must be written even mid-hop, so it stays OUT-of-band with NO epoch fence):
	  * ``seq`` is allocated UNDER the conversation FOR UPDATE lock (shared with the
	    assistant-placeholder + user-message seq, D3 Race 1 / D1 #7) so two
	    concurrent receipts (or a receipt racing the placeholder) never collide on
	    ``MAX(seq)+1``;
	  * ``(conversation, tool_call_id)`` is the durable idempotency key — a duplicate
	    callback for the SAME tool call dedupes instead of double-writing. ``tool_call_id``
	    is null for legacy callbacks that carry no id (no dedupe then, but
	    the seq race is still fixed).

	``flip_token`` (PR 1 - action-card overhaul): when set, a matching PENDING
	action-row (tool_call_id=flip_token, tool_status='pending') is FLIPPED IN PLACE
	into this receipt (a real UPDATE, NOT the dedupe-insert - which would no-op on the
	matching row), so the pre-action card and the post-action receipt are ONE durable
	row. Falls back to the normal INSERT when there is no pending row to flip (a
	directly-minted token, a File-Box auto-apply, or a token parked before this
	shipped) - exactly one terminal row per token either way.

	``overwrite_outcomes``: with ``flip_token``, also re-flip that token's row when it
	already carries one of these chips (a pending-action settle whose executed or
	failed outcome must replace a sweep's mislabelled ``cancelled``/``superseded``)."""
	result = result or {}
	# discarded/cancelled/superseded/expired executed NOTHING; unknown/partial's
	# write state is UNVERIFIED (an interrupted or mid-dispatch outcome) - none of
	# these get envelope_ok's binary completed/error classification. The chip
	# renders off action_outcome and tool_status stays empty (a valid Select
	# option) either way.
	no_write = action_outcome in (
		"discarded",
		"cancelled",
		"superseded",
		"expired",
		"unknown",
		"partial",
	)
	if no_write:
		status = ""
	else:
		status = "completed" if result.get("ok") else "error"

	# Entity stamping (org wiki): which doc this call touched, so wiki nudges
	# can read a turn's entities off the receipt rows. Lazy + guarded: a
	# missing/broken entities module must never break receipts. Skipped on
	# no_write - either nothing ran, or whether it ran is unverified.
	ref_doctype = ref_name = None
	if not no_write:
		try:
			from jarvis.chat.entities import refs_from_tool

			# refs_from_tool expects the tool's raw data, not the {ok, data} envelope.
			ref_doctype, ref_name = refs_from_tool(
				args, result.get("data") if isinstance(result, dict) else None
			)
		except Exception:
			ref_doctype = ref_name = None

	# Run as the conversation owner so DocType perms allow it. impersonate is
	# session-safe (a bare frappe.set_user in this HTTP path would gut the
	# caller's cookie session sid + data and log them out).
	conv_owner = frappe.db.get_value("Jarvis Conversation", conv_name, "owner")
	with impersonate(conv_owner):
		# R-6: take the conversation FOR UPDATE (canonical rank 2, out-of-band, NO
		# epoch fence) so the seq allocation + the (conversation, tool_call_id)
		# dedupe are one serialized critical section — no receipt collides on seq
		# with a concurrent receipt or the assistant placeholder, and a duplicate
		# callback for the same tool call is a no-op. Commit-first so the FOR UPDATE
		# is the first statement (REPEATABLE-READ discipline).
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- fresh txn before row lock
		# PR-1 flip-else-insert: when a confirm/discard/approve passes ``flip_token``,
		# turn the PENDING action-row parked at park (tool_call_id=flip_token,
		# tool_status='pending') INTO this receipt IN PLACE - one durable "action row"
		# for the whole lifecycle, so there is no orphan pending row and no second
		# receipt row. A real UPDATE, NOT the dedupe-insert (which would no-op on the
		# matching row). Falls through to the INSERT when there is no pending row to
		# flip (a directly-minted token, a File-Box auto-apply, or a token parked
		# before this shipped); the exactly-one-terminal-row invariant holds either way.
		msg_name = None
		if flip_token:
			frappe.db.sql(
				"SELECT name FROM `tabJarvis Conversation` WHERE name=%(c)s FOR UPDATE",
				{"c": conv_name},
			)
			existing = frappe.db.get_value(
				"Jarvis Chat Message",
				{"conversation": conv_name, "tool_call_id": flip_token, "tool_status": "pending"},
				"name",
			)
			if not existing and overwrite_outcomes:
				existing = frappe.db.get_value(
					"Jarvis Chat Message",
					{
						"conversation": conv_name,
						"tool_call_id": flip_token,
						"action_outcome": ["in", list(overwrite_outcomes)],
					},
					"name",
				)
			if existing:
				frappe.db.set_value(
					"Jarvis Chat Message",
					existing,
					{
						"tool_args": frappe.as_json(args),
						"tool_result": frappe.as_json(result) if result else None,
						"tool_status": status,
						"action_outcome": action_outcome or None,
						"armed_by_macro": armed_by_macro or None,
						"armed_by_skill": armed_by_skill or None,
						"ref_doctype": ref_doctype,
						"ref_name": ref_name,
						# Minimize retained pre-action data: the receipt renders from the
						# outcome now, so the parked card snapshot is no longer needed.
						"pending_card": None,
						"content": f"{tool} → {action_outcome or status}",
					},
					update_modified=True,
				)
				msg_name = existing
		if msg_name is None:
			msg_name = _locked_insert_chat_message(
				conv_name,
				{
					"role": "tool",
					"tool_name": tool,
					"tool_args": frappe.as_json(args),
					"tool_result": frappe.as_json(result) if result else None,
					"tool_status": status,
					"tool_call_id": tool_call_id or flip_token or None,
					"action_outcome": action_outcome or None,
					"armed_by_macro": armed_by_macro or None,
					"armed_by_skill": armed_by_skill or None,
					"ref_doctype": ref_doctype,
					"ref_name": ref_name,
					"content": f"{tool} → {action_outcome or status}",
				},
				# ``(conversation, tool_call_id)`` dedupe — a duplicate callback for the
				# SAME tool call is a no-op (R-6). Keyed on the agent call id OR the flip
				# token, so a re-confirm after a fallback insert also dedupes.
				dedupe_filters=(
					{"conversation": conv_name, "tool_call_id": tool_call_id or flip_token}
					if (tool_call_id or flip_token)
					else None
				),
			)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist receipt before reply
		if msg_name is None:
			# Duplicate callback for the same tool call — already recorded (R-6 idempotent).
			return
		publish_realtime_tool_result(
			user=conv_owner,
			conversation_id=conv_name,
			tool_message_id=msg_name,
			tool_call_id=tool_call_id or flip_token or None,
			tool_name=tool,
			args=args,
			result=result,
			status=status,
			action_outcome=action_outcome,
		)
		# Generation: if the tool produced a file artifact (download_pdf,
		# export_excel, …), attach it to the in-flight assistant message's
		# canvas field + publish a canvas event so it renders inline - the
		# same surface the agent's own canvas files use.
		_maybe_attach_artifact(conv_name, conv_owner, result)


def persist_pending_action(
	conv_name: str, tool: str, preview: dict | None, token: str, expires_at: int | None
) -> None:
	"""Write a durable role=tool PENDING row for a parked gated write (PR 1 of the
	action-card overhaul) - the pre-action twin of ``persist_tool_receipt``. The card
	then rides the message pipeline, so a reload always shows it (reload-reliable),
	instead of depending only on the best-effort ``action:pending`` push + the
	list-pending resync (the single-shared-query fragility the overhaul removes).

	Stores ONLY the perm-filtered card (``preview['card']`` - already secret-masked by
	build_card) + a legacy token's expiry. NEVER stores raw ``tool_args``: an unconfirmed (maybe-to-
	be-discarded) row must not expose unmasked args via ``get_conversation``; args are
	stamped only when the row flips to a receipt (persist_tool_receipt). The card's
	store (``Jarvis Pending Action``, or the legacy Redis token) stays the SOLE
	execution authority - this row is display-only.

	Idempotent on ``(conversation, token)`` via the shared FOR UPDATE + dedupe (a
	pending action's row is already inserted by ``park``, so this is then a no-op).
	RAISES on failure so the gate FAIL-CLOSES (rolls the token back): a token with no
	delivery row is exactly the invisible-card defect this overhaul kills, and the
	single-flight guard would treat the orphan as a live card and wedge the retry."""
	# Commit-first so the FOR UPDATE is the transaction's first statement
	# (REPEATABLE-READ discipline), matching persist_tool_receipt.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- fresh txn before row lock
	_insert_pending_row(conv_name, tool, preview, token, expires_at)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- visible to other workers


def _insert_pending_row(
	conv_name: str, tool: str, preview: dict | None, token: str, expires_at: int | None
) -> str | None:
	"""The pending row insert without any commit: ``persist_pending_action``'s core,
	and ``pending_actions.park``'s ``display_row`` (same transaction as the card, so
	the two land or roll back together). None when the conversation can't host a row
	or the row already exists. ``expires_at`` None (a pending action) = never expires."""
	# Convert a legacy token's epoch expiry to a SITE-timezone datetime the same way
	# the codebase does (now_datetime + remaining seconds), so the row's countdown is
	# correct regardless of an OS-vs-site timezone gap (Frappe Datetime fields are
	# stored in the site timezone, not the OS timezone).
	expires = None
	if expires_at is not None:
		remaining = max(0, int(expires_at) - int(time.time()))
		expires = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=remaining)
	card = preview.get("card") if isinstance(preview, dict) else None
	conv_owner = frappe.db.get_value("Jarvis Conversation", conv_name, "owner")
	if not conv_owner:
		# Not a real, hostable conversation (a synthetic/stray id, or a session-
		# resolution artifact) - it cannot host a Jarvis Chat Message row. Treat it
		# like a conv-less park: skip the row and let the owner-scoped list-pending
		# backstop deliver the card. A genuine insert FAILURE on a REAL conversation
		# still raises below, so the gate fail-closes (rolls the token back) as designed.
		return None
	with impersonate(conv_owner):
		return _locked_insert_chat_message(
			conv_name,
			{
				"role": "tool",
				"tool_name": tool,
				"tool_status": "pending",
				"tool_call_id": token,
				"pending_card": frappe.as_json(card) if card is not None else None,
				"expires_at": expires,
				"content": f"{tool} → pending",
			},
			# (conversation, token) dedupe: a re-park of the same token is a no-op.
			dedupe_filters={"conversation": conv_name, "tool_call_id": token},
		)


def cancel_pending_action_rows(conversation: str) -> None:
	"""Flip every still-PENDING action-row in ``conversation`` to a terminal
	'cancelled' receipt (PR 1). Called from the stop-run / armed-macro-stop sweep so a
	stopped run's parked card does NOT linger as a dangling 'pending' row - which would
	otherwise show as a stale (soon 'expired') card on reload while its Redis token is
	already swept. Best-effort per row; the flip runs as the conversation owner via
	persist_tool_receipt (nothing executed -> no_write -> tool_status='').
	A pending action's row is left to its own settle (the PA sweep flipped the cards
	it cancelled; an executing one must not show "cancelled")."""
	rows = frappe.get_all(
		"Jarvis Chat Message",
		filters={"conversation": conversation, "role": "tool", "tool_status": "pending"},
		fields=["tool_call_id", "tool_name"],
	)
	tokens = tuple(r.tool_call_id for r in rows if r.tool_call_id)
	owned_by_pa = set()
	if tokens and frappe.db.table_exists("Jarvis Pending Action"):
		owned_by_pa = set(
			frappe.db.sql_list(
				"SELECT name FROM `tabJarvis Pending Action` WHERE name IN %(t)s", {"t": tokens}
			)
		)
	for r in rows:
		if not r.tool_call_id or r.tool_call_id in owned_by_pa:
			continue
		try:
			persist_tool_receipt(
				conversation,
				r.tool_name or "",
				{},
				None,
				action_outcome="cancelled",
				flip_token=r.tool_call_id,
			)
		except Exception:
			frappe.log_error(
				title="cancel_pending_action_rows: flip failed",
				message=frappe.get_traceback(),
			)


def _maybe_attach_artifact(conv_name: str, user: str, result: dict) -> None:
	"""Attach a tool-produced file artifact ({file_url, filename, …}) to the
	current assistant message's ``canvas`` field and publish a canvas event so
	the chat renders it (PDF/image inline, xlsx/other as a download card)."""
	if not isinstance(result, dict) or not result.get("ok"):
		return
	data = result.get("data")
	if not isinstance(data, dict):
		return
	file_url = data.get("file_url")
	filename = data.get("filename") or data.get("file_name")
	if not file_url or not filename:
		return

	from jarvis.chat import canvas as canvas_mod

	typ = canvas_mod._type_for(filename)
	item = {
		"name": filename,
		"title": data.get("title") or canvas_mod._title_for(filename, None, typ),
		"type": typ,
		"file_url": file_url,
	}
	# Surface any degrade notes (e.g. a dropped chart, a letterhead that wasn't
	# found) DETERMINISTICALLY on the card — otherwise a degraded document looks
	# identical to a clean one and the user only learns of it if the model happens
	# to narrate it in prose. Kept as a durable card caption (the receipt-chip
	# convention: state as data, never prose).
	notes = data.get("notes")
	if isinstance(notes, list) and notes:
		item["notes"] = [str(n) for n in notes][:10]
	MSG = "Jarvis Chat Message"

	def _write_canvas() -> tuple[str, list] | None:
		"""Row-locked read-append-write, replayable: two file tools finishing together
		(or this call racing the pump's own canvas write) must not lose either card to a
		plain read-then-blind-write, which raises nothing and just overwrites, no 1020 to
		catch. No commit of our own here (policy: never a mid-request commit to dodge this
		race): the only caller, persist_tool_receipt (api.py:723, verified the sole call
		site), already committed at its own line 704 with nothing but a realtime publish
		(no DB access) in between, and a replay's own rollback already starts the next
		attempt's transaction fresh, so this unit's first statement, the get_all below, is
		already the first read of a clean transaction either way.
		"""
		# Prefer the in-flight (streaming) assistant message; fall back to the latest.
		rows = frappe.get_all(
			MSG,
			filters={"conversation": conv_name, "role": "assistant", "streaming": 1},
			order_by="seq desc",
			limit=1,
			pluck="name",
		) or frappe.get_all(
			MSG,
			filters={"conversation": conv_name, "role": "assistant"},
			order_by="seq desc",
			limit=1,
			pluck="name",
		)
		if not rows:
			return None
		msg_name = rows[0]
		existing = frappe.db.get_value(MSG, msg_name, "canvas", for_update=True)
		items = frappe.parse_json(existing) if existing else []
		if not isinstance(items, list):
			items = []
		if any(i.get("file_url") == file_url for i in items):
			return None  # already attached (dedupe) - nothing changed, nothing to publish
		items.append(item)
		# update_modified=False: an artifact attach must never trip check_if_latest for
		# a concurrent send/pump write holding this same reply row.
		frappe.db.set_value(MSG, msg_name, "canvas", frappe.as_json(items), update_modified=False)
		return msg_name, items

	won = txn.replay_on_conflict(_write_canvas, label=f"attach artifact {conv_name}:{file_url}")
	if won is None:
		return
	msg_name, items = won
	# Publish AFTER the winning commit (never inside the unit): a replay must not
	# publish twice, and a realtime event for a card the transaction later loses
	# would be worse than none.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before realtime publish
	frappe.publish_realtime(
		"jarvis:event",
		{"kind": "canvas", "conversation_id": conv_name, "message_id": msg_name, "items": items},
		user=user,
	)


def publish_realtime_tool_result(
	*,
	user: str,
	conversation_id: str,
	tool_message_id: str,
	tool_name: str,
	args: dict,
	result: dict,
	status: str,
	action_outcome: str | None = None,
	tool_call_id: str | None = None,
) -> None:
	"""Wrapper around frappe.publish_realtime so tests can mock at this seam.

	``action_outcome`` (confirmed/discarded/failed) rides along so the SPA can
	render the row as a receipt chip immediately, without waiting for a full
	transcript reload. ``tool_call_id`` is the id the row was saved with, so the
	SPA can bind a live report to the reply it belongs to (the "Reports
	consulted" card) the same way the saved rows do."""
	frappe.publish_realtime(
		"jarvis:event",
		{
			"kind": "tool:result",
			"conversation_id": conversation_id,
			"tool_message_id": tool_message_id,
			"tool_call_id": tool_call_id,
			"tool_name": tool_name,
			"args": args,
			"result": result,
			"status": status,
			"action_outcome": action_outcome,
		},
		user=user,
	)


# P0d (decision 16): a batch key the model sends ALONGSIDE single-record args
# (``create_doc({doctype, values, docs: []})``) means "no batch" - not "a
# batch of nothing" - but ~20 tools route any non-None batch key straight to
# batch mode and fail on the empty list. See the h400jb1fdk File Box incident.
_BATCH_KEYS = ("docs", "names", "updates", "messages")

# Tools whose signature carries a batch key ALONGSIDE the single-record form
# (a ``doctype`` param), derived from the registry's own accepted-params map
# so this can't drift from the tool signatures. This is also what naturally
# excludes the two dual-form-shaped exceptions: ``create_docs`` (batch only -
# no ``doctype`` param at all; keeps its own clear "docs must be non-empty"
# error) and ``get_my_access`` (``docs`` is a query list of doctype/name
# pairs to check access on, not a batch of writes - also no ``doctype`` param).
_DUAL_FORM_TOOLS = frozenset(
	name
	for name, params in _ACCEPTED_PARAMS.items()
	if "doctype" in params and params.intersection(_BATCH_KEYS)
)


def _parse_args(args: dict | str | None, tool: str | None = None) -> dict:
	"""Decode the args input into a dict + strip legacy identity markers +
	normalise an empty batch array for a dual-form tool (P0d).

	Raises ``InvalidArgumentError`` (a JarvisError subclass) on JSON
	parse failure - that mirrors what tools raise for malformed input
	and lets ``_run_tool`` translate it via the same path. The previous
	shape returned either a dict OR an error envelope dict, which made
	every caller branch on the result type.

	Mutates ``args`` IN PLACE when it is already a dict (only reassigning it
	for a JSON-string/falsy input), so every downstream reader sharing the
	same reference - persistence, the step timeline, held_writes, the gate,
	preview, seal, dispatch - sees the identical normalised call. Runs before
	all of them (``_run_tool``), which is what keeps the transcript and the
	executed args in agreement.

	``tool`` is optional (the delegate-denial call site parses args only for
	an audit row, before ``tool`` gating even runs) - normalisation simply
	no-ops without it, since there is nothing to key the dual-form check on.
	"""
	if isinstance(args, str):
		try:
			args = json.loads(args)
		except json.JSONDecodeError as e:
			raise InvalidArgumentError(f"args is not valid JSON: {e}")
	args = args or {}
	if isinstance(args, dict):
		# Defensive: strip LLM-hallucinated "_user" / "_session" fields. The
		# old MCP design used them as in-band identity carriers; Path A moved
		# identity to HTTPS headers. If the LLM has been trained on the older
		# convention it may emit these even though our tool schemas don't list
		# them - dropping them silently keeps such calls dispatching cleanly.
		for legacy in ("_user", "_session", "_session_key"):
			args.pop(legacy, None)
		if tool in _DUAL_FORM_TOOLS:
			tool_params = _ACCEPTED_PARAMS.get(tool, frozenset())
			for key in _BATCH_KEYS:
				# Only a key THIS tool actually declares - never touch one it
				# doesn't accept (dispatch's own param filter would drop it
				# anyway, but this keeps the persisted/audited args honest).
				if key in tool_params and args.get(key) in ([], ""):
					del args[key]
	return args


# Mutating tools: audited on every call, and the only tools that accept
# ``preview`` (a dry-run with every DB write rolled back).
_WRITE_TOOLS = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"submit_doc",
		"cancel_doc",
		"amend_doc",
		"delete_doc",
		"run_method",
		"run_import",
		"apply_workflow_action",
		"send_email",
		"add_comment",
		"update_comment",
		"share_doc",
		"unshare_doc",
		"assign_to",
		"unassign_from",
		"add_tag",
		"remove_tag",
		"follow_document",
		"unfollow_document",
		"attach_to_doc",
		"create_dashboard_chart",
		"create_dashboard",
		# #884 chat/builder dashboard delivery: inserts/updates a Jarvis Dashboard
		# mid-turn. Audited but NEVER gated (like record_agent_run below) - the
		# build must land without a card for the builder canvas to render it;
		# scope is pinned to User server-side and the row is one-per-conversation.
		"save_dashboard",
		"create_custom_skill",
		"update_wiki",
		# Audited but NOT gated (see _GATED_WRITES comment below):
		# download_pdf/export_excel both insert a private, unattached File doc
		# - real DB writes that need an audit trail, not a
		# confirmation card (audit-findings.md F24/F25). record_agent_run is the
		# delegate's Phase-3 findings writeback: it inserts Jarvis Agent Run/Finding
		# rows deterministically (validated, coverage-scoped) from a detached agent
		# turn where nobody can click a confirm card, so it is audited but NEVER
		# gated - the human review happens on the Findings board, not a card.
		# record_app_wiki is the Custom App Learning scribe delegate's wiki
		# writeback: like record_agent_run it lands its declared output (wiki
		# pages) from a detached, unattended turn through a reviewed server funnel
		# (apply_extracted_page_updates + the controller sanitizer), so it is
		# audited but NEVER gated (and NOT the gated update_wiki card path).
		# finish_app_learning_run is the scribe delegate's TERMINAL run finalizer:
		# it flips the bound Jarvis Agent Run to completed (with the pages tally
		# record_app_wiki stamped) from a detached turn — audited but NEVER gated
		# (no card to click), like record_agent_run.
		# save_agent_dashboard inserts the delegate's Jarvis Dashboard document
		# (a real DB write of customer-derived audit output) from the same
		# detached turn as record_agent_run beside it — audited, never gated.
		"download_pdf",
		"export_excel",
		# export_query / report_pdf / export_document all insert a private File
		# (export_query + report_pdf via the shared save_export_file seam,
		# export_document via its own save_file) - the same File-write class as
		# download_pdf/export_excel, so they are audited (never gated: no card, the
		# artifact is the point). export_query in particular is the highest-throughput
		# bulk-export path (server-side, up to the row ceiling, never transiting the
		# visible transcript), so a "who exported what" audit row is load-bearing.
		"export_query",
		"report_pdf",
		"export_document",
		"record_agent_run",
		"record_app_wiki",
		"finish_app_learning_run",
		"save_agent_dashboard",
	}
)
# Writes whose park preview is a sandboxed trial run (``_run_preview``). Only the
# build-from-args create / update tools: the person edits those values on the card,
# so the trial is the point. submit / cancel / amend / delete are NOT trial-run at
# park (round-2 R2-2): a trial fires their on_submit / on_cancel hooks, and what a
# hook sends (email, calls to other systems, live notifications, error log rows)
# cannot be rolled back. They park a described card with a consequence line, and a
# failure shows at Confirm. run_method is never trial-run (its target's inline side
# effects would fire unconfirmed).
_PREVIEWABLE = frozenset({"create_doc", "create_docs", "update_doc"})
# The verbs that park a described card (see above), and the note it carries.
_DESCRIBED_VERBS = frozenset({"submit_doc", "cancel_doc", "amend_doc", "delete_doc"})
_DESCRIBED_VERB_NOTE = (
	"Nothing has run yet: this card describes the action, which runs only when you confirm. "
	"Any problem with it shows at that point."
)
# Writes that MUST get a human confirmation before executing (issue #186).
# As of the action-card overhaul (design A1) the light collaboration mutators
# (add_comment/update_comment/add_tag/remove_tag/attach_to_doc/unshare_doc/
# unassign_from) ARE gated too - "every real change asks" - so they are members
# below; only genuinely non-mutating tools (dashboard-create, follow/unfollow,
# exports, detached write-backs) stay ungated. share_doc/assign_to were the first
# of that widening (their own descriptors promise "ALWAYS confirm" -
# audit-findings.md F17/F20/F23). None of these is in _PREVIEWABLE/_DRY_RUN_ON_PARK:
# no side-effect-free sandbox preview is meaningful for a share grant, a comment, or
# a ToDo+notification email, so they fall through to the described-intent park path
# (like send_email) rather than a sandboxed dry-run.
_GATED_WRITES = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"submit_doc",
		"cancel_doc",
		"amend_doc",
		"delete_doc",
		"run_method",
		"run_import",
		"apply_workflow_action",
		"send_email",
		"create_custom_skill",
		"update_wiki",
		"share_doc",
		"assign_to",
		"call_connector",
		# Light collaboration writes (design A1): each mutates a real ERP record
		# (a comment, a tag, an attachment, a share/assignment revocation), so every
		# one now asks in ordinary chat too - "every real change goes through the
		# gate". Their BULK forms already parked (the is_write + _is_bulk_call entry
		# above); this adds the SINGLE forms. None is destructive, so all fall in
		# _COVERED (they run uncarded in an armed macro / approved skill), not _BRAKE.
		# build_card has no bespoke shape for them (like call_connector) - they take
		# the described-intent park preview and render the summary fallback.
		"add_comment",
		"update_comment",
		"add_tag",
		"remove_tag",
		"attach_to_doc",
		"unshare_doc",
		"unassign_from",
	}
)
# #493: the agent-facing wiki surface, refused wholesale when the operator has
# switched "Enable Business Wiki" off. Deliberately just the two tools the issue
# names - record_app_wiki (the app-learning scribe's audited-not-gated writeback)
# is a different feature's pipeline and is out of scope here.
_WIKI_TOOLS = frozenset({"read_wiki", "update_wiki"})
# File Box unattended wiki write-back (feat/filebox-skills-wiki): a file_box run's
# update_wiki is routed through the append-only, provenance-FENCED funnel
# (jarvis.chat.wiki.apply_extracted_page_updates), NOT the raw update_wiki tool -
# which defaults scope=Org + ignore_permissions and stamps the human-curated
# "tool" kind, so a raw write could clobber a curated page. FILE_BOX_WIKI_KIND is
# the provenance stamped on every file-box page; it MUST start with
# FILE_BOX_WIKI_FENCE (the fence prefix that lets a file-box run refresh only its
# OWN pages and REFUSES a slug colliding with any human/other page) and MUST NOT be
# a _HUMAN_SOURCE_KINDS member ("file-box:" is neither). Mirrors record_app_wiki's
# source=/provenance_prefix= pair.
FILE_BOX_WIKI_KIND = "file-box:"
FILE_BOX_WIKI_FENCE = "file-box"
# Approval Request `source` value for a HELD file-box wiki write-back proposal.
# A wiki write from an unattended file-box run is no longer applied at drop time;
# it is parked as a Pending row a Jarvis reviewer (the dropper too, with a reviewer
# role) approves before it lands. Distinct from "File Box" (document-classification
# approvals) so it routes to the reviewer lane and off the customer decide board.
FILE_BOX_WIKI_SOURCE = "File Box Wiki"
# Irreversible/consequential subset - gated even when a user has auto-apply
# on (Task 4 uses this; define it here so the sets live together).
_DESTRUCTIVE = frozenset(
	{"delete_doc", "cancel_doc", "amend_doc", "send_email", "apply_workflow_action", "run_import"}
)
# Writes that auto-apply may fast-path without a confirmation click. Strictly
# the reversible create/update pair, per spec. submit_doc, run_method and every
# _DESTRUCTIVE tool ALWAYS park even with auto-apply on: run_method's
# default-unrestricted allowlist under auto-apply + a prompt injection would be
# an unconfirmed arbitrary whitelisted method call, so it never fast-paths.
_AUTO_APPLYABLE = frozenset({"create_doc", "update_doc"})


def _gating_badge(tool: str) -> str:
	"""Safety badge for the in-product capability catalog.

	Classifies a tool's DEFAULT single-call confirmation behaviour, derived from
	the SAME frozensets that gate in ``_run_tool`` so a badge can never disagree
	with the default gate. NOT an absolute guarantee: the File Box fast-path, an
	armed macro (skip_confirmation), an approved skill run (skill_autorun), and a
	request-scoped "confirm all" (request_autorun), and a per-chat auto mode
	(auto_mode, chosen with the chat's first message) can run some tools uncarded -
	the catalog legend says so. (Admin Auto-Apply was removed.) Total over any string.
	"""
	if tool not in _WRITE_TOOLS:
		return "reads_only"
	if tool in _DESTRUCTIVE or (tool in _GATED_WRITES and tool not in _AUTO_APPLYABLE):
		return "always_asks"
	if tool in _AUTO_APPLYABLE:
		return "asks_to_approve"
	return "writes_directly"


# The BRAKE (design §3 A3, "Option A"): the always-ask set. delete_doc,
# cancel_doc, amend_doc, create_custom_skill, and call_connector ALWAYS park a
# confirmation card - in ordinary chat, inside an armed skip-macro, AND inside an
# approved "Approve & run" skill. The irreversible ERPNext trio is destructive;
# create_custom_skill is a consequential meta-write (a skill that writes another
# skill); a connector WRITE's per-action reversibility is opaque to the bench (its
# action can be anything the third-party service defines), so no armed mode may
# fire it uncarded. A connector SAFE READ (user-allowed, read-only, non-destructive)
# is the one exception and is NOT decided here: _run_tool's action-aware carve-out
# (_connector_call_is_safe_read) runs a safe read BEFORE the gate block, so a read
# never force-stops an armed run - the brake governs only connector WRITES.
_BRAKE = frozenset(
	{
		"delete_doc",
		"cancel_doc",
		"amend_doc",
		"create_custom_skill",
		"call_connector",
	}
)
# The unified COVERED set (design §3 A4): every gated write that is NOT a brake
# runs uncarded in BOTH armed modes - an owner-armed macro (skip_confirmation) and
# an approved "Approve & run" skill (skill_autorun). Deriving it as
# ``_GATED_WRITES - _BRAKE`` makes the two modes ONE rule that can never drift again
# (they previously diverged on create_custom_skill, a real bug the action-card
# overhaul fixes) and keeps the partition invariant (COVERED and BRAKE disjoint,
# together == _GATED_WRITES) true by construction. FAIL-CLOSED signal for a FUTURE
# gated tool: adding a tool to _GATED_WRITES auto-covers it in the armed modes, so
# the pinned membership test (test_covered_is_the_exact_expected_set) goes RED until
# a human consciously confirms the new tool may auto-run - or files it in _BRAKE.
# run_method is covered (gates in ordinary chat; skips only inside an armed macro /
# approved skill). Bulk covered writes skip too; the F16 over-size cap still bounces
# an oversized batch.
_COVERED = _GATED_WRITES - _BRAKE
# Back-compat aliases: the gate branches and the existing suites refer to the
# per-mode names; both modes now point at the SAME unified sets so they cannot drift
# (design §3 A4 - the old macro/skill divergence on create_custom_skill is gone).
_ARMED_SKIP_COVERED = _COVERED
_ARMED_SKIP_NEVER = _BRAKE
_SKILL_AUTORUN_COVERED = _COVERED
_SKILL_AUTORUN_NEVER = _BRAKE
# Sliding-TTL horizon for an approved run: the auto-run branch runs a covered write
# uncarded only while the LAST covered write (skill_autorun_at, which slides forward
# on each success) is within this window. It must comfortably EXCEED the longest idle
# gap between two consecutive covered writes - a model round-trip + latency + a
# non-covered intervening step - which is minutes, so 900s (matching the confirm
# token TTL) is ample. A stranded flag (worker died -> writes stop -> timestamp
# freezes) therefore falls out of the window within the TTL and is cleared by the
# reaper (task #42). This TTL bounds a normal idle gap, NOT a runaway - that is
# bounded by the destructive carve-out, hard-stop-on-error, the Halt cancel-gate,
# and per-skill un-arm.
_SKILL_AUTORUN_IDLE_S = 1800
# Sliding-TTL horizon for a request-scoped "confirm all" (design Layer B). Same
# rationale + value as the skill run's: it bounds a normal idle gap between two
# covered writes of ONE request's fan-out, so a finished request (or a dead worker)
# falls out of the window within the TTL and the reaper reaps it. The runaway bound
# is the brake carve-out + hard-stop-on-error + the Halt cancel-gate + the reset on
# the next human message.
_REQUEST_AUTORUN_TTL_S = 900
# Gated writes we dry-run in the sandbox AT PARK TIME and BLOCK on if the dry-run
# fails, so a deterministic failure (missing mandatory field, bad link, no create
# permission) is returned to the model BEFORE a confirmation card is shown instead
# of surfacing after the human confirms a doomed card. Preview and confirm build
# the same doc as the same exec_user, so a preview failure faithfully predicts the
# confirm failure. Scoped to the build-from-args create/update pair - the reported
# mandatory-field case. submit_doc/cancel_doc/delete_doc/amend_doc are not dry-run
# at park at all (R2-2, see _PREVIEWABLE): their card is described, so a failure
# (state-based: docstatus, link integrity, stock) shows at Confirm. run_method is
# never sandbox-run at park (its target's inline non-DB side effects would fire
# unconfirmed).
#
# create_docs joins the build-from-args creates: its whole batch is dry-run in
# the sandbox at park, so a bad link / missing mandatory in ANY item bounces to
# the model instead of a doomed card. Deliberately NOT in _AUTO_APPLYABLE - the
# batch card is the human checkpoint against duplicate masters.
_DRY_RUN_ON_PARK = frozenset({"create_doc", "create_docs", "update_doc"})

# Batch payload keys: a call carrying a non-empty list under any of these is a
# BULK call (many targets in one gated card), not a single-doc write.
_BULK_ARG_KEYS = ("names", "updates", "docs", "messages")


def _is_bulk_call(args) -> bool:
	"""True when a tool call carries a batch payload. Used to keep a bulk
	create_doc/update_doc from auto-applying - a batch always parks behind the
	card - and to gate a bulk light write. (A bulk submit / cancel / amend /
	delete no longer needs routing here: no verb is trial-run at park, R2-2.)"""
	if not isinstance(args, dict):
		return False
	return any(isinstance(args.get(k), list) and args.get(k) for k in _BULK_ARG_KEYS)


def _bulk_len(args) -> int:
	"""Number of targets in a bulk call - the length of the first non-empty batch
	list (the batch keys are mutually exclusive per the tool contracts). 0 for a
	non-bulk call. Used to bounce an over-size batch at park (F16)."""
	if not isinstance(args, dict):
		return 0
	for k in _BULK_ARG_KEYS:
		v = args.get(k)
		if isinstance(v, list) and v:
			return len(v)
	return 0


def _bulk_targets(args: dict) -> list:
	"""Display names for a batch call: ``names[]`` directly, else the per-item
	name / recipients / doctype from ``updates`` / ``messages`` / ``docs``."""
	if isinstance(args.get("names"), list):
		return list(args["names"])
	for key in ("updates", "messages"):
		items = args.get(key)
		if isinstance(items, list):
			return [(it.get("name") or it.get("recipients") or "?") for it in items if isinstance(it, dict)]
	docs = args.get("docs")
	if isinstance(docs, list):
		return [it.get("doctype", "?") for it in docs if isinstance(it, dict)]
	return []


def _as_bool(value) -> bool:
	"""Coerce an agent-supplied flag to bool. A JSON client may send the
	string ``"false"``/``"0"``; ``bool("false")`` is True, so treat the common
	falsy strings as False rather than trusting plain ``bool()``."""
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def _armed_skip_disabled() -> bool:
	"""Site-wide kill switch for armed macro skip-confirmation. When the
	operator flips ``Jarvis Settings.disable_armed_skip`` on, every armed covered
	write re-gates behind its card immediately, no deploy - the escape hatch if an
	armed macro misbehaves. Read only when an armed covered write is about to skip."""
	return bool(frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "disable_armed_skip")))


# A skill and its child tables: what an armed macro's steps apply (``Apply these
# skills: /slug``), so its reads of them are judged (``_skill_rows_read`` below). A
# WRITE to one asks in every mode, with the rest of ``_write_risk.SKILL_CONFIG_DOCTYPES``
# (``_writes_skill_config``).
_SKILL_DOCTYPES = frozenset(
	{"Jarvis Custom Skill", "Jarvis Custom Skill Share", "Jarvis Custom Skill Allowed Role"}
)
# Compared as Frappe finds a doctype: in any case, and spaces around it trimmed.
_SKILL_DOCTYPES_FOLDED = frozenset(dt.casefold() for dt in _SKILL_DOCTYPES)
# Every argument name a gated tool gives a doctype under.
_DOCTYPE_KEYS = ("doctype", "parenttype", "reference_doctype", "target_doctype", "ref_doctype", "dt")
# Endpoints that start a learning sweep, which writes learned patterns: a person
# sees the call (uncarded, the sweep's own saves would be refused as unseen).
_LEARNING_ENTRY_PREFIXES = ("jarvis.chat.personalise_api.", "jarvis.chat.voice_notes_api.")


def _run_method_brakes(args) -> bool:
	"""A run_method call that deletes or cancels (``run_method.needs_brake``)."""
	from jarvis.tools.run_method import needs_brake

	return needs_brake(args)


def _workflow_brakes(args) -> bool:
	"""An apply_workflow_action whose action can cancel (``workflow_action_cancels``)."""
	from jarvis.tools._write_risk import workflow_action_cancels

	args = args if isinstance(args, dict) else {}
	return workflow_action_cancels(args.get("doctype"), args.get("action"))


_ARMING_REFUSED = (
	"Approve & run is switched on from the skill's own page, not from chat. "
	"Leave allow_approve_run out of this change."
)
_ARMING_FIELD = "allow_approve_run"
_TRUE_TEXT = frozenset({"true", "yes", "on", "y"})


def _switched_on(value) -> bool:
	if isinstance(value, str):
		return value.strip().casefold() in _TRUE_TEXT or bool(frappe.utils.cint(value))
	return bool(value)


def _arms_a_skill(tool: str, args) -> bool:
	"""Whether a call sets a skill's ``allow_approve_run`` on. The controller refuses
	that inside any tool call (``_guard_allow_approve_run_enable``); the gate refuses
	it first, so no card is parked that could only fail at Confirm. Judged on the
	arguments alone (no read of the stored skill): a change that leaves the switch
	out is never refused here."""
	from jarvis.tools import _write_risk

	if tool in ("create_doc", "create_docs", "update_doc"):
		return any(
			target.doctype == "Jarvis Custom Skill" and _switched_on(target.values.get(_ARMING_FIELD))
			for target in _write_risk._targets(tool, args)
		)
	if tool == "run_method":
		# ``frappe.client.set_value`` and the like: the field named beside a skill.
		try:
			text = json.dumps(args, default=str)
		except (TypeError, ValueError):
			return False
		return _ARMING_FIELD in text and _names_a_skill_doctype(args, depth=0)
	return False


def _calls_a_denied_method(args) -> str | None:
	"""The method a ``run_method`` call names that the tool refuses
	(``run_method._is_denied_path``), as its ``method`` or as text anywhere in its
	arguments (a whitelisted helper that calls the method it is handed). The gate
	refuses such a call before parking a card nobody could confirm."""
	from jarvis.tools.run_method import _is_denied_path

	if not isinstance(args, dict):
		return None
	for text in _texts([args.get("method"), args.get("args")]):
		path = text.strip()
		if path.startswith("jarvis.") and _is_denied_path(path):
			return path
	return None


def _writes_skill_config(tool: str, args) -> bool:
	"""Whether a gated write names a skill or learned-skill doctype anywhere in its
	arguments (``_write_risk.SKILL_CONFIG_DOCTYPES``: a single or batch ``update_doc``
	/ ``create_doc``, a share, a comment, ``run_method`` on a doc or through
	``frappe.client``, a JSON ``doc`` string included), or starts a learning sweep.
	Such a call asks in every mode: what these records say is what later chats do.
	Raises InvalidArgumentError for a doctype that is not text."""
	from jarvis.tools._write_risk import SKILL_CONFIG_DOCTYPES

	if tool == "run_method" and isinstance(args, dict):
		if str(args.get("method") or "").strip().startswith(_LEARNING_ENTRY_PREFIXES):
			return True
	folded = frozenset(dt.casefold() for dt in SKILL_CONFIG_DOCTYPES)
	return _names_a_skill_doctype(args, depth=0, folded=folded)


def _names_a_skill_doctype(value, *, depth: int, folded: frozenset = _SKILL_DOCTYPES_FOLDED) -> bool:
	if depth > 6:
		return False
	if isinstance(value, str):
		text = value.strip()
		if not text.startswith(("{", "[")):
			return False
		try:
			value = json.loads(text)
		except ValueError:
			return False
	if isinstance(value, dict):
		named = [value.get(key) for key in _DOCTYPE_KEYS if value.get(key) is not None]
		if depth == 0 and any(not isinstance(dt, str) for dt in named):
			# A list or a dict for the call's own doctype: the model's mistake, a tool error.
			raise InvalidArgumentError("doctype must be the name of a document type")
		# Deeper, a list can be a filter (``["=", "Jarvis Custom Skill"]``).
		if any(text.strip().casefold() in folded for dt in named for text in _texts(dt)):
			return True
		value = list(value.values())
	if isinstance(value, list):
		return any(_names_a_skill_doctype(item, depth=depth + 1, folded=folded) for item in value)
	return False


def _is_a_skill_doctype(text: str) -> bool:
	return text.strip().casefold() in _SKILL_DOCTYPES_FOLDED


def _texts(value) -> list:
	"""The strings in ``value``, a string or a nested list or dict of them."""
	if isinstance(value, str):
		return [value]
	if isinstance(value, dict):
		value = list(value.values())
	if isinstance(value, list | tuple):
		return [text for item in value for text in _texts(item)]
	return []


def _skill_autorun_slide(conv: str) -> None:
	"""Advance the sliding last-write timestamp after a covered auto-run write, and
	commit immediately (mirroring the macro's per-step row write). Committing now
	means a worker death right after this write leaves an ACCURATE freeze-point, so
	the sliding-TTL reaper (task #42) reaps the stranded run at the right time."""
	frappe.db.set_value(
		"Jarvis Conversation", conv, "skill_autorun_at", frappe.utils.now_datetime(), update_modified=False
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- freeze-point survives a worker death


def _skill_autorun_clear(conv: str) -> None:
	"""Hard-stop the approved run: drop skill_autorun (+ the armed skill docname it was
	opened on) so the next covered write re-cards. Committed immediately so the cleared
	state survives a worker death (a concurrent 0->0 clear is an idempotent no-op)."""
	frappe.db.set_value(
		"Jarvis Conversation",
		conv,
		{"skill_autorun": 0, "skill_autorun_skill": None},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- clear seen by other workers


def _request_autorun_arm(conv: str, msg_id: str | None) -> None:
	"""Arm request-scoped 'confirm all' for the CURRENT request (design Layer B): the
	user approved the whole request, so its covered writes run uncarded. Raw db_set
	(bypasses the controller guard, exactly as approve_and_run arms skill_autorun);
	stamps the sliding timestamp = now and records the originating message (observability
	only - correctness rides the new-message reset + sliding TTL). Committed immediately
	so the armed state survives the browser POST -> RQ worker handoff."""
	frappe.db.set_value(
		"Jarvis Conversation",
		conv,
		{
			"request_autorun": 1,
			"request_autorun_at": frappe.utils.now_datetime(),
			"request_autorun_msg": msg_id or "",
		},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- arm seen by other workers


def _request_autorun_slide(conv: str) -> None:
	"""Advance the sliding last-write timestamp after a request-scoped covered write, and
	commit immediately, so a worker death leaves an accurate freeze-point for the reaper."""
	frappe.db.set_value(
		"Jarvis Conversation",
		conv,
		"request_autorun_at",
		frappe.utils.now_datetime(),
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- freeze-point survives a worker death


def _request_autorun_clear(conv: str) -> None:
	"""End the request-scoped run: drop request_autorun (+ its sliding timestamp and the
	originating-message breadcrumb) so the next covered write re-cards and a support query
	on request_autorun_at never surfaces an already-cleared row. Committed immediately so
	the cleared state survives a worker death (a concurrent 0->0 clear is an idempotent
	no-op)."""
	frappe.db.set_value(
		"Jarvis Conversation",
		conv,
		{"request_autorun": 0, "request_autorun_at": None, "request_autorun_msg": None},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- clear seen by other workers


def _resolve_approve_run_offer(conversation: str) -> tuple[str | None, str | None]:
	"""Decide whether the parked card may offer "Approve & run" and, if so, return
	the ARMED Jarvis Custom Skill ``(docname, slug)`` to stamp on its token (skill
	"Approve & run the plan", design §3.3). This is the OFFER side only: it stamps
	the trust signal so a later ``approve_and_run`` endpoint can authorize off the
	docname (re-checking arming live). It sets NO run flag and opens NO run.

	Forgery-proof + fail-safe - ALL must hold, else ``(None, None)``:

	1. A turn->message binding exists for this conversation (the exact user message
	   that triggered the running turn - not a racy "latest hidden=0 message").
	2. That message resolves to an ``owner`` and ``content``.
	3. ``content`` invokes EXACTLY ONE custom skill under the MESSAGE OWNER's
	   identity (never the ambient session / exec user). 2+ invoked skills -> no
	   offer: the run flag is conversation-wide with no per-write skill
	   attribution, so a co-invoked (even unarmed) skill's writes would otherwise
	   ride the approval.
	4. That one slug resolves to a single live-armed row the owner would invoke
	   (:func:`jarvis.chat.custom_skills.resolve_armed_skill_docname` - the same
	   owned/shared/role-scoped resolution :func:`invoked_skill_slugs` uses, so the
	   two can never drift apart).

	Best-effort: any exception -> ``(None, None)``; this is an additive nicety on
	the hot gate path and must never break the park."""
	try:
		if not conversation:
			return None, None
		from jarvis.chat.custom_skills import invoked_skill_slugs, resolve_armed_skill_docname
		from jarvis.chat.turn_message_binding import current_turn_message_id

		msg_id = current_turn_message_id(conversation)
		if not msg_id:
			return None, None
		msg = frappe.db.get_value("Jarvis Chat Message", msg_id, ["owner", "content", "hidden"], as_dict=True)
		if not msg or not msg.get("owner") or not msg.get("content"):
			return None, None
		# The offer anchors on a HUMAN message (design §3.3 "human-message anchor"):
		# a hidden (system / continuation) turn - a macro step, a run continuation,
		# an intake continuation - must NOT re-drive the offer even if its content
		# echoes a `/armedslug`, or a covered write inside a continuation would keep
		# re-offering a run the user only approved once.
		if msg.get("hidden"):
			return None, None
		# Resolve under the MESSAGE SENDER, never frappe.session.user / exec_user.
		slugs = invoked_skill_slugs(msg["content"], user=msg["owner"])
		if len(slugs) != 1:
			return None, None
		slug = next(iter(slugs))
		docname = resolve_armed_skill_docname(slug, msg["owner"])
		if not docname:
			return None, None
		return docname, slug
	except Exception:
		# This runs on EVERY park fleet-wide; a swallowed failure must not be silent.
		# A park is not every-tool-call, so a plain (deduped by title) log is fine.
		frappe.log_error(title="_resolve_approve_run_offer failed", message=frappe.get_traceback())
		return None, None


_PREVIEW_NOT_UNDONE = (
	"A trial run cannot undo email or calls to other systems sent by the document's own code, live "
	"notifications, or error log entries."
)


def _job_count(n: int) -> str:
	return "1 background job" if n == 1 else f"{n} background jobs"


def _preview_note(hook_rolled_back: bool, queue) -> str:
	"""The note on a dry-run result (create / update; R2-2 stopped the trial for
	submit / cancel / amend / delete). Written for a person, and stored with the
	pending record and in the transcript, so it stands alone in plain language,
	names no key, and states only what the sandbox really guarantees. Neither
	client shows it when a structured card exists; there the card's warning line
	(``card.warning``) carries the job count and a rolled-back hook, and the model
	relays the rest. Both render it on the plain fallback card.
	``queue`` is the ``WillQueue`` of the dry run.

	Nothing here about the audit job a dry run does send (SURVIVES_DRY_RUN): it
	is sent when an automation BLOCKS the save, which raises, so no note is
	built for that dry run. That fact lives in the tools README."""
	if hook_rolled_back:
		parts = [
			"The document's own code rolled back part-way through this trial run (it took a failure "
			"path), so this preview may not reflect what Confirm will do. Every database write was "
			"rolled back; nothing is saved until you confirm."
		]
	elif queue.queued_action:
		# Not "checked": the dry run only QUEUED the action.
		parts = ["Trial run: every database write was rolled back. Nothing is saved until you confirm."]
	else:
		parts = [
			"Trial run: the change was checked and every database write was rolled back. Nothing is "
			"saved until you confirm."
		]
	if queue.jobs:
		parts.append(f"Confirming will also start {_job_count(queue.jobs)}, which did not run here.")
	if queue.queued_action:
		parts.append(
			"The action itself will run in the background after you confirm, so it has not been "
			"validated here and may still fail."
		)
	return " ".join([*parts, _PREVIEW_NOT_UNDONE])


def _will_queue_note(queue) -> str:
	"""The sentence that goes with ``will_queue``. Read by the model only, and
	no persona text explains these keys, so it says what to do with them."""
	count = _job_count(queue.jobs)
	if queue.kinds != queue.jobs:
		count += f" of {queue.kinds} kind" + ("" if queue.kinds == 1 else "s")
	note = (
		f"Confirming will also start {count}, named in will_queue. None of this ran in the preview, "
		"so its outcome is unknown. Before the user confirms, tell them in plain words that "
		"confirming starts this background work."
	)
	if queue.queued_action:
		note += (
			" One of these jobs is the action itself (frappe.model.document.execute_action): on "
			"confirm it runs in the background, so it was NOT validated by this preview and can still "
			"fail afterwards. Say so, and after the user confirms do not call the action done until "
			"you have read the document back."
		)
	return note


def _run_preview(tool: str, args: dict) -> dict:
	"""Dispatch a write tool with all DB effects sandboxed (mechanics in
	``jarvis.tools._preview_sandbox``, shared with preview_doc). Side effects
	fired directly inside hooks (inline HTTP calls) are NOT sandboxed.

	Background jobs a hook enqueues ARE sandboxed: the sandbox drops them and
	hands back their names, returned here as ``will_queue`` (only when there
	are any to announce; see ``will_queue_summary`` for what is counted, named
	and left out) with ``will_queue_note``, a sentence that tells the model
	what to do with them, and ``will_queue_jobs``, their real count, which the
	card's warning line shows. Before, a parked Payroll Entry submit put the real
	salary-slip job on the queue while the card was being built.

	``hook_rolled_back`` (only when true) says a hook called a bare
	``frappe.db.rollback()`` during the dry run: its code took a failure path
	(HRMS then writes ``status: Failed``), so ``would`` may not be what Confirm
	produces, and the note says so instead of "checked".

	The whole dict is kept on the pending record and in the transcript, not
	only shown to the model. The card itself is built from the call's
	arguments and carries the trial's findings as ``card.warning``; the
	frontends show ``would`` under Details, and ``note`` only on the plain
	fallback card (see ``_preview_note``).

	Raises ``PreviewSandboxLost`` when the dry run could not be undone cleanly;
	every caller turns that into a refusal (see the class)."""
	from jarvis.tools import _write_risk
	from jarvis.tools._preview_sandbox import preview_sandbox, will_queue_summary

	_write_risk.take_refusal()
	with preview_sandbox() as dropped:
		would = dispatch(tool, args)
	hidden = _write_risk.take_refusal()
	if hidden is not None:
		raise hidden  # the dry run swallowed a refusal: never park a card on it
	# Read after the ``with``: after-commit jobs are named as the sandbox exits.
	queue = will_queue_summary(dropped)
	out = {"preview": True, "would": would, "note": _preview_note(dropped.hook_rolled_back, queue)}
	if queue.jobs:
		out["will_queue"] = queue.names
		out["will_queue_note"] = _will_queue_note(queue)
		# The real number of jobs (``will_queue`` names each kind once): the card's
		# warning line shows this count (confirm_card._add_warning).
		out["will_queue_jobs"] = queue.jobs
	if dropped.hook_rolled_back:
		out["hook_rolled_back"] = True
	return out


def _preview_error(e: Exception) -> dict:
	"""Translate a sandboxed dry-run exception into the model-facing error
	envelope. NEVER audited: a dry-run commits nothing, so there is no write to
	record. Shared by the model-facing ``preview=True`` path and the park gate's
	pre-park validation so both classify the same exceptions identically."""
	from jarvis.tools import _write_risk

	if isinstance(e, PreviewSandboxLost):
		# The dry run could not be undone cleanly (the sandbox already rolled
		# back in full and logged it). The same retryable refusal as a park
		# that could not be staged: no card, and the message says what to check.
		return _error("ConfirmationUnavailableError", str(e))
	if not isinstance(e, WriteRefusedError) and _write_risk.is_implicit_commit(e):
		# The dry run reached DDL: Frappe refused it before the ALTER ran.
		e = _write_risk.structure_refusal()
	if isinstance(e, WriteRefusedError):
		return _write_risk.refused_envelope(e)
	if isinstance(e, JarvisError):
		return _error(_error_code(e), str(e))
	if isinstance(e, frappe.PermissionError):
		return _error("PermissionDeniedError", str(e) or "permission denied")
	# frappe.ValidationError (incl. MandatoryError) / frappe.DuplicateEntryError
	return _error("InvalidArgumentError", str(e) or type(e).__name__)


def _describe_call(tool: str, args: dict) -> str:
	"""Short human string of a tool call + its key args, for a pending card
	whose write cannot be dry-run (send_email) or whose preview was
	unavailable. No secrets: only structural fields are surfaced."""
	a = args if isinstance(args, dict) else {}
	if _is_bulk_call(a):
		# Batch card: verb + count + doctype/action + a few targets, so a parked
		# "cancel_doc count=6 doctype=Purchase Order ..." reads clearly (and the
		# reload-resync path via list_pending_confirmations shows the same).
		targets = _bulk_targets(a)
		parts = [tool, f"count={len(targets)}"]
		for key in ("doctype", "action"):
			if a.get(key):
				parts.append(f"{key}={a[key]}")
		shown = ", ".join(str(t) for t in targets[:10])
		if len(targets) > 10:
			shown += f", +{len(targets) - 10} more"
		if shown:
			parts.append(f"targets=[{shown}]")
		return " ".join(str(p) for p in parts)
	parts = [tool]
	for key in (
		"doctype",
		"name",
		"docname",
		"target_doctype",
		"target_name",
		"method",
		"action",
		"recipients",
		"to",
		"subject",
		"user",
		"skill_name",
		"slug",
		"title",
		"scope",
	):
		val = a.get(key)
		if val:
			parts.append(f"{key}={val}")
	return " ".join(str(p) for p in parts)


def _pending_preview(tool: str, args: dict) -> dict:
	"""Build the preview shown alongside a parked gated write. The create /
	update tools (``_PREVIEWABLE``, single or bulk) reuse the sandboxed
	``_run_preview`` (all DB writes rolled back); everything else gets a
	described-intent dict that makes clear it is NOT a dry run - the real thing
	runs on confirm. That covers submit / cancel / amend / delete (R2-2: their
	hooks' email and outside calls would fire at park, single or bulk),
	run_method (its target's inline side effects would fire and its result reach
	the model), send_email and every other gated write, and a create / update
	whose validation could not be dry-run."""
	described = {
		"preview": False,
		"described": True,
		"summary": _describe_call(tool, args),
		"note": _DESCRIBED_VERB_NOTE
		if tool in _DESCRIBED_VERBS
		else "not a dry run - this will send/execute on confirm",
	}
	if tool not in _PREVIEWABLE:
		return described
	try:
		return _run_preview(tool, args)
	except PreviewSandboxLost:
		# The park must FAIL: a card saying "preview unavailable" would invite
		# a Confirm on a write whose dry run has already misbehaved.
		raise
	except (JarvisError, frappe.PermissionError, frappe.ValidationError, frappe.DuplicateEntryError) as e:
		# The call would fail validation - surface that in the card rather
		# than blocking the park, so the human sees why before confirming.
		described["note"] = f"preview unavailable: {e}"
		return described


def _import_park(args: dict) -> tuple[dict | None, dict | None]:
	"""Park-time validation for ``run_import`` (analogous to the ``_DRY_RUN_ON_PARK``
	pre-park block, but for a tool that cannot be sandbox-run). Computes the read-only
	import preview AS THE CALLER and REFUSES - returns an error envelope, mints no token,
	shows no card - when the import cannot run:

	  * a non-importable doctype (blocked / ``allow_import`` off),
	  * blocking warnings (invalid values / an unmapped mandatory field / Update-Upsert
	    without an ID column) - so the human never confirms a card that imports nothing,
	  * a caller who cannot create Data Imports (usually not a System Manager) - so a
	    non-admin never gets a confirm-then-fail.

	On success returns ``(None, preview)`` where ``preview`` carries the whole import
	payload the card renders from (``preview["import"]``) and a described-intent note
	(so a build_card miss still shows the guaranteed floor, never a blank card)."""
	from jarvis.tools._import_preview import build_import_preview

	if not isinstance(args, dict):
		return _error("InvalidArgumentError", "run_import needs arguments"), None
	# Kill-switch honored AT PARK: refuse before showing a card, never confirm-then-fail.
	if frappe.conf.get("jarvis_import_disabled"):
		return (
			_error(
				"FeatureDisabledError",
				"imports are switched off on this site right now; ask an administrator to re-enable them.",
			),
			None,
		)
	try:
		prev = build_import_preview(
			doctype=args.get("doctype"),
			file_url=args.get("file_url"),
			filename=args.get("filename"),
			import_type=args.get("import_type") or "Insert New Records",
			mapping=args.get("mapping"),
		)
	except (JarvisError, frappe.PermissionError, frappe.ValidationError) as e:
		return _preview_error(e), None

	if not prev.get("importable"):
		return _error(
			"InvalidArgumentError", prev.get("reason") or "this doctype does not allow import"
		), None

	blocking = (prev.get("warnings") or {}).get("blocking") or []
	if blocking:
		reasons = "; ".join(b.get("message", "") for b in blocking if b.get("message"))
		return (
			_error(
				"ImportBlockedError",
				f"the import can't run yet - {reasons}. Fix the file or adjust the column "
				"mapping (call preview_import to see the details), then try again.",
			),
			None,
		)

	if not prev.get("can_run"):
		return (
			_error(
				"PermissionDeniedError",
				"you don't have permission to run imports on this site (it needs the Data "
				"Import create permission, usually System Manager); ask an administrator.",
			),
			None,
		)

	# Thread the RESOLVED file identity into the stored args so confirm imports the EXACT
	# file the card showed. A bare ``filename`` re-resolves to "most recent readable match"
	# at confirm and could drift to a different file uploaded meanwhile. ``args`` is the
	# same dict api.py stores in the confirmation token, so this canonicalizes both.
	resolved = prev.get("resolved_file_url")
	if resolved:
		args["file_url"] = resolved
		args.pop("filename", None)

	# Nothing here should surface a validation msgprint on the success path.
	frappe.clear_messages()

	preview = {
		"preview": False,
		"described": True,
		"summary": _describe_call("run_import", args),
		"note": f"not a dry run - this imports the file into {prev.get('doctype')} on confirm",
		"import": prev,
	}
	return None, preview


# --- Enriched write-error translation (shared by the model/confirm path here
# and the human draft-apply path in chat.actions_api) ---------------------------
#
# A failed ERP write surfaces as flat "permission denied" today because the useful
# reason is generated by Frappe and then discarded: a record-level PermissionError
# is raised BARE (str(e) == "") with the human text in ``frappe.flags.error_message``,
# and the specific blocker (e.g. a User Permission on a link value) is msgprinted
# into ``frappe.local.message_log`` by ``has_permission``'s check-log decorator.
# We HARVEST that safe reason at the catch site rather than re-running the check
# (the unsaved doc is gone by then, and has_permission(debug=True) emits
# developer-oriented allowed-doc dumps). Harvesting message_log also keeps
# Frappe's masking: on a doctype-level denial ``check_doctype_permission`` swaps
# in a fresh log, so the record-level specifics (which linked value blocked it)
# are discarded - ``detail`` at most names the doctype the caller asked for.

# A fixable value (R2-3): the message names the field. Nothing is highlighted here;
# the draft panel's own field marks (``actions_api._mark_fields``) bring their
# own hint.
_FIXABLE_HINT = "Correct the value named above, then try again."

# "what you can do" lines, keyed by the wire ``code``. Deliberately tiny.
_ERROR_HINTS = {
	"PermissionDeniedError": (
		"You don't have access to do this. If you believe you should, ask your "
		"administrator to review your permissions."
	),
	"InvalidArgumentError": _FIXABLE_HINT,
	"OutgoingEmailError": (
		"Ask your administrator to configure an enabled default outgoing Email Account before retrying."
	),
	# R2-3: a deadlock, a lock wait that timed out, or a lock another write holds.
	"RetryLaterError": "Nothing was changed. Try again in a moment.",
	# JF-017. The agent's tool surface is fixed when it is published, so unlike a
	# permission denial there is nothing the USER can change - the remedy is the
	# bundle. (The delegate's own "retrying will not help" instruction rides in the
	# message; the plugin relays only code + message to the model.)
	"CapabilityDeniedError": (
		"This agent can only use the tools it was published with. Ask your "
		"administrator to review the agent's bundle if it needs another one."
	),
	# #493. A settings checkbox, not a role, so pointing at permissions would send
	# the user looking in the wrong place entirely.
	"FeatureDisabledError": (
		"This feature is switched off for your workspace. Ask your administrator to "
		"turn it back on in Jarvis Settings if you need it."
	),
}
# Frappe's User-Permission link denial reads "...not allowed to access this X
# record because it is linked to Y '...' in field Z" - a more specific hint than
# the generic role-permission one.
_USER_PERM_HINT = (
	"Your access is limited to specific records by a User Permission. Ask your "
	"administrator to review your User Permissions for this record."
)


def _msglog_mark() -> int:
	"""Snapshot the current message_log length, so a failure branch can harvest
	only the reasons THIS operation logged."""
	log = getattr(frappe.local, "message_log", None)
	return len(log) if log else 0


def _harvest_reason(mark: int) -> str:
	"""User-safe reason text Frappe accumulated in message_log during a failed
	write (has_permission check-logs, throw() messages). HTML-stripped, joined,
	~500-char capped. REMOVES the harvested entries so they don't also ride out
	as ``_server_messages`` (double-surfacing). Empty when nothing was logged -
	e.g. a doctype-level denial where Frappe masks the reason."""
	log = getattr(frappe.local, "message_log", None)
	if not log or len(log) <= mark:
		return ""
	parts = []
	for entry in log[mark:]:
		raw = entry.get("message") if isinstance(entry, dict) else entry
		text = strip_html(str(raw or "")).strip()
		if text:
			parts.append(text)
	del log[mark:]  # pop harvested entries so they don't become _server_messages
	return " ".join(parts)[:500]


def _flags_message() -> str:
	"""The human-facing text Frappe stashes in ``flags.error_message`` (set by
	``raise_no_permission_to`` for a bare PermissionError), HTML-stripped."""
	return strip_html(str(frappe.flags.get("error_message") or "")).strip()


def _hint_for(code: str, detail: str, *, kind: str | None = None) -> str:
	"""The "what you can do" line. ``kind`` (``jarvis._failure_kind``): a business rule
	raised as a plain ValidationError is enveloped as ``InvalidArgumentError`` too, and
	"correct the value" does not fit something no value fixes (insufficient stock), so
	only a fixable one gets that hint."""
	from jarvis import _failure_kind

	if code == "PermissionDeniedError" and (
		"linked to" in detail.lower() or "not allowed to access" in detail.lower()
	):
		return _USER_PERM_HINT
	if code == "InvalidArgumentError" and kind not in (None, _failure_kind.FIXABLE):
		return ""
	return _ERROR_HINTS.get(code, "")


def _duplicate_message(e: Exception) -> str:
	"""A DuplicateEntryError's ``str(e)`` is a ``(doctype, name, IntegrityError)``
	args repr - internal table/key + driver text. Build a clean user-facing line
	from its args instead of surfacing that repr."""
	args = getattr(e, "args", ()) or ()
	doctype = args[0] if len(args) > 0 else ""
	name = args[1] if len(args) > 1 else ""
	if doctype and name:
		return f"A {doctype} named '{name}' already exists."
	if doctype:
		return f"That {doctype} already exists."
	return "A record with these values already exists."


def _error_code(e: JarvisError) -> str:
	"""The wire code of a Jarvis error: its class name, unless the class keeps an
	established code (``InvalidFieldValueError`` -> ``InvalidArgumentError``)."""
	return getattr(e, "envelope_code", None) or type(e).__name__


_BUSY_MESSAGE = "The system was busy (a lock or timeout) and nothing was saved."


def _bad_date_message(found: dict) -> str:
	"""A database refusal of a date / datetime / time value (error 1292), named by field."""
	fieldname, doctype = found["fieldname"], found["doctype"]
	label = fieldname
	try:
		if doctype and frappe.db.exists("DocType", doctype):
			label = frappe.get_meta(doctype).get_label(fieldname) or fieldname
	except Exception:
		pass
	shape = {"date": "YYYY-MM-DD", "datetime": "YYYY-MM-DD HH:MM:SS", "time": "HH:MM:SS"}[found["type"]]
	where = f"{label} ({fieldname})" if label != fieldname else fieldname
	return f"{where} has an invalid {found['type']} value '{found['value'][:80]}'; use {shape}."


def _translate_write_error(e: Exception, mark: int, *, doctype: str = "") -> dict | None:
	"""Enriched ``{ok:false, error}`` envelope for a KNOWN write-path exception,
	promoting Frappe's discarded reason into ``message``/``detail``/``hint``.
	Returns ``None`` for an unexpected exception - the caller MUST re-raise it so
	a real bug still surfaces as a 500 (never enveloped, never leaks a traceback).
	``mark`` is a ``_msglog_mark()`` taken before the write ran.

	``error.kind`` (R2-3, ``jarvis._failure_kind``) says whether the request can be
	corrected (``fixable``), the site was busy (``retry_later``: a deadlock or a lock
	wait timeout, now enveloped instead of a 500) or neither (``not_fixable``). A
	database refusal of a date value is enveloped as a fixable
	``InvalidArgumentError`` naming the field (Frappe does not validate dates).

	A refusal by the write-risk guard keeps its own code (``structure_refused`` ...)
	and Desk path, and is never fixable. ``ImplicitCommitError`` is one too: Frappe raises it before DDL
	inside the no-commit fence or a dry run, so the save would have changed the
	database structure (a doctype outside the structure list); ``doctype`` (the
	call's target, when known) names it and its Desk page."""
	from jarvis import _failure_kind
	from jarvis.tools import _write_risk

	if not isinstance(e, WriteRefusedError) and _write_risk.is_implicit_commit(e):
		e = _write_risk.structure_refusal(doctype)
	if isinstance(e, WriteRefusedError):
		detail = _harvest_reason(mark)
		envelope = _write_risk.refused_envelope(e, "" if detail == strip_html(str(e)).strip() else detail)
		envelope["error"]["kind"] = _failure_kind.NOT_FIXABLE
		return envelope

	kind = _failure_kind.kind_of(e)
	if isinstance(e, JarvisError):
		code, message = _error_code(e), strip_html(str(e)).strip()
	elif isinstance(e, frappe.PermissionError):
		code = "PermissionDeniedError"
		message = strip_html(str(e)).strip() or _flags_message() or "permission denied"
	elif isinstance(e, frappe.DuplicateEntryError):
		# str(e) here is an args-tuple repr, not a message - clean it up.
		code, message = "InvalidArgumentError", _duplicate_message(e)
	elif isinstance(e, frappe.OutgoingEmailError):
		code = "OutgoingEmailError"
		message = strip_html(str(e)).strip() or "Outgoing email is not configured."
	elif isinstance(e, frappe.ValidationError):
		code = "InvalidArgumentError"
		message = strip_html(str(e)).strip() or _flags_message() or type(e).__name__
	elif kind == _failure_kind.RETRY_LATER:
		code, message = "RetryLaterError", _BUSY_MESSAGE
	elif _failure_kind.bad_date(e):
		code, message = "InvalidArgumentError", _bad_date_message(_failure_kind.bad_date(e))
	else:
		return None
	detail = _harvest_reason(mark)
	# Don't repeat the message under "Show details".
	if detail and detail == strip_html(message).strip():
		detail = ""
	envelope = _error(code, message, detail=detail, hint=_hint_for(code, detail, kind=kind))
	envelope["error"]["kind"] = kind
	return envelope


def _dispatch_and_wrap(
	tool: str,
	args: dict,
	is_write: bool,
	*,
	provenance: str = "chat",
	provenance_name: str = "",
	precheck=None,
) -> dict:
	"""Dispatch + translate exceptions into the ``{ok, data}`` / ``{ok, error}``
	envelope + audit write tools. This is the shared core of ``_run_tool``'s
	execute path, reused verbatim by ``confirm_tool`` so a confirmed write runs
	through the exact same translation + audit as an inline one.

	``provenance``/``provenance_name`` identify HOW this write reached dispatch
	(chat / auto_apply / macro / skill) and ride into the durable
	``Jarvis Agent Write`` audit row. They are threaded params, NOT read from
	``_agent_run_ctx`` here — the manager audit is recorded from the two
	committing branches below (never the re-raise/500 branch).

	A write is scoped in a SAVEPOINT so a KNOWN failure is rolled back before we
	RETURN the ``{ok:false}`` envelope. Frappe commits any endpoint that returns
	normally (frappe/app.py), so without this a tool that half-applied before
	raising - e.g. a submit whose ``on_submit`` hook throws AFTER ``docstatus=1``
	was written - would be persisted at end-of-request. Rolling back to the
	savepoint (not a full rollback) undoes ONLY this tool, leaving the caller's
	surrounding writes intact (``confirm_tool``'s failure receipt/continuation,
	the model path's tool-call row). Unexpected exceptions re-raise unchanged:
	Frappe's handler does a full request rollback (nothing persists, no envelope,
	no traceback to the client).

	``precheck`` (a guarded structure write's checks, run again under its lock) runs
	first, inside the same translation and audit: what it raises is this call's
	failure, and the tool is never dispatched."""
	mark = _msglog_mark()
	sp = f"jarvis_{frappe.generate_hash(length=10)}" if is_write else None
	if sp:
		saved_queues = {
			name: tuple(getattr(frappe.db, name)._functions)
			for name in ("before_commit", "after_commit", "before_rollback", "after_rollback")
			if hasattr(frappe.db, name)
		}
		realtime_log = getattr(frappe.local, "_realtime_log", None)
		saved_realtime = list(realtime_log) if realtime_log is not None else None
		frappe.db.savepoint(sp)
	from jarvis.tools import _write_risk

	_write_risk.take_refusal()  # nothing stale from an earlier call
	try:
		if precheck is not None:
			precheck()
		data = dispatch(tool, args)
		# A method that swallowed the ORM guard's refusal (reset_password does) must
		# not report success: the refusal wins, and the savepoint undoes the rest.
		hidden = _write_risk.take_refusal()
		if hidden is not None:
			raise hidden
	except Exception as e:
		hidden = _write_risk.take_refusal()
		if hidden is not None and not isinstance(e, WriteRefusedError):
			e = hidden  # re-raised as something else: still the refusal
		a = args if isinstance(args, dict) else {}
		envelope = _translate_write_error(e, mark, doctype=a.get("doctype") or "")
		if envelope is None:  # unexpected - audit then re-raise to Frappe (500)
			if is_write:
				audit.record(
					tool=tool, args=args, ok=False, error_code=type(e).__name__, error_message=str(e)
				)
			raise
		if sp:
			# Undo this tool's partial writes. Guard against a tool that committed
			# mid-op (the savepoint would be gone) so we never turn a clean tool
			# failure into a 500.
			try:
				frappe.db.rollback(save_point=sp)
			except Exception:
				if envelope["error"].get("kind") == "retry_later":
					# A deadlock already rolled the whole transaction back in the
					# database (the savepoint went with it); a full rollback here also
					# drops what the tool queued for after commit, as the 500 it
					# replaced did. It also resets ``before_commit``, so a caller that
					# watches for a mid-dispatch commit with a sentinel there
					# (pending_actions._execute) is told what was still armed just
					# before this rollback: a sentinel still there saw no commit.
					armed = tuple(frappe.db.before_commit._functions)
					frappe.db.rollback()
					frappe.local.jarvis_rolled_back_after_failure = armed
			else:
				# SQL savepoint rollback leaves callbacks and realtime events queued.
				for name, functions in saved_queues.items():
					getattr(frappe.db, name)._functions = deque(functions)
				if saved_realtime is None:
					from frappe.realtime import clear_realtime_log

					clear_realtime_log()
				else:
					frappe.local._realtime_log = saved_realtime
		if is_write:
			err_obj = envelope["error"]
			audit.record(
				tool=tool, args=args, ok=False, error_code=err_obj["code"], error_message=err_obj["message"]
			)
			# Manager audit: a KNOWN failure. Recorded AFTER the savepoint rollback so
			# the tool's partial write is undone but this row rides the envelope commit.
			# A write the risk guard refused is filed as refused, not failed, against
			# the doctype it refused (a method's argument may name another one).
			from jarvis import agent_audit

			refused = err_obj.get("code") in _write_risk.REFUSAL_CODES
			agent_audit.record_write(
				actor=frappe.session.user,
				tool=tool,
				args=args,
				result=None,
				outcome="refused" if refused else "failed",
				provenance=provenance,
				provenance_name=provenance_name,
				ref_doctype=err_obj.get("doctype") if refused else None,
			)
		return envelope
	if sp:
		try:
			frappe.db.release_savepoint(sp)
		except Exception:
			pass
	if is_write:
		audit.record(tool=tool, args=args, ok=True, result=data)
		# Manager audit: a dispatched write (one metadata-only row; bulk collapses
		# to one row + bulk_count). Own savepoint, never commits. This branch
		# (version-16-hotfix) has no envelope_ok / _ENVELOPE_TOOLS machinery, so a
		# dispatched write is recorded "applied" — consistent with the audit.record
		# ok=True above; the call_connector inner-failure nuance is part of the
		# connector-envelope feature that is not on this branch.
		from jarvis import agent_audit

		agent_audit.record_write(
			actor=frappe.session.user,
			tool=tool,
			args=args,
			result=data,
			outcome="applied",
			provenance=provenance,
			provenance_name=provenance_name,
		)
	return {"ok": True, "data": data}


def dispatch_confirmed(
	tool: str,
	args: dict,
	*,
	provenance: str = "chat",
	provenance_name: str = "",
	allow_risky: bool = False,
	uncarded: bool = False,
) -> dict:
	"""Execute a confirmed gated write. Public seam used by ``confirm_tool``
	AFTER ``pending_confirm.consume`` has validated owner + single-use. Runs
	the stored call directly (a gated write is always a _WRITE_TOOL, so it is
	audited) WITHOUT re-entering ``_run_tool``'s gate - the gate would just
	park it again. This is design option (a): the model can never execute a
	gated write; only a confirmed human click reaches dispatch.

	``provenance`` defaults to ``chat`` (a human confirm click); the auto-apply
	and armed macro/skill call-sites pass their own kind so the manager audit
	row is labelled correctly.

	Write-risk guard (``jarvis.tools._write_risk``): a structure create / update /
	delete is refused here on every route, including a card parked before the
	guard existed (run_method is not judged by its arguments, R2-13; the ORM guard
	judges what it saves). ``allow_risky`` is passed ONLY by the
	paths that run a card a human confirmed (the pending-action execute and the
	legacy token dispatch): the ORM guard then admits the root writes of exactly
	the structure / sensitive records the card's arguments name, each once.
	``uncarded`` (auto mode / "confirm all" / armed macro / approved skill / File
	Box) runs a create / update behind the no-commit fence, so a doctype outside the
	structure list whose save runs DDL is refused before the ALTER.

	A guarded structure write (one new Custom Field, a column-free Custom Field edit;
	R2-10) runs only for a confirmed card (``allow_risky``) whose confirm holds that
	form's structure lock (``pending_actions.execute`` takes it before the claim):
	any other caller gets the structure refusal. Its checks run again under the lock,
	the ALTER waits only a few seconds for the table, a refusal by the database is
	enveloped in plain words, and its failure is never fixable (E3)."""
	from jarvis.tools import _guarded_structure, _write_risk

	try:
		risk = _write_risk.check(tool, args, guarded=allow_risky)
	except WriteRefusedError as e:
		return _refuse_risky_write(tool, args, e, provenance=provenance, provenance_name=provenance_name)
	guarded = _guarded_structure.handler_for(risk, args)
	# Its form's lock must be held (the card's own confirm), and the call must be in
	# the exact form the park sealed: a doctype in another letter case would still
	# save on a case-insensitive database, around everything keyed on the name.
	exact = isinstance(args, dict) and args.get("doctype") == _write_risk.canonical(args.get("doctype"))
	if guarded is not None and not (exact and _guarded_structure.lock_held(guarded.lock_doctype)):
		refusal = _write_risk.refusal_of(tool, args)
		return _refuse_risky_write(
			tool, args, refusal, provenance=provenance, provenance_name=provenance_name
		)
	if tool == "run_import" and risk == "sensitive":
		# Also at Confirm: an import parked before this rule (its card sampled a few
		# rows, clipped) that writes something sensitive is refused, nothing staged.
		return _refuse_sensitive_import(tool, args, provenance=provenance, provenance_name=provenance_name)
	allow = _write_risk.allow_entries(tool, args) if allow_risky else []
	fence = _write_risk.no_commit_fence() if uncarded and tool in _FENCED_TOOLS else nullcontext()
	# An uncarded write may not delete or cancel a document, however it gets there
	# (_write_risk.brake_doc_event): those verbs always park a card. A run_method
	# picks its own code, so it may not even nest one in another save.
	with (
		_write_risk.guard_scope(
			allow, brake=uncarded, brake_nested=uncarded and tool == "run_method"
		) as state,
		fence,
		_guarded_structure.short_lock_wait() if guarded is not None else nullcontext(),
	):
		mark = _msglog_mark()
		try:
			result = _dispatch_and_wrap(
				tool,
				args,
				is_write=True,
				provenance=provenance,
				provenance_name=provenance_name,
				precheck=guarded.check if guarded is not None else None,
			)
		except Exception as e:
			# The ALTER was refused for row size (MariaDB 1118): the known way for a
			# new column to fail, said in plain words. Anything else stays a crash.
			refused = _guarded_structure.database_refusal(e) if guarded is not None else None
			if refused is None:
				raise
			result = _translate_write_error(refused, mark)
	if guarded is not None and not result.get("ok"):
		_never_fixable(result)
	# One line per sensitive record the card's allow actually let through (none when
	# a confirmed call touched nothing sensitive).
	outcome = "applied" if result.get("ok") else "failed"
	for doctype, name in state.admitted:
		_write_risk.log_line(
			risk if guarded is not None else "sensitive",
			doctype,
			name,
			outcome,
			tool=tool,
			provenance=provenance,
		)
	return result


def _never_fixable(result: dict) -> None:
	"""E3: a failed structure write is never offered a correction, whatever failed
	(a missing value included). A busy failure stays "try again"."""
	err = result.get("error") if isinstance(result, dict) else None
	if isinstance(err, dict) and err.get("kind") == "fixable":
		err["kind"] = "not_fixable"
		err["hint"] = _hint_for(err.get("code") or "", err.get("detail") or "", kind="not_fixable")


# Uncarded writes that run behind the no-commit fence: the build-from-args pair.
_FENCED_TOOLS = frozenset({"create_doc", "create_docs", "update_doc"})


def _refuse_risky_write(
	tool: str, args: dict, e: WriteRefusedError, *, provenance: str = "chat", provenance_name: str = ""
) -> dict:
	"""Refuse a write the risk guard will not run: the refusal envelope, the audit
	line, and a ``refused`` row in Jarvis Agent Write (E4). Nothing is dispatched."""
	from jarvis import agent_audit
	from jarvis.tools import _write_risk

	env = _write_risk.refused_envelope(e)
	doctype, name = _write_risk.first_risky_target(tool, args)
	risk = "structure" if e.code == "structure_refused" else "sensitive"
	_write_risk.log_line(risk, e.doctype or doctype, name, "refused", tool=tool, code=e.code)
	audit.record(tool=tool, args=args, ok=False, error_code=e.code, error_message=env["error"]["message"])
	agent_audit.record_write(
		actor=frappe.session.user,
		tool=tool,
		args=args,
		result=None,
		outcome="refused",
		provenance=provenance,
		provenance_name=provenance_name,
		ref_doctype=e.doctype or None,
	)
	return env


def _build_park_card(
	tool: str, args: dict, preview: dict, *, sensitive: bool, structural: list | None = None
) -> tuple[dict | None, dict | None]:
	"""``(card, refusal)`` for a park: the chat gate and the File Box hold share it.

	A guarded structure write (``structural``: its further risk lines, a list;
	R2-10) is shown exactly like a sensitive one, in full and never clipped, and
	leads with the structural line in the banner, then what else the change does
	(code for an ``eval:`` expression, access, a fetch; ``card.structural`` marks it
	for the clients). It reuses the ``risk: "sensitive"`` rendering, so every client
	that shows a sensitive card in full shows this one in full too.

	A sensitive call (R2-8 / R2-12) gets ``preview["risk"]`` / ``risk_line`` (every
	risk it carries, in target order), which makes ``build_card`` show it in full,
	and the same keys on the card. It is refused instead of parked when the call is
	over the size limit (checked first, so a huge script never reaches the line
	diff), when no card could be built (the raw fallback has no banner and no full
	view), or when the card itself is over the limit (a sensitive card is never
	clipped)."""
	from jarvis.chat import confirm_card
	from jarvis.tools import _write_risk

	if not sensitive and structural is None:
		return confirm_card.build_card(tool, args, preview), None
	if confirm_card.too_large(None, args):
		return None, _refuse_unshowable_card(tool, args, too_large=True)
	if structural is not None:
		from jarvis.tools._guarded_structure import STRUCTURAL_LINE

		line = " ".join([STRUCTURAL_LINE, *structural])
		preview["structural"] = True
	else:
		line = " ".join(_write_risk.risk_lines(tool, args))
	preview["risk"] = "sensitive"
	if line:
		preview["risk_line"] = line
	card = confirm_card.build_card(tool, args, preview)
	if not isinstance(card, dict):
		return None, _refuse_unshowable_card(tool, args, too_large=False)
	card["risk"] = "sensitive"
	if structural is not None:
		card["structural"] = True
	if line:
		card["risk_line"] = line
	if confirm_card.too_large(card, args):
		return None, _refuse_unshowable_card(tool, args, too_large=True)
	return card, None


def _can_card_structure(conversation: str | None) -> bool:
	"""Whether a guarded structure write can get its card here: a chat whose cards
	are pending actions (the confirm that takes the structure lock and keeps the
	before-image runs only there), and not a File Box chat (its writes are held for
	the Approval Board, which cannot run one)."""
	from jarvis.chat import pending_confirm

	if not conversation or not pending_confirm.pa_minting(conversation):
		return False
	return not frappe.db.get_value("Jarvis Conversation", conversation, "file_box")


def _structure_park(tool: str, args: dict, risk: str) -> tuple:
	"""``(rejected, preview, lines, canonical_args)`` for a guarded structure write at
	park: its checks, then a described preview, the risk lines its card shows after
	the structural one, and the call in its stored form (``unique: "true"`` as 1, the
	doctype by its exact name), which is what is carded, sealed and run: the card
	can never show one thing and the save write another. Nothing is trial-run, so no row and no column exist
	until Confirm. A refusal that is a structure refusal is audited like every
	other; a value the request can correct comes back as fixable, naming the field."""
	from jarvis.tools import _guarded_structure, _write_risk

	mark = _msglog_mark()
	handler = _guarded_structure.handler_for(risk, args)
	try:
		handler.check()
	except WriteRefusedError as e:
		return _refuse_risky_write(tool, args, e), None, None, None
	except (JarvisError, frappe.PermissionError, frappe.ValidationError) as e:
		return _translate_write_error(e, mark) or _preview_error(e), None, None, None
	canonical = handler.canonical_args()
	handler = _guarded_structure.handler_for(risk, canonical)
	doctype, name = _write_risk.first_risky_target(tool, canonical)
	_write_risk.log_line(risk, doctype, name, "parked", tool=tool)
	preview = {
		"preview": False,
		"described": True,
		"summary": _describe_call(tool, canonical),
		"note": "not a dry run - this changes the database structure on confirm",
	}
	return None, preview, handler.risk_lines(), canonical


def _refuse_sensitive_import(tool: str, args: dict, **provenance) -> dict:
	"""Refuse a ``run_import`` that would write something sensitive (``_write_risk``:
	a sensitive doctype, or an access-granting column such as portal users or an
	Employee's user, or a file that cannot be read to tell): its card samples a few
	rows and cannot show every record in full, so it is set up through Data Import in
	Desk. Audited like every refusal. An ordinary import parks its card as before."""
	from jarvis.tools import _write_risk

	doctype = _write_risk.first_risky_target(tool, args)[0] or "these"
	e = SensitiveWriteRefusedError(
		f"Importing {doctype} records changes sensitive settings, and an import cannot be shown in "
		"full on a confirmation card, so it is not run from chat. Do not retry it with another tool; "
		"tell the user to use Data Import in Desk.",
		doctype=doctype,
	)
	env = _refuse_risky_write(tool, args, e, **provenance)
	env["error"]["hint"] = "Use Data Import in Desk: open /app/data-import/new."
	env["error"]["person_message"] = (
		f"An import of {doctype} records changes sensitive settings and cannot be shown in full on a "
		"confirmation card, so it cannot be run from chat. Use Data Import in Desk."
	)
	return env


def _refuse_unshowable_card(tool: str, args: dict, *, too_large: bool) -> dict:
	"""Refuse a sensitive write whose card cannot be shown in full (R2-8): over
	``confirm_card.MAX_FULL_CARD_BYTES``, or no card could be built. Audited like
	every refusal, with a hint that sends the person to Desk. ``error.person_message``
	is the same refusal for a person (the draft panel shows it)."""
	from jarvis.tools import _write_risk

	doctype = _write_risk.first_risky_target(tool, args)[0]
	if too_large:
		why = "is too large to show in full on a confirmation card (over 256 KB)"
		person = "This change is too large to show in full on a confirmation card"
	else:
		why = "cannot be shown in full on a confirmation card"
		person = "This change cannot be shown in full on a confirmation card"
	e = SensitiveWriteRefusedError(
		f"This {doctype or 'record'} change {why}, so it is not made from chat. Do not retry it "
		"with another tool; tell the user to make it in Desk.",
		doctype=doctype,
	)
	env = _refuse_risky_write(tool, args, e)
	env["error"]["hint"] = "Make this change in Desk, where the whole record can be reviewed."
	env["error"]["person_message"] = f"{person}, so it cannot be made from chat. Make it in Desk."
	return env


def _apply_run_method_read_filter(tool: str, result) -> None:
	"""I9/I6: ``run_method`` returns its target Document verbatim (unlike get_doc /
	create_doc, which permlevel-filter before ``as_dict``). Strip permlevel>0 fields the
	acting agent cannot read from that Document return BEFORE it reaches the receipt chip
	or the model's continuation dump - otherwise a rate/valuation leaks into the model
	context on an uncarded auto-run.

	Best-effort: a filter hiccup must never fail an already-committed write (that would
	roll it back for a cosmetic step). Shared by the confirmed paths
	(``_confirm_core`` / ``approve_and_run``) and the gate-branch auto-run core
	(``_run_covered_write``) so the filter is written ONCE, not triple-maintained (I6)."""
	if tool != "run_method" or not (isinstance(result, dict) and result.get("ok")):
		return
	_ret = result.get("data")
	if hasattr(_ret, "apply_fieldlevel_read_permissions"):
		try:
			_ret.apply_fieldlevel_read_permissions()
		except Exception:
			frappe.log_error(
				title="run_method receipt permlevel filter failed",
				message=frappe.get_traceback(),
			)


def _auto_mode_fields() -> tuple[str, ...]:
	"""``("auto_mode",)`` once the column exists, else ``()``. The gate reads the
	conversation's flags by column name, so new code running ahead of its migrate must
	not name a column the table does not have yet (every gated write would error)."""
	return ("auto_mode",) if frappe.get_meta("Jarvis Conversation").has_field("auto_mode") else ()


def _run_covered_write(
	tool: str,
	args: dict,
	*,
	conv: str,
	owner_user: str,
	provenance_kind: str,
	provenance_name: str | None,
) -> dict:
	"""Shared core for a gate-branch COVERED WRITE that runs uncarded under an armed
	macro (``skip_confirmation``), an approved skill run (``skill_autorun``), a
	request-scoped "confirm all" (``request_autorun``) or a per-chat auto mode (``auto_mode``).

	The two gate branches drifted apart (each hand-rolled the dispatch + a subset of the
	cross-cutting steps), so the review found real gaps - the skill branch leaked
	permlevel fields and dropped a step-2+ import announcement. This helper is the ONE
	place the cross-cutting core lives, applied identically on both branches:

	  1. ``dispatch_confirmed`` the stored call;
	  2. I9 - run_method permlevel read-filter (strip fields the agent can't read);
	  3. I5 - run_import completion announcement (the gate-branch twin of the confirm
	     path's ``bind_after_run_import``, so an uncarded/unattended import still reports
	     done - the skill branch had dropped this);
	  4. I2 - provenance marker (consume-once) so the receipt persist labels the row
	     ``auto_applied`` + a queryable ``armed_by_macro`` / ``armed_by_skill``.

	Callers keep their OWN pre-dispatch guards (macro: kill-switch; skill: cancel-gate +
	live-disarm re-check + sliding TTL) and their OWN post-dispatch flag handling (skill:
	slide/clear on ``result.ok``). This helper is ONLY the dispatch + filter + announce +
	provenance core - deliberately not the receipt/continuation settlement tail."""
	result = dispatch_confirmed(
		tool, args, provenance=provenance_kind, provenance_name=provenance_name or "", uncarded=True
	)
	_apply_run_method_read_filter(tool, result)
	if tool == "run_import" and isinstance(result, dict) and result.get("ok"):
		# The gate branch bypasses _confirm_core, where a confirmed run_import normally
		# binds its completion announcement. Synthesize the record from the gate locals
		# (there is no pending-confirm record here); bind is self-gating + best-effort.
		from jarvis.chat.import_announce import bind_after_run_import

		bind_after_run_import({"tool": "run_import", "conversation": conv, "owner": owner_user}, result)
	from jarvis.tools import _agent_run_ctx

	# I2: stash the arming provenance for the receipt persist (consume-once). macro and
	# skill use distinct markers so the receipt chip / audit field never mislabels one as
	# the other (a skill run is NOT "armed macro X").
	if provenance_kind == "macro":
		_agent_run_ctx.set_armed_by_macro(provenance_name)
	elif provenance_kind == "skill":
		_agent_run_ctx.set_armed_by_skill(provenance_name)
	elif provenance_kind in ("request", "auto_mode"):
		# Request-scoped "confirm all" and per-chat auto mode (#581): a boolean marker
		# (no armer name - the user themselves chose it), so the receipt persist labels
		# the row auto_applied just like the macro/skill paths.
		_agent_run_ctx.set_request_autorun_applied()
	return result


def _file_box_wiki_write(
	args: dict, conv: str, *, user: str | None = None, provenance: str = "auto_apply"
) -> dict:
	"""Land a File Box conversation's ``update_wiki`` through the append-only,
	provenance-FENCED wiki funnel instead of the raw ``update_wiki`` tool.

	``user`` is the file-box dropper (``conversation.owner``), NOT the acting
	session. At review-before-landing execute time the session is the REVIEWER;
	passing the dropper keeps the write ATTRIBUTED to them - it sets the funnel's
	``sources`` ``user`` field and the manager-board audit actor, so an approved
	write reads as the dropper's content, not the reviewer's. (The own-pages FENCE
	itself keys on the app-level provenance PREFIX ``FILE_BOX_WIKI_FENCE`` +
	Org scope, not on this user - it is what refuses a human/other-app page; the
	``user`` is attribution, not the isolation boundary.) Defaults to the session
	user for the legacy auto-apply path. ``provenance`` labels the manager-board
	write-audit row (``auto_apply`` | ``reviewer_approved``).

	A file_box run is unattended (nobody can click a confirm card), so its
	write-back must NOT reach the raw tool (scope=Org + ignore_permissions,
	full-body replace, stamps the human-curated "tool" kind). It lands through
	``apply_extracted_page_updates`` fenced to ``FILE_BOX_WIKI_FENCE`` with
	``allow_body_replace=False`` + ``preserve_curated=True`` and stamped
	``FILE_BOX_WIKI_KIND`` - so it can only create/refresh its OWN file-box pages,
	appends (never replaces), and is REFUSED (never overwrites) on a slug that
	collides with a human/other page. A ``replace_body_md`` is downgraded to an
	append. ``refuse_overflow=True`` (W): an append/create that would exceed the
	page's length cap is likewise REFUSED rather than clipped, so approved File
	Box knowledge is never silently truncated; the outcome reads
	``reason="page_full"``. Mirrors ``jarvis.tools.record_app_wiki``. Returns a
	raw-update_wiki-compatible result dict the model reads (the caller wraps it in
	the standard ``{ok, data}`` envelope)."""
	from jarvis.chat.wiki import apply_extracted_page_updates

	update = {
		"slug": args.get("slug"),
		"title": args.get("title"),
		"page_type": args.get("page_type"),
		"ref_doctype": args.get("ref_doctype"),
		"ref_name": args.get("ref_name"),
		"summary": args.get("summary"),
		# Append-only: a replace becomes an append (the raw tool's full-body
		# rewrite never runs on this unattended path).
		"append_md": args.get("append_md") or args.get("replace_body_md"),
		"scope": args.get("scope") or "Org",
	}
	try:
		outcomes = apply_extracted_page_updates(
			[update],
			source=FILE_BOX_WIKI_KIND,
			user=user or frappe.session.user,
			ref=conv,
			provenance_prefix=FILE_BOX_WIKI_FENCE,
			allow_body_replace=False,
			preserve_curated=True,
			return_outcomes=True,
			# W: never truncate an approved File Box note — refuse an overflowing
			# append/create outright instead of _clip_body silently dropping the
			# page's oldest knowledge.
			refuse_overflow=True,
		)
		outcome = outcomes[0] if outcomes else {"slug": None, "ok": False, "reason": "skipped"}
	except Exception:
		# Best-effort: a transaction-FATAL wiki error (deadlock / lock-wait) must NOT
		# 500 the unattended run - the File Box prompt promises never to fail the run
		# over a wiki write. This request's txn holds only this single-page write, so
		# rolling it back cannot mis-report other work (the batch phantom-tally hazard
		# that makes the funnel re-raise needs a multi-update batch). Recoverable
		# per-page refusals never reach here (the funnel returns them as ok=False).
		frappe.db.rollback()
		frappe.log_error(title="file_box wiki write-back failed", message=frappe.get_traceback())
		outcome = {"slug": update.get("slug"), "ok": False, "reason": "error"}
	applied = bool(outcome.get("ok"))
	if applied:
		result = {"ok": True, "slug": outcome.get("slug"), "detail": "recorded to the wiki"}
	else:
		result = {"ok": False, "reason": outcome.get("reason") or "refused"}
	# AUDIT - both rows a dispatched write gets via _dispatch_and_wrap, because this
	# fenced branch returns BEFORE it: (1) the tool_audit logger line, and (2) the
	# durable Jarvis Agent Write manager-board row. Without (2), an unattended
	# Org-scope wiki write would be missing from the manager write-audit board that
	# every other file_box write (create/update, via dispatch_confirmed) files.
	# Both reflect the real fenced outcome so a refused write reads as failed.
	audit.record(
		tool="update_wiki",
		args=args,
		ok=applied,
		result=result,
		error_message=None if applied else result["reason"],
	)
	from jarvis import agent_audit

	agent_audit.record_write(
		actor=user or frappe.session.user,
		tool="update_wiki",
		args=args,
		result=result,
		outcome="applied" if applied else "failed",
		provenance=provenance,
		provenance_name=conv,
	)
	return result


def _same_file_wiki_proposal(conv: str, slug: str, owner: str) -> str | None:
	"""A Pending or Approved File Box wiki note for ``slug`` that another upload of the
	same file bytes, by the same dropper, already proposed (#663), or None."""
	rows = frappe.db.sql(
		"""SELECT ar.name
		FROM `tabJarvis Approval Request` ar
		JOIN `tabJarvis Conversation` c ON c.name = ar.conversation
		JOIN `tabFile` f ON f.name = c.filebox_source_file
		JOIN `tabJarvis Conversation` this ON this.name = %(conv)s
		JOIN `tabFile` tf ON tf.name = this.filebox_source_file
		WHERE ar.source = %(src)s AND ar.ref_name = %(slug)s AND ar.status IN ('Pending', 'Approved')
		  AND ar.conversation != %(conv)s AND c.owner = %(owner)s
		  AND COALESCE(f.content_hash, '') != '' AND f.content_hash = tf.content_hash
		ORDER BY ar.creation DESC LIMIT 1""",
		{"conv": conv, "src": FILE_BOX_WIKI_SOURCE, "slug": slug, "owner": owner},
	)
	return rows[0][0] if rows else None


def _propose_file_box_wiki_write(args: dict, conv: str) -> dict:
	"""HOLD a File Box run's ``update_wiki`` as a Pending reviewer proposal
	instead of landing it (review-before-landing).

	An unattended file-box run may DRAFT a wiki note but must not write to the
	shared org wiki unreviewed. This parks the fenced write as a
	``Jarvis Approval Request`` (source :data:`FILE_BOX_WIKI_SOURCE`) carrying the
	raw args as ``wiki_payload``; a Jarvis reviewer - NOT the dropper (separation
	of duties) - approves it via ``approvals_api.approve_wiki_write``, which
	replays it through the SAME fenced funnel (:func:`_file_box_wiki_write`).
	Returns a raw-update_wiki-compatible result telling the model the note was
	recorded for reviewer approval (never that it landed), so it does not
	re-propose. Idempotent per (conversation, slug): a retried turn re-emitting
	the same write folds into the existing Pending row instead of stacking
	duplicates for the reviewer. Another upload of the same file proposes nothing
	when that file's note for the page is already Pending or Approved (#663).

	W: refuses BEFORE creating the Approval Request when the note would overflow
	the page (see :func:`jarvis.chat.wiki.file_box_append_would_overflow`) - a
	reviewer should never see a proposal that can only ever land as page_full - or
	any other refusal the funnel would make (a bad slug, a User-scope note, a new
	page without a title / page_type, a page the own-pages fence refuses)."""
	slug = (args.get("slug") or "").strip()
	if not slug:
		# A slugless update_wiki is degenerate - the fenced funnel derives no page
		# from it and refuses it anyway. Refuse at PROPOSE so slugless writes don't
		# all collapse onto a single ref_name="" dedupe row (each overwriting the
		# last). The model reads ok=False and moves on (best-effort).
		return {"ok": False, "reason": "no page slug - nothing proposed to the wiki"}
	# Dropper = the conversation owner (read server-side, never a client claim):
	# the AR is stamped to them, and it is the provenance ``user=`` the reviewer
	# replays the write under so the fenced own-pages rule keys on the dropper's
	# namespace, not the reviewer's.
	from jarvis.chat.approvals_api import wiki_digest

	owner = frappe.db.get_value("Jarvis Conversation", conv, "owner") or frappe.session.user

	# W pre-check: refuse BEFORE any Approval Request exists rather than filing one a
	# reviewer can never approve. Mirrors — in a SEPARATE function, not one shared
	# helper — the same merge arithmetic _file_box_wiki_write's funnel applies at
	# land time, so "would overflow" here PREDICTS a page_full refusal there too;
	# the two must be kept in sync by hand (a boundary test pins them in lockstep).
	# This is advisory only: approve-time re-runs the real fenced merge under
	# refuse_overflow=True and is the actual gate, so a failure here fails OPEN
	# (log + let the proposal proceed) rather than blocking a legitimate note.
	#
	# target_user is always None here, not args.get("target_user"): the funnel's
	# own `update` dict (_file_box_wiki_write, above) never forwards one either,
	# so predicting an overflow check the land-time funnel never actually runs
	# would be misleading.
	from jarvis.chat.turn_handler import _safe_label_name
	from jarvis.chat.wiki import (
		MAX_BODY_LEN,
		PAGE_TYPES,
		_log_page_full_refusal,
		_normalize_slug,
		file_box_append_would_overflow,
	)

	norm_slug = _normalize_slug(slug) or slug
	try:
		overflow = file_box_append_would_overflow(
			slug,
			args.get("append_md"),
			args.get("replace_body_md"),
			provenance_prefix=FILE_BOX_WIKI_FENCE,
			scope=args.get("scope"),
			target_user=None,
			reader=owner,
			title=args.get("title"),
			page_type=args.get("page_type"),
		)
	except Exception:
		frappe.log_error(title="jarvis.wiki.propose_overflow_check_failed", message=frappe.get_traceback())
		overflow = {"overflow": False, "existing_len": 0, "readable": True}
	refused = overflow.get("refused")
	if refused:
		# The funnel refuses it at land time too: never file a dead proposal.
		safe_slug = _safe_label_name(norm_slug)
		reasons = {
			"slug": "That page slug has no usable letters or digits - nothing was proposed. Use a slug "
			"like 'party-<name>'.",
			"user_scope": "A File Box note goes on the shared wiki, never a personal (User-scope) page - "
			"nothing was proposed. Record it again without a scope.",
			"identity": f"There is no wiki page '{safe_slug}' yet, and a new page needs a title and a "
			f"page_type (one of {', '.join(PAGE_TYPES)}) - nothing was proposed. Record it again "
			"with both.",
			"fenced": f"The wiki page '{safe_slug}' is kept by people or another feature, so a File "
			"Box run can't add to it - nothing was proposed. Record this note on a File Box page of "
			f"its own instead (a new slug, e.g. '{safe_slug}-<topic>').",
		}
		return {"ok": False, "reason": reasons[refused]}
	if overflow["overflow"]:
		incoming_len = len(str(args.get("append_md") or args.get("replace_body_md") or "").strip())
		_log_page_full_refusal(norm_slug, existing_len=overflow["existing_len"], incoming_len=incoming_len)
		safe_slug = _safe_label_name(norm_slug)
		if overflow["existing_len"] <= 0:
			# No existing text to blame - the note itself is too long for a new page.
			reason = (
				f"That note is too long on its own for a new wiki page '{safe_slug}' - "
				"nothing was proposed. Shorten it, or split it across more than one note."
			)
		elif overflow["readable"]:
			reason = (
				f"The wiki page '{safe_slug}' is full ({overflow['existing_len']} of "
				f"{MAX_BODY_LEN:,} characters) - nothing was proposed. Record this note on "
				f"a related page (a different slug, e.g. '{safe_slug}-<topic>') or shorten it."
			)
		else:
			# Never reveal how full an unreadable page is.
			reason = (
				f"The wiki page '{safe_slug}' can't take this note - nothing was proposed. "
				"Record it on a different page or shorten it."
			)
		return {"ok": False, "reason": reason}

	title = (args.get("title") or slug or "wiki note")[:100]
	payload = json.dumps(args, default=str, sort_keys=True)
	# Dedupe: one Pending wiki proposal per (conversation, slug). A retried turn
	# re-emitting the same write refreshes the held payload rather than stacking
	# duplicate rows the reviewer would have to triage.
	existing = frappe.db.get_value(
		"Jarvis Approval Request",
		{
			"conversation": conv,
			"source": FILE_BOX_WIKI_SOURCE,
			"status": "Pending",
			"ref_name": slug,
		},
		"name",
	)
	if existing:
		# Status-GUARDED refresh: only mutate the held payload while the row is
		# still Pending. If a reviewer decided it between the read above and here,
		# the refresh matches 0 rows and we fall through to a FRESH proposal - a
		# re-emitted write must never rewrite what a reviewer already approved /
		# rejected (they reviewed the payload they saw).
		# The refresh re-mints the digest in the same UPDATE (F8).
		frappe.db.sql(
			"""update `tabJarvis Approval Request`
			set wiki_payload=%s, wiki_digest=%s, title=%s, modified=%s, modified_by=%s
			where name=%s and status='Pending'""",
			(
				payload,
				wiki_digest(existing, conv, payload),
				title,
				frappe.utils.now(),
				frappe.session.user,
				existing,
			),
		)
		if frappe.db.get_value("Jarvis Approval Request", existing, "status") != "Pending":
			existing = None
	if not existing:
		earlier = _same_file_wiki_proposal(conv, slug, owner)
		if earlier:
			# #663: an upload of the same file again proposes the same note; one is
			# enough. Never rewritten: its digest binds it to its own conversation.
			return {
				"ok": True,
				"proposed": False,
				"approval": earlier,
				"detail": "the same file already proposed a note for this page (waiting for "
				"review or approved) - nothing new was proposed",
			}
	if existing:
		name = existing
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": title,
				"status": "Pending",
				"source": FILE_BOX_WIKI_SOURCE,
				"conversation": conv,
				"question": f"Wiki write proposed by a File Box run - refresh the page '{title}'.",
				# NB: the agent-generated summary is intentionally NOT copied to
				# context_md - the reviewer panel renders it (from wiki_payload) as
				# escaped text, and leaving context_md empty keeps this row off the
				# board's markdown v-html sink (get_approval -> renderMarkdown).
				"ref_name": slug,
				"wiki_payload": payload,
				"apply_status": "Pending",
			}
		)
		# ignore_permissions: the permlevel-1 fields (status / wiki_payload /
		# apply_status) and a row naming the dropper's conversation are both
		# beyond what the file-box run's session may write directly. The server
		# flag passes the AR tamper guard, which refuses every other writer.
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		name = doc.name
		# Minted post-name, in the same transaction, over the STORED payload (the
		# insert may have sanitized it; the raw-SQL refresh above stores it verbatim).
		frappe.db.set_value(
			"Jarvis Approval Request",
			name,
			"wiki_digest",
			wiki_digest(name, doc.conversation, doc.wiki_payload),
			update_modified=False,
		)
		# v16 set_user_and_timestamp stamps owner=session on insert; re-stamp the
		# dropper AFTER (the idiom chat_asks.materialize_from_turn uses).
		if doc.owner != owner:
			frappe.db.set_value("Jarvis Approval Request", name, "owner", owner, update_modified=False)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- proposal durable before tool reply
	return {
		"ok": True,
		"proposed": True,
		"approval": name,
		"detail": "recorded for reviewer approval (not yet on the wiki)",
	}


def _stamp_file_box_draft(conv: str, data) -> None:
	"""Record a File Box run's created draft (a SUBMITTABLE doctype) on its
	conversation: the first draft is the row's result, later ones only count."""
	doctype = data.get("doctype") if isinstance(data, dict) else None
	name = data.get("name") if isinstance(data, dict) else None
	if not (doctype and name):
		return
	try:
		if not frappe.get_meta(doctype).is_submittable:
			return
	except frappe.DoesNotExistError:
		return
	frappe.db.sql(
		"""UPDATE `tabJarvis Conversation`
		SET filebox_result_doctype = IF(COALESCE(filebox_result_name, '') = '', %(dt)s, filebox_result_doctype),
			filebox_result_name = IF(COALESCE(filebox_result_name, '') = '', %(name)s, filebox_result_name),
			filebox_result_count = COALESCE(filebox_result_count, 0) + 1
		WHERE name = %(conv)s""",
		{"dt": doctype, "name": name, "conv": conv},
	)


def _single_flight_error() -> dict:
	"""F16: a card already waits in this conversation. Cards never expire, so the
	model must stop until the user answers it."""
	return _error(
		"ConfirmationPendingError",
		"a confirmation card for a previous action is still awaiting the user in this "
		"conversation. Only one runs at a time - do NOT retry this call; stop and end "
		"your turn now. The card does not expire: the user confirms or discards it, and "
		"once they confirm you'll get a follow-up turn to continue with the next step.",
	)


def _run_tool(tool: str, raw_args: dict | str | None, *, conversation: str | None = None) -> dict:
	"""Parse args + dispatch + wrap in the bench's standard envelope.

	The translation layer between tool-level Python exceptions and
	the wire-shape ``{ok, data}`` / ``{ok, error}`` envelope. JarvisError
	subclasses (InvalidArgumentError, PermissionDeniedError,
	ToolNotFoundError, ...) carry their class name as the wire
	``code`` so the bench's admin_client + tests can branch on it
	without parsing the message.

	frappe.PermissionError is caught here so a tool that goes through
	``frappe.has_permission`` (rather than raising PermissionDeniedError
	itself) still translates to the bench's envelope rather than
	Frappe's native 403 page. Anything else (programming errors, real
	exceptions) is audited (for write tools) and re-raised to Frappe's
	native handler so a 500 surfaces at the seam where the bug lives.

	Replaces the old _dispatch_safe + _parse_args dance: parse_args
	used to mix "dict-or-error-envelope" returns with this function's
	try/except, splitting the translation across two helpers. Folded
	into one to match the reviewer's "native handler" pattern note
	from the 2026-06-16 punch list.
	"""
	# S6: a tool body (e.g. run_method -> call_tool) must never re-enter the gate.
	refuse_in_tool_dispatch()
	# #493: "Enable Business Wiki" is the operator's only wiki kill switch, so it
	# must refuse the agent-facing wiki tools too, not only the automatic
	# behaviours. Checked HERE, ahead of everything, because update_wiki is a
	# _GATED_WRITES tool: without this it parks a confirmation card on a workspace
	# whose wiki UI is hidden, and the card's only possible outcome is the very
	# refusal below. The tools carry the same gate themselves, so no dispatch path
	# is left open; this one exists to stop the card, not to be the only guard.
	if tool in _WIKI_TOOLS:
		from jarvis.chat.wiki import WIKI_DISABLED_MESSAGE, wiki_enabled

		if not wiki_enabled():
			code = FeatureDisabledError.__name__
			return _error(code, WIKI_DISABLED_MESSAGE, hint=_hint_for(code, ""))

	is_write = tool in _WRITE_TOOLS
	try:
		args = _parse_args(raw_args, tool)
	except JarvisError as e:
		return _error(type(e).__name__, str(e))

	# Write-risk guard (round 2, jarvis.tools._write_risk): ONE choke point, ahead of
	# every route below (File Box, the uncarded modes, the park). A structure change
	# is refused here with its Desk path and a ``refused`` audit row; sensitive configuration comes back as "sensitive" and
	# must park behind its own card in EVERY mode (``_must_card`` below). The ORM
	# guard on the save itself backs this up for what arguments cannot show.
	# A guarded structure write (one new Custom Field, a column-free Custom Field
	# edit; R2-10) comes back as its own class: it parks a card in every mode too,
	# on a chat that can hold a pending-action card, and is refused anywhere else.
	_risk = None
	_guarded = False
	if is_write:
		from jarvis.tools import _write_risk

		try:
			_risk = _write_risk.check(tool, args, guarded=True)
		except WriteRefusedError as e:
			return _refuse_risky_write(tool, args, e)
		_guarded = _risk in _write_risk.GUARDED_STRUCTURE
		if _guarded and not _can_card_structure(conversation):
			return _refuse_risky_write(tool, args, _write_risk.refusal_of(tool, args))
	# Sensitive configuration never runs uncarded (R2-8): auto mode, "confirm all",
	# an armed macro and an approved skill run all fall through to the park. Nor
	# does a run_method or a workflow action that deletes or cancels: those verbs are
	# braked as tools (_BRAKE), and nothing else may be the way around that.
	_must_card = (
		_risk == "sensitive"
		or _guarded
		or (tool == "run_method" and _run_method_brakes(args))
		or (tool == "apply_workflow_action" and _workflow_brakes(args))
	)
	# Nor does a write to a skill or a learned skill, whichever tool names it: what
	# they say is what later chats do, so a person sees each change. Two calls are
	# refused outright, before a card nobody could confirm is parked: a run_method
	# the tool itself refuses, and one that switches a skill's Approve & run on.
	if tool in _GATED_WRITES:
		from jarvis.tools import _write_risk

		try:
			denied = _calls_a_denied_method(args) if tool == "run_method" else None
			if denied:
				_write_risk.log_line("denied_method", "", denied, "refused", tool=tool)
				return _error("PermissionDeniedError", f"method {denied!r} cannot be called from a tool")
			if _arms_a_skill(tool, args):
				_write_risk.log_line("instructions", "Jarvis Custom Skill", "", "refused", tool=tool)
				return _error("PermissionDeniedError", _ARMING_REFUSED)
			_must_card = _must_card or _writes_skill_config(tool, args)
		except InvalidArgumentError as e:
			return _error(type(e).__name__, str(e))
		except RecursionError:
			_must_card = True  # arguments too deep to read: a person looks at them

	# File Box write policy (PR-2c): FIRST, after the P0d normalisation, so an
	# unattended File Box run never reaches the preview / park / auto-apply paths
	# below with a write the policy did not decide. None = not its call.
	if conversation and is_write:
		from jarvis.chat import held_writes

		verdict = held_writes.apply(tool, args, conversation)
		if verdict is not None:
			return verdict
	if _must_card and tool == "run_import":
		# Every mode (after the File Box policy, which refuses imports its own way): a
		# sensitive import cannot be carded in full (owner default, J1-cards review).
		return _refuse_sensitive_import(tool, args)
	# An armed macro's run follows only skills its owner controls: judged here, on the
	# row this fetch serves, before its body goes back (``macros.check_a_fetched_skill``).
	skill_note = ""
	if conversation and tool == "get_skill":
		from jarvis.chat import macros

		skill_note = macros.check_a_fetched_skill(
			conversation, args.get("skill_name") if isinstance(args, dict) else None
		)
	# File Box skill routing: only an eligible skill loads (and is recorded).
	if conversation and tool == "get_skill":
		from jarvis.chat import filebox_skills

		verdict = filebox_skills.gate_get_skill(args, conversation)
		if verdict is not None:
			return _with_note(verdict, skill_note)

	# ``preview`` is read, not popped: dispatch() filters args to the tool's
	# signature so the flag never reaches the tool anyway, and leaving ``args``
	# unmutated keeps the shared dict the session-persistence path holds intact.
	#
	# ``and tool not in _GATED_WRITES``: the model-facing preview branch is a
	# dry-run that only rolls back DB writes - inline non-DB side effects fired
	# directly inside hooks (an on_submit that POSTs/emails, a run_method target
	# with real effects) STILL fire with no confirmation. Every _PREVIEWABLE
	# tool is also gated, so a model could otherwise call a gated write with
	# preview=True and trigger those side effects while dodging the gate. Gated
	# tools therefore always fall through to the gate/park below, which builds
	# its own preview via _pending_preview - the model never needs (nor is
	# allowed) preview=True on a gated write.
	if (
		isinstance(args, dict)
		and _as_bool(args.get("preview"))
		and tool not in _GATED_WRITES
		and not (is_write and _is_bulk_call(args))
	):
		if tool not in _PREVIEWABLE:
			return _error("InvalidArgumentError", f"preview is not supported for {tool}")
		# A dry-run: surface its validation errors, but never audit - nothing
		# is committed, so there is no write to record.
		try:
			return {"ok": True, "data": _run_preview(tool, args)}
		except (JarvisError, frappe.PermissionError, frappe.ValidationError, frappe.DuplicateEntryError) as e:
			return _preview_error(e)

	# Write-safety confirmation gate (issue #186): a gated write is NEVER
	# executed on the model path. Park it - build a preview, mint a single-use
	# token bound to the acting user + conversation, and return a non-executing
	# ``pending_confirmation`` status. Only ``confirm_tool`` (a human click) can
	# then run the stored call via ``dispatch_confirmed``. CRITICAL: the token
	# is stored, not returned - the model must not see it. It is delivered to
	# the UI out-of-band below, over the realtime channel (Task 3).
	# A BULK write ALWAYS gates - even a normally-ungated light write (add_tag /
	# add_comment / follow / unshare / unassign): a 20-record mass mutation is a
	# batch the human should confirm, and the plugin/persona promise exactly one
	# card for it. Non-_PREVIEWABLE bulk writes get a described-intent card via
	# _pending_preview (no sandbox); single calls to these tools are unchanged.
	if tool in _GATED_WRITES or (is_write and _is_bulk_call(args)):
		from jarvis.chat import events, pending_confirm
		from jarvis.tools._bulk import _MAX_BATCH

		# preview=True on a gated write is a category error (issue #186, #14):
		# the model never needs preview here - it calls the tool directly and the
		# bench shows a confirmation card. Silently parking a preview=True call was
		# confusing for a transition-window model that used preview to dry-run.
		# Return a legible signal instead of a premature pending card. (We do NOT
		# sandbox-execute - that path fires inline non-DB side effects unconfirmed.)
		if isinstance(args, dict) and _as_bool(args.get("preview")):
			return _error(
				"InvalidArgumentError",
				f"preview is not needed for {tool}: call it directly and the "
				"bench will show a confirmation card",
			)

		# Batch cap at PARK (F16): a bulk call over the shared max bounces to the
		# model now with a split-and-sequence instruction, instead of parking a
		# card that only dies at execution. create/update/create_docs already
		# bounce via their park-time dry-run (run_atomic_batch), but the
		# consequential bulk writes (submit/cancel/delete/amend/workflow) take a
		# described-intent preview with NO dry-run, so without this an over-size
		# batch would fail only AFTER the user confirmed. One rule for all bulk.
		if _is_bulk_call(args):
			batch_n = _bulk_len(args)
			if batch_n > _MAX_BATCH:
				# Uncarded-run-aware wording (minor, review): under an approved skill run OR a
				# request-scoped "confirm all" there is no card to confirm - the covered
				# allowlist runs uncarded - so telling the model to "confirm each one" is
				# actively wrong there. A cheap read (this is a one-shot park-time rejection,
				# not a hot loop) picks the matching instruction; everything else about F16
				# (the cap itself) is unchanged.
				_autorun_flags = (
					frappe.db.get_value(
						"Jarvis Conversation",
						conversation,
						["skill_autorun", "request_autorun", *_auto_mode_fields()],
						as_dict=True,
					)
					if conversation
					else None
				) or {}
				if (
					_autorun_flags.get("skill_autorun")
					or _autorun_flags.get("request_autorun")
					or _autorun_flags.get("auto_mode")
				):
					next_step = (
						f"Split into batches of {_MAX_BATCH}; the approved run keeps executing "
						"each batch automatically before the next one starts - there is nothing "
						"to confirm."
					)
				else:
					next_step = (
						f"Split into batches of {_MAX_BATCH} and confirm each one before starting the "
						"next (only one card can wait at a time: propose one batch, then end your turn)."
					)
				return _error(
					"InvalidArgumentError",
					f"too many records in one batch ({batch_n}); the max is {_MAX_BATCH}. {next_step}",
				)

		# The parked call binds to the conversation resolved server-side upstream
		# (from the session_key). When it is unresolved the token falls back to ""
		# and is bound by OWNER alone, which is the real security boundary;
		# conversation binding is a secondary replay guard. ``run_id`` is not
		# tracked on this path, so the token carries "" and the sibling-run filter
		# in pending_confirm.clear_for_conversation no-ops.
		conv = conversation or ""
		run_id = ""
		# Two identities (issue #186, #1/#5/#6):
		#   owner_user = the CONVERSATION OWNER - the human who sees the card,
		#     clicks Confirm, and whose browser is subscribed. Deliver + bind +
		#     confirm all key off THIS user. In a shared conversation it is the
		#     owner, not necessarily the acting user (frappe.session.user).
		#   exec_user = frappe.session.user - the scoped model-execution identity
		#     the confirmed write must run AS, so a confirm can never exceed the
		#     model path's permission scope.
		# Fall back to the acting user when the conversation/owner cannot be
		# resolved (managed direct-Python calls) so the gate still functions.
		exec_user = frappe.session.user
		owner_user = (
			frappe.db.get_value("Jarvis Conversation", conv, "owner") if conv else None
		) or exec_user
		# One flags read shared by the skip-the-card bypasses below (all key off the
		# conversation row). An empty/unresolved conv -> {} -> every flag OFF (the
		# safe default: the write parks). skill_autorun + skill_autorun_at +
		# skill_autorun_skill ride the SAME single query (design §3.4): the skill
		# auto-run branch needs the flag, its sliding timestamp for the inline TTL
		# check, and the armed skill docname for the LIVE disarm re-check (C2). The
		# request-scoped "confirm all" branch (design Layer B) adds request_autorun +
		# its sliding request_autorun_at to the same read (no request_autorun_msg -
		# that is observability-only, not read in the hot gate).
		_conv_flags = (
			frappe.db.get_value(
				"Jarvis Conversation",
				conv,
				[
					"file_box",
					"skip_confirmation",
					"skill_autorun",
					"skill_autorun_at",
					"skill_autorun_skill",
					"request_autorun",
					"request_autorun_at",
					*_auto_mode_fields(),
				],
				as_dict=True,
			)
			if conv
			else None
		) or {}
		# File Box direct-apply (a draft of a submittable doctype) and the held
		# writes are decided by held_writes.apply at the top of _run_tool; a File
		# Box write never reaches this point except update_wiki (below). Its audit
		# provenance stays "auto_apply" (the Jarvis Agent Write enum value).
		# File Box unattended wiki write-back: route update_wiki through the
		# append-only, provenance-FENCED funnel (never the raw update_wiki tool,
		# which defaults scope=Org + ignore_permissions and can clobber a curated
		# page). ``and file_box`` (NOT ``or``) keeps attended chat + admin auto_apply
		# on the park-a-card path. Placed BEFORE the single-flight so an unattended
		# run never parks a wiki card. The wiki
		# kill-switch at the top of _run_tool already refused a wiki-off write before
		# here; stray batch args never divert it to a park card (whose confirm would
		# run the raw, unfenced tool). Placed ABOVE the armed-macro, skill-autorun and
		# "confirm all" branches too, so no uncarded run pre-empts the fence into the
		# raw covered write (a typed "confirm all" arms request_autorun on any chat).
		if conv and tool == "update_wiki" and _conv_flags.get("file_box"):
			# REVIEW-BEFORE-LANDING: an unattended file-box run never writes the
			# shared org wiki directly. HOLD the fenced write as a Pending reviewer
			# proposal (approvals_api.approve_wiki_write replays it through the same
			# funnel on approval). Enforced HERE in the dispatch so the agent cannot
			# bypass review by any prompt path. _propose_file_box_wiki_write returns
			# the raw-update_wiki-compatible result as the DATA payload; wrap it in
			# the standard envelope so the agent-session path's result["data"] read
			# stays valid and the model reads it like a dispatched update_wiki result.
			return {"ok": True, "data": _propose_file_box_wiki_write(args, conv)}
		# Armed-skip bypass (macro skip-confirmation): an armed macro's run
		# conversation carries skip_confirmation=1 (stamped by run_macro), so the
		# BROAD covered set - incl. run_method / submit / send_email / run_import -
		# runs uncarded. This is distinct from and wider than the create/update-only
		# File Box fast-path (held_writes.apply). The irreversible trio (delete/cancel/amend) is NOT in
		# _ARMED_SKIP_COVERED, so it falls through to park (an armed macro that hits
		# one stops the run - D5). Cheap frozenset membership test first; the
		# kill-switch Settings read runs only when a covered write is actually armed.
		# Bulk covered writes skip too (an automation must not stall on a batch card);
		# the F16 over-size cap above still bounces an oversized batch. A write the
		# write-risk guard must card (``_must_card``) parks. So does a write to a skill
		# (``_writes_skill_config``, part of ``_must_card``): a macro step applies
		# skills, so an armed run must not rewrite what its own later runs follow.
		armed = bool(tool in _ARMED_SKIP_COVERED and not _must_card and _conv_flags.get("skip_confirmation"))
		if armed and not _armed_skip_disabled():
			# Provenance for the receipt (T8): which armed macro authorized this uncarded
			# write. Always label an armed write (we are in the armed branch, so it IS
			# armed): resolve the human macro_name (Jarvis Macro autoname is a hash, so the
			# run's `macro` docname is opaque) and fall back to a generic marker if the
			# run-row lookup misses (the abnormal lingering-flag state) - never silently
			# unlabelled. The covered-write CORE (dispatch + run_method read-filter +
			# run_import announcement + provenance stash) is shared with the skill branch
			# via _run_covered_write so the two can never drift apart again.
			_armed_run_macro = frappe.db.get_value(
				"Jarvis Macro Run", {"conversation": conv, "status": "running"}, "macro"
			)
			_macro_name = (
				frappe.db.get_value("Jarvis Macro", _armed_run_macro, "macro_name")
				if _armed_run_macro
				else None
			) or "an armed macro"
			armed_write_before = frappe.flags.get(ARMED_MACRO_WRITE_FLAG)
			frappe.flags[ARMED_MACRO_WRITE_FLAG] = True
			try:
				return _run_covered_write(
					tool,
					args,
					conv=conv,
					owner_user=owner_user,
					provenance_kind="macro",
					provenance_name=_macro_name,
				)
			finally:
				frappe.flags[ARMED_MACRO_WRITE_FLAG] = armed_write_before
		# Skill "Approve & run" auto-run bypass (design §3.4): a conversation in an
		# APPROVED skill run (skill_autorun=1, stamped by approve_and_run on step-1
		# success) runs the explicit _SKILL_AUTORUN_COVERED allowlist uncarded, sliding
		# skill_autorun_at forward on each covered write. Placed AFTER the macro branch
		# so a (structurally-unreachable, but defense-in-depth) both-flags conversation
		# resolves as a macro run for deterministic provenance. The irreversible trio +
		# create_custom_skill are NOT covered here, so they fall through to park + PAUSE
		# the run. Narrower than the macro's _ARMED_SKIP_COVERED (no create_custom_skill)
		# and additionally gated on a SLIDING TTL + a bench-guaranteed Halt cancel-gate.
		if tool in _SKILL_AUTORUN_COVERED and not _must_card and _conv_flags.get("skill_autorun"):
			from jarvis.chat import turn_message_binding

			# Cancel-gate FIRST (the Halt compensating control): stop_run sets a
			# transport-independent run-cancel signal; if it is set, HARD-STOP this
			# covered write - clear the flag, clear the signal, and REFUSE the write
			# WITHOUT executing it. This is what makes Halt stop the auto-run chain
			# within one write, bench-guaranteed, regardless of the container honouring
			# chat_abort. Checked before the TTL so a halted stale run still stops here.
			if turn_message_binding.is_run_cancel_requested(conv):
				_skill_autorun_clear(conv)
				turn_message_binding.clear_run_cancel(conv)
				code = RunHaltedError.__name__
				return _error(code, "the run was halted - re-approve to continue")
			# LIVE disarm re-check (C2 - the documented kill lever): re-read
			# allow_approve_run off the EXACT armed skill docname this run was opened on
			# (stamped on skill_autorun_skill by approve_and_run). An admin un-arming the
			# skill mid-run - or a run whose docname is missing/inconsistent - must STOP
			# the auto-run, not keep skipping cards. When it is falsy, HARD-STOP: clear
			# the flag and refuse WITHOUT dispatching, within one write, no deploy. Placed
			# after the cancel-gate and before the TTL/dispatch so it fires on every
			# would-be uncarded covered write regardless of the run's age.
			autorun_skill = _conv_flags.get("skill_autorun_skill")
			if not autorun_skill or not frappe.db.get_value(
				"Jarvis Custom Skill", autorun_skill, "allow_approve_run"
			):
				_skill_autorun_clear(conv)
				return _error(RunDisarmedError.__name__, "the skill was disarmed - run stopped")
			# No-activity net: auto-run only while the run was active recently. The
			# stamp moves on each covered write and when one of the run's cards is
			# confirmed (``turn_message_binding.keep_skill_autorun_open``), so waiting on
			# a card never costs the run. A missing stamp, or one older than the net,
			# means a run nothing is driving (a worker died): DON'T auto-run - fall
			# through to the normal park below (the reaper clears the stale flag; a park
			# here is a safe, correct pause).
			autorun_at = _conv_flags.get("skill_autorun_at")
			if (
				autorun_at
				and (frappe.utils.now_datetime() - frappe.utils.get_datetime(autorun_at)).total_seconds()
				<= _SKILL_AUTORUN_IDLE_S
			):
				# The covered-write CORE (dispatch + run_method read-filter + run_import
				# announcement + provenance stash) is shared with the macro branch via
				# _run_covered_write so the two can never drift; the skill run's provenance is
				# the armed skill docname (queryable as armed_by_skill). The slide/clear below
				# is skill-only post-dispatch handling and stays in this branch.
				try:
					result = _run_covered_write(
						tool,
						args,
						conv=conv,
						owner_user=owner_user,
						provenance_kind="skill",
						provenance_name=autorun_skill,
					)
				except Exception:
					# A write that raised ends the run like one that answered ok:False.
					# Roll back first: the clear commits, and must not keep what the
					# failed write left half done.
					frappe.db.rollback()
					_skill_autorun_clear(conv)
					raise
				# Hard-stop-on-error: the first covered ok:False clears the flag so the
				# next write re-cards; a success slides the timestamp forward. On failure,
				# append a one-line note to the tool's OWN error so the model (and the
				# transcript) sees that the approved run also ended - not just that this
				# one call failed - so it re-cards the next covered write instead of
				# retrying it uncarded (agent legibility).
				if result.get("ok"):
					_skill_autorun_slide(conv)
				else:
					_skill_autorun_clear(conv)
					err_obj = result.get("error")
					if isinstance(err_obj, dict) and err_obj.get("message"):
						err_obj["message"] += " The approved run has also ended - re-approve to continue."
				return result
			# No recent activity / no timestamp: fall through to the normal park.
		# Request-scoped "confirm all" (design Layer B): the user approved this whole
		# request's fan-out at once, so a COVERED write (_GATED_WRITES - _BRAKE) runs
		# uncarded while the request is live - armed by _typed_confirmation / the upfront
		# detector, sliding request_autorun_at within the TTL. The brake is NOT covered,
		# so delete/cancel/amend/create_custom_skill/connector still park (the brake
		# protects what the user has not seen). Placed AFTER the skill-autorun branch (an
		# approved skill run keeps its own provenance) and AFTER the File Box wiki fence.
		# Bulk covered writes skip too (one approval covers the batch); the F16 over-size
		# cap above still bounces an oversized batch.
		if tool in _COVERED and not _must_card and _conv_flags.get("request_autorun"):
			from jarvis.chat import turn_message_binding

			# Cancel-gate FIRST (Halt): stop_run sets a transport-independent run-cancel
			# signal; if set, HARD-STOP this covered write - clear the flag, clear the
			# signal, and REFUSE without executing, so Halt stops the request chain within
			# one write regardless of the container honouring chat_abort.
			if turn_message_binding.is_run_cancel_requested(conv):
				_request_autorun_clear(conv)
				turn_message_binding.clear_run_cancel(conv)
				return _error(RunHaltedError.__name__, "the run was halted - re-approve to continue")
			# Sliding TTL: run uncarded only while the last covered write is recent. A
			# missing/expired timestamp (a finished request, or a stranded flag after a
			# worker death) does NOT auto-run - fall through to the normal park (a safe
			# re-card); the reaper clears the stale flag.
			autorun_at = _conv_flags.get("request_autorun_at")
			if (
				autorun_at
				and (frappe.utils.now_datetime() - frappe.utils.get_datetime(autorun_at)).total_seconds()
				<= _REQUEST_AUTORUN_TTL_S
			):
				result = _run_covered_write(
					tool,
					args,
					conv=conv,
					owner_user=owner_user,
					provenance_kind="request",
					provenance_name="",
				)
				# Hard-stop-on-error: the first covered ok:False clears the flag so the next
				# write re-cards; a success slides the timestamp forward. On failure, append a
				# one-line note to the tool's OWN error so the model re-cards the next covered
				# write instead of retrying it uncarded (agent legibility).
				if result.get("ok"):
					_request_autorun_slide(conv)
				else:
					_request_autorun_clear(conv)
					err_obj = result.get("error")
					if isinstance(err_obj, dict) and err_obj.get("message"):
						err_obj["message"] += " The approved request has also ended - re-approve to continue."
				return result
			# TTL-expired / no timestamp: fall through to the normal park.
		# Per-chat auto mode (#581): the user chose it with the chat's first message and
		# it never turns off, so unlike request_autorun there is no TTL and a failed
		# write does not end it. The brake is not covered, so delete/cancel/amend/
		# create_custom_skill/connector writes still park below. Placed AFTER the
		# request branch (an armed request keeps its own provenance and TTL) and after
		# the File Box wiki fence. Halt refuses the write and clears the cancel signal
		# but leaves the mode on (it is a chat property, not a run).
		if tool in _COVERED and not _must_card and _conv_flags.get("auto_mode"):
			from jarvis.chat import turn_message_binding

			if turn_message_binding.is_run_cancel_requested(conv):
				# Consumed like the skill/request branches: one Stop refuses one write.
				# It cannot hold for the rest of the run, because the gate does not know
				# which run a call belongs to and a lingering signal would also refuse a
				# message that was already queued behind the stopped reply.
				turn_message_binding.clear_run_cancel(conv)
				return _error(RunHaltedError.__name__, "the run was halted")
			return _run_covered_write(
				tool,
				args,
				conv=conv,
				owner_user=owner_user,
				provenance_kind="auto_mode",
				provenance_name="",
			)
		# Sequential confirmation (F16): at most ONE live confirmation card per
		# conversation. If one is already awaiting the user here, REFUSE to park a
		# second and tell the model to stop - the continuation turn fired after the
		# pending card is confirmed + executed is where it issues the next batch.
		# This makes "batch 2's card appears only after batch 1 completes" a bench
		# guarantee (not just persona discipline) and is the server-side
		# single-flight for the confirm path. Auto-apply above is deliberately NOT
		# gated by this (it parks no card). Cards never expire, so an ignored card
		# yields only to a proposal made after the user spoke again (D7 supersede,
		# re-checked under park's lock). Strict on the conversation: a
		# conversation-LESS token (F1) must not block a legitimate new card here.
		try:
			conversation_pending = conv and pending_confirm.blocks_new_card(owner_user, conv)
		except pending_confirm.PendingConfirmStorageError:
			return _error(
				"ConfirmationUnavailableError",
				"could not check whether another confirmation is already pending "
				"(a storage error). Nothing was changed. You may retry this exact "
				"call once; if it still fails, tell the user and stop - do not loop.",
			)
		if conversation_pending:
			return _single_flight_error()
		# Validate BEFORE parking. For a create/update (build-from-args) write,
		# run the real call in the rollback sandbox now: a deterministic failure
		# (missing mandatory field, bad link, no create permission) means the
		# confirmed write would fail identically - preview and confirm build the
		# same doc as the same exec_user - so return the error to the model NOW
		# instead of showing a confirmation card that dies on click. clear_messages
		# so the validation msgprint does not leak into the turn (mirrors
		# preview_doc). Every other gated write (submit/cancel/delete/amend since
		# R2-2, send_email/run_method/create_custom_skill/update_wiki) parks a
		# described-intent card via _pending_preview: nothing runs until Confirm.
		_structure_lines = None
		if tool == "run_import":
			# run_import cannot be sandbox-run (staging + a background job), so it has its
			# own pre-park validation: refuse a blocked / non-importable / non-admin import
			# BEFORE minting a token, exactly as _DRY_RUN_ON_PARK refuses a doomed create.
			rejected, preview = _import_park(args)
			if rejected is not None:
				frappe.clear_messages()
				return rejected
		elif _guarded:
			# No trial run (R2-10): the save would commit its row and then run DDL.
			# The park checks refuse what is known to fail, naming the field.
			rejected, preview, _structure_lines, canonical = _structure_park(tool, args, _risk)
			if rejected is not None:
				frappe.clear_messages()
				return rejected
			# From here on the call IS its stored form (a new dict: the caller's own
			# arguments are left as they were): the card is built from it, it is what
			# gets sealed, and it is what Confirm runs.
			args = canonical
		elif tool in _DRY_RUN_ON_PARK:
			try:
				preview = _run_preview(tool, args)
			except (
				JarvisError,
				frappe.PermissionError,
				frappe.ValidationError,
				frappe.DuplicateEntryError,
			) as e:
				frappe.clear_messages()
				return _preview_error(e)
		else:
			try:
				preview = _pending_preview(tool, args)
			except PreviewSandboxLost as e:
				frappe.clear_messages()
				return _preview_error(e)
		# Render-ready confirmation summary (F9) + wall-clock expiry (F15), attached
		# ONCE here at park: F2 stores the preview in the token record and resync
		# returns it verbatim, so the card + expiry ride the event, the record, and
		# every resync identically (never rebuilt -> cannot diverge). build_card
		# returns None for uncovered shapes; the SPA falls back to the raw preview.
		# time comes from the module import - a local import here would shadow
		# it for ALL of _run_tool and break the read path's perf_counter.
		from jarvis.chat import confirm_card

		# "Approve & run" offer (skill "Approve & run the plan", design §3.3): if the
		# turn was triggered by a message invoking exactly one live-armed custom skill
		# - resolved forgery-proof under the MESSAGE OWNER via the turn->message
		# binding - stamp that skill's docname on the token so a later approve_and_run
		# can authorize off it, and flag the card so the frontend can show the
		# affordance (P0 = just the flag; the plan outline is a later task). Best-effort
		# + fail-safe: no offer on any doubt, and it NEVER sets the run flag or
		# auto-runs (that is a later task) - it only decorates this park.
		#
		# Offer ONLY on a _SKILL_AUTORUN_COVERED first write (I4): a destructive /
		# create_custom_skill park is a _SKILL_AUTORUN_NEVER tool that approve_and_run
		# refuses to run as step 1, so stamping it would dangle an unusable offer and
		# invite an approve-a-NEVER-tool click. A NEVER first write parks as an ordinary
		# card with no run offer.
		# Never on a structure card: confirming it must not also open an approved run.
		skill_docname, skill_slug = (
			_resolve_approve_run_offer(conv)
			if tool in _SKILL_AUTORUN_COVERED and not _guarded
			else (None, None)
		)
		if isinstance(preview, dict):
			# A draft-panel write turned into this card (actions_api._park_sensitive):
			# the clients read it to keep that draft "waiting" across a reload.
			from_draft = frappe.flags.get("jarvis_park_from_draft")
			if from_draft:
				preview["from_draft"] = from_draft
			card, refusal = _build_park_card(
				tool,
				args,
				preview,
				sensitive=_risk == "sensitive",
				structural=_structure_lines,
			)
			if refusal is not None:
				frappe.clear_messages()
				return refusal
			preview["card"] = card
			if from_draft and isinstance(preview.get("card"), dict):
				# On the card too: a reloaded desktop seeds the card from its message
				# row (pending_card), which carries only the card.
				preview["card"]["from_draft"] = from_draft
			if skill_docname and isinstance(preview.get("card"), dict):
				preview["card"]["approve_run"] = True
				preview["card"]["skill_slug"] = skill_slug
		# A pending-action card never expires; a legacy (Redis) token keeps its 15 minutes.
		now_s = int(time.time())
		expires_at = None if pending_confirm.pa_minting(conv) else now_s + pending_confirm._TTL_S
		try:
			token = pending_confirm.mint(
				conversation=conv,
				owner=owner_user,
				tool=tool,
				args=args,
				run_id=run_id,
				exec_user=exec_user,
				preview=preview,
				expires_at=expires_at,
				skill_docname=skill_docname,
			)
		except pending_confirm.ConfirmationPendingError:
			# Park re-checks single-flight under its lock: a card that could not be
			# superseded after all (another actor holds it) is not a storage error.
			return _single_flight_error()
		# mint returns None when it could not stage the park (a transient cache
		# failure that it already rolled back, so nothing is persisted). Do NOT
		# publish a card against a token whose record does not exist - that card is
		# un-confirmable and wedges the turn on an "expired" toast. Surface a
		# RETRYABLE tool error instead: nothing changed, so the model can simply call
		# the exact same tool again and the confirmation card will appear.
		if not token:
			return _error(
				"ConfirmationUnavailableError",
				"could not stage the confirmation for this action (a storage error). "
				"Nothing was changed. You may retry the exact same call once; if it "
				"still fails, tell the user the confirmation could not be shown right "
				"now and stop - do not loop.",
			)
		# Durable pending action-row (PR 1): the card rides the message pipeline so a
		# reload always shows it (reload-reliable), not only via the best-effort push
		# below. Conv-less parks ("") can't host a row -> the list-pending backstop
		# delivers those (no reliability gain there, no regression). FAIL-CLOSED: if the
		# row write fails after a successful mint, roll the token back (no orphan token
		# that the single-flight guard would treat as a live card and wedge the retry)
		# and surface the retryable error - nothing ran.
		if conv:
			try:
				persist_pending_action(conv, tool, preview, token, expires_at)
			except Exception:
				# Discard any partial row write so "nothing changed" stays airtight even
				# if a future controller hook fails post-SQL, THEN roll the Redis token
				# back (no orphan) and surface the retryable error.
				frappe.db.rollback()
				frappe.log_error(
					title="persist_pending_action failed; rolling back token",
					message=frappe.get_traceback(),
				)
				pending_confirm.rollback_token(token, owner_user)
				return _error(
					"ConfirmationUnavailableError",
					"could not stage the confirmation for this action (a storage error). "
					"Nothing was changed. You may retry the exact same call once; if it "
					"still fails, tell the user the confirmation could not be shown right "
					"now and stop - do not loop.",
				)
		# Deliver the token to the human's UI out-of-band, over the realtime
		# channel, NEVER via the function return below - the model must never
		# see it. Published to the OWNER (the subscribed browser), not the acting
		# session user. Best-effort: a publish hiccup must not crash the tool call
		# or the turn, and must NOT execute the write - the token still lives
		# in pending_confirm either way, so a retry or a future resync can
		# still surface it.
		try:
			origin_page = frappe.db.get_value("Jarvis Conversation", conv, "origin_page") or ""
			# Same shared item shape the resync endpoint + run:end terminal use, so
			# the live push can't drift from them (and gets the summary guard too).
			item = pending_confirm._pending_item(
				token=token,
				tool=tool,
				args=args,
				preview=preview,
				conversation=conv,
				run_id=run_id,
				expires_at=expires_at,
				created_at=now_s,
			)
			events.publish_to_user(
				owner_user,
				{
					"kind": "action:pending",
					"origin_page": origin_page,
					**pending_confirm.with_recency([item])[0],
				},
			)
		except Exception:
			frappe.log_error(
				title="action:pending publish failed",
				message=frappe.get_traceback(),
			)
		# The model-facing return carries the raw preview but NOT the human ``card``
		# (it is duplicate UX for the model's context; the model gets tool + args +
		# would already).
		model_preview = (
			{k: v for k, v in preview.items() if k != "card"} if isinstance(preview, dict) else preview
		)
		return {
			"ok": True,
			"data": {
				"status": "pending_confirmation",
				"preview": model_preview,
				"tool": tool,
			},
		}

	# Read-path telemetry; fast no-op for untracked tools, never raises.
	t0 = time.perf_counter()
	result, read_note = _dispatch_judging_skills(tool, args, is_write, conversation)
	telemetry.record_tool(
		tool=tool,
		args=args,
		conversation=conversation,
		duration_ms=int((time.perf_counter() - t0) * 1000),
		result=result,
	)
	return _with_note(result, skill_note or read_note)


def _dispatch_judging_skills(tool: str, args, is_write: bool, conversation) -> tuple[dict, str]:
	"""``_dispatch_and_wrap``, and the note for the agent ("" when nothing changed).

	An armed macro's run follows only skills its owner controls, and a skill's text
	reaches the turn by more routes than ``get_skill`` (judged in ``_run_tool``):
	``find_skills`` lists only skills the owner controls (``ARMED_SKILL_OWNER_FLAG``),
	and a read that returns rows of a skill doctype (``_reads_skill_rows``) is judged
	on them (``macros.check_skill_rows_read``), the chat disarmed and committed before
	they go back. Nothing is read for a chat that is not armed but its flag, and only
	for those two."""
	if not conversation or is_write or tool == "get_skill":
		return _dispatch_and_wrap(tool, args, is_write), ""
	from jarvis.chat import macros

	if tool == "find_skills":
		armed = macros.armed_skill_owner(conversation)
		before = frappe.flags.get(ARMED_SKILL_OWNER_FLAG)
		frappe.flags[ARMED_SKILL_OWNER_FLAG] = armed[1] if armed else None
		try:
			return _dispatch_and_wrap(tool, args, is_write), ""
		finally:
			frappe.flags[ARMED_SKILL_OWNER_FLAG] = before
	# A skill list is asked, in an armed chat only, for the field that says which skill
	# each row is of under a name of its own (``_JUDGE_ALIAS``), judged on that, and the
	# field taken out again before the rows go back: the read is judged on exactly its
	# rows, and the columns the agent asked for come back as it asked for them.
	key = _judging_key(tool, args)
	if key and not macros.armed_skill_owner(conversation):
		key = None
	alias = f"{_JUDGE_ALIAS}{key}" if key else None
	judged = {**args, "fields": [*(args.get("fields") or ["name"]), f"{key} as {alias}"]} if key else args
	result = _dispatch_and_wrap(tool, judged, is_write)
	if not result.get("ok") or not _reads_skill_rows(tool, args):
		return _without_column(result, alias), ""
	data = result.get("data")
	note = macros.check_skill_rows_read(conversation, lambda: _skill_rows_read(args, data, alias))
	return _without_column(result, alias), note


# The name a skill list's own key is asked for under: not one a caller's column has.
_JUDGE_ALIAS = "__jv_judge_"


def _judging_key(tool: str, args) -> str | None:
	"""The field that tells a ``get_list`` row of a skill doctype apart (``name``, or
	``parent`` for a child table); else None. Not for fields that are not plain
	records' (an aggregate, ``distinct``), whose rows are not one per record: those are
	judged on every skill the reader can read, as before."""
	if tool != "get_list" or not isinstance(args, dict):
		return None
	doctype = args.get("doctype")
	if not isinstance(doctype, str) or not _is_a_skill_doctype(doctype):
		return None
	key = "name" if doctype.strip().casefold() == "jarvis custom skill" else "parent"
	fields = args.get("fields") or ["name"]  # get_list's own default
	if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
		return None
	if any("(" in f or "distinct" in f.casefold() for f in fields):
		return None
	return key


def _without_column(result: dict, column: str | None) -> dict:
	"""``result`` with ``column`` (added by ``_dispatch_judging_skills``) taken out of
	each row."""
	if column and result.get("ok") and isinstance(result.get("data"), list):
		for row in result["data"]:
			if isinstance(row, dict):
				row.pop(column, None)
	return result


# Reads that return no stored row of the doctype they name (its schema, a would-be
# record built from the call's own values, a file's rows): never judged as a skill read.
_NO_STORED_ROWS = frozenset(
	{
		"get_schema",
		"describe_customizations",
		"get_my_access",
		"get_report_filters",
		"get_naming_series_preview",
		"get_workflow_transitions",
		"resolve_links",
		"preview_doc",
		"preview_import",
	}
)


def _reads_skill_rows(tool: str, args) -> bool:
	"""Whether a read may return rows of a skill doctype: its arguments use one as a
	doctype (a ``doctype``, a query's ``from`` or join, a parent, or a filter on a field
	that holds a doctype, JSON included), or it runs a report on one. A Script or Query
	Report's own SQL is not seen here."""
	if tool in _NO_STORED_ROWS:
		return False
	if tool in ("run_report", "report_pdf") and isinstance(args, dict):
		report = args.get("report_name")
		ref = frappe.get_cached_value("Report", report, "ref_doctype") if isinstance(report, str) else None
		if ref and _is_a_skill_doctype(ref):
			return True
	if not _mentions_a_skill_doctype(args, depth=0, keys=None):
		return False  # no skill doctype's name anywhere in it: no meta read
	keys = _DOCTYPE_ARG_KEYS.union(*(_doctype_fields(dt) for dt in _doctypes_named(args, depth=0)))
	return _mentions_a_skill_doctype(args, depth=0, keys=keys)


# Where a read's arguments use a value as a doctype: the tool's own arguments (its
# ``doctype``, a query's ``from`` and a join's ``doctype``, a child table's parent),
# ``parenttype``, and fields Frappe keeps a doctype in as plain text, which their
# doctype's meta cannot tell (``Deleted Document.deleted_doctype``). Every other field
# is told by the meta of the doctypes the read names (``_doctype_fields``). A skill
# doctype's name anywhere else (a ToDo's description, a document's name) is a value
# like any other, not a read of skill rows.
_DOCTYPE_ARG_KEYS = frozenset(
	{"doctype", "from", "parent_doctype", "parenttype", "reference_doctype", "ref_doctype", "deleted_doctype"}
)
# The argument keys that name the doctypes a read reads.
_READ_DOCTYPE_KEYS = ("doctype", "from", "parent_doctype")


@request_cache
def _doctype_fields(doctype: str) -> frozenset:
	"""The fields of ``doctype`` that hold a doctype, casefolded: a Link to DocType, and
	the field a Dynamic Link takes its doctype from. Empty for a doctype that is not."""
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return frozenset()
	held = set()
	for df in meta.fields:
		if df.fieldtype == "Link" and df.options == "DocType":
			held.add(df.fieldname)
		elif df.fieldtype == "Dynamic Link" and df.options:
			held.add(df.options)
	return frozenset(field.casefold() for field in held)


def _doctypes_named(value, *, depth: int) -> set:
	"""The doctypes a read's arguments name as the ones it reads: under
	``_READ_DOCTYPE_KEYS`` and as a four- or five-part filter's first part, JSON
	included."""
	if depth > 6:
		return set()
	value = _parsed(value)
	named = set()
	if isinstance(value, dict):
		named.update(
			dt.strip() for key in _READ_DOCTYPE_KEYS if isinstance(dt := value.get(key), str) and dt.strip()
		)
		value = list(value.values())
	if isinstance(value, list | tuple):
		if len(value) in (4, 5) and isinstance(value[0], str) and value[0].strip():
			named.add(value[0].strip())
		for item in value:
			named |= _doctypes_named(item, depth=depth + 1)
	return named


def _parsed(value):
	"""``value``, or the JSON a string that starts as an object or a list holds."""
	if isinstance(value, str) and value.strip().startswith(("{", "[")):
		try:
			return json.loads(value)
		except ValueError:
			return value
	return value


def _is_doctype_key(key, keys) -> bool:
	"""Whether ``key`` (an argument, or a filter's field, ``alias.field`` included) is
	one of ``keys``, the fields that hold a doctype."""
	return isinstance(key, str) and key.split(".")[-1].strip(" `").casefold() in keys


def _mentions_a_skill_doctype(value, *, depth: int, keys, as_doctype: bool = False) -> bool:
	"""Whether ``value`` holds a skill doctype's name where ``keys`` say a doctype is
	held; with ``keys`` None, anywhere."""
	if depth > 6:
		return False
	if isinstance(value, str):
		if (keys is None or as_doctype) and _is_a_skill_doctype(value):
			return True
		value = _parsed(value)
		if isinstance(value, str):
			return False
	if keys is None:
		if isinstance(value, dict):
			value = list(value.values())
		if isinstance(value, list | tuple):
			return any(_mentions_a_skill_doctype(item, depth=depth + 1, keys=None) for item in value)
		return False
	if isinstance(value, dict):
		# A query's where clause: ``{"field": "c.reference_doctype", "value": ...}``.
		keyed = as_doctype or _is_doctype_key(value.get("field"), keys)
		return any(
			_mentions_a_skill_doctype(
				item, depth=depth + 1, keys=keys, as_doctype=keyed or _is_doctype_key(key, keys)
			)
			for key, item in value.items()
		)
	if isinstance(value, list | tuple):
		# A filter as a list: ``[field, op, value]``, or ``[doctype, field, op, value]``
		# whose first part is a doctype too.
		keyed = as_doctype or any(_is_doctype_key(part, keys) for part in value[:2])
		if len(value) == 4 and all(isinstance(part, str) for part in value[:2]):
			if _is_a_skill_doctype(value[0]):
				return True
		return any(
			_mentions_a_skill_doctype(item, depth=depth + 1, keys=keys, as_doctype=keyed) for item in value
		)
	return False


def _skill_rows_read(args, data, column: str | None = None):
	"""The ``Jarvis Custom Skill`` docnames a skill read asked for or returned, or
	None when they cannot be told apart (every skill the reader can read is judged).
	Known: the record(s) named by ``name`` / ``names`` on a skill doctype (a child
	row's skill is its parent), and the rows of a ``get_list`` asked for its key under
	``column`` (``_judging_key``). No column of the caller's stands in for it: a
	``name`` can be another table's, or another field's alias."""
	doctype = args.get("doctype") if isinstance(args, dict) else None
	if not isinstance(doctype, str) or not _is_a_skill_doctype(doctype):
		return None
	doctype = next(dt for dt in _SKILL_DOCTYPES if dt.casefold() == doctype.strip().casefold())
	names = args.get("names") if isinstance(args.get("names"), list) else []
	names = [n for n in [args.get("name"), *names] if n]
	if names:
		if not all(isinstance(n, str) for n in names):
			return None
		if doctype == "Jarvis Custom Skill":
			return names
		return frappe.get_all(doctype, filters={"name": ["in", names]}, pluck="parent")
	if column and isinstance(data, list) and all(isinstance(row, dict) and row.get(column) for row in data):
		return [row[column] for row in data]
	return None


def _with_note(result: dict, note: str) -> dict:
	"""``result`` with ``note`` added to its data, for the agent to read: a key of a
	dict, and a list (``get_list``'s rows) wrapped as ``{"rows": ..., "note": ...}``,
	the shape a truncated list already has. Without a note nothing changes."""
	if not (note and isinstance(result, dict) and result.get("ok")):
		return result
	if isinstance(result.get("data"), dict):
		result["data"]["note"] = note
	elif isinstance(result.get("data"), list):
		result["data"] = {"rows": result["data"], "note": note}
	return result


# _error lives in jarvis/_responses.py - single source of truth for the
# customer-facing envelope shape, shared with jarvis/oauth/api.py. The
# success envelope (returned inline at lines like
# ``{"ok": True, "data": data}``) is the matching ``ok()`` there if a
# caller wants it explicitly.
from jarvis._responses import err as _error


def _get_header(name: str) -> str:
	"""Read a request header, returning empty string when no request is bound.

	``frappe.request`` is an unbound LocalProxy in direct-Python contexts
	(unit tests, ``bench execute``), so accessing attributes on it raises
	``RuntimeError``. We tolerate that and treat absent headers as empty.
	"""
	try:
		value = frappe.request.headers.get(name)
	except (AttributeError, RuntimeError):
		return ""
	return (value or "").strip()


def _request_body_bytes() -> bytes:
	"""Best-effort raw request body for HMAC validation.

	Returns b"" in direct-Python contexts (tests, ``bench execute``)
	where ``frappe.request`` isn't bound. Phase-2 signed clients can
	then either skip the signature in tests or use the test harness's
	header-faking shape; an unsigned legacy request returns b"" which
	doesn't matter because the signature path isn't entered.
	"""
	try:
		data = frappe.request.get_data(cache=True)
	except (AttributeError, RuntimeError):
		return b""
	return data if isinstance(data, (bytes, bytearray)) else (data or "").encode("utf-8")


@frappe.whitelist(methods=["POST"])
def rotate_agent_token() -> dict:
	"""Rotate the plugin agent_token (C2 PR-3C orchestrator).

	System Manager only. Generates a fresh 32-byte random token,
	pushes it via admin -> fleet-agent -> container env, and only
	persists locally after admin confirms the container is healthy
	against the new value. This keeps the bench's notion of the
	token in lockstep with what the container holds: a mid-rotation
	failure leaves both ends on the OLD token.

	Operators run this after a suspected leak or as routine hygiene.
	The container is briefly unavailable (~10-30s) during the
	``compose up -d`` recreate that the fleet-agent runs.

	Returns:
	  {"ok": true, "data": {"rotated_at": "<isoformat>"}} on success
	  {"ok": false, "error": {"code": ..., "message": ...}} on any failure;
	      old token is preserved on the bench; admin's response carries
	      the precise failure code (NoRunningTenant 409, RateLimited 429,
	      etc.)

	The old token stops working on the container as soon as the
	recreate completes; legitimate in-flight plugin requests presenting
	the old token will start hitting 401 from the bench (the new
	token doesn't match) AND from the container's plugin (the new
	JARVIS_GATEWAY_TOKEN doesn't match what the plugin remembers).
	Both sides re-sync naturally on the next call.
	"""
	import secrets

	# Block non-System-Manager callers explicitly. allow_guest defaults
	# to False; this is defense-in-depth against a future @whitelist
	# expansion shipping with relaxed defaults.
	frappe.only_for("System Manager")

	new_token = secrets.token_hex(32)  # 64 hex chars

	# Push to admin FIRST. We only persist locally after admin confirms
	# the container is healthy against the new value. If admin fails,
	# the bench's stored token is unchanged - the container also still
	# holds the old token (fleet-agent rolled back per PR-3A), so both
	# ends stay in lockstep.
	from jarvis import admin_client

	try:
		admin_client.post_rotate_agent_token(new_token=new_token)
	except admin_client.AdminAuthError as e:
		frappe.local.response.http_status_code = 502
		return {
			"ok": False,
			"error": {
				"code": "AdminAuthError",
				"message": f"admin rejected our credentials: {e}",
			},
		}
	except admin_client.AdminUnreachableError as e:
		frappe.local.response.http_status_code = 502
		return {
			"ok": False,
			"error": {
				"code": "AdminUnreachableError",
				"message": f"admin not reachable: {e}",
			},
		}
	except admin_client.AdminRateLimitedError as e:
		frappe.local.response.http_status_code = 429
		return {
			"ok": False,
			"error": {
				"code": "RateLimitExceeded",
				"message": "admin rate-limit hit; retry later",
				"retry_after_seconds": e.retry_after_seconds,
			},
		}
	except admin_client.AdminValidationError as e:
		# Admin raised a Frappe ValidationError - typically a 4xx input
		# problem the operator can fix (e.g. malformed token; though our
		# token comes from secrets.token_hex so that's unlikely here).
		frappe.local.response.http_status_code = 400
		return {
			"ok": False,
			"error": {
				"code": "AdminValidationError",
				"message": str(e),
			},
		}
	except Exception as e:
		frappe.local.response.http_status_code = 502
		frappe.log_error(
			title="rotate_agent_token: unexpected admin failure",
			message=frappe.get_traceback(),
		)
		return {
			"ok": False,
			"error": {
				"code": type(e).__name__,
				"message": f"unexpected error during rotation: {e}",
			},
		}

	# Admin succeeded -> the container is now running against new_token.
	# Persist it locally so the bench's future plugin-auth validations
	# match what the container holds.
	from jarvis._password_utils import set_settings_password

	settings = frappe.get_single("Jarvis Settings")
	now = frappe.utils.now()
	# agent_token is a Password field - db_set would write the rotated
	# secret straight into tabSingles as plaintext; encrypt it into __Auth
	# first (see _password_utils module docstring).
	set_settings_password(settings, "agent_token", new_token)
	# C2 time-bound: stamp issued_at so plugin_auth's expiry check has
	# a reference point. Tolerate the column not existing yet on a
	# pre-migration deploy state.
	try:
		settings.db_set("agent_token_issued_at", now)
	except Exception:
		pass

	return {"ok": True, "data": {"rotated_at": now}}
