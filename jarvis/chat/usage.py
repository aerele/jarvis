"""Real per-turn token-usage accounting for managed (gateway) chat.

The openclaw gateway's ``sessions.list`` rows carry per-session
``inputTokens`` / ``outputTokens`` that are **last-completed-run** numbers
(not cumulative), ``totalTokens`` (context size), and ``totalTokensFresh``
(validity marker). Accurate accounting therefore records a *delta at each
turn end* while the turn handler still holds the pooled gateway connection —
see ``jarvis.chat.turn_handler`` and the design at
``docs/superpowers/specs/2026-07-10-user-settings-usage-design.md``.

Three entry points:
  * ``get_or_create_user_settings(user)`` — race-safe lazy row creation with an
    explicit ``owner`` so the ``if_owner`` grant holds when an admin triggers it.
  * ``record_turn_usage(session_key, row)`` — atomic SQL increments on both the
    per-user ``Jarvis User Settings`` (month-rollover aware) and the cumulative
    ``Jarvis Chat Session`` fields. Never raises into the turn.
  * ``refresh_session_snapshots(rows)`` — the admin "sync from agent" sweep;
    refreshes per-session snapshot fields WITHOUT accumulating counters.
"""

from __future__ import annotations

import time
from datetime import datetime

import frappe

# The pool-mode "Auto" sentinel model id. turn_handler._session_model_for
# patches an unpinned pool conversation's openclaw SESSION to this value
# (jarvis#299), so the gateway's sessions.list row reports it as `model` for
# that turn - see record_turn_usage's docstring below for what that means for
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


def current_month_key() -> str:
	"""The current usage bucket as ``"YYYY-MM"`` (site timezone, matching the
	``now_datetime()`` stamps used for ``last_usage_at``)."""
	return frappe.utils.now_datetime().strftime("%Y-%m")


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


def record_turn_usage(session_key: str, row: dict | None) -> str:
	"""Record one completed turn's token delta from a ``sessions.list`` row.

	``row`` is the gateway row for THIS session (matched by ``key`` upstream).
	``inputTokens``/``outputTokens`` are last-run values, so the turn delta is
	their sum; ``totalTokens`` is the context size (snapshot only). A row with
	``totalTokensFresh`` false/missing, or null token fields, is do-not-record.

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
		if delta <= 0:
			return USAGE_VALID_ZERO
		context_tokens = int(row.get("totalTokens") or 0)

		user = frappe.db.get_value(CHAT_SESSION, {"session_key": session_key}, "user")
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
				last_usage_at = %(now)s
			WHERE session_key = %(session_key)s
			""",
			params,
		)
		# Per-model attribution (fleet spec §7): the gateway sessions row
		# carries whatever model the SESSION was patched to for this turn
		# (turn_handler._session_model_for). For a pinned model that's the
		# real model. For an unpinned pool "Auto" conversation it is instead
		# POOL_VIRTUAL_MODEL - Bifrost picks the actual per-request model
		# server-side, and that choice never comes back to the bench, so
		# every Auto turn is attributed to the sentinel, not the real model.
		# That's an intentionally honest bucket ("pool auto-routed"), not a
		# bug: pool tenants get true per-model data from Bifrost logs on the
		# admin side. Missing/blank model → aggregate only, no per-model row.
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
		model = (row.get("model") or "").strip()
		if model:
			try:
				_upsert_model_usage(user, model, month, input_tokens, output_tokens, now)
			except Exception:
				frappe.log_error(
					title="jarvis usage: per-model write failed",
					message=frappe.get_traceback(),
				)
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
			updated_ms = row.get("updatedAt")
			if updated_ms:
				# Naive system-tz datetime, matching how Frappe stores Datetime.
				last_at = datetime.fromtimestamp(int(updated_ms) / 1000)
				frappe.db.sql(
					"""
					UPDATE `tabJarvis Chat Session`
					SET last_total_tokens = %(ctx)s, last_usage_at = %(at)s
					WHERE session_key = %(session_key)s
					""",
					{"ctx": context_tokens, "at": last_at, "session_key": session_key},
				)
			else:
				frappe.db.sql(
					"""
					UPDATE `tabJarvis Chat Session`
					SET last_total_tokens = %(ctx)s
					WHERE session_key = %(session_key)s
					""",
					{"ctx": context_tokens, "session_key": session_key},
				)
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
