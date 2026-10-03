"""A Jarvis Admin's view of every user's macros: list them, open one read-only,
stop a run.

The owner's endpoints (``jarvis.chat.macros_api``) are untouched and stay
owner-scoped. Nothing here changes who may save, run or delete a macro:
``has_macro_permission``, the doctype's role rows and the generic document API are
as they were, so an admin still cannot edit or run someone else's macro. These
endpoints read with the role gate below instead of the document permission, and
the one write is a stop.

Every endpoint:

* is POST-only;
* starts with ``refuse_in_tool_dispatch()`` and ``require_jarvis_admin()``. The
  first matters as much as the second: an admin's own assistant must not be able
  to call these, or one tool call would put every user's prompts in the model's
  context. The module is on ``run_method``'s denied prefixes for the same reason
  (``jarvis/tools/run_method.py``): tools call through ``frappe.call``, which skips
  the HTTP-method check;
* refuses a ``name`` that is not text before it reads anything (a dict reaches the
  database layer as a filter and would match some other row), loads the row, and
  keys everything after that on the loaded row's own name.

Privacy: step prompts and summaries, which their owners wrote as private rows, are
readable here by a Jarvis Admin (a System Manager could already read them). A
run's conversation is never returned.
"""

from __future__ import annotations

import frappe
from frappe import _

from jarvis.chat import list_filters
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_admin, require_jarvis_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

# What the run's owner reads, on the run row and as the closing line of its chat.
STOPPED_BY_ADMIN = "Stopped by an administrator."

# How many of a macro's runs the read-only view shows.
RECENT_RUNS = 20

_SORTABLE = {
	"macro_name": "macro_name",
	"owner": "owner",
	"modified": "modified",
	"next_run_at": "next_run_at",
}
_FILTERS = {"owner", "armed", "scheduled", "live_run"}

_LIST_COLUMNS = """m.name, m.macro_name, m.owner, m.enabled, m.skip_confirmation,
	m.schedule_enabled, m.schedule_frequency, m.schedule_weekday, m.schedule_day_of_month,
	m.schedule_time, m.next_run_at, m.modified"""
_HAS_LIVE_RUN = """EXISTS (SELECT 1 FROM `tabJarvis Macro Run` r
	WHERE r.macro = m.name AND r.status IN %(live)s)"""


def _name_arg(value, what: str) -> str:
	"""``value`` as a document name, or a refusal. Checked here and not left to the
	type hint: Frappe validates hints on an HTTP request only."""
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} must be a name.").format(what), frappe.ValidationError)
	return value.strip()


def _full_names(users) -> dict:
	users = sorted({u for u in users if u})
	if not users:
		return {}
	return {
		u.name: u.full_name or u.name
		for u in frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "full_name"])
	}


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_list_macros(
	search: str = "",
	filters: str | dict | None = None,
	sort_field: str = "",
	sort_dir: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Every user's macros, one page at a time: who owns each, whether it is on,
	scheduled or armed, and how its last run went. No step and no summary text:
	those are read one macro at a time (``admin_get_macro``).

	``filters``: ``owner`` (a user), ``armed``, ``scheduled``, ``live_run`` (0 or 1).
	Envelope ``{rows, total, has_more, start, page_length, owners}``; ``owners`` is
	everyone who has a macro, for the owner filter.

	Every predicate is written here and every value is bound: nothing the caller
	sends becomes SQL text."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.chat.macros import _LIVE_RUN_STATUSES

	start, page_length = list_filters.clamp_page(start, page_length)
	f = list_filters.load_legacy_filters(filters, _FILTERS)
	conditions = ["1=1"]
	params: dict = {"start": start, "page_length": page_length, "live": _LIVE_RUN_STATUSES}

	if search and isinstance(search, str):
		conditions.append("(m.macro_name LIKE %(q)s OR m.owner LIKE %(q)s)")
		params["q"] = f"%{list_filters.escape_like(search.strip())}%"
	if "owner" in f:
		if not isinstance(f["owner"], str):
			frappe.throw(_("Invalid owner filter."))
		conditions.append("m.owner = %(owner)s")
		params["owner"] = f["owner"]
	if "armed" in f:
		conditions.append("m.skip_confirmation = %(armed)s")
		params["armed"] = list_filters.bool01(f["armed"])
	if "scheduled" in f:
		conditions.append("m.schedule_enabled = %(scheduled)s")
		params["scheduled"] = list_filters.bool01(f["scheduled"])
	if "live_run" in f:
		conditions.append(_HAS_LIVE_RUN if list_filters.bool01(f["live_run"]) else f"NOT {_HAS_LIVE_RUN}")

	where = " AND ".join(conditions)
	order = list_filters.order_by(sort_field, sort_dir, _SORTABLE, "macro_name", "asc", prefix="m.")
	total = list_filters.bounded_sql(f"SELECT COUNT(*) FROM `tabJarvis Macro` m WHERE {where}", params)[0][0]
	rows = list_filters.bounded_sql(
		f"""SELECT {_LIST_COLUMNS}
		FROM `tabJarvis Macro` m
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	from jarvis.chat.macro_scheduler import is_retry_pending

	names = [r.name for r in rows]
	last_runs = _last_runs(names)
	live_runs = _live_runs(names)
	owners = _owners()
	full_names = {o["user"]: o["full_name"] for o in owners}
	now = frappe.utils.now_datetime()
	for r in rows:
		r["owner_full_name"] = full_names.get(r.owner) or r.owner
		r["last_run"] = last_runs.get(r.name)
		r["live_run"] = live_runs.get(r.name, "")
		# Pure arithmetic on the row, no query (as in the owner's list).
		r["next_run_is_retry"] = int(is_retry_pending(r, now))
		# Time renders as a timedelta over raw SQL; stringify for a stable payload.
		if r.get("schedule_time") is not None:
			r["schedule_time"] = str(r["schedule_time"])

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": page_length,
		"owners": owners,
	}


def _owners() -> list[dict]:
	"""Everyone who owns a macro, with how many: the owner filter's choices."""
	counts = frappe.db.sql(
		"SELECT owner, COUNT(*) AS n FROM `tabJarvis Macro` GROUP BY owner ORDER BY owner", as_dict=True
	)
	full_names = _full_names(c.owner for c in counts)
	return [{"user": c.owner, "full_name": full_names.get(c.owner) or c.owner, "macros": c.n} for c in counts]


def _last_runs(macro_names: list[str]) -> dict:
	"""``{macro: its most recent run}``, whoever owns the run row. One query for a
	whole page.

	Not ``macros_api._last_runs``: that one is scoped to an owner's run rows, which
	is every run of their macro only while the macro has had one owner. Run rows
	keep the owner they were made for, so after a hand-over it finds nothing.

	Status, trigger and times only. The run's conversation is not for an admin, and
	its reason can quote what a step tried to do: that is shown with the opened
	macro, not in a list. ``name`` is set only while the run is live."""
	if not macro_names:
		return {}
	from jarvis.chat.macros import _LIVE_RUN_STATUSES

	rows = frappe.db.sql(
		"""SELECT r.name, r.macro, r.status, r.`trigger`, r.started_at, r.finished_at
		FROM `tabJarvis Macro Run` r
		JOIN (
			SELECT macro, MAX(creation) AS latest FROM `tabJarvis Macro Run`
			WHERE macro IN %(names)s GROUP BY macro
		) m ON m.macro = r.macro AND m.latest = r.creation""",
		{"names": tuple(macro_names)},
		as_dict=True,
	)
	return {
		r.macro: {
			"name": r.name if r.status in _LIVE_RUN_STATUSES else "",
			"status": r.status,
			"trigger": r.trigger,
			"started_at": r.started_at,
			"finished_at": r.finished_at,
		}
		for r in rows
	}


def _live_runs(macro_names: list[str]) -> dict:
	"""``{macro: its newest run that has not ended}``: what Stop acts on. Usually the
	last run; an older run can still be going after a newer one has ended, and it
	must not be left with nothing to stop it."""
	if not macro_names:
		return {}
	from jarvis.chat.macros import _LIVE_RUN_STATUSES

	rows = frappe.db.sql(
		"""SELECT name, macro FROM `tabJarvis Macro Run`
		WHERE macro IN %(names)s AND status IN %(live)s
		ORDER BY creation ASC""",
		{"names": tuple(macro_names), "live": _LIVE_RUN_STATUSES},
		as_dict=True,
	)
	return {r.macro: r.name for r in rows}  # ascending: the newest is kept


# --------------------------------------------------------------------------- #
# One macro, read-only
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_get_macro(name: str) -> dict:
	"""One macro of any user, to look at: its settings, its steps, its summarized
	prompt, and its recent runs as status, trigger, times, step counts and the
	reason each ended. Never a run's conversation.

	There is nothing to save it with: an admin does not edit or run another user's
	macro."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	name = _name_arg(name, _("Macro"))
	# No check_permission: reading other users' macros is what the role gate above
	# grants, and the document permission (owner only) is deliberately unchanged.
	doc = frappe.get_doc(MACRO, name)
	from jarvis.chat.macro_scheduler import is_retry_pending
	from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import step_skills

	runs = frappe.db.sql(
		"""SELECT name, status, `trigger`, creation, started_at, finished_at,
			current_step, total_steps, run_mode, error
		FROM `tabJarvis Macro Run`
		WHERE macro = %(macro)s
		ORDER BY creation DESC
		LIMIT %(limit)s""",
		{"macro": doc.name, "limit": RECENT_RUNS},
		as_dict=True,
	)
	for r in runs:
		r["error"] = r["error"] or ""
	return {
		"name": doc.name,
		"macro_name": doc.macro_name,
		"owner": doc.owner,
		"owner_full_name": _full_names([doc.owner]).get(doc.owner) or doc.owner,
		"description": doc.description or "",
		"enabled": int(doc.enabled or 0),
		"stop_on_error": int(doc.stop_on_error or 0),
		"skip_confirmation": int(doc.skip_confirmation or 0),
		"schedule_enabled": int(doc.schedule_enabled or 0),
		"schedule_frequency": doc.schedule_frequency or "daily",
		"schedule_weekday": doc.schedule_weekday or None,
		"schedule_day_of_month": doc.schedule_day_of_month or None,
		# `is None`, not truthiness: midnight is timedelta(0), which is falsy.
		"schedule_time": "" if doc.schedule_time is None else str(doc.schedule_time),
		"next_run_at": str(doc.next_run_at or ""),
		"next_run_is_retry": int(is_retry_pending(doc)),
		"modified": str(doc.modified or ""),
		"merged_prompt": doc.merged_prompt or "",
		"merge_status": doc.merge_status or "",
		"steps": [
			{
				"label": s.label or "",
				"prompt": s.prompt or "",
				"model_override": s.model_override or "",
				"thinking_override": s.thinking_override or "",
				# How many skills the step uses. Their names are the owner's.
				"skill_count": len(step_skills(s)),
			}
			for s in (doc.steps or [])
		],
		"runs": runs,
		"live_run": _live_runs([doc.name]).get(doc.name, ""),
	}


# --------------------------------------------------------------------------- #
# Stop a run
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_stop_run(run: str) -> dict:
	"""Stop a live run of any user's macro. The stop itself is the engine's
	(``macros._stop_run``): it takes the run lock, disarms the run's chat, cancels a
	step that has not started, and leaves the reason as the last line of the chat.
	A step already with the agent still finishes; no further step starts.

	The owner is told an administrator stopped it (the run's reason). Who that was
	is recorded as a Comment on the macro.

	A run that has already ended is left as it ended: ``stopped`` is False and
	``message`` says so."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	run = _name_arg(run, _("Run"))
	row = frappe.db.get_value(RUN, {"name": run}, ["name", "macro"], as_dict=True)
	if not row:
		frappe.throw(_("This run no longer exists."), frappe.DoesNotExistError)
	from jarvis.chat import macros

	admin = frappe.session.user
	# Nothing is pending and no lock is held here, which is what `_stop_run` asks of
	# its caller: it waits for the run lock, and it commits.
	if not macros._stop_run(row.name, reason=STOPPED_BY_ADMIN, by=admin):
		return {
			"ok": True,
			"stopped": False,
			"status": frappe.db.get_value(RUN, row.name, "status") or "",
			"message": _("This run had already ended."),
		}
	try:
		_leave_comment(row.macro, f"{admin} stopped a run of this macro ({row.name}).")
		frappe.db.commit()
	except Exception:
		# The run is stopped and that is committed. Failing the request now would
		# tell the admin it was not.
		frappe.db.rollback()
		frappe.log_error(
			title=f"jarvis.chat.macros_admin_api.comment_failed: {row.name}",
			message=frappe.get_traceback(),
		)
	return {"ok": True, "stopped": True, "status": "stopped", "message": ""}


def _leave_comment(macro: str | None, text: str) -> None:
	"""Record an admin's action on the macro's timeline. What ``Document.add_comment``
	inserts, without loading or saving the macro (as the scheduler does when it
	switches a leaver's schedules off). Nothing to write on when the macro is gone."""
	if not macro or not frappe.db.exists(MACRO, macro):
		return
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"comment_email": frappe.session.user,
			"reference_doctype": MACRO,
			"reference_name": macro,
			"content": frappe.utils.escape_html(text),
		}
	).insert(ignore_permissions=True)
