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

Arguments are checked HERE, by hand. This module has ``from __future__ import
annotations``, which turns every type hint into a string, and Frappe's request-time
type check skips string hints (``frappe/utils/typing_validations.py``, Frappe 15 and
16 alike). So the hints below document the arguments and guard nothing: a list or a
dict posted as JSON reaches the function body as it is.

Privacy: descriptions, step prompts and summaries, which their owners wrote as
private rows, are readable here by a Jarvis Admin (a System Manager could already
read them). A run's conversation is never returned.
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
# The owner filter's choices are a select: past this many it is no longer one, and
# the search box (which matches an owner's name and address) is the way in.
OWNERS_MAX = 200
# A search box, not a document: anything longer is cut, not refused.
SEARCH_MAX = 140

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
	"""``value`` as a document name, or a refusal. This check is the only guard:
	Frappe does not type-check this module's arguments (see the module docstring)."""
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} must be a name.").format(what), frappe.ValidationError)
	return value.strip()


def _text_arg(value, what: str) -> str:
	"""``value`` as optional text ("" when absent), or a refusal. A list or a dict is
	refused, not ignored: ignoring a search would answer with the unfiltered list."""
	if value is None:
		return ""
	if not isinstance(value, str):
		frappe.throw(_("{0} must be text.").format(what), frappe.ValidationError)
	return value.strip()


def _filters_arg(filters) -> dict:
	"""``filters`` as a dict, from a dict or a JSON object; nothing sent is ``{}``.
	Anything else (a list, text that is not JSON) is refused: the shared reader
	(``list_filters.load_legacy_filters``) drops it, and a filter that silently does
	nothing answers with every user's macros."""
	if filters is None or (isinstance(filters, str) and not filters.strip()):
		return {}
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters)
		except Exception:
			frappe.throw(_("Filters could not be read."), frappe.ValidationError)
	if not isinstance(filters, dict):
		frappe.throw(_("Filters must be an object of filter names and values."), frappe.ValidationError)
	return filters


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

	``search`` matches the macro's name, its owner's address and its owner's full
	name (the list shows the full name, so that is what an admin types).
	``filters``: ``owner`` (a user), ``armed``, ``scheduled``, ``live_run`` (0 or 1).
	Envelope ``{rows, total, has_more, start, page_length}``. The owner filter's
	choices are their own call (``admin_macro_owners``): they do not change with the
	page or the search, so they are not recomputed with every keystroke.

	Every predicate is written here and every value is bound: nothing the caller
	sends becomes SQL text."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.chat.macros import _LIVE_RUN_STATUSES

	search = _text_arg(search, _("Search"))[:SEARCH_MAX]
	sort_field = _text_arg(sort_field, _("Sort field"))
	sort_dir = _text_arg(sort_dir, _("Sort direction"))
	start, page_length = list_filters.clamp_page(start, page_length)
	f = list_filters.load_legacy_filters(_filters_arg(filters), _FILTERS)
	conditions = ["1=1"]
	params: dict = {"start": start, "page_length": page_length, "live": _LIVE_RUN_STATUSES}

	if search:
		# One statement: the full name is matched by a subquery on the user table,
		# not looked up per row.
		conditions.append(
			"""(m.macro_name LIKE %(q)s OR m.owner LIKE %(q)s
			OR m.owner IN (SELECT u.name FROM `tabUser` u WHERE u.full_name LIKE %(q)s))"""
		)
		params["q"] = f"%{list_filters.escape_like(search)}%"
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
	full_names = _full_names(r.owner for r in rows)
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
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_macro_owners() -> dict:
	"""The owner filter's choices: who owns a macro, with how many. Asked for once
	when the pane opens (and on Refresh), not with every page of the list.

	``{owners: [{user, full_name, macros}], more}``. At most ``OWNERS_MAX``, the ones
	with the most macros, in name order; ``more`` says the site has others, who are
	reached through the search box. Only people who own a macro: this is not a
	directory of the site's users."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	counts = list_filters.bounded_sql(
		"""SELECT owner, COUNT(*) AS n FROM `tabJarvis Macro`
		GROUP BY owner ORDER BY n DESC, owner ASC LIMIT %(limit)s""",
		{"limit": OWNERS_MAX + 1},
		as_dict=True,
	)
	more = len(counts) > OWNERS_MAX
	counts = counts[:OWNERS_MAX]
	full_names = _full_names(c.owner for c in counts)
	owners = [
		{"user": c.owner, "full_name": full_names.get(c.owner) or c.owner, "macros": c.n} for c in counts
	]
	owners.sort(key=lambda o: (o["full_name"].lower(), o["user"]))
	return {"owners": owners, "more": more}


def _last_runs(macro_names: list[str]) -> dict:
	"""``{macro: its most recent run}``, whoever owns the run row. One query for a
	whole page.

	Not ``macros_api._last_runs``: that one is scoped to an owner's run rows, which
	is every run of their macro only while the macro has had one owner. Run rows
	keep the owner they were made for, so after a hand-over it finds nothing.

	Status, trigger and times only. The run's conversation is not for an admin, and
	its reason can quote what a step tried to do: that is shown with the opened
	macro, not in a list. The one thing the list says about the reason is a yes or
	no, ``stopped_by_admin``: whether it is exactly this module's own fixed sentence.
	That lets the pane tell an admin's stop from the owner's own without any of the
	run's text leaving the server. The run Stop acts on is the row's ``live_run``.

	Cost: the grouped subquery finds each macro's rows through the ``macro`` index
	but reads every one of them for ``creation``, and run rows are never purged. A
	composite ``(macro, creation)`` index would answer it from the index alone. It
	is NOT added here: on an existing site ``on_doctype_update`` only runs when the
	doctype is reloaded, so the index needs a patch (as ``owner_creation_index``
	did, ``patches/v2_12_reload_macro_doctypes_for_indexes.py``), and this change
	ships none. Until then the statement is bounded in time (``bounded_sql``) like
	the list's own two, and by the page: at most 100 macros."""
	if not macro_names:
		return {}

	rows = list_filters.bounded_sql(
		"""SELECT r.macro, r.status, r.`trigger`, r.started_at, r.finished_at, r.error
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
			"status": r.status,
			"trigger": r.trigger,
			"started_at": r.started_at,
			"finished_at": r.finished_at,
			"stopped_by_admin": int(r.status == "stopped" and r.error == STOPPED_BY_ADMIN),
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

	rows = list_filters.bounded_sql(
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
	if not frappe.db.exists(MACRO, name):
		# In words an admin can act on: the list they clicked in was loaded earlier.
		frappe.throw(_("This macro was deleted."), frappe.DoesNotExistError)
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

	The owner is told an administrator stopped it: the run's reason, the closing line
	of its chat, and a notification (they are probably not watching). Who that was
	is recorded as a Comment on the macro, and only there: the run row has no column
	for it. The Comment is best-effort; when it cannot be written the Error Log
	names the admin and the run instead.

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
		# tell the admin it was not. The Comment was the only record of WHO: say it
		# here instead (ids only, never the macro's text).
		frappe.db.rollback()
		frappe.log_error(
			title=f"jarvis.chat.macros_admin_api.comment_failed: {row.name}",
			message=(
				f"{admin} stopped run {row.name} of macro {row.macro}; the Comment that records it "
				f"could not be written.\n\n{frappe.get_traceback()}"
			),
		)
	_tell_the_owner(row.macro, admin)
	return {"ok": True, "stopped": True, "status": "stopped", "message": ""}


def _tell_the_owner(macro: str | None, admin: str) -> None:
	"""A notification for the macro's owner that an administrator stopped its run.
	``_stop_run`` posts the chat's closing line and the realtime event, which reach
	someone looking at the app; the engine's own notification (``macros._announce``)
	is for runs that end from inside. Same shape as that one: no document attached,
	the fixed reason as its body. Not sent to an admin stopping their own macro's
	run. Best-effort: the stop is committed and must not fail on this."""
	try:
		from jarvis.chat import macros

		owner, macro_name = frappe.db.get_value(MACRO, macro, ["owner", "macro_name"]) or (None, None)
		if not owner or owner == admin:
			return
		macros.notify_owner(
			owner,
			subject=f"{macros._OUTCOME_SUBJECT['stopped']}: {macro_name}",
			body=STOPPED_BY_ADMIN,
		)
	except Exception:
		frappe.log_error(
			title=f"jarvis.chat.macros_admin_api.notify_failed: {macro}", message=frappe.get_traceback()
		)


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
