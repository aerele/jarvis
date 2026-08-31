import json
import time

import frappe
from frappe.utils import strip_html

from jarvis import audit, telemetry
from jarvis._http import validate_bearer as _validate_bearer
from jarvis._plugin_auth import PluginAuthError, validate_plugin_request
from jarvis._session import impersonate
from jarvis.exceptions import (
	CapabilityDeniedError,
	FeatureDisabledError,
	InvalidArgumentError,
	JarvisError,
	RunHaltedError,
)
from jarvis.permissions import has_jarvis_access
from jarvis.tools._result_guard import enforce_result_budget
from jarvis.tools.registry import dispatch


@frappe.whitelist(allow_guest=True, methods=["POST"])
def call_tool(tool: str, args: dict | str | None = None) -> dict:
	"""Whitelisted entry point for Jarvis tool dispatch.

	Two authentication modes are supported:

	1. **Standard Frappe auth** (external callers): session cookie or
	   ``Authorization: token <api_key>:<api_secret>``. The calling user is
	   whoever Frappe's session resolves to; their permissions are what the
	   tool sees. Guest is rejected.

	2. **Plugin auth** (``jarvis-openclaw-plugin`` Path A): two custom headers
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
		if not frappe.db.exists("User", plugin_user):
			frappe.local.response.http_status_code = 400
			return _error(
				"InvalidArgumentError",
				f"session references unknown user: {plugin_user}",
			)

		# NOTE: no Jarvis-access role gate on the plugin path. It is
		# machine-authenticated (token/HMAC proves the call came from agent),
		# and `plugin_user` is the real chat user — already gated when they
		# started the conversation. Per-DocType perms still apply under
		# _dispatch_from_session.

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
			# (fleet renders it into openclaw.json); it is not authorization, so a
			# compromised container/plugin or a leaked run bearer would otherwise
			# reach ANY registered tool the run-as user's Frappe roles permit. Fails
			# closed: a run with no snapshot and no legacy marker is refused outright.
			# Non-delegate sessions (ordinary chat) resolve to None and are untouched.
			denial = _delegate_capability.tool_denial(session_key, tool)
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
					attempted = _parse_args(args)
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
				parsed_args = _parse_args(args)
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
			result = _run_tool(tool, parsed_args, conversation=conv)
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

	armed_by_macro = _agent_run_ctx.take_armed_by_macro()
	# A FAILED armed write must still be visible (it ran uncarded, unattended, and
	# broke - exactly what T8 surfaces): label it "failed" (a red chip carrying the
	# macro provenance) rather than letting it fall into the collapsed Activity row.
	if armed_by_macro:
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
	tool_call_id: str | None = None,
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
	    the seq race is still fixed)."""
	result = result or {}
	discarded = action_outcome == "discarded"
	if discarded:
		# Nothing executed; the chip renders off action_outcome, and tool_status
		# stays empty (a valid Select option) rather than a misleading completed/error.
		status = ""
	else:
		status = "completed" if result.get("ok") else "error"

	# Entity stamping (org wiki): which doc this call touched, so wiki nudges
	# can read a turn's entities off the receipt rows. Lazy + guarded: a
	# missing/broken entities module must never break receipts. Skipped for a
	# discard - it touched no document.
	ref_doctype = ref_name = None
	if not discarded:
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
		frappe.db.commit()
		msg_name = _locked_insert_chat_message(
			conv_name,
			{
				"role": "tool",
				"tool_name": tool,
				"tool_args": frappe.as_json(args),
				"tool_result": frappe.as_json(result) if result else None,
				"tool_status": status,
				"tool_call_id": tool_call_id or None,
				"action_outcome": action_outcome or None,
				"armed_by_macro": armed_by_macro or None,
				"ref_doctype": ref_doctype,
				"ref_name": ref_name,
				"content": f"{tool} → {action_outcome or status}",
			},
			# ``(conversation, tool_call_id)`` dedupe — a duplicate callback for the SAME
			# tool call is a no-op (R-6 out-of-band idempotency). Null id: no dedupe, but
			# the seq race is still fixed by the shared FOR UPDATE.
			dedupe_filters=(
				{"conversation": conv_name, "tool_call_id": tool_call_id} if tool_call_id else None
			),
		)
		frappe.db.commit()
		if msg_name is None:
			# Duplicate callback for the same tool call — already recorded (R-6 idempotent).
			return
		publish_realtime_tool_result(
			user=conv_owner,
			conversation_id=conv_name,
			tool_message_id=msg_name,
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
		return
	msg_name = rows[0]
	existing = frappe.db.get_value(MSG, msg_name, "canvas")
	items = frappe.parse_json(existing) if existing else []
	if not isinstance(items, list):
		items = []
	if any(i.get("file_url") == file_url for i in items):
		return  # already attached
	items.append(item)
	frappe.db.set_value(MSG, msg_name, "canvas", frappe.as_json(items))
	frappe.db.commit()
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
) -> None:
	"""Wrapper around frappe.publish_realtime so tests can mock at this seam.

	``action_outcome`` (confirmed/discarded/failed) rides along so the SPA can
	render the row as a receipt chip immediately, without waiting for a full
	transcript reload."""
	frappe.publish_realtime(
		"jarvis:event",
		{
			"kind": "tool:result",
			"conversation_id": conversation_id,
			"tool_message_id": tool_message_id,
			"tool_name": tool_name,
			"args": args,
			"result": result,
			"status": status,
			"action_outcome": action_outcome,
		},
		user=user,
	)


def _parse_args(args: dict | str | None) -> dict:
	"""Decode the args input into a dict + strip legacy identity markers.

	Raises ``InvalidArgumentError`` (a JarvisError subclass) on JSON
	parse failure - that mirrors what tools raise for malformed input
	and lets ``_run_tool`` translate it via the same path. The previous
	shape returned either a dict OR an error envelope dict, which made
	every caller branch on the result type.
	"""
	if isinstance(args, str):
		try:
			args = json.loads(args)
		except json.JSONDecodeError as e:
			raise InvalidArgumentError(f"args is not valid JSON: {e}")
	args = args or {}
	# Defensive: strip LLM-hallucinated "_user" / "_session" fields. The
	# old MCP design used them as in-band identity carriers; Path A moved
	# identity to HTTPS headers. If the LLM has been trained on the older
	# convention it may emit these even though our tool schemas don't list
	# them - dropping them silently keeps such calls dispatching cleanly.
	if isinstance(args, dict):
		for legacy in ("_user", "_session", "_session_key"):
			args.pop(legacy, None)
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
		# download_pdf/export_excel both insert a File doc (download_pdf also
		# attaches it) - real DB writes that need an audit trail, not a
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
_PREVIEWABLE = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"submit_doc",
		"cancel_doc",
		"amend_doc",
		"delete_doc",
		"run_method",
	}
)
# Writes that MUST get a human confirmation before executing (issue #186).
# The lighter mutators in _WRITE_TOOLS (comments/tags/attach/dashboard-create)
# are intentionally NOT gated - they never fire the card. share_doc/assign_to
# WERE in that "lighter" bucket but their own descriptors promise "ALWAYS
# confirm" (share_doc: re-share/everyone=true grants; assign_to: emails a
# third party) - audit-findings.md F17/F20/F23 - so they now gate too. Neither
# is in _PREVIEWABLE/_DRY_RUN_ON_PARK: no side-effect-free sandbox preview is
# meaningful for a share grant or a ToDo+notification email, so both fall
# through to the described-intent park path (like send_email) rather than a
# sandboxed dry-run.
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
	}
)
# #493: the agent-facing wiki surface, refused wholesale when the operator has
# switched "Enable Business Wiki" off. Deliberately just the two tools the issue
# names - record_app_wiki (the app-learning scribe's audited-not-gated writeback)
# is a different feature's pipeline and is out of scope here.
_WIKI_TOOLS = frozenset({"read_wiki", "update_wiki"})
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
# Armed-skip (macro skip-confirmation): an admin-armed macro (Jarvis Macro
# .skip_confirmation, stamped onto its run conversation's skip_confirmation) runs
# these WITHOUT a confirmation card - the BROAD covered set, far wider than the
# create/update-only _AUTO_APPLYABLE, and INCLUDING run_method (which gates in
# ordinary chat; skips only inside an armed macro). This is an explicit ALLOWLIST,
# NOT `_GATED_WRITES - _ARMED_SKIP_NEVER`: a new gated tool must NOT become
# armed-skippable by default. The partition invariant (test_armed_skip_partition)
# asserts COVERED and NEVER are disjoint and together == _GATED_WRITES, so adding a
# gated tool to neither set turns that test RED until a human consciously files it.
# The irreversible trio (_ARMED_SKIP_NEVER) always parks; an armed macro that hits
# one stops the run (D5). Bulk covered writes DO skip; the F16 over-size cap still
# bounces them. A gated tool NOT in COVERED (e.g. a bulk light write) parks in an
# armed run like any other excluded write.
_ARMED_SKIP_COVERED = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"submit_doc",
		"run_method",
		"run_import",
		"apply_workflow_action",
		"send_email",
		"share_doc",
		"assign_to",
		"create_custom_skill",
		"update_wiki",
	}
)
_ARMED_SKIP_NEVER = frozenset({"cancel_doc", "delete_doc", "amend_doc"})
# Skill "Approve & run the plan" (design §3.4, D-COVERED): a conversation in an
# APPROVED skill run (Jarvis Conversation.skill_autorun=1, stamped by the
# approve_and_run endpoint on step-1 success) runs THESE covered writes without a
# confirmation card. Like _ARMED_SKIP_COVERED this is an EXPLICIT fail-CLOSED
# allowlist, NOT `_ARMED_SKIP_COVERED - {...}`: a new gated tool must NOT become
# auto-runnable by default (this path has no site-wide kill switch, so an
# unclassified tool defaults to carding). It DIVERGES from the macro set in ONE
# tool: create_custom_skill is COVERED for a macro but NEVER here - a skill that
# writes another skill is a consequential meta-write the user should still confirm.
# The irreversible trio (delete/cancel/amend) + create_custom_skill make up
# _SKILL_AUTORUN_NEVER and always park (a park mid-run is a legit PAUSE that
# resumes on confirm). The partition invariant (test_covered_and_never_partition_
# gated_writes) asserts COVERED and NEVER are disjoint and together == _GATED_WRITES,
# so a gated tool filed in neither turns that test RED until a human classifies it.
_SKILL_AUTORUN_COVERED = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"submit_doc",
		"run_import",
		"apply_workflow_action",
		"send_email",
		"share_doc",
		"assign_to",
		"update_wiki",
		"run_method",
	}
)
_SKILL_AUTORUN_NEVER = frozenset({"delete_doc", "cancel_doc", "amend_doc", "create_custom_skill"})
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
_SKILL_AUTORUN_TTL_S = 900
# Gated writes we dry-run in the sandbox AT PARK TIME and BLOCK on if the dry-run
# fails, so a deterministic failure (missing mandatory field, bad link, no create
# permission) is returned to the model BEFORE a confirmation card is shown instead
# of surfacing after the human confirms a doomed card. Preview and confirm build
# the same doc as the same exec_user, so a preview failure faithfully predicts the
# confirm failure. Scoped to the build-from-args create/update pair - the reported
# mandatory-field case. submit_doc/cancel_doc/delete_doc/amend_doc are _PREVIEWABLE
# too and are STILL dry-run at park via _pending_preview, but keep the legacy
# park-with-note: their failures are state-based (already exists, docstatus, link
# integrity) and dry-running them fires on_submit/on_cancel hooks - extending the
# block to them is a separate, larger change. run_method is never sandbox-run at
# park at all (its target's inline non-DB side effects would fire unconfirmed).
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
	"""True when a tool call carries a batch payload. Used to (1) keep a bulk
	create_doc/update_doc from auto-applying - a batch always parks behind the
	card - and (2) route consequential bulk writes to the described-intent
	preview instead of a sandbox dry-run that would fire on_submit / on_cancel
	hooks N times at park."""
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
	"""Site-wide kill switch for admin-armed macro skip-confirmation. When the
	operator flips ``Jarvis Settings.disable_armed_skip`` on, every armed covered
	write re-gates behind its card immediately, no deploy - the escape hatch if an
	armed macro misbehaves. Read only when an armed covered write is about to skip."""
	return bool(frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "disable_armed_skip")))


def _skill_autorun_slide(conv: str) -> None:
	"""Advance the sliding last-write timestamp after a covered auto-run write, and
	commit immediately (mirroring the macro's per-step row write). Committing now
	means a worker death right after this write leaves an ACCURATE freeze-point, so
	the sliding-TTL reaper (task #42) reaps the stranded run at the right time."""
	frappe.db.set_value(
		"Jarvis Conversation", conv, "skill_autorun_at", frappe.utils.now_datetime(), update_modified=False
	)
	frappe.db.commit()


def _skill_autorun_clear(conv: str) -> None:
	"""Hard-stop the approved run: drop skill_autorun so the next covered write
	re-cards. Committed immediately so the cleared state survives a worker death
	(a concurrent 0->0 clear is an idempotent no-op)."""
	frappe.db.set_value("Jarvis Conversation", conv, "skill_autorun", 0, update_modified=False)
	frappe.db.commit()


def _armed_skill_docname_for_slug(slug: str, owner: str) -> str | None:
	"""The single live-ARMED Jarvis Custom Skill row ``owner`` would invoke by
	``/slug``, or ``None`` (skill "Approve & run the plan", design §3.3 rule 4).

	Mirrors :func:`invoked_skill_slugs`' owner/shared/role precedence: the owner's
	OWN enabled row wins the invocation; failing that, a shared or role-scoped
	enabled row. ``allow_approve_run`` is read LIVE off the resolved row and must
	be 1. Fail-safe on ambiguity - a slug can map to several rows across
	owned/shared/role-scoped, so ``None`` when the winning tier holds 2+ rows OR
	when 2+ ARMED invocable rows exist anywhere (an ambiguous arm cannot authorize
	a run). O(1)-ish: a couple of indexed reads, no per-skill N+1."""
	from jarvis.chat.custom_skills import role_scoped_skill_rows

	# The owner's OWN enabled rows named `slug`.
	own = {
		r.name: int(r.allow_approve_run or 0)
		for r in frappe.get_all(
			"Jarvis Custom Skill",
			filters={"enabled": 1, "owner": owner, "skill_name": slug},
			fields=["name", "allow_approve_run"],
		)
	}
	# Enabled rows SHARED with the owner, or reachable via a role the owner holds,
	# named `slug` (deduped - a row can be both shared and role-scoped).
	shared_role: dict[str, int] = {}
	shared_names = [
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Share",
			filters={"user": owner, "parenttype": "Jarvis Custom Skill"},
			fields=["parent"],
		)
	]
	if shared_names:
		for r in frappe.get_all(
			"Jarvis Custom Skill",
			filters={"enabled": 1, "name": ["in", shared_names], "skill_name": slug},
			fields=["name", "allow_approve_run"],
		):
			shared_role[r.name] = int(r.allow_approve_run or 0)
	for r in role_scoped_skill_rows(owner, ["name", "skill_name", "allow_approve_run"]):
		if r.skill_name == slug:
			shared_role.setdefault(r.name, int(r.allow_approve_run or 0))

	# Precedence: the owner's own row wins; only fall through to shared/role when
	# the owner has no own row for this slug.
	tier = own if own else shared_role
	if len(tier) != 1:
		return None  # no candidate, or an ambiguous winning tier -> fail safe
	docname, armed = next(iter(tier.items()))
	if not armed:
		return None  # the row the owner would actually invoke is not armed
	# Global ambiguity guard: refuse if the slug maps to 2+ ARMED invocable rows
	# anywhere (owned + shared + role), even across tiers.
	if sum(1 for v in {**shared_role, **own}.values() if v) != 1:
		return None
	return docname


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
	   (:func:`_armed_skill_docname_for_slug`).

	Best-effort: any exception -> ``(None, None)``; this is an additive nicety on
	the hot gate path and must never break the park."""
	try:
		if not conversation:
			return None, None
		from jarvis.chat.custom_skills import invoked_skill_slugs
		from jarvis.chat.turn_message_binding import current_turn_message_id

		msg_id = current_turn_message_id(conversation)
		if not msg_id:
			return None, None
		msg = frappe.db.get_value("Jarvis Chat Message", msg_id, ["owner", "content"], as_dict=True)
		if not msg or not msg.get("owner") or not msg.get("content"):
			return None, None
		# Resolve under the MESSAGE SENDER, never frappe.session.user / exec_user.
		slugs = invoked_skill_slugs(msg["content"], user=msg["owner"])
		if len(slugs) != 1:
			return None, None
		slug = next(iter(slugs))
		docname = _armed_skill_docname_for_slug(slug, msg["owner"])
		if not docname:
			return None, None
		return docname, slug
	except Exception:
		return None, None


def _run_preview(tool: str, args: dict) -> dict:
	"""Dispatch a write tool with all DB effects sandboxed (mechanics in
	``jarvis.tools._preview_sandbox``, shared with preview_doc). Side effects
	fired directly inside hooks (inline HTTP calls) are NOT sandboxed."""
	from jarvis.tools._preview_sandbox import preview_sandbox

	with preview_sandbox():
		would = dispatch(tool, args)
	return {
		"preview": True,
		"would": would,
		"note": (
			"Validated with all DB writes rolled back; nothing was "
			"committed or queued. Side effects fired directly inside "
			"hooks (inline HTTP calls in on_submit / on_cancel) are "
			"not sandboxed by preview."
		),
	}


def _preview_error(e: Exception) -> dict:
	"""Translate a sandboxed dry-run exception into the model-facing error
	envelope. NEVER audited: a dry-run commits nothing, so there is no write to
	record. Shared by the model-facing ``preview=True`` path and the park gate's
	pre-park validation so both classify the same exceptions identically."""
	if isinstance(e, JarvisError):
		return _error(type(e).__name__, str(e))
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
	"""Build the preview shown alongside a parked gated write. Previewable
	tools reuse the sandboxed ``_run_preview`` (all DB writes rolled back);
	everything else (send_email, or a previewable whose validation could not
	be dry-run) gets a described-intent dict that makes clear it is NOT a dry
	run - the real thing runs on confirm."""
	described = {
		"preview": False,
		"described": True,
		"summary": _describe_call(tool, args),
		"note": ("not a dry run - this will send/execute on confirm"),
	}
	# run_method is _PREVIEWABLE for the dry-run path, but it must NEVER be
	# sandbox-executed to build a park preview: even inside the rollback
	# sandbox the target method's inline non-DB side effects (HTTP/email fired
	# directly, not via DB writes) would fire unconfirmed and its result would
	# be returned to the model. Route it to the described-intent path (like
	# send_email) so parking a run_method never executes it - the real call
	# runs only on confirm.
	# A BULK submit/cancel/delete/amend routes to described-intent: sandbox-
	# running the batch would fire on_submit / on_cancel hooks - incl. non-
	# rollback-able inline side effects (e-invoice / webhook HTTP) - once per doc,
	# N times, at PARK. Bulk create/update/create_docs stay SANDBOXED even here
	# (the resync path reaches _pending_preview directly, bypassing the
	# _DRY_RUN_ON_PARK branch): they are _DRY_RUN_ON_PARK - rolled back, no
	# consequential hooks - so exclude them from the bulk described routing.
	if (
		tool not in _PREVIEWABLE
		or tool == "run_method"
		or (_is_bulk_call(args) and tool not in _DRY_RUN_ON_PARK)
	):
		return described
	try:
		return _run_preview(tool, args)
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

# "what you can do" lines, keyed by the wire ``code``. Deliberately tiny.
_ERROR_HINTS = {
	"PermissionDeniedError": (
		"You don't have access to do this. If you believe you should, ask your "
		"administrator to review your permissions."
	),
	"InvalidArgumentError": (
		"Some of the values need attention - check the highlighted fields and try again."
	),
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


def _hint_for(code: str, detail: str) -> str:
	if code == "PermissionDeniedError" and (
		"linked to" in detail.lower() or "not allowed to access" in detail.lower()
	):
		return _USER_PERM_HINT
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


def _translate_write_error(e: Exception, mark: int) -> dict | None:
	"""Enriched ``{ok:false, error}`` envelope for a KNOWN write-path exception,
	promoting Frappe's discarded reason into ``message``/``detail``/``hint``.
	Returns ``None`` for an unexpected exception - the caller MUST re-raise it so
	a real bug still surfaces as a 500 (never enveloped, never leaks a traceback).
	``mark`` is a ``_msglog_mark()`` taken before the write ran."""
	if isinstance(e, JarvisError):
		code, message = type(e).__name__, strip_html(str(e)).strip()
	elif isinstance(e, frappe.PermissionError):
		code = "PermissionDeniedError"
		message = strip_html(str(e)).strip() or _flags_message() or "permission denied"
	elif isinstance(e, frappe.DuplicateEntryError):
		# str(e) here is an args-tuple repr, not a message - clean it up.
		code, message = "InvalidArgumentError", _duplicate_message(e)
	elif isinstance(e, frappe.ValidationError):
		code = "InvalidArgumentError"
		message = strip_html(str(e)).strip() or _flags_message() or type(e).__name__
	else:
		return None
	detail = _harvest_reason(mark)
	# Don't repeat the message under "Show details".
	if detail and detail == strip_html(message).strip():
		detail = ""
	return _error(code, message, detail=detail, hint=_hint_for(code, detail))


def _dispatch_and_wrap(tool: str, args: dict, is_write: bool) -> dict:
	"""Dispatch + translate exceptions into the ``{ok, data}`` / ``{ok, error}``
	envelope + audit write tools. This is the shared core of ``_run_tool``'s
	execute path, reused verbatim by ``confirm_tool`` so a confirmed write runs
	through the exact same translation + audit as an inline one.

	A write is scoped in a SAVEPOINT so a KNOWN failure is rolled back before we
	RETURN the ``{ok:false}`` envelope. Frappe commits any endpoint that returns
	normally (frappe/app.py), so without this a tool that half-applied before
	raising - e.g. a submit whose ``on_submit`` hook throws AFTER ``docstatus=1``
	was written - would be persisted at end-of-request. Rolling back to the
	savepoint (not a full rollback) undoes ONLY this tool, leaving the caller's
	surrounding writes intact (``confirm_tool``'s failure receipt/continuation,
	the model path's tool-call row). Unexpected exceptions re-raise unchanged:
	Frappe's handler does a full request rollback (nothing persists, no envelope,
	no traceback to the client)."""
	mark = _msglog_mark()
	sp = f"jarvis_{frappe.generate_hash(length=10)}" if is_write else None
	if sp:
		frappe.db.savepoint(sp)
	try:
		data = dispatch(tool, args)
	except Exception as e:
		envelope = _translate_write_error(e, mark)
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
				pass
		if is_write:
			err_obj = envelope["error"]
			audit.record(
				tool=tool, args=args, ok=False, error_code=err_obj["code"], error_message=err_obj["message"]
			)
		return envelope
	if sp:
		try:
			frappe.db.release_savepoint(sp)
		except Exception:
			pass
	if is_write:
		audit.record(tool=tool, args=args, ok=True, result=data)
	return {"ok": True, "data": data}


def dispatch_confirmed(tool: str, args: dict) -> dict:
	"""Execute a confirmed gated write. Public seam used by ``confirm_tool``
	AFTER ``pending_confirm.consume`` has validated owner + single-use. Runs
	the stored call directly (a gated write is always a _WRITE_TOOL, so it is
	audited) WITHOUT re-entering ``_run_tool``'s gate - the gate would just
	park it again. This is design option (a): the model can never execute a
	gated write; only a confirmed human click reaches dispatch."""
	return _dispatch_and_wrap(tool, args, is_write=True)


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
		args = _parse_args(raw_args)
	except JarvisError as e:
		return _error(type(e).__name__, str(e))

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
				return _error(
					"InvalidArgumentError",
					f"too many records in one batch ({batch_n}); the max is "
					f"{_MAX_BATCH}. Split into batches of {_MAX_BATCH} and confirm "
					"each one before starting the next.",
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
		# safe default: the write parks). skill_autorun + skill_autorun_at ride the
		# SAME single query (design §3.4): the skill auto-run branch needs both the
		# flag and its sliding timestamp for the inline TTL check.
		_conv_flags = (
			frappe.db.get_value(
				"Jarvis Conversation",
				conv,
				["auto_apply", "file_box", "skip_confirmation", "skill_autorun", "skill_autorun_at"],
				as_dict=True,
			)
			if conv
			else None
		) or {}
		# Armed-skip bypass (macro skip-confirmation): an admin-armed macro's run
		# conversation carries skip_confirmation=1 (stamped by run_macro), so the
		# BROAD covered set - incl. run_method / submit / send_email / run_import -
		# runs uncarded. This is distinct from and wider than the create/update-only
		# auto_apply below. The irreversible trio (delete/cancel/amend) is NOT in
		# _ARMED_SKIP_COVERED, so it falls through to park (an armed macro that hits
		# one stops the run - D5). Cheap frozenset membership test first; the
		# kill-switch Settings read runs only when a covered write is actually armed.
		# Bulk covered writes skip too (an automation must not stall on a batch card);
		# the F16 over-size cap above still bounces an oversized batch.
		if (
			tool in _ARMED_SKIP_COVERED
			and _conv_flags.get("skip_confirmation")
			and not _armed_skip_disabled()
		):
			result = dispatch_confirmed(tool, args)
			# Provenance for the receipt (T8): which armed macro authorized this uncarded
			# write. Consumed once by the receipt persist, which labels the row
			# action_outcome=auto_applied + armed_by_macro so an armed write is a visible,
			# queryable receipt - not silence-by-omission.
			from jarvis.tools import _agent_run_ctx

			# Always label an armed write (we are in the armed branch, so it IS armed):
			# resolve the human macro_name (Jarvis Macro autoname is a hash, so the run's
			# `macro` docname is opaque) and fall back to a generic marker if the run-row
			# lookup misses (the abnormal lingering-flag state) - never silently unlabelled.
			_armed_run_macro = frappe.db.get_value(
				"Jarvis Macro Run", {"conversation": conv, "status": "running"}, "macro"
			)
			_agent_run_ctx.set_armed_by_macro(
				(
					frappe.db.get_value("Jarvis Macro", _armed_run_macro, "macro_name")
					if _armed_run_macro
					else None
				)
				or "an armed macro"
			)
			if tool == "run_import":
				# The armed branch bypasses _confirm_core, which is where a CONFIRMED
				# run_import normally gets its completion announcement bound. Bind it here
				# too so an armed (unattended) import still reports done/failed into the run
				# log. Synthesize the record from the gate locals (conv + owner_user) - there
				# is no pending-confirm record on this path. Self-gating + best-effort (it
				# no-ops on a non-ok result, a missing data_import, or the kill switch).
				from jarvis.chat.import_announce import bind_after_run_import

				bind_after_run_import(
					{"tool": "run_import", "conversation": conv, "owner": owner_user}, result
				)
			return result
		# Skill "Approve & run" auto-run bypass (design §3.4): a conversation in an
		# APPROVED skill run (skill_autorun=1, stamped by approve_and_run on step-1
		# success) runs the explicit _SKILL_AUTORUN_COVERED allowlist uncarded, sliding
		# skill_autorun_at forward on each covered write. Placed AFTER the macro branch
		# so a (structurally-unreachable, but defense-in-depth) both-flags conversation
		# resolves as a macro run for deterministic provenance. The irreversible trio +
		# create_custom_skill are NOT covered here, so they fall through to park + PAUSE
		# the run. Narrower than the macro's _ARMED_SKIP_COVERED (no create_custom_skill)
		# and additionally gated on a SLIDING TTL + a bench-guaranteed Halt cancel-gate.
		if tool in _SKILL_AUTORUN_COVERED and _conv_flags.get("skill_autorun"):
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
			# Sliding TTL: auto-run only while the last covered write is recent. If the
			# timestamp is missing or older than the horizon, DON'T auto-run - fall
			# through to the normal park below (the reaper, task #42, clears the stale
			# flag; a park here is a safe, correct pause).
			autorun_at = _conv_flags.get("skill_autorun_at")
			if (
				autorun_at
				and (frappe.utils.now_datetime() - frappe.utils.get_datetime(autorun_at)).total_seconds()
				<= _SKILL_AUTORUN_TTL_S
			):
				result = dispatch_confirmed(tool, args)
				# Hard-stop-on-error: the first covered ok:False clears the flag so the
				# next write re-cards; a success slides the timestamp forward.
				if result.get("ok"):
					_skill_autorun_slide(conv)
				else:
					_skill_autorun_clear(conv)
				return result
			# TTL-expired / no timestamp: fall through to the normal park.
		# Auto-apply bypass (issue #186, Task 4 + #5): the OTHER path where a gated
		# write runs without a confirmation token. Strictly limited to
		# {a resolved conversation, admin-enabled auto_apply, an _AUTO_APPLYABLE
		# (reversible create/update) tool}. Everything outside create/update -
		# submit_doc, and every destructive tool (delete/cancel/amend/send_email) -
		# ALWAYS parks under auto_apply (only armed-skip above runs the wider set).
		#
		# conv is never a client claim - it is resolved server-side from the
		# session_key upstream - so there is no owner to re-check here: an
		# owner comparison against owner_user (itself read from this same conv
		# a few lines up) would just be comparing one DB read to another read of
		# the identical field, not a real access-control boundary.
		# A bulk create/update (docs[] / updates[]) NEVER fast-paths - the batch
		# card is the human checkpoint against a 20-doc mistake; only a single
		# reversible create/update may auto-apply.
		if conv and tool in _AUTO_APPLYABLE and not _is_bulk_call(args):
			# Two direct-apply paths for reversible create/update (destructive
			# tools above are excluded and always park): admin-enabled
			# auto_apply, OR a File Box conversation - an unattended directed
			# run where nobody can click a confirm card and review happens on
			# the created Draft + the approval board. Both flags are
			# server-controlled and admin-gated against generic saves.
			if _conv_flags.get("auto_apply") or _conv_flags.get("file_box"):
				return dispatch_confirmed(tool, args)
		# Sequential confirmation (F16): at most ONE live confirmation card per
		# conversation. If one is already awaiting the user here, REFUSE to park a
		# second and tell the model to stop - the continuation turn fired after the
		# pending card is confirmed + executed is where it issues the next batch.
		# This makes "batch 2's card appears only after batch 1 completes" a bench
		# guarantee (not just persona discipline) and is the server-side
		# single-flight for the confirm path. Auto-apply above is deliberately NOT
		# gated by this (it parks no card).
		#   STRICT conversation match: list_for_owner returns conversation-LESS
		#   tokens under any filter (F1), so re-filter to record.conversation == conv
		#   - an unrelated conv-less token (a rare session-resolution miss) must not
		#   block a legitimate new card here.
		try:
			conversation_pending = conv and any(
				t.get("conversation") == conv
				for t in pending_confirm.list_for_owner(owner_user, conversation=conv, strict=True)
			)
		except pending_confirm.PendingConfirmStorageError:
			return _error(
				"ConfirmationUnavailableError",
				"could not check whether another confirmation is already pending "
				"(a storage error). Nothing was changed. You may retry this exact "
				"call once; if it still fails, tell the user and stop - do not loop.",
			)
		if conversation_pending:
			return _error(
				"ConfirmationPendingError",
				"a confirmation card for a previous action is still awaiting the "
				"user in this conversation. Only one runs at a time - do NOT retry "
				"this call; stop and end your turn now. Once the user confirms the "
				"pending card you'll get a follow-up turn to continue with the next "
				"step.",
			)
		# Validate BEFORE parking. For a create/update (build-from-args) write,
		# run the real call in the rollback sandbox now: a deterministic failure
		# (missing mandatory field, bad link, no create permission) means the
		# confirmed write would fail identically - preview and confirm build the
		# same doc as the same exec_user - so return the error to the model NOW
		# instead of showing a confirmation card that dies on click. clear_messages
		# so the validation msgprint does not leak into the turn (mirrors
		# preview_doc). Every other gated write (submit/cancel/delete/amend get a
		# sandboxed preview; send_email/run_method/create_custom_skill/update_wiki
		# a described-intent one) parks via _pending_preview exactly as before.
		if tool == "run_import":
			# run_import cannot be sandbox-run (staging + a background job), so it has its
			# own pre-park validation: refuse a blocked / non-importable / non-admin import
			# BEFORE minting a token, exactly as _DRY_RUN_ON_PARK refuses a doomed create.
			rejected, preview = _import_park(args)
			if rejected is not None:
				frappe.clear_messages()
				return rejected
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
			preview = _pending_preview(tool, args)
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
		skill_docname, skill_slug = _resolve_approve_run_offer(conv)
		if isinstance(preview, dict):
			preview["card"] = confirm_card.build_card(tool, args, preview)
			if skill_docname and isinstance(preview.get("card"), dict):
				preview["card"]["approve_run"] = True
				preview["card"]["skill_slug"] = skill_slug
		expires_at = int(time.time()) + pending_confirm._TTL_S
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
		# Deliver the token to the human's UI out-of-band, over the realtime
		# channel, NEVER via the function return below - the model must never
		# see it. Published to the OWNER (the subscribed browser), not the acting
		# session user. Best-effort: a publish hiccup must not crash the tool call
		# or the turn, and must NOT execute the write - the token still lives
		# in pending_confirm either way, so a retry or a future resync can
		# still surface it.
		try:
			events.publish_to_user(
				owner_user,
				# Same shared item shape the resync endpoint + run:end terminal use, so
				# the live push can't drift from them (and gets the summary guard too).
				{
					"kind": "action:pending",
					**pending_confirm._pending_item(
						token=token,
						tool=tool,
						args=args,
						preview=preview,
						conversation=conv,
						run_id=run_id,
						expires_at=expires_at,
					),
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
	result = _dispatch_and_wrap(tool, args, is_write)
	telemetry.record_tool(
		tool=tool,
		args=args,
		conversation=conversation,
		duration_ms=int((time.perf_counter() - t0) * 1000),
		result=result,
	)
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
	frappe.db.commit()

	return {"ok": True, "data": {"rotated_at": now}}
