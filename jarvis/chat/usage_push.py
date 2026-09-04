"""Daily month-to-date usage rollup push to admin (Architecture A, fleet usage
spec §3/§5).

The bench holds month-to-date running counters (per user + per model), not a
per-day ledger, so the push is an idempotent month-to-date SNAPSHOT: admin
upserts on (tenant, user, month) and owns history. Best-effort - a push failure
never affects chat. Un-onboarded benches (no admin credentials) simply don't
push; admin then shows "no usage".
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

import frappe

from jarvis.chat import usage
from jarvis.exceptions import AdminAuthError

USER_SETTINGS = usage.USER_SETTINGS
MODEL_USAGE = usage.MODEL_USAGE
MODEL_USAGE_FIELD = usage.MODEL_USAGE_FIELD
TURN_USAGE = usage.TURN_USAGE

# Hard cap on users per push (spec §7). Bounds payload size; extra users are
# dropped (highest-usage first kept) and the truncation is logged.
_MAX_USERS = 500

# Caps on the rollup's task-U2 aggregate lists (contract settled 2026-08-17).
_TOP_TOOLS_USER_CAP = 10
_TOP_TOOLS_TENANT_CAP = 15

# Sub-project C1, pinned wire contract (schema_version 2): the "who's using
# Jarvis most" admin dashboard adds a per-user context snapshot and a daily
# per-(user, day, model) breakdown on top of the month-to-date rollup above.
# ``users_daily`` window/caps and ``context`` null-handling are all spelled
# out in the pinned contract - do not add/rename/reshape keys without
# re-checking it, the admin ingest side is built from the same text.
SCHEMA_VERSION = 2
_USERS_DAILY_WINDOW_DAYS = 35
_USERS_DAILY_ROW_CAP = 5000
_USERS_DAILY_PER_MODEL_CAP = 20
_CONTEXT_OVER_PCT = 80

# Mirrors the admin ingest validator exactly (contract settled 2026-08-17): a
# bare name, or a "jarvis__" prefixed tool with an UNBOUNDED suffix. A name
# that matches neither would 400 the whole push (payload-atomic), so it is
# dropped and logged here instead of ever being emitted - see _valid_tool_name.
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
_JARVIS_TOOL_NAME_RE = re.compile(r"jarvis__[A-Za-z0-9_]+")

# Mirrors the admin profile validator: "" | "full" | role-[a-z0-9+-]{1,63}.
# profile_agent_id already stores "" or a well-formed "role-*" id in
# practice; this is cheap insurance against ever emitting something the
# validator would 400 on.
_PROFILE_RE = re.compile(r"role-[a-z0-9+-]{1,63}")


def _admin_configured() -> bool:
	"""jarvis_admin_url is set (site config outranks the Settings field). Mirrors
	installed_apps_sync._admin_configured. Credential *presence* is not checked
	here - admin_client raises AdminAuthError when unonboarded, which the caller
	treats as a quiet skip."""
	try:
		if (frappe.conf.get("jarvis_admin_url") or "").strip():
			return True
		settings = frappe.get_cached_doc("Jarvis Settings")
		return bool((settings.get("jarvis_admin_url") or "").strip())
	except Exception:
		return False


def _build_rollup(cap: int = _MAX_USERS) -> tuple[dict, bool]:
	"""Month-to-date snapshot: every settings row on the CURRENT month, highest
	usage first, capped. Returns (rollup, truncated). per_model is a dict keyed by
	model -> {in, out} (the pinned ingest contract).

	Task U2 adds, sourced from ``Jarvis Turn Usage`` / ``Jarvis Chat Message``
	(NOT from ``Jarvis User Settings``, which only carries token sums): each
	user's profile attribution, cache/tool-call aggregates and top_tools, plus
	the tenant-wide ``by_profile`` and ``top_tools`` blocks. ``users[]`` stays
	settings-sourced - a settings row with no Turn Usage rows this month just
	gets the empty/zero defaults, never an entry invented from Turn Usage.

	Schema version 2 (sub-project C1) adds, on top of the unchanged shape
	above: ``schema_version``, a per-user ``context`` block (from ``Jarvis Chat
	Session`` snapshots - see ``usage._context_capacity_and_pct``) and a
	top-level ``users_daily`` list (from ``Jarvis Turn Usage``, grouped by
	user/day/model, last 35 days including today). Both are additive and
	optional per the pinned contract: an old-shape rollup (no Turn Usage /
	Chat Session data at all) still round-trips, ``context`` fields going null
	and ``users_daily`` going empty rather than either key being fabricated."""
	month = usage.current_month_key()
	start, next_month = _month_range(month)
	rows = frappe.get_all(
		USER_SETTINGS,
		filters={"usage_month": month},
		fields=["name as user", "month_input_tokens", "month_output_tokens", "month_tokens"],
		order_by="month_tokens desc",
	)
	truncated = len(rows) > cap
	rows = rows[:cap]
	per_model_by_user = _per_model_totals([s.user for s in rows], month)
	turn_aggregates_by_user = _turn_usage_user_aggregates(start, next_month)
	top_tools_by_user, tool_calls_by_user, top_tools_tenant = _tool_message_aggregates(start, next_month)
	context_by_user = _context_by_user([s.user for s in rows])
	users = []
	for s in rows:
		agg = turn_aggregates_by_user.get(s.user, {})
		users.append(
			{
				"email": s.user,
				"tokens_in": int(s.month_input_tokens or 0),
				"tokens_out": int(s.month_output_tokens or 0),
				"total_tokens": int(s.month_tokens or 0),
				"per_model": per_model_by_user.get(s.user, {}),
				"profile": agg.get("profile", "full"),
				"turns": agg.get("turns", 0),
				"cache_read": agg.get("cache_read", 0),
				"cache_write": agg.get("cache_write", 0),
				"cache_reported": agg.get("cache_reported", False),
				# Deliberately from _tool_message_aggregates (the SAME
				# role=tool-message population top_tools counts), NOT from
				# Turn Usage - see that function's docstring (finding #2).
				"tool_calls": tool_calls_by_user.get(s.user, 0),
				"top_tools": top_tools_by_user.get(s.user, []),
				"context": context_by_user.get(s.user, _EMPTY_CONTEXT),
			}
		)
	rollup = {
		"month_key": month,
		"schema_version": SCHEMA_VERSION,
		"users": users,
		"by_profile": _by_profile(start, next_month),
		"top_tools": top_tools_tenant,
		"users_daily": _users_daily_rollup(),
	}
	return rollup, truncated


def _month_range(month: str) -> tuple[str, str]:
	"""Half-open ``[start, next)`` date strings ``"YYYY-MM-DD"`` for a
	``"YYYY-MM"`` month key - usable against both a Date column (Turn Usage's
	``day``) and a Datetime column (Chat Message's ``creation``)."""
	year, mon = (int(part) for part in month.split("-"))
	start = f"{month}-01"
	next_month = f"{year + 1}-01-01" if mon == 12 else f"{year}-{mon + 1:02d}-01"
	return start, next_month


def _valid_tool_name(name: str | None) -> bool:
	"""True iff ``name`` matches one of the admin ingest validator's two
	accepted shapes. Anything else must never be emitted (the push is
	payload-atomic on the admin side - one bad name 400s the whole rollup)."""
	if not name:
		return False
	return bool(_TOOL_NAME_RE.fullmatch(name) or _JARVIS_TOOL_NAME_RE.fullmatch(name))


def _normalize_profile(raw: str | None) -> str:
	"""Empty string -> "full"; a well-formed "role-*" id passes through unchanged;
	anything else is coerced to "full" and logged - should never fire in
	practice (profile_agent_id is only ever written as "" or "role-*"), but
	the admin's profile validator would 400 the whole push on a bad value."""
	raw = (raw or "").strip()
	if not raw:
		return "full"
	if _PROFILE_RE.fullmatch(raw):
		return raw
	frappe.logger("jarvis.usage_push").warning("dropping invalid profile_agent_id %r from rollup", raw)
	return "full"


def _turn_usage_user_aggregates(start: str, next_month: str) -> dict[str, dict]:
	"""Per-user turns / cache sums AND profile attribution for the month, ONE
	grouped query (``GROUP BY user, profile_agent_id``) instead of two
	separate scans of the same table/window (review finding #6). Blank-user
	rows (U1: VALID_ZERO turns with an unmapped session, documented on the
	Jarvis Turn Usage DocType) are excluded - every rollup aggregate over
	this table must filter them.

	Profile: most-frequent ``profile_agent_id`` this month, mapped to the
	payload's "full" spelling; ties broken by the most recent turn. ``ORDER
	BY user, cnt DESC, last_seen DESC`` puts each user's winning row first,
	so ``setdefault`` only ever sets ``profile`` from that first row while
	every row (winner or not) still folds into the running sums below.

	NOTE: does NOT return ``tool_calls`` - see ``_tool_message_aggregates``
	(review finding #2): a user's ``tool_calls`` must describe the SAME
	role=tool-message population ``top_tools`` counts (including
	errored/in-flight turns), not Turn Usage's per-COMPLETED-turn count."""
	out: dict[str, dict] = {}
	for r in frappe.db.sql(
		"""
		SELECT user,
			   profile_agent_id,
			   COUNT(*) AS cnt,
			   MAX(creation) AS last_seen,
			   SUM(cache_read) AS cache_read,
			   SUM(cache_write) AS cache_write,
			   MAX(cache_reported) AS cache_reported
		FROM `tabJarvis Turn Usage`
		WHERE user != '' AND day >= %(start)s AND day < %(next_month)s
		GROUP BY user, profile_agent_id
		ORDER BY user, cnt DESC, last_seen DESC
		""",
		{"start": start, "next_month": next_month},
		as_dict=True,
	):
		bucket = out.setdefault(
			r.user,
			{
				"profile": _normalize_profile(r.profile_agent_id),
				"turns": 0,
				"cache_read": 0,
				"cache_write": 0,
				"cache_reported": False,
			},
		)
		bucket["turns"] += int(r.cnt or 0)
		bucket["cache_read"] += int(r.cache_read or 0)
		bucket["cache_write"] += int(r.cache_write or 0)
		bucket["cache_reported"] = bucket["cache_reported"] or bool(r.cache_reported)
	return out


def _by_profile(start: str, next_month: str) -> list[dict]:
	"""Tenant-wide per-profile block: users / turns / token+cache sums from
	Turn Usage, plus n_skills / n_tools - static per profile, read off any
	Chat Session row referenced by this month's turns for that profile (LEFT
	JOIN + MAX, no N+1 and no "most recent session" ordering to get right).

	Grouped by (user, profile_agent_id) - NOT profile_agent_id alone - so
	``users`` is deduplicated AFTER mapping to the payload's profile name
	(review finding #1): a user whose rows split across raw values that both
	normalize to the same bucket (e.g. "" and an invalid value both folding
	into "full") must be counted once, which a raw ``COUNT(DISTINCT user)``
	grouped by the UNMAPPED ``profile_agent_id`` cannot do."""
	bucket_users: dict[str, set[str]] = {}
	merged: dict[str, dict] = {}
	for r in frappe.db.sql(
		"""
		SELECT tu.user AS user,
			   tu.profile_agent_id AS profile_agent_id,
			   COUNT(*) AS turns,
			   SUM(tu.tokens_in) AS tokens_in,
			   SUM(tu.tokens_out) AS tokens_out,
			   SUM(tu.cache_read) AS cache_read,
			   MAX(cs.profile_n_skills) AS n_skills,
			   MAX(cs.profile_n_tools) AS n_tools
		FROM `tabJarvis Turn Usage` tu
		LEFT JOIN `tabJarvis Chat Session` cs ON cs.session_key = tu.session_key
		WHERE tu.user != '' AND tu.day >= %(start)s AND tu.day < %(next_month)s
		GROUP BY tu.user, tu.profile_agent_id
		""",
		{"start": start, "next_month": next_month},
		as_dict=True,
	):
		profile = _normalize_profile(r.profile_agent_id)
		bucket = merged.setdefault(
			profile,
			{
				"profile": profile,
				"users": 0,
				"turns": 0,
				"tokens_in": 0,
				"tokens_out": 0,
				"cache_read": 0,
				"n_skills": 0,
				"n_tools": 0,
			},
		)
		bucket_users.setdefault(profile, set()).add(r.user)
		bucket["turns"] += int(r.turns or 0)
		bucket["tokens_in"] += int(r.tokens_in or 0)
		bucket["tokens_out"] += int(r.tokens_out or 0)
		bucket["cache_read"] += int(r.cache_read or 0)
		bucket["n_skills"] = max(bucket["n_skills"], int(r.n_skills or 0))
		bucket["n_tools"] = max(bucket["n_tools"], int(r.n_tools or 0))
	for profile, bucket in merged.items():
		bucket["users"] = len(bucket_users[profile])
	return sorted(merged.values(), key=lambda b: b["turns"], reverse=True)


def _tool_message_aggregates(
	start: str, next_month: str
) -> tuple[dict[str, list[dict]], dict[str, int], list[dict]]:
	"""ONE scan of role=tool ``Jarvis Chat Message`` rows this month, deriving
	THREE results from the same grouped result set instead of two separate
	table scans (review finding #5 - the tenant-wide total no longer re-scans
	the table, it is summed from the per-user rows below):

	  * per-user top-``_TOP_TOOLS_USER_CAP`` tool lists (count desc, name asc)
	  * per-user UNCAPPED tool-call totals - deliberately the SAME population
	    ``top_tools`` counts: ALL role=tool messages this month, INCLUDING
	    errored/in-flight turns (review finding #2). This is NOT Turn Usage's
	    per-COMPLETED-turn ``tool_calls`` sum - that field only ever counts
	    turns that reached ``record_turn_usage``, so without this fix a user
	    could show ``tool_calls=0`` with a non-empty ``top_tools`` for the
	    same month. Turn Usage's own per-turn ``tool_calls`` field (seq-bounded
	    to ONE turn, written at record time by ``usage._write_turn_usage_row``)
	    is a different, unrelated field and is untouched by this change.
	  * tenant-wide top-``_TOP_TOOLS_TENANT_CAP`` list, summed across users.

	Tool names come from the STRUCTURED ``tool_name`` field on role=tool rows
	(read at the insert site - ``jarvis.chat.pump._insert_tool_start_row`` -
	not parsed from the content prefix; a distinct field already carries the
	name). Attribution is via each message's conversation OWNER - Jarvis Chat
	Message carries no direct user field, and the brief pins this route. A
	message whose conversation has no resolvable owner (INNER JOIN miss) is
	excluded from all three results, same as a message with no owner was
	already excluded from the per-user results before this change."""
	top_tools_by_user: dict[str, list[dict]] = {}
	tool_calls_by_user: dict[str, int] = {}
	tenant_counts: dict[str, int] = {}
	for r in frappe.db.sql(
		"""
		SELECT c.owner AS user, m.tool_name AS tool, COUNT(*) AS cnt
		FROM `tabJarvis Chat Message` m
		INNER JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.role = 'tool' AND m.tool_name IS NOT NULL AND m.tool_name != ''
		  AND m.creation >= %(start)s AND m.creation < %(next_month)s
		GROUP BY c.owner, m.tool_name
		ORDER BY c.owner, cnt DESC, m.tool_name ASC
		""",
		{"start": start, "next_month": next_month},
		as_dict=True,
	):
		if not r.user:
			continue
		if not _valid_tool_name(r.tool):
			frappe.logger("jarvis.usage_push").warning("dropping invalid tool name %r from rollup", r.tool)
			continue
		count = int(r.cnt or 0)
		tool_calls_by_user[r.user] = tool_calls_by_user.get(r.user, 0) + count
		bucket = top_tools_by_user.setdefault(r.user, [])
		if len(bucket) < _TOP_TOOLS_USER_CAP:
			bucket.append({"tool": r.tool, "count": count})
		tenant_counts[r.tool] = tenant_counts.get(r.tool, 0) + count
	ranked = sorted(tenant_counts.items(), key=lambda kv: (-kv[1], kv[0]))
	top_tools_tenant = [{"tool": tool, "count": count} for tool, count in ranked[:_TOP_TOOLS_TENANT_CAP]]
	return top_tools_by_user, tool_calls_by_user, top_tools_tenant


def _per_model_totals(users: list[str], month: str) -> dict[str, dict[str, dict]]:
	"""Current-month per-model {in, out} totals for every user in ``users``, in
	ONE query (``parent IN (...)``) GROUP BY parent, model - instead of one
	GROUP-BY-model query per user (N+1). Bucketed in Python by parent so each
	user's per_model dict only ever sees its own rows.

	GROUP BY (parent, model) + SUM rather than a plain per-row dict
	assignment: a write-side race in usage._upsert_model_usage (closed by an
	atomic upsert + unique index, but pre-existing duplicates or any future
	anomaly must still be tolerated here) could otherwise leave two child
	rows for the same (user, model, month), and a last-row-wins assignment
	would silently drop one row's tokens from the pushed total."""
	if not users:
		return {}
	out: dict[str, dict[str, dict]] = {u: {} for u in users}
	for r in frappe.db.sql(
		"""
		SELECT parent,
			   model,
			   SUM(month_input_tokens) AS in_,
			   SUM(month_output_tokens) AS out_
		FROM `tabJarvis User Model Usage`
		WHERE parent IN %(users)s AND parenttype = %(ptype)s
		  AND parentfield = %(pfield)s AND month_key = %(month)s
		GROUP BY parent, model
		""",
		{
			"users": tuple(users),
			"ptype": USER_SETTINGS,
			"pfield": MODEL_USAGE_FIELD,
			"month": month,
		},
		as_dict=True,
	):
		if not r.model:
			continue
		out.setdefault(r.parent, {})[r.model] = {
			"in": int(r.in_ or 0),
			"out": int(r.out_ or 0),
		}
	return out


# Contract default for a user with no Jarvis Chat Session data at all: the
# object is always present (never omitted), with every field null/zero per
# the pinned contract ("capacity null when no session has reported one").
_EMPTY_CONTEXT = {
	"used_max": 0,
	"capacity": None,
	"pct_max": None,
	"sessions_over_80": 0,
	"last_seen_at": None,
}


def _iso_utc_z(value) -> str | None:
	"""A stored ``Datetime`` value (naive, SYSTEM timezone - see
	``frappe.utils.now_datetime``, the writer of ``Jarvis Chat
	Session.last_usage_at``) as an ISO-8601 UTC string with a trailing ``Z``,
	per the pinned contract's ``context.last_seen_at``. ``None``/falsy passes
	through as ``None``."""
	if not value:
		return None
	dt = frappe.utils.get_datetime(value)
	aware = dt.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return aware.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context_by_user(users: list[str]) -> dict[str, dict]:
	"""Per-user context-usage summary from ``Jarvis Chat Session`` snapshots
	(the only writers of ``context_capacity`` / ``context_pct`` /
	``last_total_tokens`` are ``usage.record_turn_usage`` and
	``usage.refresh_session_snapshots``).

	``used_max`` is the largest ``last_total_tokens`` across the user's
	sessions; ``capacity`` comes from whichever of the user's sessions most
	recently reported a non-zero ``context_capacity`` (a user's sessions can,
	in principle, run different models with different context windows, so
	"the newest row with one" per the spec, not an arbitrary/first row);
	``pct_max`` is recomputed from ``used_max`` / that ``capacity`` rather than
	trusting any single session's stored ``context_pct``, since the session
	that had the highest usage need not be the same one capacity was read
	from. Returns ``{}`` for an empty ``users`` list; callers fall back to
	``_EMPTY_CONTEXT`` per user."""
	if not users:
		return {}
	summary = {
		r.user: r
		for r in frappe.db.sql(
			"""
			SELECT user,
				   MAX(last_total_tokens) AS used_max,
				   SUM(CASE WHEN context_pct >= %(over_pct)s THEN 1 ELSE 0 END) AS sessions_over_80,
				   MAX(last_usage_at) AS last_seen_at
			FROM `tabJarvis Chat Session`
			WHERE user IN %(users)s
			GROUP BY user
			""",
			{"users": tuple(users), "over_pct": _CONTEXT_OVER_PCT},
			as_dict=True,
		)
	}
	# COALESCE(last_usage_at, modified): refresh_session_snapshots only stamps
	# last_usage_at when the gateway row carried updatedAt (see its docstring);
	# a session refreshed without one still has a real context_capacity, and
	# without the fallback it would silently lose the newest-row-wins ordering
	# (NULL never wins a MAX/join match against a real timestamp).
	capacity_by_user: dict[str, int] = {}
	for r in frappe.db.sql(
		"""
		SELECT t1.user AS user, t1.context_capacity AS context_capacity
		FROM `tabJarvis Chat Session` t1
		INNER JOIN (
			SELECT user, MAX(COALESCE(last_usage_at, modified)) AS max_at
			FROM `tabJarvis Chat Session`
			WHERE user IN %(users)s AND context_capacity > 0
			GROUP BY user
		) newest ON newest.user = t1.user AND newest.max_at = COALESCE(t1.last_usage_at, t1.modified)
		WHERE t1.context_capacity > 0
		""",
		{"users": tuple(users)},
		as_dict=True,
	):
		# A tie on last_usage_at (rare) just keeps whichever row SQL returns
		# last; both would carry the same model's capacity in practice.
		capacity_by_user[r.user] = int(r.context_capacity or 0)
	out: dict[str, dict] = {}
	for user in users:
		row = summary.get(user)
		used_max = int(row.used_max or 0) if row else 0
		capacity = capacity_by_user.get(user)
		out[user] = {
			"used_max": used_max,
			"capacity": capacity,
			"pct_max": round(100 * used_max / capacity, 1) if capacity else None,
			"sessions_over_80": int(row.sessions_over_80 or 0) if row else 0,
			"last_seen_at": _iso_utc_z(row.last_seen_at) if row else None,
		}
	return out


def _users_daily_rollup() -> list[dict]:
	"""Per-(user, day, model) breakdown from ``Jarvis Turn Usage`` for the last
	``_USERS_DAILY_WINDOW_DAYS`` days INCLUDING today (site-local day, matching
	the ``day`` column - the pinned contract labels this "the bench day").

	Only days with at least one turn are emitted (a plain GROUP BY over an
	append-only table naturally skips empty days). One row per (email, day);
	``per_model`` capped at ``_USERS_DAILY_PER_MODEL_CAP`` entries, highest
	token volume first (the SQL ORDER BY ensures the first models folded into
	each bucket below are the biggest ones) - a day's ``turns`` /
	``tokens_in`` / ``tokens_out`` totals still cover EVERY model that day,
	even one that overflowed the per_model cap; only the per-model breakdown
	is trimmed. Total row count is capped at ``_USERS_DAILY_ROW_CAP``,
	trimming the LOWEST-token rows first and logging once (spec: bench trims,
	admin 400s if it ever still sees more)."""
	today = frappe.utils.today()
	start = frappe.utils.add_days(today, -(_USERS_DAILY_WINDOW_DAYS - 1))
	grouped: dict[tuple[str, str], dict] = {}
	for r in frappe.db.sql(
		f"""
		SELECT user, day, model,
			   COUNT(*) AS turns,
			   SUM(tokens_in) AS tokens_in,
			   SUM(tokens_out) AS tokens_out
		FROM `tab{TURN_USAGE}`
		WHERE user != '' AND day >= %(start)s AND day <= %(today)s
		GROUP BY user, day, model
		ORDER BY user, day, (SUM(tokens_in) + SUM(tokens_out)) DESC
		""",
		{"start": start, "today": today},
		as_dict=True,
	):
		email = (r.user or "").strip().lower()
		if not email:
			continue
		key = (email, str(r.day))
		bucket = grouped.setdefault(
			key,
			{"email": email, "day": str(r.day), "turns": 0, "tokens_in": 0, "tokens_out": 0, "per_model": {}},
		)
		tokens_in = int(r.tokens_in or 0)
		tokens_out = int(r.tokens_out or 0)
		bucket["turns"] += int(r.turns or 0)
		bucket["tokens_in"] += tokens_in
		bucket["tokens_out"] += tokens_out
		model = (r.model or "").strip()
		if model and len(bucket["per_model"]) < _USERS_DAILY_PER_MODEL_CAP:
			bucket["per_model"][model] = {"in": tokens_in, "out": tokens_out}
	return _trim_users_daily_rows(list(grouped.values()))


def _trim_users_daily_rows(rows: list[dict], cap: int = _USERS_DAILY_ROW_CAP) -> list[dict]:
	"""Enforce the pinned contract's ``users_daily`` row cap: at most ``cap``
	rows, dropping the LOWEST-token rows first and logging once when trimming
	actually occurs (spec: bench trims and warns; admin 400s if it ever still
	sees more than the cap). Split out as its own pure function - unlike the
	grouping above, this is a plain list transform, so cap behaviour is
	unit-testable directly without seeding thousands of Turn Usage rows
	against a live bench."""
	if len(rows) <= cap:
		return rows
	rows = sorted(rows, key=lambda row: row["tokens_in"] + row["tokens_out"])
	dropped = len(rows) - cap
	kept = rows[dropped:]
	frappe.logger("jarvis.usage_push").warning(
		"users_daily rollup trimmed %s lowest-token rows (cap %s)", dropped, cap
	)
	return kept


def push_usage_rollup() -> None:
	"""Daily scheduler entry. Self-gating + best-effort; NEVER raises."""
	try:
		if not _admin_configured():
			return
		rollup, truncated = _build_rollup()
		if truncated:
			frappe.logger("jarvis.usage_push").warning("usage rollup truncated at %s users", _MAX_USERS)
		if not rollup["users"]:
			return
		from jarvis import admin_client

		admin_client.push_usage_rollup(rollup)
	except AdminAuthError:
		# Not onboarded / no admin credentials. Nothing to push; not an error
		# condition, so don't log_error.
		return
	except Exception:
		frappe.log_error(
			title="jarvis usage: rollup push failed",
			message=frappe.get_traceback(),
		)
