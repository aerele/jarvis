"""Admin "User activity" read model: the events list and the per-person summary.

Shared by the Script Report and the whitelisted API (plan:
``.claude/workflow/plans/2026-11-06-user-activity-report.md`` 4.5). Two sources are
unified into one stream:

- ``Jarvis Activity Event`` (reads, exports, direct app actions, refused calls);
- ``Jarvis Agent Write`` (every agent write, with its outcome).

Rules this module owns:

- **Metadata only.** Every query names an explicit field allowlist (never ``*``);
  the doctypes have no content columns and the output dicts are built key by key.
- **Exports are counted once.** An export tool is also a write tool, so the tool path
  writes an Agent Write row AND an ``export`` event. An ``applied`` export Agent Write
  row is therefore hidden from the events list and excluded from "writes" (the
  ``export`` event represents it). A refused / failed export has no event, so its
  Agent Write row stays visible and counts toward refused / failed.
- **Times are site-local, naive datetimes** (as stored). "Day" is the site calendar
  day of ``at`` and the off-hours window is read off the stored clock time, so no
  timezone conversion happens anywhere here. A date range is inclusive of both
  days: ``from 00:00:00`` up to, not including, ``to + 1 day 00:00:00``.
- **The summary reads pre-aggregated days.** ``Jarvis Activity Daily`` holds one row per
  (person, day), written hourly by ``jarvis.activity_rollup`` through ``day_counters``,
  the SAME function the live path uses for days the rollup has not reached and for today's
  tail after the last full hour. See ``summary`` and the plan's rev 4 / rev 4.3 sections
  (D13, D14).
- **Every entry function (``events``, ``summary``, ``first_event_at``) calls
  ``require_jarvis_admin()`` first.** The helpers below them are internal and do not.
"""

import datetime as _dt
from collections import defaultdict
from typing import NamedTuple

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Max, Min, Sum
from frappe.utils import add_to_date, cint, get_datetime, getdate, now_datetime
from frappe.utils.scheduler import is_scheduler_disabled, is_scheduler_inactive
from pypika.enums import DatePart
from pypika.functions import Date as _SqlDate
from pypika.functions import Extract
from pypika.terms import Case

from jarvis import activity_actions
from jarvis.activity_log import EVENTS as _EVENTS
from jarvis.activity_log import EXPORT_TOOLS
from jarvis.activity_log import OUTCOMES as _EVENT_OUTCOMES
from jarvis.agent_audit import OUTCOMES as _WRITE_OUTCOMES
from jarvis.permissions import require_jarvis_admin

ACTIVITY_EVENT = "Jarvis Activity Event"
AGENT_WRITE = "Jarvis Agent Write"
TURN_USAGE = "Jarvis Turn Usage"
DAILY = "Jarvis Activity Daily"
ROLLUP = "Jarvis Activity Rollup"

# ---- Flag thresholds (E3, owner-confirmed). Module constants, shown in the page legend.
DELETE_BURST_COUNT = 50  # ERP deletes by one person ...
DELETE_BURST_MINUTES = 60  # ... inside any rolling window this long
BIG_EXPORT_ROWS = 50_000  # one export of at least this many rows
DAILY_EXPORT_ROWS = 100_000  # rows exported by one person in one site-calendar day
REFUSALS_PER_DAY = 5  # refused / denied calls by one person in one site-calendar day
BUSINESS_HOURS_START = 8  # events with a clock hour < 8 or >= 20 are "off hours"
BUSINESS_HOURS_END = 20

THRESHOLDS = {
	"delete_burst_count": DELETE_BURST_COUNT,
	"delete_burst_minutes": DELETE_BURST_MINUTES,
	"big_export_rows": BIG_EXPORT_ROWS,
	"daily_export_rows": DAILY_EXPORT_ROWS,
	"refusals_per_day": REFUSALS_PER_DAY,
	"business_hours_start": BUSINESS_HOURS_START,
	"business_hours_end": BUSINESS_HOURS_END,
}

# The summary lists at most this many people (flagged first, then busiest): a site with
# thousands of users must not ship, and render, every one of them. ``people_truncated`` says so.
PEOPLE_CAP = 500

# ---- Paging limits
DEFAULT_LIMIT = 200
MAX_LIMIT = 5_000
EXPORT_MAX_LIMIT = 50_000

# Activity Event ``event`` values that come from the tool path (the agent), vs the
# app (``action``). Drives the per-source "recorded from" dates.
_AGENT_EVENTS = tuple(sorted(_EVENTS - {"action"}))

# Activity shown for an Agent Write row: the verb of its tool. Explicit on purpose;
# anything unmapped (a new write tool, the redacted delegate label) reads "write".
_FALLBACK_VERB = "write"
_TOOL_VERBS = {
	"create_doc": "create",
	"update_doc": "update",
	"submit_doc": "submit",
	"cancel_doc": "cancel",
	"amend_doc": "amend",
	"delete_doc": "delete",
	"run_method": "run",
	"run_import": "import",
	"apply_workflow_action": "workflow",
	"send_email": "email",
	"add_comment": "comment",
	"update_comment": "comment",
	"share_doc": "share",
	"unshare_doc": "unshare",
	"assign_to": "assign",
	"unassign_from": "unassign",
	"add_tag": "tag",
	"remove_tag": "untag",
	"attach_to_doc": "attach",
	**dict.fromkeys(EXPORT_TOOLS, "export"),
}
_DELETE_TOOLS = tuple(t for t, v in _TOOL_VERBS.items() if v == "delete")
_VERBS = frozenset(_TOOL_VERBS.values()) | {_FALLBACK_VERB}

# Agent Write ``provenance`` -> channel label, where it names a surface. Anything not
# listed (request / approval / reviewer_approved / auto_mode) has no honest channel.
_PROVENANCE_CHANNEL = {"chat": "chat", "auto_apply": "chat", "macro": "macro", "skill": "agent_run"}

# The ONLY keys that leave this module (a test pins them: no content-like key can
# reach the report, the API or an export).
EVENT_ROW_KEYS = (
	"at",
	"actor",
	"actor_name",
	"activity",
	"method",
	"label",
	"ref_doctype",
	"ref_name",
	"rows",
	"outcome",
	"channel",
	"code",
	"source",
)
PERSON_KEYS = (
	"user",
	"name",
	"turns",
	"tokens",
	"reads",
	"rows_read",
	"exports",
	"rows_exported",
	"writes",
	"erp_deletes",
	"refused_denied",
	"failed",
	"actions",
	"flags",
)
FLAG_KEYS = ("key", "label", "value")

_EVENT_FIELDS = (
	"name",
	"at",
	"actor",
	"actor_name",
	"event",
	"method",
	"label",
	"ref_doctype",
	"ref_name",
	"row_count",
	"outcome",
	"channel",
	"code",
)

# A subset of ``agent_audit.AGENT_WRITE_FIELDS`` (a test pins that): no
# ``provenance_name``, which is a free label this view has no use for.
_WRITE_FIELDS = (
	"name",
	"at",
	"actor",
	"actor_name",
	"tool",
	"outcome",
	"provenance",
	"ref_doctype",
	"ref_name",
	"bulk_count",
)


# --------------------------------------------------------------------------- helpers


def _day_bounds(filters):
	"""``(start, end_exclusive)`` datetimes for the inclusive from/to dates; either
	side ``None`` when absent. Dates are coerced with ``getdate`` (defence in depth:
	the query builder parameterizes too) and clamped to the kept window: ``from_date``
	no earlier than the horizon, ``to_date`` no later than today. The upper clamp is also
	what keeps ``9999-12-31 + 1 day`` from overflowing ``datetime``."""
	start = end = None
	now_day = today()
	if filters.get("from_date"):
		start = _dt.datetime.combine(max(getdate(filters["from_date"]), horizon_day(now_day)), _dt.time.min)
	if filters.get("to_date"):
		last = min(getdate(filters["to_date"]), now_day)
		end = _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time.min)
	return start, end


def _text(value):
	return str(value).strip() if value else ""


def _clamp(limit, cap):
	try:
		limit = int(limit)
	except (TypeError, ValueError):
		limit = DEFAULT_LIMIT
	return max(1, min(limit, cap))


def _encode_cursor(at, name):
	return f"{get_datetime(at).isoformat(sep=' ')}|{name}"


def _decode_cursor(cursor):
	if not cursor:
		return None
	try:
		at, name = str(cursor).rsplit("|", 1)
		# ``get_datetime`` turns the zero dates ("0000-00-00 ...") into ``None``: a cursor
		# that would compare ``at < NULL`` and silently return nothing.
		when = get_datetime(at) if at.strip() else None
		if when is None or not name:
			raise ValueError("empty cursor part")
		return when, name
	except Exception:
		frappe.throw(_("Invalid cursor."), frappe.ValidationError)


def _humanise(tool):
	# The tool-path label. The twin for direct app actions is ``activity_actions.label_for``
	# (humanised function name + LABEL_OVERRIDES): change how one reads and check the other.
	return (tool or "").replace("_", " ").strip().capitalize()


def _verb(tool):
	return _TOOL_VERBS.get(tool, _FALLBACK_VERB)


def _is_hidden_export(tool, outcome):
	"""An ``applied`` export Agent Write row: represented by the ``export`` event."""
	return tool in EXPORT_TOOLS and outcome == "applied"


def _write_visible(tbl):
	"""SQL form of "not a hidden export row"."""
	return tbl.tool.notin(list(EXPORT_TOOLS)) | (tbl.outcome != "applied")


def _event_row(r):
	return {
		"at": r.at,
		"actor": r.actor or "",
		"actor_name": r.actor_name or "",
		"activity": r.event,
		"method": r.method or "",
		"label": r.label or "",
		"ref_doctype": r.ref_doctype or "",
		"ref_name": r.ref_name or "",
		"rows": cint(r.row_count),
		"outcome": r.outcome or "",
		"channel": r.channel or "",
		"code": r.code or "",
		"source": "event",
	}


def _write_row(r):
	return {
		"at": r.at,
		"actor": r.actor or "",
		"actor_name": r.actor_name or "",
		"activity": _verb(r.tool),
		"method": r.tool or "",
		"label": _humanise(r.tool),
		"ref_doctype": r.ref_doctype or "",
		"ref_name": r.ref_name or "",
		"rows": cint(r.bulk_count) or 1,
		"outcome": r.outcome or "",
		"channel": _PROVENANCE_CHANNEL.get(r.provenance, ""),
		"code": "",
		"source": "write",
	}


def _after_cursor(tbl, cursor):
	if not cursor:
		return None
	c_at, c_name = cursor
	return (tbl.at < c_at) | ((tbl.at == c_at) & (tbl.name < c_name))


def _fetch_events(filters, cursor, n):
	activity = _text(filters.get("activity"))
	outcome = _text(filters.get("outcome"))
	if activity and activity not in _EVENTS:
		return []  # a write verb (or junk): not an Activity Event
	if outcome and outcome not in _EVENT_OUTCOMES:
		return []
	t = frappe.qb.DocType(ACTIVITY_EVENT)
	q = frappe.qb.from_(t).select(*[getattr(t, f) for f in _EVENT_FIELDS])
	start, end = _day_bounds(filters)
	if start:
		q = q.where(t.at >= start)
	if end:
		q = q.where(t.at < end)
	if _text(filters.get("user")):
		q = q.where(t.actor == _text(filters["user"]))
	if activity:
		q = q.where(t.event == activity)
	if outcome:
		q = q.where(t.outcome == outcome)
	crit = _after_cursor(t, cursor)
	if crit is not None:
		q = q.where(crit)
	return (
		q.orderby(t.at, order=frappe.qb.desc).orderby(t.name, order=frappe.qb.desc).limit(n).run(as_dict=True)
	)


def _fetch_writes(filters, cursor, n):
	activity = _text(filters.get("activity"))
	outcome = _text(filters.get("outcome"))
	if activity and activity not in _VERBS:
		return []  # an Activity Event kind (or junk)
	if outcome and outcome not in _WRITE_OUTCOMES:
		return []
	t = frappe.qb.DocType(AGENT_WRITE)
	q = frappe.qb.from_(t).select(*[getattr(t, f) for f in _WRITE_FIELDS])
	q = q.where(_write_visible(t))
	start, end = _day_bounds(filters)
	if start:
		q = q.where(t.at >= start)
	if end:
		q = q.where(t.at < end)
	if _text(filters.get("user")):
		q = q.where(t.actor == _text(filters["user"]))
	if outcome:
		q = q.where(t.outcome == outcome)
	if activity:
		if activity == _FALLBACK_VERB:
			q = q.where(t.tool.notin(list(_TOOL_VERBS)))
		else:
			q = q.where(t.tool.isin([k for k, v in _TOOL_VERBS.items() if v == activity]))
	crit = _after_cursor(t, cursor)
	if crit is not None:
		q = q.where(crit)
	return (
		q.orderby(t.at, order=frappe.qb.desc).orderby(t.name, order=frappe.qb.desc).limit(n).run(as_dict=True)
	)


# --------------------------------------------------------------------------- events


def events(filters=None, after=None, limit=DEFAULT_LIMIT, *, for_export=False):
	"""One page of the unified activity stream, newest first.

	``filters``: ``from_date`` / ``to_date`` (inclusive days), ``user``, ``activity``
	(an event kind or a write verb), ``outcome`` (as stored in the source).

	Keyset paging on ``(at, name)`` descending: ``after`` is the ``next_cursor`` of the
	previous page. Each source is asked for ``limit + 1`` rows strictly after the
	cursor and the merged stream is cut at ``limit``, so there are no duplicates or
	gaps even when many rows share one ``at``, and ``next_cursor`` is ``None`` exactly
	when nothing is left. (``name`` is a random hash; a collision between the two
	tables would need identical 10-character names at an identical ``at``.)

	``limit`` is clamped to 1..5,000. ``for_export=True`` (the report's Excel path)
	raises the ceiling to 50,000 and adds ``truncated`` (more rows existed)."""
	require_jarvis_admin()
	filters = filters or {}
	limit = _clamp(limit, EXPORT_MAX_LIMIT if for_export else MAX_LIMIT)
	cursor = _decode_cursor(after)
	start, end = _day_bounds(filters)
	if start and end and start >= end:  # the range lies wholly outside what is kept
		return {"rows": [], "next_cursor": None, **({"truncated": False} if for_export else {})}

	merged = [("event", r) for r in _fetch_events(filters, cursor, limit + 1)]
	merged += [("write", r) for r in _fetch_writes(filters, cursor, limit + 1)]
	merged.sort(key=lambda sr: (get_datetime(sr[1].at), sr[1].name), reverse=True)

	has_more = len(merged) > limit
	page = merged[:limit]
	rows = [(_event_row if s == "event" else _write_row)(r) for s, r in page]
	next_cursor = _encode_cursor(page[-1][1].at, page[-1][1].name) if has_more and page else None
	out = {"rows": rows, "next_cursor": next_cursor}
	if for_export:
		out["truncated"] = has_more
	return out


# --------------------------------------------------------------------------- notes

# The plain-English lines shown above the data: what the view covers and why its totals
# may be incomplete. ONE builder for the Desk report (which joins them into its message)
# and the in-app page (which renders them as a list), so the two can never say different
# things about the same result. Each is a whole translatable sentence, never fragments.


def _day(value):
	return str(getdate(value)) if value else ""


def scope_line(first_event_at):
	"""What this view covers, and since when. Desk is out of scope on purpose, and
	the two sources started recording on different days (the app-action hook shipped
	after the tool-path one), so each states its own start. One whole template per case:
	a translator needs the full sentence, not fragments to join."""
	agent = _day((first_event_at or {}).get("agent"))
	app = _day((first_event_at or {}).get("app"))
	if agent and app:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"Agent activity recorded from {0}; app actions from {1}."
		).format(agent, app)
	if agent:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"Agent activity recorded from {0}. App actions are not recorded yet."
		).format(agent)
	if app:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"No agent activity recorded yet; app actions from {0}."
		).format(app)
	return _(
		"Covers actions taken in Jarvis only (not Desk). "
		"No agent activity recorded yet. App actions are not recorded yet."
	)


def kept_note():
	return _("Data is kept for {0} days.").format(HORIZON_DAYS)


def stale_note(n):
	if n == 1:
		return _("Totals for 1 day are still being prepared.")
	return _("Totals for {0} days are still being prepared.").format(n)


def partial_note(n):
	"""Days the summary left out because the daily totals were too far behind to compute
	them on request (``LIVE_DAY_CAP``)."""
	if n == 1:
		return _("Totals for the oldest day in this range are not included yet.")
	return _("Totals for the {0} oldest days in this range are not included yet.").format(n)


def people_truncated_note():
	return _(
		"Showing the {0} people with red flags or the most activity. Narrow the dates or pick a person to see others."
	).format(PEOPLE_CAP)


def app_warn_note():
	return _(
		"Jarvis app actions are not being recorded. Ask your system administrator to check the activity log."
	)


def preparing_note():
	return _("Activity totals are still being prepared. Please try again in a few minutes.")


def health_notes(health):
	"""Plain-English warnings from the logging-health block, in a fixed order."""
	notes = []
	if health.get("warn"):
		notes.append(
			_(
				"Warning: agents ran tools in the last 24 hours but no activity was recorded, "
				"so logging may be broken."
			)
		)
	if health.get("app_warn"):
		# Its own sentence and its own check: the agent warning above is about the tool path.
		notes.append(app_warn_note())
	if health.get("scheduler_disabled"):
		notes.append(_("Background jobs are paused on this site, so daily totals are not being updated."))
	lag = health.get("rollup_lag_days") or 0
	if lag > 1:
		notes.append(
			_(
				"Daily totals are {0} days behind. Ask your system administrator to check the scheduler."
			).format(lag)
		)
	return notes


def before_horizon(from_date):
	"""``events`` does not clamp; say so when the requested start predates what is kept."""
	return bool(from_date) and getdate(from_date) < horizon_day()


def summary_notes(res):
	"""The notes for a ``summary()`` result, in display order."""
	if res.get("preparing"):
		return [preparing_note()]
	notes = [scope_line(res.get("first_event_at"))]
	if res.get("clamped"):
		notes.append(kept_note())
	stale = res.get("stale_days") or 0
	if stale > 0:
		notes.append(stale_note(stale))
	if res.get("partial") and res.get("excluded_days"):
		notes.append(partial_note(res["excluded_days"]))
	if res.get("people_truncated"):
		notes.append(people_truncated_note())
	notes += health_notes(res.get("logging_health") or {})
	return notes


def events_notes(first_event_at, from_date=None):
	"""The notes for an events view: the scope line (``first_event_at()``) and, when the
	range starts before the kept window, that it is kept for 90 days."""
	notes = [scope_line(first_event_at)]
	if before_horizon(from_date):
		notes.append(kept_note())
	return notes


# --------------------------------------------------------------------------- counters

# Retention is 90 days, so the report reaches back to ``today - 89`` (the horizon) and
# nothing before it is rolled up, read live or bannered (plan rev 4, RV4-1). A test pins
# this to the two ``default_log_clearing_doctypes`` values in hooks.py.
HORIZON_DAYS = 90

# A request computes at most this many unrolled CLOSED days live (one grouped pass over
# the raw tables each, about 2 s at 100 users x 600 events a day). Older unrolled days are
# left out of the totals and the result says so (``partial``): with the rollup off for
# weeks, an uncapped request would scan millions of rows on the web worker.
LIVE_DAY_CAP = 7

# The per-(person, day) counters. ONE definition of each, computed by ``day_counters``
# and used by BOTH the live summary and ``jarvis.activity_rollup`` (which stores them in
# ``Jarvis Activity Daily`` under these very names), so a rolled day and a live day can
# never count differently.
COUNTER_KEYS = (
	"turns",
	"tokens",
	"reads",
	"rows_read",
	"exports",
	"rows_exported",
	"max_export_rows",
	"actions",
	"denied",
	"failed",
	"writes",
	"refused",
	"erp_deletes",
	"off_hours",
)


def now():
	"""The site clock. The one clock the summary and the rollup share (a test drives it)."""
	return now_datetime()


def today():
	"""The site calendar day."""
	return now().date()


def horizon_day(on=None):
	return (on or today()) - _dt.timedelta(days=HORIZON_DAYS - 1)


def _read_watermark_raw(for_update=False):
	"""The watermark's ``tabSingles`` row(s), the ONE place that reads it. Raw, not
	``get_single_value``: that casts the seeded empty value to 0001-01-01, and v15's
	``Database.get_single_value`` has no ``for_update``. The rollup passes
	``for_update=True`` to row-lock it for its transaction."""
	sql = "select `value` from `tabSingles` where `doctype` = %s and `field` = %s"
	return frappe.db.sql(sql + (" for update" if for_update else ""), (ROLLUP, "rolled_through"))


def watermark():
	"""The last day rolled into ``Jarvis Activity Daily``, or ``None`` when nothing was.
	The summary may read it cached: a stale value is always an OLDER day, so it only
	makes the summary compute more days live, never fewer."""
	rows = _read_watermark_raw()
	return getdate(rows[0][0]) if rows and rows[0][0] else None


def event_snapshots(actors, start, end):
	"""``actor -> actor_name`` as stamped on their Activity Event rows in the window
	(kept for deleted users, D12)."""
	ev = frappe.qb.DocType(ACTIVITY_EVENT)
	q = (
		frappe.qb.from_(ev)
		.select(ev.actor, Max(ev.actor_name).as_("actor_name"))
		.where(ev.actor.isin(actors))
	)
	found = {}
	for r in _ranged(q, ev, ev.at, start, end, None).groupby(ev.actor).run(as_dict=True):
		if r.actor_name:
			found[r.actor] = r.actor_name
	return found


def day_counters(first_day, last_day, user=None, *, since=None, until=None):
	"""Per-person, per-day counters for the inclusive days ``first_day`` .. ``last_day``
	(site calendar days), computed from the three source tables with ONE grouped query
	each. ``since`` / ``until`` (datetimes) narrow the window to ``[since, until)`` inside
	those days, for today's partial roll and the summary's live tail (plan rev 4.3, D14):
	Activity Event and Agent Write are cut by ``at``; Turn Usage by ``creation`` (it is
	stamped together with ``day`` at insert, chat/usage.py), within ``day`` in the range.
	Either bound alone is allowed, and the two halves of a split window add up to the
	whole (``[00:00, H)`` + ``[H, end)`` == the day).

	Returns ``({(actor, day): {counter: n}}, {actor: name snapshot from Agent Write})``;
	``actor`` is ``""`` for the No-person bucket. Used by the live summary and by the
	rollup, so they cannot disagree.

	Counting rules (the summary's, unchanged): ``reads`` / ``exports`` / ``actions`` and
	``rows_*`` count ``ok`` Activity Event rows; ``denied`` / ``failed`` count that outcome
	on Activity Events; ``refused`` is Agent Write ``refused``, ``failed`` also counts Agent
	Write ``failed``, ``writes`` is every Agent Write row except an applied export, and
	``erp_deletes`` sums ``bulk_count`` (1 for a single) of applied ``delete_doc`` rows.
	``off_hours`` counts every Activity Event and every visible Agent Write row whose
	clock hour is outside the business hours. ``turns`` / ``tokens`` come from Turn Usage by
	its ``day``."""
	start = since or _dt.datetime.combine(first_day, _dt.time.min)
	end = until or _dt.datetime.combine(last_day + _dt.timedelta(days=1), _dt.time.min)
	out: dict[tuple, dict] = defaultdict(lambda: dict.fromkeys(COUNTER_KEYS, 0))
	snapshots: dict[str, str] = {}

	# -- turns and tokens (Turn Usage, one grouped query)
	tu = frappe.qb.DocType(TURN_USAGE)
	q = (
		frappe.qb.from_(tu)
		.select(
			tu.user, tu.day, Count("*").as_("n"), Sum(tu.tokens_in).as_("tin"), Sum(tu.tokens_out).as_("tout")
		)
		.where((tu.day >= first_day) & (tu.day <= last_day))
	)
	if since:
		q = q.where(tu.creation >= since)
	if until:
		q = q.where(tu.creation < until)
	if user:
		q = q.where(tu.user == user)
	for r in q.groupby(tu.user, tu.day).run(as_dict=True):
		c = out[(r.user or "", getdate(r.day))]
		c["turns"] += cint(r.n)
		c["tokens"] += cint(r.tin) + cint(r.tout)

	# -- Activity Event: ONE pass over the window, grouped by (person, site day) with
	# conditional sums. The grouping key is kept narrow on purpose: grouping by event
	# and outcome as well multiplied the groups 5x and (with the wide varchar keys)
	# spilled MariaDB's in-memory temp table to disk, 4x slower.
	ev = frappe.qb.DocType(ACTIVITY_EVENT)

	def count_if(cond):
		return Sum(Case().when(cond, 1).else_(0))

	def rows_if(cond):
		return Sum(Case().when(cond, ev.row_count).else_(0))

	hour = Extract(DatePart.hour, ev.at)
	is_ok = ev.outcome == "ok"
	is_read, is_export, is_action = (is_ok & (ev.event == e) for e in ("read", "export", "action"))
	day = _SqlDate(ev.at)
	q = frappe.qb.from_(ev).select(
		ev.actor,
		day.as_("d"),
		count_if((hour < BUSINESS_HOURS_START) | (hour >= BUSINESS_HOURS_END)).as_("off"),
		count_if(is_read).as_("reads"),
		rows_if(is_read).as_("rows_read"),
		count_if(is_export).as_("exports"),
		rows_if(is_export).as_("rows_exported"),
		Max(Case().when(is_export, ev.row_count).else_(0)).as_("biggest"),
		count_if(is_action).as_("actions"),
		count_if(ev.outcome == "denied").as_("denied"),
		count_if(ev.outcome == "failed").as_("failed"),
	)
	q = _ranged(q, ev, ev.at, start, end, user)
	for r in q.groupby(ev.actor, day).run(as_dict=True):
		c = out[(r.actor or "", getdate(r.d))]
		c["off_hours"] += cint(r.off)
		c["reads"] += cint(r.reads)
		c["rows_read"] += cint(r.rows_read)
		c["exports"] += cint(r.exports)
		c["rows_exported"] += cint(r.rows_exported)
		c["max_export_rows"] = max(c["max_export_rows"], cint(r.biggest))
		c["actions"] += cint(r.actions)
		c["denied"] += cint(r.denied)
		c["failed"] += cint(r.failed)

	# -- Agent Write, grouped by person / day / tool / outcome (applied exports excluded)
	aw = frappe.qb.DocType(AGENT_WRITE)
	bulk = Case().when(aw.bulk_count > 0, aw.bulk_count).else_(1)
	wday = _SqlDate(aw.at)
	whour = Extract(DatePart.hour, aw.at)
	q = frappe.qb.from_(aw).select(
		aw.actor,
		wday.as_("d"),
		aw.tool,
		aw.outcome,
		Count("*").as_("n"),
		Sum(bulk).as_("units"),
		Sum(Case().when((whour < BUSINESS_HOURS_START) | (whour >= BUSINESS_HOURS_END), 1).else_(0)).as_(
			"off"
		),
		Max(aw.actor_name).as_("actor_name"),
	)
	q = _ranged(q, aw, aw.at, start, end, user).where(_write_visible(aw))
	for r in q.groupby(aw.actor, wday, aw.tool, aw.outcome).run(as_dict=True):
		c = out[(r.actor or "", getdate(r.d))]
		if r.actor_name and r.actor:
			snapshots.setdefault(r.actor, r.actor_name)
		c["writes"] += cint(r.n)
		c["off_hours"] += cint(r.off)
		if r.outcome == "refused":
			c["refused"] += cint(r.n)
		elif r.outcome == "failed":
			c["failed"] += cint(r.n)
		elif r.outcome == "applied" and r.tool in _DELETE_TOOLS:
			c["erp_deletes"] += cint(r.units)

	return out, snapshots


def _daily_rows(first_day, last_day, user=None):
	"""Rolled days from ``Jarvis Activity Daily``: ``(counters, snapshots, throughs)``,
	the first two in the shapes ``day_counters`` returns so the two fold identically.
	``throughs`` is the set of ``through`` stamps found (``None`` for a row without one),
	read in the SAME query as the counters so the two can never describe different rolls."""
	d = frappe.qb.DocType(DAILY)
	q = (
		frappe.qb.from_(d)
		.select(d.actor, d.day, d.actor_name, d.through, *[getattr(d, k) for k in COUNTER_KEYS])
		.where((d.day >= first_day) & (d.day <= last_day))
	)
	if user:
		q = q.where(d.actor == user)
	out: dict[tuple, dict] = {}
	snapshots: dict[str, str] = {}
	throughs: set = set()
	for r in q.run(as_dict=True):
		actor = r.actor or ""
		out[(actor, getdate(r.day))] = {k: cint(r[k]) for k in COUNTER_KEYS}
		throughs.add(get_datetime(r.through) if r.through else None)
		if actor and r.actor_name:
			snapshots.setdefault(actor, r.actor_name)
	return out, snapshots, throughs


def _add_counters(into, other):
	"""Fold one day's counters into another: every counter SUMS except
	``max_export_rows``, which is a maximum. Never a dict update: the partial roll and
	the live tail describe the same (person, day) and both halves count."""
	for k in COUNTER_KEYS:
		into[k] = max(into[k], other[k]) if k == "max_export_rows" else into[k] + other[k]


def _today_counters(now_day, user):
	"""``(counters, snapshots)`` for today: its rolled partial rows (``through`` = H, the
	last full hour at the last roll) summed with a live pass over everything after H.
	With no rows (the job is behind, or nobody was active before H) the live pass covers
	all of today. The live start is the rows' OWN ``through``, never a separate read of
	the rollup state: a value older or newer than the rows double-counts or drops an hour
	(plan rev 4.3, D14). Rows that disagree on ``through`` (or lack it) cannot be
	stitched exactly, so they are ignored and today is computed whole."""
	midnight = _dt.datetime.combine(now_day, _dt.time.min)
	# All of today's rows, not the filtered user's: the stamp must be found even for a
	# person with no rows (about one row per person; the user filter is applied below).
	rows, snaps, throughs = _daily_rows(now_day, now_day)
	through = next(iter(throughs)) if len(throughs) == 1 else None
	if through is None or not midnight < through <= midnight + _dt.timedelta(days=1):
		rows, snaps, through = {}, {}, None
	if user:
		rows = {k: v for k, v in rows.items() if k[0] == user}
	live, live_snaps = day_counters(now_day, now_day, user, since=through)
	for key, c in live.items():
		if key in rows:
			_add_counters(rows[key], c)
		else:
			rows[key] = c
	for actor, name in live_snaps.items():
		snaps.setdefault(actor, name)
	return rows, snaps


# --------------------------------------------------------------------------- summary


def _blank_person(user):
	person = dict.fromkeys(PERSON_KEYS, 0)  # the counters; the three below are not numbers
	person.update(user=user, name=_("No person") if not user else "", flags=[])
	return person


def _ranged(q, tbl, col, start, end, user):
	if start:
		q = q.where(col >= start)
	if end:
		q = q.where(col < end)
	if user:
		q = q.where(tbl.actor == user)
	return q


def _max_delete_window(rows, start, end):
	"""Largest ``bulk_count`` sum inside any rolling ``DELETE_BURST_MINUTES`` window
	whose last delete falls in ``[start, end)``. ``rows`` are ``(at, bulk)`` sorted
	ascending and may reach ``DELETE_BURST_MINUTES`` before ``start`` (a burst that
	straddles the range start still counts its early rows)."""
	span = _dt.timedelta(minutes=DELETE_BURST_MINUTES)
	best = lo = running = 0
	for at, bulk in rows:
		running += bulk
		while at - rows[lo][0] > span:
			running -= rows[lo][1]
			lo += 1
		if (start is None or at >= start) and (end is None or at < end):
			best = max(best, running)
	return best


class _Plan(NamedTuple):
	rolled_hi: _dt.date | None  # last day read from Jarvis Activity Daily (None: none)
	behind: int  # closed days the rollup has not reached
	excluded: int  # of those, the oldest, left out of the totals (beyond LIVE_DAY_CAP)
	live_lo: _dt.date  # first closed day computed live
	closed_hi: _dt.date  # last closed day in the range


def _closed_plan(lo, hi, rolled_through, yesterday):
	"""How the CLOSED days of ``lo`` .. ``hi`` (already clamped to the horizon and today,
	``lo <= hi``) are served.

	Days up to ``rolled_hi`` come from ``Jarvis Activity Daily``. ``behind`` is how many
	closed days the rollup has not reached (``rolled_hi`` + 1 .. ``closed_hi``). Only the
	``LIVE_DAY_CAP`` most recent of those are computed live, from ``live_lo``; the older
	``excluded`` ones are left out of the totals (a request must not run an unbounded pass
	over the raw tables when the job has been off for weeks). Pure arithmetic: no query, so
	the caller can enqueue the rollup before computing anything."""
	one_day = _dt.timedelta(days=1)
	closed_hi = min(hi, yesterday)
	rolled_hi = min(closed_hi, rolled_through) if rolled_through else None
	behind_lo = lo if rolled_hi is None or rolled_hi < lo else rolled_hi + one_day
	behind = max(0, (closed_hi - behind_lo).days + 1)
	live_lo = max(behind_lo, closed_hi - one_day * (LIVE_DAY_CAP - 1)) if behind else behind_lo
	return _Plan(rolled_hi, behind, (live_lo - behind_lo).days if behind else 0, live_lo, closed_hi)


def _people(lo, hi, user, plan, yesterday):
	"""The per-person rows for the inclusive days ``lo`` .. ``hi`` (``plan`` is
	``_closed_plan``'s answer). Rolled days come from ``Jarvis Activity Daily``; the
	recent closed days the rollup has not reached, and today, are computed live by
	``day_counters``; the older unreached days (beyond ``LIVE_DAY_CAP``) are not in the
	totals. Today is its rolled rows plus a live tail (``_today_counters``)."""
	one_day = _dt.timedelta(days=1)
	rolled_hi, live_lo, closed_hi = plan.rolled_hi, plan.live_lo, plan.closed_hi
	counters: dict[tuple, dict] = {}
	snapshots: dict[str, str] = {}
	if rolled_hi is not None and rolled_hi >= lo:
		counters, snapshots, _throughs = _daily_rows(lo, rolled_hi, user)
	if live_lo <= closed_hi:
		live, live_snapshots = day_counters(live_lo, closed_hi, user)
		counters.update(live)  # disjoint days, so nothing is overwritten
		for actor, name in live_snapshots.items():
			snapshots.setdefault(actor, name)
	# Today is never "unrolled": its partial rows plus a live tail, so it adds nothing to
	# stale_days. (Yesterday's partial rows, left until the midnight roll replaces them,
	# are not read: only days up to the watermark come from the table.)
	if hi > yesterday:
		today_counters, today_snapshots = _today_counters(yesterday + one_day, user)
		counters.update(today_counters)  # a day of its own: disjoint from the closed days
		for actor, name in today_snapshots.items():
			snapshots.setdefault(actor, name)

	start = _dt.datetime.combine(lo, _dt.time.min)
	end = _dt.datetime.combine(hi + one_day, _dt.time.min)
	people: dict[str, dict] = {}
	names: dict[str, str] = {}

	def person(u):
		u = u or ""
		if u not in people:
			people[u] = _blank_person(u)
		return people[u]

	# -- fold the per-day counters into per-person totals, and the day-based flags
	big_export: dict[str, int] = {}
	daily_export: dict[str, int] = {}  # most rows exported in one site-calendar day
	refusals: dict[str, int] = {}  # most refused / denied in one site-calendar day
	off: dict[str, int] = defaultdict(int)
	for (actor, _day), c in counters.items():
		p = person(actor)
		for k in ("turns", "tokens", "reads", "rows_read", "exports", "rows_exported"):
			p[k] += c[k]
		for k in ("writes", "erp_deletes", "failed", "actions"):
			p[k] += c[k]
		refused_denied = c["denied"] + c["refused"]
		p["refused_denied"] += refused_denied
		off[actor] += c["off_hours"]
		if c["exports"]:
			daily_export[actor] = max(daily_export.get(actor, 0), c["rows_exported"])
			big_export[actor] = max(big_export.get(actor, 0), c["max_export_rows"])
		if refused_denied:
			refusals[actor] = max(refusals.get(actor, 0), refused_denied)

	flags: dict[str, list] = defaultdict(list)

	# -- delete burst: Python sliding window (a SQL rolling window is not portable). A raw
	# query over applied delete rows, NOT rolled: a burst may straddle two days, one of
	# them rolled and one live, and the window must see both.
	aw = frappe.qb.DocType(AGENT_WRITE)
	q = frappe.qb.from_(aw).select(aw.actor, aw.at, aw.bulk_count)
	q = q.where((aw.tool.isin(list(_DELETE_TOOLS))) & (aw.outcome == "applied"))
	q = q.where(aw.at >= add_to_date(start, minutes=-DELETE_BURST_MINUTES)).where(aw.at < end)
	if user:
		q = q.where(aw.actor == user)
	by_actor: dict[str, list] = defaultdict(list)
	for r in q.orderby(aw.at).orderby(aw.name).run(as_dict=True):
		by_actor[r.actor or ""].append((get_datetime(r.at), cint(r.bulk_count) or 1))
	for actor, rows in by_actor.items():
		worst = _max_delete_window(rows, start, end)
		if worst >= DELETE_BURST_COUNT:
			label = _("{0} deletes in 1 hour").format(f"{worst:,}")
			flags[actor].append({"key": "delete_burst", "label": label, "value": worst})
			person(actor)

	# -- big single export
	for actor, biggest in big_export.items():
		if biggest >= BIG_EXPORT_ROWS:
			label = _("One export of {0} rows").format(f"{biggest:,}")
			flags[actor].append({"key": "big_export", "label": label, "value": biggest})

	# -- rows exported in one site-calendar day
	for actor, rows in daily_export.items():
		if rows >= DAILY_EXPORT_ROWS:
			label = _("{0} rows exported in one day").format(f"{rows:,}")
			flags[actor].append({"key": "daily_export", "label": label, "value": rows})

	# -- refused / denied in one site-calendar day (Agent Write refusals join the events')
	for actor, n in refusals.items():
		if n >= REFUSALS_PER_DAY:
			label = _("{0} refused in one day").format(n)
			flags[actor].append({"key": "refusals", "label": label, "value": n})

	# -- off-hours (events plus visible Agent Write rows; applied export rows are
	# represented by their event)
	for actor, n in off.items():
		if n:
			# Two full templates, never a plural suffix glued on in English.
			template = _("{0} event outside {1} to {2}") if n == 1 else _("{0} events outside {1} to {2}")
			label = template.format(n, f"{BUSINESS_HOURS_START:02d}:00", f"{BUSINESS_HOURS_END:02d}:00")
			flags[actor].append({"key": "off_hours", "label": label, "value": n})
			person(actor)

	# -- names: the User record, else the Agent Write / rolled-day snapshot, else the
	# Activity Event snapshot (kept for deleted users, D12; asked only for the few still
	# unnamed, over the live window)
	real = [u for u in people if u]
	if real:
		for r in frappe.get_all("User", filters={"name": ["in", real]}, fields=["name", "full_name"]):
			if r.full_name:
				names[r.name] = r.full_name
	missing = [u for u in real if u not in names and u not in snapshots]
	if missing:
		for actor, name in event_snapshots(missing, start, end).items():
			snapshots.setdefault(actor, name)
	for u, p in people.items():
		if u:
			p["name"] = names.get(u) or snapshots.get(u) or u
		p["flags"] = flags.get(u, [])

	def activity_total(p):
		return p["reads"] + p["exports"] + p["writes"] + p["actions"] + p["refused_denied"] + p["failed"]

	ordered = sorted(people.values(), key=lambda p: (not p["flags"], -activity_total(p), p["name"].lower()))
	return ordered


def _tables_ready():
	"""False on a site running this code before ``bench migrate`` has created the tables
	(the cached table list is cleared by migrate). The summary answers "being prepared"
	then, instead of a 500 on the first admin who opens the page."""
	return all(frappe.db.table_exists(dt) for dt in (ACTIVITY_EVENT, DAILY))


def _preparing():
	"""The empty payload for ``_tables_ready()`` False: every key the page reads."""
	return {
		"people": [],
		"people_truncated": False,
		"first_event_at": {"agent": None, "app": None},
		"logging_health": {
			"warn": False,
			"tool_calls_24h": 0,
			"events_24h": 0,
			"rollup_lag_days": 0,
			"scheduler_disabled": False,
			"app_warn": False,
			"app_seen_24h": 0,
			"app_actions_24h": 0,
			"app_failures_24h": 0,
			"app_dropped_24h": 0,
		},
		"thresholds": dict(THRESHOLDS),
		"stale_days": 0,
		"clamped": False,
		"partial": False,
		"excluded_days": 0,
		"preparing": True,
	}


def summary(filters=None):
	"""Per-person usage and red flags for the range, plus scope / health metadata.

	``filters``: ``from_date`` / ``to_date`` (inclusive days) and ``user``. The events
	view's ``activity`` / ``outcome`` filters do not apply here: a summary that hid
	rows by outcome would hide the very flags it exists to raise.

	Counting rules: see ``day_counters`` (one definition, shared with the rollup).

	The range is clamped to what is kept: no ``from_date`` means the horizon
	(``today - 89``); an earlier one is raised to it and the result carries
	``clamped: True``; ``to_date`` past today is cut to today; a range wholly outside
	the kept window is empty (and not ``clamped``). Days up to the rollup watermark are
	read from ``Jarvis Activity Daily``, later closed days are computed live, and today is
	its rolled rows (through the last full hour, ``through``) plus a live pass after that.

	``people`` holds at most ``PEOPLE_CAP`` entries (flagged people first, then the busiest);
	``people_truncated`` is true when more matched.

	``stale_days`` is how many closed days the rollup has not reached. When it is above
	zero the rollup is enqueued FIRST (before any live computation, so even a request that
	then runs slowly or dies has asked for the catch-up), and at most ``LIVE_DAY_CAP`` of
	those days, the most recent, are computed live. The older ones are left out of the
	totals: the result then carries ``partial: True`` and ``excluded_days`` (how many), and
	the report says those days are not included yet. With the tables missing (code before
	migrate) the payload is empty and ``preparing: True``."""
	require_jarvis_admin()
	if not _tables_ready():
		return _preparing()
	filters = filters or {}
	user = _text(filters.get("user"))
	now_day = today()
	yesterday = now_day - _dt.timedelta(days=1)
	horizon = horizon_day(now_day)
	req_from = getdate(filters["from_date"]) if filters.get("from_date") else None
	req_to = getdate(filters["to_date"]) if filters.get("to_date") else None
	lo = max(req_from, horizon) if req_from else horizon
	hi = min(req_to, now_day) if req_to else now_day

	people, stale_days, excluded_days, clamped = [], 0, 0, False
	if lo <= hi:
		clamped = bool(req_from and req_from < horizon)
		plan = _closed_plan(lo, hi, watermark(), yesterday)
		stale_days, excluded_days = plan.behind, plan.excluded
		if stale_days:
			_enqueue_rollup()
		people = _people(lo, hi, user, plan, yesterday)

	people_truncated = len(people) > PEOPLE_CAP
	return {
		"people": people[:PEOPLE_CAP],  # already flagged first, then busiest (``_people``)
		"people_truncated": people_truncated,
		"first_event_at": first_event_at(),
		"logging_health": _logging_health(),
		"thresholds": dict(THRESHOLDS),
		"stale_days": stale_days,
		"clamped": clamped,
		"partial": excluded_days > 0,
		"excluded_days": excluded_days,
		"preparing": False,
	}


def _enqueue_rollup():
	"""Ask a long-queue worker to catch the rollup up. ``job_id`` + ``deduplicate`` make
	the framework skip the call while one is queued or running (the id is namespaced by
	site, so two sites never block each other). Never fails the summary: the scheduled
	hourly run is the other path."""
	try:
		frappe.enqueue(
			"jarvis.activity_rollup.rollup_days",
			queue="long",
			job_id="jarvis_activity_rollup",
			deduplicate=True,
		)
	except Exception as e:
		frappe.logger("jarvis.activity_log").error(f"activity rollup: enqueue failed: {type(e).__name__}")


def first_event_at():
	"""Earliest recorded activity per source, for the scope line. Not range-filtered:
	it states when recording began. One lookup per event kind on the (event, at)
	index, then the minimum in Python. "agent" is the earlier of the first agent event
	and the first Agent Write row (the older log), so pruning the events does not move it
	later than the writes that outlive them."""
	require_jarvis_admin()
	ev = frappe.qb.DocType(ACTIVITY_EVENT)

	def earliest(kinds):
		times = []
		for kind in kinds:
			rows = frappe.qb.from_(ev).select(ev.at).where(ev.event == kind).orderby(ev.at).limit(1).run()
			if rows and rows[0][0] is not None:
				times.append(rows[0][0])
		return min(times) if times else None

	aw = frappe.qb.DocType(AGENT_WRITE)
	first_write = frappe.qb.from_(aw).select(Min(aw.at)).run()
	first_write = first_write[0][0] if first_write else None
	agent = [t for t in (earliest(_AGENT_EVENTS), first_write) if t is not None]
	return {"agent": min(agent) if agent else None, "app": earliest(("action",))}


def _logging_health():
	"""``warn`` when agents ran tools in the last 24 h yet not one Activity Event was
	recorded: the activity log is probably broken (or the table is missing).

	``app_warn`` is the same question for the app-action hooks: they saw a LOGGED request
	(or failed to snapshot / record one) in the last 24 h but wrote no app row (an event
	``action`` / ``read`` / ``export`` whose ``method`` is a dotted endpoint name, which a
	tool-path row, named by a bare tool, never is). ``app_actions_24h`` is 0 or 1 (an
	existence check: the question is "any?", not "how many?"). ``app_dropped_24h`` is how many
	rows flood control dropped (data only: it raises no warning)."""
	since = add_to_date(now_datetime(), hours=-24)
	tu = frappe.qb.DocType(TURN_USAGE)
	tool_calls = cint(
		frappe.qb.from_(tu)
		.select(Sum(tu.tool_calls))
		.where((tu.day >= since.date()) & (tu.creation >= since))
		.run()[0][0]
	)
	ev = frappe.qb.DocType(ACTIVITY_EVENT)
	# Agent events only: an app ``action`` proves the app logs, not that the TOOL path does,
	# so a broken tool-path logger would hide behind any admin clicking around.
	events_24h = cint(
		frappe.qb.from_(ev)
		.select(Count("*"))
		.where((ev.at >= since) & ev.event.isin(list(_AGENT_EVENTS)))
		.run()[0][0]
	)
	# The app-action logger (``activity_actions``) is checked on its own: requests it saw
	# against rows it wrote. The check above must stay agent-only (an admin clicking around
	# writes app rows and would mask a dead tool-path logger); this one must stay app-only
	# (agents running tools write agent rows and would mask a dead request hook).
	counters = activity_actions.health_counters()
	app_actions = len(
		frappe.qb.from_(ev)
		.select(ev.name)
		.where((ev.at >= since) & ev.event.isin(["action", "read", "export"]) & ev.method.like("%.%"))
		.limit(1)
		.run()
	)
	# Why totals may be behind: days the rollup has not reached, and whether anything
	# is running it (pause_scheduler / maintenance mode / the System Settings switch).
	now_day = today()
	base = horizon_day(now_day) - _dt.timedelta(days=1)  # "nothing rolled" = the day before the horizon
	rolled = max(watermark() or base, base)
	return {
		"warn": tool_calls > 0 and events_24h == 0,
		"tool_calls_24h": tool_calls,
		"events_24h": events_24h,
		"rollup_lag_days": max(0, (now_day - _dt.timedelta(days=1) - rolled).days),
		"scheduler_disabled": bool(
			is_scheduler_inactive(verbose=False) or is_scheduler_disabled(verbose=False)
		),
		# A request the hook saw (or failed to snapshot / record) with no app row to show for it.
		"app_warn": (counters["seen"] > 0 or counters["failures"] > 0) and app_actions == 0,
		"app_seen_24h": counters["seen"],
		"app_actions_24h": app_actions,
		"app_failures_24h": counters["failures"],
		"app_dropped_24h": counters["dropped"],
	}


# --------------------------------------------------------------------------- API

API_VIEWS = ("summary", "events")


def _arg(value, name):
	"""A filter argument as a stripped string: nothing (``None`` / ``""``) is "no filter",
	and anything that is not text is a clear refusal rather than a 500 from a query."""
	if value is None:
		return ""
	if not isinstance(value, str):
		frappe.throw(_("{0} must be text.").format(frappe.bold(name)), frappe.ValidationError)
	return value.strip()


def _date_arg(value, name):
	text = _arg(value, name)
	if not text:
		return ""
	try:
		return str(getdate(text))
	except Exception:
		frappe.throw(_("{0} is not a valid date.").format(frappe.bold(name)), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def get_user_activity(
	view: str = "summary",
	from_date: str | None = None,
	to_date: str | None = None,
	user: str | None = None,
	activity: str | None = None,
	outcome: str | None = None,
	cursor: str | None = None,
) -> dict:
	"""The in-app "User activity" page's one endpoint (plan 4.6), Jarvis Admin /
	System Manager only.

	``view="summary"``: ``summary()`` (people, flags, thresholds, ``first_event_at``,
	``logging_health``, ``stale_days`` / ``clamped`` / ``partial`` / ``excluded_days`` /
	``preparing``) plus ``notes``. ``view="events"``: one page (200 rows) of ``events()``
	plus ``thresholds``; ``cursor`` is the previous page's ``next_cursor``. The FIRST page
	(no cursor) also carries ``first_event_at`` and ``logging_health``, as the summary does.

	``notes`` is the list of plain, translated sentences the page shows above the data (the
	Desk report builds its message from the same helpers). A follow-up events page (one
	with a ``cursor``) carries an empty list: the first page's notes still stand, and the
	scope line would cost two index lookups per click of "Load more".

	Only allowlisted keys leave this function; no content columns exist in the source."""
	require_jarvis_admin()
	view = _arg(view, "view") or "summary"
	if view not in API_VIEWS:
		frappe.throw(_("Unknown view {0}").format(frappe.bold(view)), frappe.ValidationError)
	filters = {
		"from_date": _date_arg(from_date, "from_date"),
		"to_date": _date_arg(to_date, "to_date"),
		"user": _arg(user, "user"),
		"activity": _arg(activity, "activity"),
		"outcome": _arg(outcome, "outcome"),
	}
	filters = {k: v for k, v in filters.items() if v}
	after = _arg(cursor, "cursor") or None

	if view == "summary":
		res = summary(filters)
		return {**res, "notes": summary_notes(res)}

	if not frappe.db.table_exists(ACTIVITY_EVENT):  # code deployed, migrate not run yet
		blank = _preparing()
		return {
			"rows": [],
			"next_cursor": None,
			"thresholds": dict(THRESHOLDS),
			"preparing": True,
			"notes": [preparing_note()],
			"first_event_at": blank["first_event_at"],
			"logging_health": blank["logging_health"],
		}
	res = events(filters, after=after, limit=DEFAULT_LIMIT)
	out = {
		"rows": res["rows"],
		"next_cursor": res["next_cursor"],
		"thresholds": dict(THRESHOLDS),
		"preparing": False,
		"notes": [],
	}
	if not after:
		# The first page also carries what the summary does, so a reload or a deep link straight
		# to the Events tab can show the right banner severity and first-run text without a
		# summary round trip. Follow-up pages (a cursor) carry neither.
		first = first_event_at()
		out["notes"] = events_notes(first, filters.get("from_date"))
		out["first_event_at"] = first
		out["logging_health"] = _logging_health()
	return out
