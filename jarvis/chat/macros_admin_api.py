"""A Jarvis Admin's view of every user's macros: list them, open one read-only,
stop a run, put a macro on hold or release it, delete it.

The owner's endpoints (``jarvis.chat.macros_api``) stay owner-scoped. Nothing here
changes who may save or run a macro: ``has_macro_permission``, the doctype's role
rows and the generic document API are as they were, so an admin still cannot edit
or run someone else's macro (control without impersonation). These endpoints act
with the role gate below instead of the document permission. Their writes are a
stop, the two hold fields (and switching the macro off with them), and the owner's
own delete.

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
from jarvis.chat.macros import _MACRO_HELD_ERROR, is_held
from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import (
	HOLD_FIELD,
	HOLD_REASON_FIELD,
	hold_fields_exist,
)
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_admin, require_jarvis_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

# What the run's owner reads, on the run row and as the closing line of its chat.
STOPPED_BY_ADMIN = "Stopped by an administrator."

# The reasons an admin's act leaves on a run it stopped: the list's
# ``stopped_by_admin`` is whether a run ended with one of them.
_ADMIN_STOP_REASONS = (STOPPED_BY_ADMIN, _MACRO_HELD_ERROR)

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
_FILTERS = {"owner", "armed", "scheduled", "live_run", "on_hold"}
# What the hold's reason may be: it is shown to the owner on their form.
HOLD_REASON_MAX = 500

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
	held_column = hold_fields_exist()
	if "on_hold" in f:
		on_hold = list_filters.bool01(f["on_hold"])
		if held_column:
			conditions.append("COALESCE(m.admin_hold, 0) = %(on_hold)s")
			params["on_hold"] = on_hold
		elif on_hold:
			conditions.append("1=0")  # before the migrate nothing is held

	where = " AND ".join(conditions)
	order = list_filters.order_by(sort_field, sort_dir, _SORTABLE, "macro_name", "asc", prefix="m.")
	total = list_filters.bounded_sql(f"SELECT COUNT(*) FROM `tabJarvis Macro` m WHERE {where}", params)[0][0]
	rows = list_filters.bounded_sql(
		f"""SELECT {_LIST_COLUMNS}{", m.admin_hold, m.admin_hold_reason" if held_column else ""}
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
		r["admin_hold"] = int(r.get("admin_hold") or 0)
		r["admin_hold_reason"] = (r.get("admin_hold_reason") or "") if r["admin_hold"] else ""
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
	no, ``stopped_by_admin``: whether it is exactly one of the two fixed sentences an
	admin's action leaves (``_ADMIN_STOP_REASONS``: a Stop, or a hold).
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
			"stopped_by_admin": int(r.status == "stopped" and r.error in _ADMIN_STOP_REASONS),
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
		"admin_hold": int(is_held(doc)),
		"admin_hold_reason": (doc.get(HOLD_REASON_FIELD) or "") if is_held(doc) else "",
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


# --------------------------------------------------------------------------- #
# Hold, release, delete
# --------------------------------------------------------------------------- #
def _reason_arg(value) -> str:
	"""The hold's reason: text, not empty, at most ``HOLD_REASON_MAX`` characters. The
	owner reads it on their form, so it is asked for, and refused rather than cut."""
	reason = _text_arg(value, _("Reason"))
	if not reason:
		frappe.throw(_("Say why the macro is put on hold: its owner reads it."), frappe.ValidationError)
	if len(reason) > HOLD_REASON_MAX:
		frappe.throw(
			_("The reason must be at most {0} characters.").format(HOLD_REASON_MAX), frappe.ValidationError
		)
	return reason


def _load_macro(name) -> frappe._dict:
	"""The macro's name and owner, by a name that was checked to be text. Every write
	after this is keyed on the loaded row's own name."""
	name = _name_arg(name, _("Macro"))
	row = frappe.db.get_value(MACRO, {"name": name}, ["name", "owner", "macro_name"], as_dict=True)
	if not row:
		frappe.throw(_("This macro was deleted."), frappe.DoesNotExistError)
	return row


def _refuse_without_the_hold_fields() -> None:
	if not hold_fields_exist():
		frappe.throw(
			_("Holding a macro needs a site update that has not run yet (bench migrate)."),
			frappe.ValidationError,
		)


def _refuse_own_macro(row, action: str) -> None:
	"""The hold verbs are for other users' macros. On their own, an admin switches the
	macro off on its form; a hold they could not lift themselves would lock them out,
	and one they could lift would not hold anything another admin set."""
	if row.owner == frappe.session.user:
		frappe.throw(
			_("This is your own macro. {0}").format(action),
			frappe.ValidationError,
		)


def _lock(row) -> frappe._dict:
	"""Take the macro's row lock (``macros._lock_macro_row``: a fresh snapshot, then the
	locking read first), the one ``run_macro`` takes before it starts a run, and read
	the row again under it: everything decided after this is decided on the present,
	not on the row as it was before the wait. The hold is read only where the site has
	its column (a hand-over works before the migrate too)."""
	from jarvis.chat import macros

	if not macros._lock_macro_row(row.name):
		frappe.db.rollback()
		frappe.throw(_("This macro was deleted."), frappe.DoesNotExistError)
	fields = ["name", "owner", "macro_name", "merge_conversation"]
	if hold_fields_exist():
		fields.append(HOLD_FIELD)
	return frappe.db.get_value(MACRO, row.name, fields, as_dict=True)


# What a held (or, before the migrate, a stood-down) macro is switched to: off,
# unscheduled, disarmed.
_SWITCHED_OFF = {"enabled": 0, "schedule_enabled": 0, "next_run_at": None, "skip_confirmation": 0}


def _stand_down(row, admin: str, own_macro: str, *, reason: str) -> frappe._dict:
	"""Step 1 of a hold and of a hand-over: under the macro's row lock, put it on hold
	with ``reason`` and switch it off, unschedule and disarm it, leave the Comment, and
	commit. ``run_macro`` reads the hold under the same lock, so from the commit on no
	run starts. Refused on the admin's own macro (``own_macro`` says what to do).

	Before the migrate there is no hold to write: the macro is only switched off,
	unscheduled and disarmed. That stops the scheduler from starting it (a scheduled
	run of a macro that is off is refused under the lock); its owner can still start
	it by hand, which is why a hand-over looks again under the lock before it writes.
	Returns the row as read under the lock."""
	row = _lock(row)
	_refuse_own_macro(row, own_macro)
	if hold_fields_exist():
		frappe.db.set_value(MACRO, row.name, {HOLD_FIELD: 1, HOLD_REASON_FIELD: reason, **_SWITCHED_OFF})
		_leave_comment(row.name, f"{admin} put this macro on hold: {reason}")
	else:
		frappe.db.set_value(MACRO, row.name, _SWITCHED_OFF)
	frappe.db.commit()  # visible, and the row lock released, from here on
	return row


def _stop_live_runs(macro: str, *, reason: str, admin: str, verb: str = "hold") -> tuple[int, list[str]]:
	"""Stop each live run of ``macro``; ``(how many this stopped, the runs that could
	not be stopped)``. Each run on its own: one that raised used to leave every run
	after it going. Nothing is pending and no lock is held here: what ``_stop_run``
	asks of its caller."""
	from jarvis.chat import macros

	stopped, failed = 0, []
	for run in macros.live_runs_of(macro):
		try:
			stopped += bool(macros._stop_run(run, reason=reason, by=admin))
		except Exception:
			frappe.db.rollback()
			# A stop refused with frappe.throw left its own message in the request; the
			# client shows the first one, so the admin would read "this run could not be
			# stopped" and never learn what did go through.
			frappe.clear_last_message()
			frappe.log_error(
				title=f"jarvis.chat.macros_admin_api.{verb}_stop_failed: {run}",
				message=frappe.get_traceback(),
			)
			frappe.db.commit()  # the log only; the caller's error rolls the request back
			failed.append(run)
	return stopped, failed


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_hold(macro: str, reason: str = "") -> dict:
	"""Put another user's macro on hold: it does not run, by hand, on its schedule or
	by a parked run resuming, until an admin releases it. The hold also switches the
	macro off, takes it off its schedule and disarms it, and the owner cannot switch
	any of those back on while it lasts (the controller refuses; the form shows why).

	In this order, as the design pins it (not one transaction: a stop commits):

	1. the macro's row lock, then the hold fields and the switches in one raw write,
	   the Comment, commit. ``run_macro`` reads the hold under the same lock, so a run
	   that had not committed by then waits and is refused;
	2. each run that is still live is stopped (``macros._stop_run``, its own run lock,
	   compare-and-set): it committed before the hold. Each is tried on its own; the
	   ones that could not be stopped are named in an error raised after all were
	   tried. The hold stands, and such a run ends at its next step's end.

	Works on a macro that is already off, and again on one already held (the reason is
	the new one). Every step can be repeated: a request that failed part-way is sent
	again. Refused on the admin's own macro."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	row = _load_macro(macro)
	reason = _reason_arg(reason)
	_refuse_without_the_hold_fields()
	admin = frappe.session.user
	row = _stand_down(row, admin, _("Switch it off on the macro form instead."), reason=reason)
	stopped, failed = _stop_live_runs(row.name, reason=_MACRO_HELD_ERROR, admin=admin)
	if failed:
		# The hold stands (committed above), and a run it missed still ends at its next
		# step's end (``macros._apply_step_end``). Holding again retries the stop.
		frappe.throw(
			_(
				"The macro is on hold, but {0} of its runs could not be stopped ({1}). Put it on hold again to retry."
			).format(len(failed), ", ".join(failed)),
			frappe.ValidationError,
		)
	return {"ok": True, "held": True, "stopped_runs": stopped}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_release(macro: str) -> dict:
	"""Lift an admin's hold. That is all: the macro stays off, unscheduled and
	disarmed, and its owner turns back on what they want. A macro that is not held is
	left as it is (``released`` False). Refused on the admin's own macro: a hold
	another admin set is not theirs to lift."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	row = _load_macro(macro)
	_refuse_without_the_hold_fields()
	admin = frappe.session.user
	row = _lock(row)
	_refuse_own_macro(row, _("Another admin has to release it."))
	if not frappe.utils.cint(row.get(HOLD_FIELD)):
		frappe.db.commit()  # releases the row lock
		return {"ok": True, "released": False}
	frappe.db.set_value(MACRO, row.name, {HOLD_FIELD: 0, HOLD_REASON_FIELD: None})
	_leave_comment(row.name, f"{admin} released the hold on this macro.")
	frappe.db.commit()
	return {"ok": True, "released": True}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_delete(macro: str) -> dict:
	"""Delete any user's macro: the owner's own delete (``macros_api.delete_loaded_macro``),
	so its live runs are stopped first and the month's scheduled run rows are kept for
	the owner's budget. The Comments go with the macro; what stays on record is Frappe's
	Deleted Document row (written as the admin) and a notice to the owner that names no
	document, which their Macros list shows until they dismiss it.

	Repeating it after it went through answers "This macro was deleted."."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.chat import macros, macros_api

	row = _load_macro(macro)
	admin = frappe.session.user
	doc = frappe.get_doc(MACRO, row.name)
	out = macros_api.delete_loaded_macro(doc, ignore_permissions=True)
	if row.owner != admin:
		macros.notify_owner(
			row.owner,
			subject=f"{macros_api.ADMIN_DELETE_NOTICE}: {row.macro_name}",
			body=_("An admin deleted your macro {0}.").format(row.macro_name),
		)
	return {"ok": True, "deleted": True, "stopped_runs": out.get("stopped_runs") or 0}


# --------------------------------------------------------------------------- #
# Hand-over
# --------------------------------------------------------------------------- #
# How many times a hand-over stops the macro's live runs and looks again under the
# lock before it gives up. With the hold no run can start after step 1, so one round
# is all it takes; the others are for a site before the migrate, where the owner can
# still start the macro by hand until the owner changes.
_HANDOVER_ROUNDS = 3
# How many people the hand-over dialog's search offers at a time.
TARGETS_MAX = 20


class MacroHandoverError(frappe.ValidationError):
	"""The macro cannot go to that user."""


def _new_owner_arg(value) -> str:
	"""The new owner as ``User.name`` exactly as stored. The database matches a name
	whatever its case and Python does not: written as typed, ``Asha@x`` would own a
	macro that ``asha@x`` (the session user) does not."""
	name = _name_arg(value, _("New owner"))
	stored = frappe.db.get_value("User", {"name": name}, "name")
	if not stored:
		frappe.throw(_("There is no user {0}.").format(name), MacroHandoverError)
	return stored


def _handover_refusal(row, new_owner: str) -> str:
	"""Why ``row`` cannot go to ``new_owner``, or "" when it can. The same people who may
	own a macro that runs on its own (``_schedule_block_reason``'s checks: never
	Administrator or Guest, an enabled account, with Jarvis access), under the
	per-owner cap, with no macro of the same name. Read from the database as it is
	now: called again under the lock before the write."""
	from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import MAX_MACROS_PER_OWNER
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	if new_owner == row.owner:
		return _("{0} already owns this macro.").format(new_owner)
	if new_owner in ("Administrator", "Guest"):
		return _("A macro cannot be handed to {0}. Choose a named user.").format(new_owner)
	if not is_valid_unattended_owner(new_owner):
		return _("{0} is disabled.").format(new_owner)
	if not has_jarvis_access(new_owner):
		return _("{0} does not have access to Jarvis.").format(new_owner)
	if frappe.db.count(MACRO, {"owner": new_owner}) >= MAX_MACROS_PER_OWNER:
		return _("{0} already has {1} macros, the most one user can have.").format(
			new_owner, MAX_MACROS_PER_OWNER
		)
	if frappe.db.exists(MACRO, {"owner": new_owner, "macro_name": row.macro_name}):
		return _("{0} already has a macro named {1}.").format(new_owner, row.macro_name)
	return ""


def _refuse_handover(row, new_owner: str) -> None:
	reason = _handover_refusal(row, new_owner)
	if reason:
		frappe.throw(frappe.utils.escape_html(reason), MacroHandoverError)


def _write_handover(row, new_owner: str) -> None:
	"""The owner change and everything that must not follow the macro to its new
	owner, in the one transaction the caller holds the row lock in:

	* off, unscheduled, disarmed, and the hold (that step 1 set) cleared;
	* no summary and no summary in progress: a summary runs INSTEAD of the steps and
	  the old owner could have written anything in it;
	* no skills on any step: a step's skill can be a private skill of the old owner;
	* no shares, no open assignments (each also shared the macro), nobody following it.

	Raw writes, as every admin verb's: the document permission is the owner's, and
	``validate`` would judge the change as the admin. The child rows' ``owner`` goes
	with the macro's, so no row of it still names the old owner."""
	values = {
		"owner": new_owner,
		**_SWITCHED_OFF,
		"merged_prompt": "",
		"merge_status": "",
		"merge_conversation": "",
		"_assign": None,
	}
	if hold_fields_exist():
		values.update({HOLD_FIELD: 0, HOLD_REASON_FIELD: None})
	frappe.db.set_value(MACRO, row.name, values)
	frappe.db.sql(
		"""UPDATE `tabJarvis Macro Step` SET skills = '[]', owner = %(owner)s
		WHERE parent = %(macro)s AND parenttype = %(doctype)s""",
		{"owner": new_owner, "macro": row.name, "doctype": MACRO},
	)
	frappe.db.delete("DocShare", {"share_doctype": MACRO, "share_name": row.name})
	frappe.db.sql(
		"""UPDATE `tabToDo` SET status = 'Cancelled'
		WHERE reference_type = %(doctype)s AND reference_name = %(macro)s AND status != 'Cancelled'""",
		{"doctype": MACRO, "macro": row.name},
	)
	frappe.db.delete("Document Follow", {"ref_doctype": MACRO, "ref_docname": row.name})


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_handover(macro: str, new_owner: str) -> dict:
	"""Hand another user's macro to ``new_owner``. It arrives switched off,
	unscheduled and disarmed, with no summary, no skills on its steps, no shares, no
	assignments and nobody following it; only its new owner can turn any of that back
	on. Both owners are told (a notice their Macros list shows) and a Comment on the
	macro names the admin and both owners.

	In this order (not one transaction: a stop commits):

	1. the macro is put on hold, under its row lock, and committed, so nothing starts
	   it from here on (before the migrate it is only switched off: see
	   ``_stand_down``);
	2. its live runs are stopped, each on its own. One that cannot be stopped ends the
	   request with the hold standing: the macro does not change hands over a live run
	   of its old owner. Sending the hand-over again retries;
	3. under the row lock again, read fresh: if a run is live again (only possible
	   before the migrate), let go and repeat from 2, at most ``_HANDOVER_ROUNDS``
	   times; otherwise check the new owner again and write the owner change and the
	   clears (``_write_handover``), with the Comment, in one commit.

	Past run rows keep the old owner: they are that person's history and month usage,
	including after the new owner deletes the macro. Every step can be repeated.

	Refused on the admin's own macro, as hold and release are: the hand-over starts
	with a hold, and a hold is for another user's macro. Another admin can hand it
	over. Handing a macro TO oneself is allowed."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.chat import macros

	row = _load_macro(macro)
	new_owner = _new_owner_arg(new_owner)
	admin = frappe.session.user
	own_macro = _("Another admin has to hand it over.")
	_refuse_own_macro(row, own_macro)
	_refuse_handover(row, new_owner)  # before anything is written

	old_owner = row.owner
	row = _stand_down(row, admin, own_macro, reason=f"Being handed over to {new_owner}.")
	stopped = 0
	for _round in range(_HANDOVER_ROUNDS):
		n, failed = _stop_live_runs(row.name, reason=STOPPED_BY_ADMIN, admin=admin, verb="handover")
		stopped += n
		if failed:
			frappe.throw(
				_(
					"The macro was not handed over: {0} of its runs could not be stopped ({1}). It stays {2}. Hand it over again to retry."
				).format(
					len(failed), ", ".join(failed), _("on hold") if hold_fields_exist() else _("switched off")
				),
				MacroHandoverError,
			)
		row = _lock(row)
		if row.owner != old_owner:
			frappe.db.commit()  # releases the row lock
			frappe.throw(_("This macro changed hands meanwhile. Refresh and try again."), MacroHandoverError)
		if macros.live_runs_of(row.name):
			frappe.db.commit()  # let go of the macro row: a stop waits for a run lock
			continue
		_refuse_handover(row, new_owner)
		_write_handover(row, new_owner)
		_leave_comment(row.name, f"{admin} handed this macro over from {old_owner} to {new_owner}.")
		frappe.db.commit()
		break
	else:
		frappe.throw(
			_("The macro kept starting runs and was not handed over. Hand it over again."),
			MacroHandoverError,
		)

	if row.merge_conversation:
		# A summary that was being written for the old owner: its result now finds no
		# macro waiting and is ignored. Its throwaway chat goes, if it is still theirs.
		from jarvis.chat import macros_api

		macros_api._delete_summary_chat(row.merge_conversation, owned_by=old_owner)
	_tell_both_owners(row.macro_name, old_owner, new_owner, admin)
	return {"ok": True, "handed_over": True, "new_owner": new_owner, "stopped_runs": stopped}


def _tell_both_owners(macro_name: str, old_owner: str, new_owner: str, admin: str) -> None:
	"""A notice to each owner that names no document (``macros.notify_owner``), which
	their Macros list shows until dismissed. Not to the admin about their own act."""
	from jarvis.chat import macros, macros_api

	subject = f"{macros_api.ADMIN_HANDOVER_NOTICE}: {macro_name}"
	if old_owner != admin:
		macros.notify_owner(
			old_owner,
			subject=subject,
			body=_("An admin handed your macro {0} to {1}.").format(macro_name, new_owner),
		)
	if new_owner != admin:
		macros.notify_owner(
			new_owner,
			subject=subject,
			body=_(
				"An admin handed you the macro {0} from {1}. It is switched off, unscheduled and not armed, "
				"and its summary and its steps' skills were cleared."
			).format(macro_name, old_owner),
		)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_handover_targets(search: str = "") -> dict:
	"""Who a macro can be handed to, for the hand-over dialog's search: enabled
	desk users with a Jarvis role, matching ``search`` on their address or full name,
	at most ``TARGETS_MAX``. ``{users: [{user, full_name, macros}], max}``: ``macros``
	is how many they own, ``max`` the per-owner cap, so the dialog can say who is
	full. The hand-over itself checks everything again."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import MAX_MACROS_PER_OWNER
	from jarvis.permissions import JARVIS_ACCESS_ROLES

	search = _text_arg(search, _("Search"))[:SEARCH_MAX]
	users = list_filters.bounded_sql(
		"""SELECT u.name, u.full_name FROM `tabUser` u
		WHERE u.enabled = 1 AND u.user_type = 'System User'
			AND u.name NOT IN ('Administrator', 'Guest')
			AND EXISTS (SELECT 1 FROM `tabHas Role` r
				WHERE r.parent = u.name AND r.parenttype = 'User' AND r.role IN %(roles)s)
			AND (u.name LIKE %(q)s OR u.full_name LIKE %(q)s)
		ORDER BY u.full_name ASC, u.name ASC
		LIMIT %(limit)s""",
		{
			"roles": tuple(JARVIS_ACCESS_ROLES),
			"q": f"%{list_filters.escape_like(search)}%",
			"limit": TARGETS_MAX,
		},
		as_dict=True,
	)
	counts = {}
	if users:
		counts = dict(
			frappe.db.sql(
				"""SELECT owner, COUNT(*) FROM `tabJarvis Macro`
				WHERE owner IN %(users)s GROUP BY owner""",
				{"users": tuple(u.name for u in users)},
			)
		)
	return {
		"users": [
			{"user": u.name, "full_name": u.full_name or u.name, "macros": int(counts.get(u.name) or 0)}
			for u in users
		],
		"max": MAX_MACROS_PER_OWNER,
	}
