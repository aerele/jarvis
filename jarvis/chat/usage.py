"""Real per-turn token-usage accounting for managed (gateway) chat.

The agent gateway's ``sessions.list`` rows carry per-session
``inputTokens`` / ``outputTokens`` that are **last-completed-run** numbers
(not cumulative), ``totalTokens`` (context size), and ``totalTokensFresh``
(validity marker). Accurate accounting therefore records a *delta at each
turn end* while the turn handler still holds the pooled gateway connection —
see ``jarvis.chat.turn_handler`` and the design at
``docs/superpowers/specs/2026-07-10-user-settings-usage-design.md``.

Three entry points:
  * ``get_or_create_user_settings(user)`` — race-safe lazy row creation with an
    explicit ``owner`` so the ``if_owner`` grant holds when an admin triggers it.
  * ``record_turn_usage(session_key, row, run_id=None)`` - atomic SQL increments
    on both the per-user ``Jarvis User Settings`` (month-rollover aware) and the
    cumulative ``Jarvis Chat Session`` fields, plus a best-effort per-turn
    ``Jarvis Turn Usage`` row (usage-dashboard Part A, task U1) carrying the
    session's role-profile fields, the resolved model, and a tool-call count.
    Never raises into the turn.
  * ``refresh_session_snapshots(rows)`` — the admin "sync from agent" sweep;
    refreshes per-session snapshot fields WITHOUT accumulating counters.
"""

from __future__ import annotations

import time
from datetime import datetime

import frappe

# The pool-mode "Auto" sentinel model id. turn_handler._session_model_for
# patches an unpinned BIFROST-fronted pool conversation's agent SESSION to
# this value (jarvis#299), so the gateway's sessions.list row reports it as
# `model` for that turn - see record_turn_usage below for what that means for
# attribution. Imported (not hardcoded) so this module and turn_handler can't
# drift on what the sentinel is; re-exported here as a module-level constant
# so callers that only care about "is this the pool auto-routed bucket" don't
# need to import turn_handler themselves. turn_handler does NOT import this
# module at module level (only lazily, inside handle_chat_send), so this
# import doesn't introduce a cycle.
from jarvis.chat.turn_handler import POOL_VIRTUAL_MODEL

USER_SETTINGS = "Jarvis User Settings"
CHAT_SESSION = "Jarvis Chat Session"
MODEL_USAGE = "Jarvis User Model Usage"
MODEL_USAGE_FIELD = "user_model_usage"
TURN_USAGE = "Jarvis Turn Usage"
CHAT_TURN = "Jarvis Chat Turn"
CHAT_MESSAGE = "Jarvis Chat Message"
TURN_USAGE_RETENTION_DAYS = 90
TURN_USAGE_PRUNE_BATCH_LIMIT = 5000


def current_month_key() -> str:
	"""The current usage bucket as ``"YYYY-MM"`` (site timezone, matching the
	``now_datetime()`` stamps used for ``last_usage_at``)."""
	return frappe.utils.now_datetime().strftime("%Y-%m")


def tenant_wide_per_model_tokens(month: str) -> list[dict]:
	"""Tenant-wide (ALL users, not just the caller) per-model token totals for
	``month``, one row per model: ``[{model, in_, out_}, ...]``.

	Deliberately no ``parent IN (...)`` / session-user filter (contrast
	``jarvis.chat.usage_push._per_model_totals``, which sums per named user for
	the admin rollup push): a bench site IS one tenant, and the direct-tenant
	cost figure (``jarvis.account._direct_llm_usage``) is a tenant-wide $/token
	total, not a per-user one."""
	return frappe.db.sql(
		"""
		SELECT model, SUM(month_input_tokens) AS in_, SUM(month_output_tokens) AS out_
		FROM `tabJarvis User Model Usage`
		WHERE parenttype = %(ptype)s AND parentfield = %(pfield)s AND month_key = %(month)s
		GROUP BY model
		""",
		{"ptype": USER_SETTINGS, "pfield": MODEL_USAGE_FIELD, "month": month},
		as_dict=True,
	)


def get_or_create_user_settings(user: str):
	"""Return the ``Jarvis User Settings`` doc for ``user``, creating it if
	absent. Insert is ``ignore_permissions`` with an explicit ``owner=user`` so
	the ``if_owner`` permlevel-0 grant holds even when an admin (not the owner)
	triggered creation. Race-safe: a concurrent creator that wins the unique
	constraint just makes us re-read theirs."""
	existing = frappe.db.exists(USER_SETTINGS, {"user": user})
	if existing:
		return frappe.get_doc(USER_SETTINGS, existing)
	try:
		doc = frappe.get_doc(
			{
				"doctype": USER_SETTINGS,
				"user": user,
				"owner": user,
			}
		)
		doc.insert(ignore_permissions=True)
		# Frappe stamps ``owner`` from the session user at insert; force it back
		# to the settings owner so ``if_owner`` holds after an admin-triggered
		# create. update_modified=False keeps the audit stamp meaningful.
		if frappe.db.get_value(USER_SETTINGS, doc.name, "owner") != user:
			frappe.db.set_value(USER_SETTINGS, doc.name, "owner", user, update_modified=False)
		return frappe.get_doc(USER_SETTINGS, doc.name)
	except frappe.DuplicateEntryError:
		# A racing turn/request created the row between our exists() and insert;
		# the unique constraint on ``user`` (and the field:user autoname) is the
		# guard. Read the winner's row.
		return frappe.get_doc(USER_SETTINGS, {"user": user})


def fetch_fresh_session_row(sess, session_key: str, attempts: int = 3, delay_s: float = 1.5) -> dict | None:
	"""Poll the gateway's ``sessions.list`` (via the already-checked-out ``sess``)
	for a FRESH row for ``session_key``, retrying up to ``attempts`` times.

	Live-reproduced gap: a session's FIRST completed run can still read back
	``totalTokensFresh=False`` (or null token fields) at the exact moment the
	turn handler checks, and since snapshots overwrite rather than accumulate,
	that turn's usage is then lost forever — not just delayed. Retrying a
	bounded number of times inside the same checkout closes that window
	without holding the pooled connection indefinitely.

	Returns the first row that is both present and fresh (has a non-null
	``inputTokens`` or ``outputTokens``). If no attempt ever produces a fresh
	row, returns the LAST row seen anyway (``record_turn_usage``'s own
	freshness gate will just no-op it, same as before this retry existed) and
	logs once so the miss is visible instead of silently dropped.
	"""
	row: dict | None = None
	for attempt in range(attempts):
		rows = sess.list_sessions()
		row = next((r for r in rows if r.get("key") == session_key), None)
		if (
			row
			and row.get("totalTokensFresh")
			and (row.get("inputTokens") is not None or row.get("outputTokens") is not None)
		):
			return row
		if attempt < attempts - 1:
			time.sleep(delay_s)
	frappe.log_error(
		title="jarvis usage: session row never went fresh (turn usage lost)",
		message=f"session_key={session_key!r} last row={row!r}",
	)
	return row


# CDX-6: record_turn_usage's explicit outcome, so the finalize usage effect can
# tell an ACTUAL record (commit the guard) from a transient no-data read (roll the
# guard back and retry) — the naked None-return silently marked usage recorded when
# nothing was persisted, undercounting the soft cap forever.
USAGE_RECORDED = "recorded"  # a real token delta was accrued (committed)
USAGE_VALID_ZERO = "valid_zero"  # a fresh row that legitimately reports no usage
USAGE_RETRY = "retry"  # stale/missing/no-fresh data — do NOT mark recorded, retry


def resolved_model_identity(row: dict | None) -> tuple[str, str]:
	"""``(model, provider)`` the gateway attributes to this session's LAST
	COMPLETED run, read off a ``sessions.list`` row.

	This is the ONE place the wire names are decoded, so the per-model usage
	bucket below and the per-message attribution stamped by
	``finalize._stamp_reply_model`` (jarvis#560) can never disagree about which
	model a turn is charged to and which model the transcript credits.

	Wire shape, verified against the shipped bundle of
	ghcr.io/openclaw/openclaw:2026.6.8 (``buildGatewaySessionRow``): the row
	carries ``model`` + ``modelProvider``, resolved as the session's SELECTED
	override when it has one, else the RUNTIME identity persisted on the session
	entry after the run (``entry.model`` / ``entry.modelProvider``). The runtime
	half is why an unpinned conversation reports the model that actually answered
	rather than the chain's primary: the agent only keeps its ``model.fallbacks``
	chain live for a session with no override (see
	``turn_handler._session_model_for``), and a failover rewrites the entry.

	No live event carries this. The terminal ``chat`` frame's projected message is
	built fresh as ``{role, content, timestamp}`` by BOTH live emitters
	(``emitChatFinal`` in ``dist/embedded-backend-*.js`` and
	``dist/server-chat-*.js``), and the lifecycle ``end``/``error`` frame carries
	only ``phase`` + terminal metadata - the runtime logs
	``model=... provider=...`` at agent end from ``lastAssistant``, but never puts
	it on the wire. So the durable session row is the earliest honest source, and
	reading it costs nothing extra: finalize already polls it for usage."""
	if not isinstance(row, dict):
		return "", ""
	return (row.get("model") or "").strip(), (row.get("modelProvider") or "").strip()


def _context_capacity_and_pct(row: dict | None, used_tokens: int) -> tuple[int, float]:
	"""``(context_capacity, context_pct)`` from a ``sessions.list`` row.

	``contextTokens`` is the model's context-WINDOW CAPACITY (verified live,
	2026-09-04: 200000), distinct from ``totalTokens`` (context tokens actually
	USED, already read as ``context_tokens`` by every caller here). ``pct`` is
	``100 * used_tokens / capacity`` rounded to 1 decimal, or ``0`` when the row
	never reported a capacity (``contextTokens`` missing/zero)."""
	capacity = int((row or {}).get("contextTokens") or 0)
	if capacity <= 0:
		return 0, 0.0
	return capacity, round(100 * used_tokens / capacity, 1)


def budget_fields_from_row(row: dict | None) -> tuple[str, int, int]:
	"""``(budget_route, reserve_tokens, compaction_count)`` from a sessions row.

	``contextBudgetStatus`` is an OBJECT on the runtime row (pre-prompt
	estimate; ``route`` is one of fits / compact_only /
	truncate_tool_results_only / compact_then_truncate). Absent or malformed
	values read as empty/zero so a missing field never breaks a turn."""
	row = row or {}
	status = row.get("contextBudgetStatus")
	if not isinstance(status, dict):
		status = {}
	route = str(status.get("route") or "")[:40]
	try:
		reserve = int(status.get("reserveTokens") or 0)
	except (TypeError, ValueError):
		reserve = 0
	try:
		count = int(row.get("compactionCheckpointCount") or 0)
	except (TypeError, ValueError):
		count = 0
	return route, max(reserve, 0), max(count, 0)


def _write_budget_fields(session_key: str, row: dict | None) -> None:
	"""UPDATE the budget snapshot columns. Same no-commit contract as
	``_refresh_session_context_snapshot``. ``compaction_count`` only moves
	forward (the row is authoritative when it reports one).

	Compaction amendment (context-meter task 4): the runtime CLEARS
	``contextBudgetStatus`` on a compaction turn, so a row with no status is
	not "route/reserve are now empty" - it is "this row didn't report a
	budget this time". Blanking ``budget_route``/``reserve_tokens`` on such a
	row would erase a good reserve right after a compaction, so when the
	status is absent this only advances ``compaction_count`` and leaves the
	other two columns untouched. A row that DOES carry a status still writes
	all three, same as before."""
	route, reserve, count = budget_fields_from_row(row)
	if not isinstance((row or {}).get("contextBudgetStatus"), dict):
		frappe.db.sql(
			"""
			UPDATE `tabJarvis Chat Session`
			SET compaction_count = GREATEST(IFNULL(compaction_count, 0), %(count)s)
			WHERE session_key = %(session_key)s
			""",
			{"count": count, "session_key": session_key},
		)
		return
	frappe.db.sql(
		"""
		UPDATE `tabJarvis Chat Session`
		SET budget_route = %(route)s,
			reserve_tokens = %(reserve)s,
			compaction_count = GREATEST(IFNULL(compaction_count, 0), %(count)s)
		WHERE session_key = %(session_key)s
		""",
		{"route": route, "reserve": reserve, "count": count, "session_key": session_key},
	)


def _refresh_session_context_snapshot(
	session_key: str, context_tokens: int, context_capacity: int, context_pct: float
) -> None:
	"""Snapshot-only UPDATE (no token/run-count accrual) of a Chat Session's
	context fields - used on ``record_turn_usage``'s VALID_ZERO path, where
	there is no token delta to accrue but the row's context snapshot can
	still have moved.

	Mirrors ``refresh_session_snapshots``'s capacity guard: ``context_capacity``
	/ ``context_pct`` are only written when THIS row actually reported a
	capacity (``context_capacity > 0``) - a row that carries none must never
	clobber a previously known capacity with 0. Uncommitted, matching the
	VALID_ZERO/RETRY paths' no-commit contract (see ``record_turn_usage``'s
	docstring) - the caller's outer commit (RECORDED) or the finalize effect's
	own commit covers it."""
	params = {
		"ctx": context_tokens,
		"now": frappe.utils.now_datetime(),
		"session_key": session_key,
	}
	capacity_set_sql = ""
	if context_capacity > 0:
		capacity_set_sql = "context_capacity = %(ctx_cap)s, context_pct = %(ctx_pct)s,"
		params["ctx_cap"] = context_capacity
		params["ctx_pct"] = context_pct
	frappe.db.sql(
		f"""
		UPDATE `tabJarvis Chat Session`
		SET last_total_tokens = %(ctx)s,
			{capacity_set_sql}
			last_usage_at = %(now)s
		WHERE session_key = %(session_key)s
		""",
		params,
	)


def record_turn_usage(session_key: str, row: dict | None, run_id: str | None = None) -> str:
	"""Record one completed turn's token delta from a ``sessions.list`` row.

	``row`` is the gateway row for THIS session (matched by ``key`` upstream).
	``inputTokens``/``outputTokens`` are last-run values, so the turn delta is
	their sum; ``totalTokens`` is the context size (snapshot only). A row with
	``totalTokensFresh`` false/missing, or null token fields, is do-not-record.

	``run_id`` (task U1, usage-dashboard Part A) is the ``Jarvis Chat Turn.name``
	finalize's usage effect calls this with, threaded through ONLY to attribute
	the best-effort ``Jarvis Turn Usage`` row and to seq-bound its tool-call
	count. Optional and defaulted to ``None`` so the standalone-caller contract
	(tests, and any future non-finalize caller) is unchanged: without it the row
	still gets written on the RECORDED/VALID_ZERO paths, just with
	``tool_calls=0`` and a blank ``run_id``.

	CDX-6 — returns an EXPLICIT outcome so a caller (finalize's usage effect) can
	distinguish a real accrual from a transient no-data read instead of treating a
	silent no-op as success:
	  * ``USAGE_RECORDED``   — a positive delta was accrued AND committed here.
	  * ``USAGE_VALID_ZERO`` — an ATTRIBUTED zero delta (a fresh row that legitimately
	                           reports no usage): nothing to record and retrying will not
	                           change that (mark done).
	  * ``USAGE_RETRY``      — stale / missing / not-fresh data, OR a fresh POSITIVE
	                           delta with no user mapping (unattributed real usage,
	                           CDX-6): the caller must NOT mark usage recorded; leave the
	                           effect pending to retry (force-done budget logs the loss).
	Commits internally ONLY on the ``USAGE_RECORDED`` path (the standalone-caller
	contract the usage tests rely on); the zero/retry paths do NOT commit, so a
	finalize caller can roll back an uncommitted guard on retry. NEVER raises — a
	usage-accounting bug must not break chat."""
	try:
		if not row or not row.get("totalTokensFresh"):
			return USAGE_RETRY
		raw_in = row.get("inputTokens")
		raw_out = row.get("outputTokens")
		if raw_in is None and raw_out is None:
			return USAGE_RETRY
		input_tokens = int(raw_in or 0)
		output_tokens = int(raw_out or 0)
		delta = input_tokens + output_tokens
		# CDX-review: ONE session fetch (user + profile_agent_id + profile_tier)
		# covers both branches below - the RECORDED path used to fetch only
		# `user` here and `_write_turn_usage_row` re-queried the same row for
		# the two profile fields; VALID_ZERO relied on that same internal
		# re-query. Widened + threaded through instead (review finding #3).
		session = (
			frappe.db.get_value(
				CHAT_SESSION,
				{"session_key": session_key},
				["user", "profile_agent_id", "profile_tier"],
				as_dict=True,
			)
			or {}
		)
		user = session.get("user") or ""
		context_tokens = int(row.get("totalTokens") or 0)
		context_capacity, context_pct = _context_capacity_and_pct(row, context_tokens)
		if delta <= 0:
			# Task U1: attribution is still worth recording even though there is
			# no token delta - the turn happened and this is the only record of
			# WHO it happened for. Isolated + never raises (see the docstring).
			_write_turn_usage_row(session_key, row, run_id, input_tokens, output_tokens, session)
			# C1 review: a zero-delta turn is still a real turn - the session's
			# context snapshot (and, when this row reports one, its capacity)
			# can have moved even though no tokens were spent this turn. Same
			# no-commit contract as the rest of this branch (see docstring).
			_refresh_session_context_snapshot(session_key, context_tokens, context_capacity, context_pct)
			_write_budget_fields(session_key, row)
			return USAGE_VALID_ZERO

		if not user:
			# CDX-6: a FRESH POSITIVE token delta with no `Jarvis Chat Session` user
			# mapping is unattributed real usage, NOT legitimate zero — it must NOT be
			# permanently marked recorded. RETRY so the finalize effect leaves its guard
			# rolled back and re-attempts (the mapping may materialize on a later cycle);
			# the bounded force-done budget logs the undercount loudly if it never does.
			return USAGE_RETRY

		# Ensure the per-user row exists (in this same transaction) before the
		# atomic UPDATE targets it.
		get_or_create_user_settings(user)

		now = frappe.utils.now_datetime()
		month = current_month_key()
		params = {
			"in": input_tokens,
			"out": output_tokens,
			"delta": delta,
			"ctx": context_tokens,
			"ctx_cap": context_capacity,
			"ctx_pct": context_pct,
			"month": month,
			"now": now,
			"user": user,
			"session_key": session_key,
		}
		# Month rollover done inside SQL so the read-modify-write is atomic:
		# when usage_month already matches, add; otherwise reset the month
		# buckets to this delta. total_tokens is all-time and never resets.
		frappe.db.sql(
			"""
			UPDATE `tabJarvis User Settings`
			SET
				month_input_tokens = CASE WHEN usage_month = %(month)s
					THEN month_input_tokens + %(in)s ELSE %(in)s END,
				month_output_tokens = CASE WHEN usage_month = %(month)s
					THEN month_output_tokens + %(out)s ELSE %(out)s END,
				month_tokens = CASE WHEN usage_month = %(month)s
					THEN month_tokens + %(delta)s ELSE %(delta)s END,
				total_tokens = total_tokens + %(delta)s,
				usage_month = %(month)s,
				last_usage_at = %(now)s,
				modified = %(now)s
			WHERE user = %(user)s
			""",
			params,
		)
		frappe.db.sql(
			"""
			UPDATE `tabJarvis Chat Session`
			SET
				input_tokens = input_tokens + %(in)s,
				output_tokens = output_tokens + %(out)s,
				run_count = run_count + 1,
				last_total_tokens = %(ctx)s,
				context_capacity = %(ctx_cap)s,
				context_pct = %(ctx_pct)s,
				last_usage_at = %(now)s
			WHERE session_key = %(session_key)s
			""",
			params,
		)
		_write_budget_fields(session_key, row)
		# Per-model attribution (fleet spec §7): the gateway sessions row
		# carries whatever model the SESSION resolved to for this turn
		# (turn_handler._session_model_for). For a pinned model that's the
		# real model. For an unpinned BIFROST pool the session is patched to
		# POOL_VIRTUAL_MODEL, so the turn lands in the sentinel bucket -
		# Bifrost picks the actual per-request model server-side and that
		# choice never comes back to the bench. That's an intentionally
		# honest bucket ("pool auto-routed"), not a bug: those tenants get
		# true per-model data from Bifrost logs on the admin side. An
		# unpinned agent-DIRECT pool no longer names a model at all (it
		# RESETS the session, which is what keeps the container's failover
		# chain live), so its row reports whatever the gateway attributes to
		# an unoverridden session - the resolved agent default, or the
		# fallback that actually ran. That is strictly more accurate than the
		# primary's id the bench used to guess, and if the gateway reports no
		# model for such a row the guard below simply records the aggregate.
		# Missing/blank model → aggregate only, no per-model row.
		#
		# Isolated in its OWN try/except: this call sits between the
		# aggregate UPDATEs above and the commit below. If it raises and is
		# left to the outer except, the function returns without committing
		# - losing the aggregate deltas that already executed in this same
		# transaction, which is worse than a missing per-model row. A bare
		# rollback here would be equally wrong (it would deterministically
		# discard the aggregate delta), so this just logs and continues,
		# letting the aggregate updates reach the commit below regardless of
		# what happened here.
		model, _provider = resolved_model_identity(row)
		if model:
			try:
				_upsert_model_usage(user, model, month, input_tokens, output_tokens, now)
			except Exception:
				frappe.log_error(
					title="jarvis usage: per-model write failed",
					message=frappe.get_traceback(),
				)
		# Task U1: the per-turn usage row, same isolation as the per-model
		# write just above - a failure here must not lose the aggregate delta
		# already applied in this transaction, so it only logs and continues.
		_write_turn_usage_row(session_key, row, run_id, input_tokens, output_tokens, session)
		frappe.db.commit()
		return USAGE_RECORDED
	except Exception:
		frappe.log_error(
			title="jarvis usage: record_turn_usage failed",
			message=frappe.get_traceback(),
		)
		# A partial write is possible; treat a hard failure as retriable so the
		# finalize usage effect leaves its guard rolled back and re-attempts.
		return USAGE_RETRY


def _write_turn_usage_row(
	session_key: str,
	row: dict | None,
	run_id: str | None,
	tokens_in: int,
	tokens_out: int,
	session: dict,
) -> None:
	"""Best-effort ``Jarvis Turn Usage`` row (task U1, usage-dashboard Part A).

	``session`` is the ``Jarvis Chat Session`` row (``user`` /
	``profile_agent_id`` / ``profile_tier``) the caller already fetched ONCE
	(review finding #3 - this used to re-query the same session by
	``session_key`` a second time; the caller's single fetch now covers both
	call sites). May be ``{}`` when the session mapping is missing (blank-user
	attribution row, U1's VALID_ZERO/unmapped-session case).

	Wrapped end-to-end so a failure here can NEVER change ``record_turn_usage``'s
	returned outcome or raise into the turn - the same isolation the per-model
	attribution write above uses, and for the same reason: this runs between the
	aggregate UPDATEs and the outer commit (RECORDED path) or before any commit at
	all (VALID_ZERO path), so a bare exception left to reach the caller would lose
	real accrual, and a rollback here would be equally wrong (it would
	deterministically discard the aggregate delta on the RECORDED path).

	cache_read / cache_write / cache_reported: live-checked against a real gateway
	(tenant jarvis-pool-68b37b, 2026-08-17) - the union of keys across 23 live
	``sessions.list`` rows carried no cache-token field at all, so these are
	always ``0 / 0 / False`` (an honest "not reported", not a fabricated zero)
	until a later agent-runtime build adds one. Re-run the Step-1 live probe from
	the task-U1 brief to check."""
	try:
		model, _provider = resolved_model_identity(row)
		fields = {
			"run_id": run_id or "",
			"session_key": session_key,
			"user": session.get("user") or "",
			"profile_agent_id": session.get("profile_agent_id") or "",
			"profile_tier": session.get("profile_tier") or "full",
			"model": model,
			"tokens_in": int(tokens_in or 0),
			"tokens_out": int(tokens_out or 0),
			"cache_read": 0,
			"cache_write": 0,
			"cache_reported": 0,
			"tool_calls": _turn_tool_call_count(run_id),
			"day": frappe.utils.today(),
		}
		_insert_turn_usage_row(fields)
	except Exception:
		frappe.log_error(
			title="jarvis usage: turn usage row write failed",
			message=frappe.get_traceback(),
		)


def _insert_turn_usage_row(fields: dict) -> None:
	"""Plain ORM insert, one row per call - no race to guard against (contrast
	the per-model child-row upsert machinery above, which merges concurrent
	writers on the SAME parent+model+month key). Split out as its own function
	so tests can monkeypatch just the write and observe that a raise here
	never escapes ``_write_turn_usage_row``."""
	frappe.get_doc({"doctype": TURN_USAGE, **fields}).insert(ignore_permissions=True)


def _turn_tool_call_count(run_id: str | None) -> int:
	"""Count of ``role=tool`` ``Jarvis Chat Message`` rows belonging to the turn
	named ``run_id``, or 0 when ``run_id`` is absent or unresolvable.

	``Jarvis Chat Message`` carries no direct run/turn link, so this MIRRORS
	(does not call - the filter shapes differ: this needs both a seq LOWER and
	UPPER bound and no ``ref_doctype`` requirement, see the sibling comment on
	``jarvis.chat.entities.entities_for_turn``) the same seq-bounded idiom that
	function applies for the same reason: tool rows strictly after the turn's
	``seed_message`` and at-or-before its ``assistant_message`` (by ``seq``,
	within the same conversation) belong to this turn and no other."""
	if not run_id:
		return 0
	turn = frappe.db.get_value(
		CHAT_TURN,
		run_id,
		["conversation", "seed_message", "assistant_message"],
		as_dict=True,
	)
	if (
		not turn
		or not turn.get("conversation")
		or not turn.get("seed_message")
		or not turn.get("assistant_message")
	):
		return 0
	seqs = frappe.db.get_all(
		CHAT_MESSAGE,
		filters={"name": ["in", [turn["seed_message"], turn["assistant_message"]]]},
		fields=["name", "seq"],
	)
	seq_by_name = {r["name"]: r["seq"] for r in seqs}
	seed_seq = seq_by_name.get(turn["seed_message"])
	asst_seq = seq_by_name.get(turn["assistant_message"])
	if seed_seq is None or asst_seq is None:
		return 0
	return frappe.db.count(
		CHAT_MESSAGE,
		filters=[
			["conversation", "=", turn["conversation"]],
			["role", "=", "tool"],
			["seq", ">", seed_seq],
			["seq", "<=", asst_seq],
		],
	)


def prune_turn_usage() -> int:
	"""Daily scheduler job (``hooks.py``) - delete ``Jarvis Turn Usage`` rows
	older than ``TURN_USAGE_RETENTION_DAYS``. Mirrors
	``jarvis.mobile.device_auth.prune_revoked_devices`` /
	``jarvis.error_push.prune_pushed_client_errors``: an append-only accounting
	table needs an explicit sweep or it grows forever. Best-effort; never
	raises out of the scheduler."""
	cutoff = frappe.utils.add_days(frappe.utils.today(), -TURN_USAGE_RETENTION_DAYS)
	names = frappe.get_all(
		TURN_USAGE, filters={"day": ["<", cutoff]}, pluck="name", limit=TURN_USAGE_PRUNE_BATCH_LIMIT
	)
	deleted = 0
	for name in names:
		try:
			frappe.delete_doc(TURN_USAGE, name, ignore_permissions=True, force=True, delete_permanently=True)
			deleted += 1
		except Exception:
			frappe.logger("jarvis.usage").warning(f"could not prune turn usage row {name}", exc_info=True)
	if deleted:
		frappe.db.commit()
	return deleted


def _next_child_idx(user: str) -> int:
	"""Next 1-based idx for a new child row under ``user``'s settings. Child idx
	is ordering-only; correctness doesn't depend on it, but keep it monotone."""
	rows = frappe.db.sql(
		"""SELECT COALESCE(MAX(idx), 0) + 1
		   FROM `tabJarvis User Model Usage`
		   WHERE parent = %(user)s AND parenttype = %(ptype)s""",
		{"user": user, "ptype": USER_SETTINGS},
	)
	return int(rows[0][0]) if rows and rows[0] else 1


def _current_model_row_name(user: str, model: str, month: str) -> str | None:
	return frappe.db.get_value(
		MODEL_USAGE,
		{
			"parent": user,
			"parenttype": USER_SETTINGS,
			"parentfield": MODEL_USAGE_FIELD,
			"model": model,
			"month_key": month,
		},
		"name",
	)


def _prior_model_limit(user: str, model: str, month: str) -> int:
	"""Newest prior-month per-model cap for (user, model), so a configured cap
	survives the month rollover instead of resetting to 0. 0 when none exists."""
	rows = frappe.get_all(
		MODEL_USAGE,
		filters={
			"parent": user,
			"parenttype": USER_SETTINGS,
			"parentfield": MODEL_USAGE_FIELD,
			"model": model,
			"month_key": ["!=", month],
		},
		fields=["monthly_token_limit"],
		order_by="month_key desc",
		limit=1,
	)
	return int(rows[0].monthly_token_limit or 0) if rows else 0


def _model_row_insert_params(
	user: str, model: str, month: str, in_tokens: int, out_tokens: int, limit: int, now
) -> dict:
	"""Shared param dict for a fresh (parent, model, month) child-row INSERT.
	Used by both ``_insert_model_row`` (plain INSERT, ``set_model_limit``'s
	no-existing-row path) and ``_atomic_insert_or_merge_model_usage`` (INSERT
	... ON DUPLICATE KEY UPDATE, the turn-accounting race path) so the two
	SQL statements can't drift on column list or value shape. ``owner``/
	``modified_by`` are not permission-load-bearing for a child row
	(parent-row scoping governs child access), so ``Administrator`` is fine."""
	return {
		"name": frappe.generate_hash(length=10),
		"now": now,
		"admin": "Administrator",
		"idx": _next_child_idx(user),
		"user": user,
		"pfield": MODEL_USAGE_FIELD,
		"ptype": USER_SETTINGS,
		"model": model,
		"month": month,
		"in": int(in_tokens),
		"out": int(out_tokens),
		"limit": int(limit),
	}


def _insert_model_row(
	user: str,
	model: str,
	month: str,
	*,
	in_tokens: int,
	out_tokens: int,
	limit: int,
	now,
) -> None:
	"""Insert a fresh child row via raw SQL (the atomic idiom this module uses).
	Direct child-doc ORM insert is not used — Frappe routes child writes through
	the parent; a raw INSERT with an explicit hash name is the reliable path."""
	frappe.db.sql(
		"""
		INSERT INTO `tabJarvis User Model Usage`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 parent, parentfield, parenttype,
			 model, month_key, month_input_tokens, month_output_tokens, monthly_token_limit)
		VALUES
			(%(name)s, %(now)s, %(now)s, %(admin)s, %(admin)s, 0, %(idx)s,
			 %(user)s, %(pfield)s, %(ptype)s,
			 %(model)s, %(month)s, %(in)s, %(out)s, %(limit)s)
		""",
		_model_row_insert_params(user, model, month, in_tokens, out_tokens, limit, now),
	)


def _atomic_insert_or_merge_model_usage(
	user: str, model: str, month: str, in_tokens: int, out_tokens: int, limit: int, now
) -> bool:
	"""Insert a fresh (parent, model, month) child row, or - if a racing writer
	already created one since our caller's existence check (two turns on
	DIFFERENT conversations can both miss ``_current_model_row_name``'s SELECT
	for the same model's first use in a month, since the single-flight guard
	in ``jarvis.chat.api`` is only per-conversation) - merge this call's delta
	into theirs instead of creating a duplicate row.

	Atomic: backed by the unique index on (parent, parentfield, model,
	month_key) added by ``jarvis.patches.v2_02_unique_model_usage_row``, via
	``INSERT ... ON DUPLICATE KEY UPDATE``, so a racing writer's delta can
	never be lost OR duplicated. ``limit`` is only applied on the winning
	INSERT - the loser's write must not clobber a cap the winner (or an
	admin, via ``set_model_limit``) may have set concurrently.

	Returns True iff THIS call's row was the one that got inserted (vs.
	merging into an existing row) - the caller uses this to gate the
	once-per-month stale-row cleanup so it only runs on the actual insert."""
	frappe.db.sql(
		"""
		INSERT INTO `tabJarvis User Model Usage`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 parent, parentfield, parenttype,
			 model, month_key, month_input_tokens, month_output_tokens, monthly_token_limit)
		VALUES
			(%(name)s, %(now)s, %(now)s, %(admin)s, %(admin)s, 0, %(idx)s,
			 %(user)s, %(pfield)s, %(ptype)s,
			 %(model)s, %(month)s, %(in)s, %(out)s, %(limit)s)
		ON DUPLICATE KEY UPDATE
			month_input_tokens = month_input_tokens + VALUES(month_input_tokens),
			month_output_tokens = month_output_tokens + VALUES(month_output_tokens),
			modified = VALUES(modified)
		""",
		_model_row_insert_params(user, model, month, in_tokens, out_tokens, limit, now),
	)
	# Read rowcount BEFORE commit (commit can reset the cursor) - MariaDB
	# reports 1 for a plain INSERT and 2 when ON DUPLICATE KEY UPDATE fired.
	# Mirrors the rowcount idiom in jarvis.chat.turn_recovery._conditional_clear
	# and jarvis_admin_v2.fleet.pool's pool-claim race guard.
	cursor = getattr(frappe.db, "_cursor", None)
	return bool(cursor and cursor.rowcount == 1)


def _upsert_model_usage(user: str, model: str, month: str, in_tokens: int, out_tokens: int, now) -> None:
	"""Upsert the (user, model, current-month) child row with this turn's delta.

	Month-to-date only on the bench (admin owns history, fleet spec §3): on
	rollover we drop this model's stale rows and start a fresh current-month row,
	inheriting any configured cap so a per-model limit is never lost. We also
	opportunistically drop the user's OTHER stale rows that carry no cap (pure
	usage history the admin already persisted); stale rows that DO carry a cap
	linger until their own model records a turn (which carries the cap forward),
	so an admin-set cap is never silently dropped."""
	name = _current_model_row_name(user, model, month)
	if name:
		# A single UPDATE on a row known to exist is race-safe on its own -
		# MySQL serializes concurrent UPDATEs to the same row - so the fast,
		# common-case path skips the atomic insert-or-merge machinery below.
		frappe.db.sql(
			"""UPDATE `tabJarvis User Model Usage`
			   SET month_input_tokens = month_input_tokens + %(in)s,
				   month_output_tokens = month_output_tokens + %(out)s,
				   modified = %(now)s
			   WHERE name = %(name)s""",
			{"in": int(in_tokens), "out": int(out_tokens), "now": now, "name": name},
		)
	else:
		# Race-prone: two turns in DIFFERENT conversations can both reach here
		# for the same model's first use in a month (see
		# _atomic_insert_or_merge_model_usage's docstring). The unique index
		# makes the loser's write merge instead of duplicating.
		limit = _prior_model_limit(user, model, month)
		inserted = _atomic_insert_or_merge_model_usage(user, model, month, in_tokens, out_tokens, limit, now)
		if inserted:
			# This model's stale-month rows (cap already carried forward) go now.
			frappe.db.sql(
				"""DELETE FROM `tabJarvis User Model Usage`
				   WHERE parent = %(user)s AND parenttype = %(ptype)s
					 AND parentfield = %(pfield)s AND model = %(model)s
					 AND month_key != %(month)s""",
				{
					"user": user,
					"ptype": USER_SETTINGS,
					"pfield": MODEL_USAGE_FIELD,
					"model": model,
					"month": month,
				},
			)
		# else: a racing writer already inserted the current-month row for
		# this model and our delta merged into it above; that writer's own
		# call already ran (or will run) the stale-row cleanup.
	# Opportunistic: drop stale usage-only rows (no cap) for all this user's models.
	frappe.db.sql(
		"""DELETE FROM `tabJarvis User Model Usage`
		   WHERE parent = %(user)s AND parenttype = %(ptype)s
			 AND parentfield = %(pfield)s AND month_key != %(month)s
			 AND COALESCE(monthly_token_limit, 0) = 0""",
		{"user": user, "ptype": USER_SETTINGS, "pfield": MODEL_USAGE_FIELD, "month": month},
	)


def set_model_limit(user: str, model: str, limit: int, now=None) -> None:
	"""Upsert the per-model cap on the current-month child row, creating the row
	(zero usage) when the model has no usage yet this month. Admin-gated by the
	caller (jarvis.chat.user_settings_api.admin_set_user_model_limit)."""
	now = now or frappe.utils.now_datetime()
	month = current_month_key()
	limit = max(0, int(limit or 0))
	name = _current_model_row_name(user, model, month)
	if name:
		frappe.db.sql(
			"""UPDATE `tabJarvis User Model Usage`
			   SET monthly_token_limit = %(limit)s, modified = %(now)s
			   WHERE name = %(name)s""",
			{"limit": limit, "now": now, "name": name},
		)
	else:
		_insert_model_row(user, model, month, in_tokens=0, out_tokens=0, limit=limit, now=now)
	frappe.db.commit()


def refresh_session_snapshots(rows: list[dict]) -> dict:
	"""Refresh per-session snapshot fields from a ``sessions.list`` sweep,
	WITHOUT accumulating counters (the "sync from agent" endpoint).

	For every gateway row that maps to a known ``Jarvis Chat Session``, snapshot
	``last_total_tokens`` (= context size) and — when the row carries the
	gateway's ``updatedAt`` (ms epoch) — ``last_usage_at`` from THAT stamp, not
	sync time (an idle session must not look freshly active after a sweep).
	Stamps the owning user's ``Jarvis User Settings.last_synced_at``. Returns a
	per-user summary ``{user: {sessions, last_total_tokens}}`` for the admin UI.
	Best effort per row; a malformed row is skipped, never raised."""
	summary: dict[str, dict] = {}
	now = frappe.utils.now_datetime()
	touched_users: set[str] = set()
	for row in rows or []:
		try:
			if not isinstance(row, dict):
				continue
			session_key = row.get("key")
			if not session_key:
				continue
			user = frappe.db.get_value(CHAT_SESSION, {"session_key": session_key}, "user")
			if not user:
				continue
			context_tokens = int(row.get("totalTokens") or 0)
			context_capacity, context_pct = _context_capacity_and_pct(row, context_tokens)
			updated_ms = row.get("updatedAt")
			# Naive system-tz datetime, matching how Frappe stores Datetime.
			last_at = datetime.fromtimestamp(int(updated_ms) / 1000) if updated_ms else None
			# ONE update (review: two near-duplicate UPDATEs made "newest row"
			# ordering unreliable elsewhere - a raw SQL UPDATE never bumps
			# modified on its own). last_usage_at means "last REAL usage", not
			# sync time (test_user_settings.TestAdminSync.
			# test_refreshes_snapshots_without_accumulating pins this: a row
			# with no updatedAt must leave last_usage_at exactly as it was -
			# untouched if never set, unchanged if it was), so COALESCE has NO
			# now()/`now` fallback here - only the row's own updatedAt stamp,
			# else whatever is already stored. modified = %(now)s always
			# advances regardless, so "just synced" stays orderable even when
			# last_usage_at itself doesn't move. context_capacity /
			# context_pct are set ONLY when THIS row actually reported a
			# capacity (review: a sweep row that carries none must never
			# clobber a previously known capacity with 0).
			params = {
				"ctx": context_tokens,
				"usage_at": last_at,
				"now": now,
				"session_key": session_key,
			}
			capacity_set_sql = ""
			if context_capacity > 0:
				capacity_set_sql = "context_capacity = %(ctx_cap)s, context_pct = %(ctx_pct)s,"
				params["ctx_cap"] = context_capacity
				params["ctx_pct"] = context_pct
			frappe.db.sql(
				f"""
				UPDATE `tabJarvis Chat Session`
				SET last_total_tokens = %(ctx)s,
					{capacity_set_sql}
					last_usage_at = COALESCE(%(usage_at)s, last_usage_at),
					modified = %(now)s
				WHERE session_key = %(session_key)s
				""",
				params,
			)
			_write_budget_fields(session_key, row)
			touched_users.add(user)
			bucket = summary.setdefault(user, {"sessions": 0, "last_total_tokens": 0})
			bucket["sessions"] += 1
			bucket["last_total_tokens"] += context_tokens
		except Exception:
			frappe.log_error(
				title="jarvis usage: snapshot refresh row failed",
				message=frappe.get_traceback(),
			)
	if touched_users:
		# Batched: one query to find which touched users already have a
		# settings row (create only the missing ones), then a single UPDATE
		# for last_synced_at instead of a get_or_create + db.set_value pair
		# per touched user.
		try:
			existing = set(
				frappe.get_all(
					USER_SETTINGS,
					filters={"user": ["in", list(touched_users)]},
					pluck="user",
				)
			)
			for user in touched_users - existing:
				get_or_create_user_settings(user)
			frappe.db.sql(
				"""
				UPDATE `tabJarvis User Settings`
				SET last_synced_at = %(now)s
				WHERE user IN %(users)s
				""",
				{"now": now, "users": tuple(touched_users)},
			)
		except Exception:
			frappe.log_error(
				title="jarvis usage: last_synced_at stamp failed",
				message=frappe.get_traceback(),
			)
	frappe.db.commit()
	return summary
