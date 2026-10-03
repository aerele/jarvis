"""SPA-facing CRUD + run/stop for customer macros.

The ``/jarvis`` Macros UI calls these whitelisted methods to manage
``Jarvis Macro`` rows (owner-scoped) and to run/stop them. Mirrors the shape of
``jarvis.chat.custom_skills_api`` (owner ``frappe.get_all``, ``{ok, data}``,
commit). Execution itself lives in ``jarvis.chat.macros``.
"""

import re

import frappe
from frappe import _

from jarvis.chat import list_filters
from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import step_skills as _step_skills
from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import step_values as _step_values
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
TURN = "Jarvis Chat Turn"


def _parse_steps(steps, *, as_posted: bool = False) -> list[dict]:
	"""Normalize the steps array (JSON string or list) into child-row dicts,
	dropping wholly empty steps. Order is preserved (child ``idx``).

	``as_posted`` keeps every tagged skill the caller named, also one that no longer
	exists or is no longer shared with them. That is what to COMPARE with the stored
	steps: the rows to store drop such a skill, so compared as stored-to-be a save
	that only renamed the macro looked like a step change (and wiped the summary) the
	first time after a tagged skill was deleted or unshared.

	A step with a label and no prompt is refused, not dropped: it used to vanish
	from the save without a word (the doctype's own "Step N has an empty prompt"
	check never saw it, the row was gone before validate ran). A step with neither
	is the blank card the form keeps to type into, and is still skipped. The Macros
	form makes the same split (``cleanSteps`` / ``stepErrors`` in MacroDetail.vue).

	The refusal carries the owner's label, and reaches an HTML toast: the form shows
	it through ``errHtml`` (frontend/src/lib/errors.js), which escapes it. It offers
	"remove the step" only when another step would be left: the form cannot remove
	a macro's only step, so there the way out is to clear the label."""
	if steps is None:
		return []
	if isinstance(steps, str):
		steps = frappe.parse_json(steps)
	if not isinstance(steps, list):
		return []
	sole = sum(isinstance(s, dict) for s in steps) == 1
	rows = []
	for i, s in enumerate(steps, start=1):
		if not isinstance(s, dict):
			continue
		prompt = (s.get("prompt") or "").strip()
		if not prompt:
			# str(): a label that is not text is still not a reason for a 500.
			label = str(s.get("label") or "").strip()
			if label:
				if sole:
					frappe.throw(
						_("Step {0} ({1}) has no prompt. Add a prompt, or clear the label.").format(i, label)
					)
				frappe.throw(
					_("Step {0} ({1}) has no prompt. Add a prompt, or remove the step.").format(i, label)
				)
			continue
		rows.append(
			{
				"label": (s.get("label") or "").strip(),
				"prompt": prompt,
				"model_override": (s.get("model_override") or "").strip(),
				"thinking_override": (s.get("thinking_override") or "").strip(),
				"skills": frappe.as_json(
					_listed_step_skills(s.get("skills")) if as_posted else _clean_step_skills(s.get("skills"))
				),
			}
		)
	return rows


# --------------------------------------------------------------------------- #
# CRUD (owner-scoped)
# --------------------------------------------------------------------------- #
def _listed_step_skills(skills) -> list[str]:
	"""One step's tagged-skills value (JSON string or list of Jarvis Custom Skill
	row-names) as a list of distinct names, in order. Whether each still exists and
	is visible to the caller is ``_clean_step_skills``."""
	if skills is None:
		return []
	if isinstance(skills, str):
		try:
			skills = frappe.parse_json(skills)
		except Exception:
			return []
	if not isinstance(skills, list):
		return []
	names, seen = [], set()
	for name in skills:
		name = (name or "").strip() if isinstance(name, str) else ""
		if not name or name in seen:
			continue
		seen.add(name)
		names.append(name)
	return names


def _clean_step_skills(skills) -> list[str]:
	"""Normalize one step's tagged-skills value (JSON string or list of Jarvis
	Custom Skill row-names) into a validated list. Only skills the current user
	OWNS or was SHARED are accepted — same visibility rule as invoking /slug in
	chat. Stored as a JSON list on the step row (child tables can't nest a
	child table, so a Table field is not an option here)."""
	names = _listed_step_skills(skills)
	if not names:
		return []
	from jarvis.chat.custom_skills_api import _skill_names_shared_with

	me = frappe.session.user
	shared = set(_skill_names_shared_with(me))
	clean = []
	for name in names:
		owner = frappe.db.get_value("Jarvis Custom Skill", name, "owner")
		if not owner:
			continue
		if owner != me and name not in shared:
			continue
		clean.append(name)
	return clean


@frappe.whitelist()
@require_jarvis_user
def list_macros() -> list[dict]:
	"""The current user's macros (no step bodies), newest first, with a count."""
	macros = frappe.get_all(
		MACRO,
		filters={"owner": frappe.session.user},
		fields=[
			"name",
			"macro_name",
			"description",
			"enabled",
			"stop_on_error",
			"schedule_enabled",
			"schedule_frequency",
			"schedule_weekday",
			"schedule_day_of_month",
			"schedule_time",
			"next_run_at",
			"last_run_at",
			"modified",
			"merged_prompt",
			"merge_status",
		],
		order_by="macro_name asc",
	)
	for m in macros:
		m["step_count"] = frappe.db.count("Jarvis Macro Step", {"parent": m["name"]})
	return macros


# --------------------------------------------------------------------------- #
# Paginated list (frozen envelope) — chat-features-page-migration-design §2.3.
# ADDITIVE: list_macros (above) STAYS for the Settings → Macro-runs dropdown.
# --------------------------------------------------------------------------- #
_MACROS_SORTABLE = {
	"macro_name": "macro_name",
	"modified": "modified",
	"last_run_at": "last_run_at",
	"next_run_at": "next_run_at",
}
_MACROS_FILTERS = {"enabled", "schedule_enabled", "schedule_frequency"}
_FREQUENCIES = {"daily", "weekly", "monthly"}


# C08-5: ``_lk`` / ``_clamp_page`` / ``_bool01`` / ``_load_filters`` /
# ``_order_by`` were copied into six list modules and had begun to drift. The
# canonical implementations now live in ``jarvis.chat.list_filters``. The private
# names stay bound here because triggers_api / dashboards_api / app_learning_api
# import them FROM this module and migrate in their own wave.
_lk = list_filters.escape_like
_clamp_page = list_filters.clamp_page
_bool01 = list_filters.bool01
_load_filters = list_filters.load_legacy_filters
_order_by = list_filters.order_by


@frappe.whitelist()
@require_jarvis_user
@list_filters.filter_errors_to_envelope
def list_macros_page(
	search: str = "",
	filters: str | dict | None = None,
	filters_v2: str | list | None = None,
	sort_field: str = "",
	sort_dir: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Owner-scoped macros, server-side search/filter/sort/paginate (no step
	bodies; ``merged_prompt`` body omitted — ``has_summary`` replaces it). Envelope
	``{rows, total, has_more, start, page_length}``.

	``filters_v2`` (plan 08 §6.2) is ADDITIVE: the canonical clause list, validated
	and compiled against this caller's schema by ``jarvis.chat.list_filters``.
	Legacy ``filters`` keeps working unchanged for the compatibility window. The
	owner predicate below is a server-authored condition on the query object, so a
	user clause can only ever narrow it — never widen it (C08-5).
	"""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _MACROS_FILTERS)

	q = list_filters.new_query("macros")
	q.server_condition("owner = %(me)s", me=me)

	if search:
		q.server_condition("(macro_name LIKE %(q)s OR description LIKE %(q)s)", q=f"%{_lk(search)}%")
	if "enabled" in f:
		q.server_condition("enabled = %(enabled)s", enabled=_bool01(f["enabled"]))
	if "schedule_enabled" in f:
		q.server_condition(
			"schedule_enabled = %(schedule_enabled)s", schedule_enabled=_bool01(f["schedule_enabled"])
		)
	if "schedule_frequency" in f:
		if f["schedule_frequency"] not in _FREQUENCIES:
			frappe.throw(_("Invalid schedule_frequency filter."))
		q.server_condition(
			"schedule_frequency = %(schedule_frequency)s", schedule_frequency=f["schedule_frequency"]
		)

	q.apply(filters_v2)
	where = q.where()
	params = q.params({"start": start, "page_length": pl})
	order = _order_by(sort_field, sort_dir, _MACROS_SORTABLE, "macro_name", "asc")

	total = list_filters.bounded_sql(f"SELECT COUNT(*) FROM `tabJarvis Macro` WHERE {where}", params)[0][0]
	rows = list_filters.bounded_sql(
		f"""SELECT name, macro_name, description, enabled, stop_on_error, skip_confirmation,
		schedule_enabled, schedule_frequency, schedule_weekday, schedule_day_of_month,
		schedule_time, next_run_at,
		last_run_at, modified, merge_status,
		CASE WHEN TRIM(COALESCE(merged_prompt, '')) != '' THEN 1 ELSE 0 END AS has_summary
		FROM `tabJarvis Macro`
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	names = [r.name for r in rows]
	step_counts: dict = {}
	if names:
		for x in frappe.db.sql(
			"""SELECT parent, COUNT(*) n FROM `tabJarvis Macro Step`
			WHERE parent IN %(names)s GROUP BY parent""",
			{"names": tuple(names)},
			as_dict=True,
		):
			step_counts[x.parent] = x.n
	from jarvis.chat.macro_scheduler import is_retry_pending

	now = frappe.utils.now_datetime()
	last_runs = _last_runs(names, me)
	for r in rows:
		r["step_count"] = step_counts.get(r.name, 0)
		r["last_run"] = last_runs.get(r.name)
		# Pure arithmetic on the row, no query: see get_macro.
		r["next_run_is_retry"] = int(is_retry_pending(r, now))
		# Time renders as a timedelta over raw SQL; stringify for a stable payload.
		if r.get("schedule_time") is not None:
			r["schedule_time"] = str(r["schedule_time"])

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


_LAST_RUN_FIELDS = ("status", "error", "started_at", "finished_at", "conversation", "trigger")


def _last_runs(macro_names: list[str], owner: str) -> dict:
	"""``{macro: its most recent run}`` for the macros named: how the last run went,
	for the list and the form. Nothing else on either screen says whether a macro
	worked the last time it ran; ``last_run_at`` is stamped by the scheduler only and
	carries no outcome.

	One query for a whole page. Scoped to ``owner``'s run rows, which is every run of
	their macro by construction. The conversation is handed back only to that owner:
	someone overseeing the macro may know that a run failed, not read the chat.

	A row kept for the budget after its macro was deleted (``delete_macro``) has no
	macro, so ``macro IN`` never matches it."""
	if not macro_names:
		return {}
	rows = frappe.db.sql(
		"""SELECT r.name, r.macro, r.status, r.error, r.started_at, r.finished_at,
			r.conversation, r.`trigger`
		FROM `tabJarvis Macro Run` r
		JOIN (
			SELECT macro, MAX(creation) AS latest FROM `tabJarvis Macro Run`
			WHERE macro IN %(names)s AND owner = %(owner)s GROUP BY macro
		) m ON m.macro = r.macro AND m.latest = r.creation
		WHERE r.owner = %(owner)s""",
		{"names": tuple(macro_names), "owner": owner},
		as_dict=True,
	)
	is_owner = frappe.session.user == owner
	out: dict = {}
	for r in rows:
		run = {f: r.get(f) for f in _LAST_RUN_FIELDS}
		run["error"] = run["error"] or ""
		run["conversation"] = (run["conversation"] or "") if is_owner else ""
		out[r.macro] = run
	return out


@frappe.whitelist()
@require_jarvis_user
def get_macro(name: str) -> dict:
	"""One macro incl. its ordered steps (owner-gated)."""
	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("read")  # get_doc alone doesn't enforce if_owner
	from jarvis.chat.macro_scheduler import is_retry_pending

	return {
		"name": doc.name,
		"macro_name": doc.macro_name,
		# A failed scheduled run is retried by writing the retry time to next_run_at;
		# this tells the form that "Next run" is that retry, not the schedule's own slot.
		"next_run_is_retry": int(is_retry_pending(doc)),
		"description": doc.description or "",
		"enabled": int(doc.enabled or 0),
		"stop_on_error": int(doc.stop_on_error or 0),
		"skip_confirmation": int(doc.skip_confirmation or 0),
		"schedule_enabled": int(doc.schedule_enabled or 0),
		"schedule_frequency": doc.schedule_frequency or "daily",
		"schedule_weekday": doc.schedule_weekday or None,
		"schedule_day_of_month": doc.schedule_day_of_month or None,
		# `is None`, not truthiness: midnight is timedelta(0), which is falsy, so
		# `or ""` sent the form "" and it fell back to (then saved) its 09:00 default.
		"schedule_time": "" if doc.schedule_time is None else str(doc.schedule_time),
		"next_run_at": str(doc.next_run_at or ""),
		"merged_prompt": doc.merged_prompt or "",
		"merge_status": doc.merge_status or "",
		"schedule_blocked_reason": _schedule_block_reason(doc.owner),
		"last_run": _last_runs([doc.name], doc.owner).get(doc.name),
		"steps": [
			{
				"label": s.label or "",
				"prompt": s.prompt or "",
				"model_override": s.model_override or "",
				"thinking_override": s.thinking_override or "",
				"skills": _step_skills(s),
			}
			for s in (doc.steps or [])
		],
	}


def _refuse_schedule_for_barred_owner(owner: str, schedule_enabled) -> None:
	"""Refuse, at SAVE, a schedule the sweep will never run.

	``macro_scheduler`` refuses to bind an unattended turn to Administrator, Guest or
	a disabled user (#469). The save accepted the schedule regardless and computed a
	"Next run", so the macro looked scheduled and every slot was then skipped
	(admin-v2#675). Shares the scheduler's own rule, so the two cannot disagree.

	Only a schedule that is ON is refused: an owner the rule bars can still keep and
	run an unscheduled macro, and can switch an existing schedule off."""
	if not frappe.utils.cint(schedule_enabled):
		return
	reason = _schedule_block_reason(owner)
	if reason:
		frappe.throw(reason, title=_("This macro cannot be scheduled"))


def _schedule_block_reason(owner: str) -> str:
	"""Why a macro owned by ``owner`` cannot be on a schedule, or "" when it can.

	The ONE wording for it: the save refusal above throws it and ``get_macro`` hands
	it to the form, so what the form warns and what the server refuses cannot drift.
	Judged from the macro's OWNER, the identity the scheduler would run as, never
	from whoever is looking at the form."""
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	if owner in ("Administrator", "Guest"):
		return _(
			"Scheduled macros cannot run as {0}. Create the macro from a named user's "
			"account to schedule it, or switch the schedule off to save this one."
		).format(owner)
	# The same two checks, in the same order, as macro_scheduler._sweep_one.
	if not is_valid_unattended_owner(owner):
		return _(
			"Scheduled macros cannot run for {0} because the account is disabled or no "
			"longer exists. Switch the schedule off to save this macro."
		).format(owner)
	if not has_jarvis_access(owner):
		return _(
			"Scheduled macros cannot run for {0} because the account no longer has "
			"access to Jarvis. Switch the schedule off to save this macro."
		).format(owner)
	return ""


@frappe.whitelist()
@require_jarvis_user
def create_macro(
	macro_name: str,
	description: str = "",
	steps: str | list | None = None,
	enabled: int = 1,
	stop_on_error: int = 1,
	skip_confirmation: int = 0,
	schedule_enabled: int = 0,
	schedule_frequency: str = "daily",
	schedule_time: str | None = None,
	schedule_weekday: str | None = None,
	schedule_day_of_month: int | None = None,
) -> dict:
	"""Create a macro. Validation (name/steps/caps, and the two schedule anchors'
	ranges) runs in the doctype validate(). Per-step tagged skills arrive INSIDE
	each step dict (``steps[].skills``). Arming (``skip_confirmation`` 0 -> 1) is
	admin-gated in the doctype validate()."""
	_refuse_schedule_for_barred_owner(frappe.session.user, schedule_enabled)
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": macro_name,
			"description": description or "",
			"enabled": int(enabled or 0),
			"stop_on_error": int(stop_on_error or 0),
			"skip_confirmation": int(skip_confirmation or 0),
			"schedule_enabled": int(schedule_enabled or 0),
			"schedule_frequency": schedule_frequency or "daily",
			"schedule_time": schedule_time or None,
			"schedule_weekday": schedule_weekday or None,
			"schedule_day_of_month": frappe.utils.cint(schedule_day_of_month) or None,
			"steps": _parse_steps(steps),
		}
	)
	doc.insert()
	frappe.db.commit()
	return {
		"ok": True,
		"data": {"name": doc.name, "macro_name": doc.macro_name, "summarize": len(doc.steps) >= 2},
	}


@frappe.whitelist()
@require_jarvis_user
def update_macro(
	name: str,
	macro_name: str | None = None,
	description: str | None = None,
	steps: str | list | None = None,
	enabled: int | None = None,
	stop_on_error: int | None = None,
	skip_confirmation: int | None = None,
	schedule_enabled: int | None = None,
	schedule_frequency: str | None = None,
	schedule_time: str | None = None,
	schedule_weekday: str | None = None,
	schedule_day_of_month: int | None = None,
	merged_prompt: str | None = None,
	merged_prompt_edited: int | None = None,
) -> dict:
	"""Update provided fields of a macro (owner-gated). When ``steps`` is given it
	replaces the whole ordered list (per-step skills ride inside each step dict).

	The summary follows what the save actually CHANGED, decided on the server and not
	by the caller. The form posts ``steps`` on every save, so "steps were sent" used to
	be read as "steps changed": a rename, a reschedule or the enabled switch wiped the
	stored summary, and during a summary the "summarizing" mark with it, so the one in
	flight never landed. Posting the summary back alongside did the same to the mark.

	* Steps as stored, summary untouched: the three summary fields stay exactly as
	  they are, "summarizing" included.
	* Summary edited by hand: it is saved, whether or not the steps changed with it.
	* Steps changed, summary untouched: the summary is stale and is cleared. The
	  controller does that (``JarvisMacro._clear_summary_of_changed_steps``), for
	  every writer and not only this endpoint.

	"Edited by hand" is what the form SAYS in ``merged_prompt_edited``, and the text
	must differ from the stored one too. The form sends the summary it shows on every
	save that leaves the steps alone (a server from before this change clears the
	summary when it is missing), and what it shows may be old: the summary landed, or
	was written in another tab, after the form loaded. Read as an edit, that old text
	replaced the fresh summary. A form from before the argument existed does not send
	it; for that one "sent, and different from the stored text" is the rule.

	``summarize`` in the answer tells the form whether to start a new summary: only
	after a real step change that left two or more steps, and not when the owner wrote
	the summary in the same save. A summary that is empty or failed is not a reason:
	that started a model call on every save of such a macro."""
	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("write")  # owner-gate (save enforces too; explicit for clarity)
	# The save below writes every column. On a document read before a summary was
	# started by another request it wrote the old status back over "summarizing".
	doc = _locked_for_writing(doc)
	if macro_name is not None:
		doc.macro_name = macro_name
	if description is not None:
		doc.description = description
	if enabled is not None:
		doc.enabled = int(enabled)
	if stop_on_error is not None:
		doc.stop_on_error = int(stop_on_error)
	if skip_confirmation is not None:
		# The doctype validate() admin-gates a 0 -> 1 transition; a non-admin flip raises.
		doc.skip_confirmation = int(skip_confirmation)
	if schedule_enabled is not None:
		doc.schedule_enabled = int(schedule_enabled)
	if schedule_frequency is not None:
		doc.schedule_frequency = schedule_frequency
	if schedule_time is not None:
		doc.schedule_time = schedule_time or None
	if schedule_weekday is not None:
		doc.schedule_weekday = schedule_weekday or None
	if schedule_day_of_month is not None:
		# cint (not the raw arg) so a stray "" or a non-numeric SPA payload lands
		# on the doctype's own 1-31 validate() rather than a TypeError here.
		doc.schedule_day_of_month = frappe.utils.cint(schedule_day_of_month) or None
	if steps is not None:
		rows = _parse_steps(steps)
		# Unchanged steps are left as they are: the rows are not deleted and inserted
		# again on every save.
		if _step_values(_parse_steps(steps, as_posted=True)) != _step_values(doc.steps):
			doc.set("steps", rows)
	written = (merged_prompt or "").strip()
	edited_by_hand = (
		merged_prompt is not None
		and written != (doc.merged_prompt or "").strip()
		and (merged_prompt_edited is None or bool(frappe.utils.cint(merged_prompt_edited)))
	)
	if edited_by_hand:
		# The text only. What follows from it (the status, and giving up a summary
		# that is being written) is the controller's rule, the same for every writer:
		# ``JarvisMacro._settle_summary_written_in_this_save``.
		doc.merged_prompt = written
	_refuse_schedule_for_barred_owner(doc.owner, doc.schedule_enabled)
	doc.save()
	frappe.db.commit()
	# An emptied summary is not one the owner wrote: with changed steps it is the same
	# as the controller's clear, and a new summary follows.
	summarize = bool(doc.flags.steps_changed) and len(doc.steps) >= 2 and not (edited_by_hand and written)
	return {"ok": True, "data": {"name": doc.name, "modified": str(doc.modified), "summarize": summarize}}


def _locked_for_writing(doc):
	"""The macro again, read under its row lock, for a writer that must see what every
	other request has committed (the summary fields are written by the engine and by
	``summarize_macro`` while a form is open).

	The lock has to be the first read of its transaction: a locking read on a snapshot
	opened by earlier reads fails outright when another request has committed to the
	row since (see jarvis.chat.txn). So the transaction is ended first; callers come
	here before they have written anything. The macro is read by its loaded name
	(the one whose permission was checked), never the raw argument."""
	from jarvis.chat import txn

	txn.fresh_snapshot(owned=True)
	frappe.db.get_value(MACRO, doc.name, "name", for_update=True)
	return frappe.get_doc(MACRO, doc.name)


# How many times a delete stops the macro's live runs and looks again before it
# gives up. A second round needs a run to START in the instant between the stop and
# the lock; three in a row is a macro being started in a loop.
_DELETE_STOP_ROUNDS = 3


class MacroBusyError(frappe.ValidationError):
	"""The macro kept starting runs while it was being deleted."""


def _refuse_busy_delete(macro_name: str, stopped: int):
	"""A delete that gave up. The runs it stopped stay stopped (each stop committed),
	so the refusal says so, and says what to do. It is also the one trace an operator
	gets of a macro being started in a loop: a ValidationError is not logged."""
	frappe.log_error(
		title=f"jarvis.chat.macros_api.delete_refused_busy: {macro_name}",
		message=(
			f"macro {macro_name}: a run was live again after each of {_DELETE_STOP_ROUNDS} rounds of "
			f"stops; {stopped} run(s) were stopped; the macro was not deleted."
		),
	)
	frappe.db.commit()  # the throw below rolls the request back
	if stopped == 1:
		said = _("The run was stopped, but another run started before the macro could be deleted.")
	elif stopped:
		said = _("{0} runs were stopped, but another run started before the macro could be deleted.").format(
			stopped
		)
	else:
		said = _("Another run started before the macro could be deleted.")
	# ``frappe.throw``, not a bare raise: only a thrown message reaches the client.
	frappe.throw(f"{said} {_('Delete it again.')}", MacroBusyError)


@frappe.whitelist()
@require_jarvis_user
def delete_macro(name: str) -> dict:
	"""Delete a macro row (owner-gated). Returns ``{"ok", "stopped_runs"}``.

	**A live run is stopped first.** Deleting the row of a run that is still going
	did not stop it: its steps kept running, in a chat that stayed armed with no run
	row left to disarm it. So, in this order:

	1. Stop each run that is ``running`` or parked for capacity (``macros._stop_run``:
	   the step that has not started is cancelled, the chat is disarmed and told
	   "Stopped: its macro was being deleted."). Each stop commits, so no lock is
	   held here.
	2. Take the macro's row lock on a fresh snapshot and LOOK AGAIN. ``run_macro``
	   takes the same lock before it inserts a run, so a run that started during 1 is
	   either committed and seen now, or still waiting for the lock. One found: let go
	   and go back to 1. After ``_DELETE_STOP_ROUNDS`` the delete is refused
	   (``MacroBusyError``): the refusal says the runs were stopped, and is logged.
	3. Still under the lock, with no commit in between, delete the run rows and the
	   macro. A run waiting for the lock then finds no macro.

	The stops are committed whatever happens after them. Any exception that leaves
	this function carries ``stopped_runs`` (how many it had stopped), which is how a
	bulk delete counts the runs of a macro it then had to skip.

	Its Macro Run history rows link the macro and would block the delete
	(LinkExistsError), so they go first — they are just execution history; the run
	conversations themselves stay.

	**The month's scheduled steps are kept.** The unattended budget is added up from
	run rows, so removing them all gave the owner the month back: delete, create
	again, start from zero. The finished scheduled rows of the current month stay,
	with no macro on them (``macros.drop_runs_of_deleted_macro``); for the user the
	run history is gone all the same, since nothing that lists runs shows a row with
	no macro (``_HAS_MACRO``). The hourly macro job removes them after the month ends.

	The runs go in ONE statement. A ``delete_doc`` per run cost about ten queries
	and a queued job each, inside the request, for a table that grows without
	bound. The filter is the LOADED document's name (the one whose permission was
	just checked), never the raw argument.

	What the set-based delete leaves out, and why that is fine here:

	* run rows have no controller hooks, no child rows, no attachments and nothing
	  links to them (a chat message's ``ref_name`` is plain Data);
	* no ``Deleted Document`` copy is kept of each run (they could not be restored
	  without their macro), and no "Deleted" feed Comment is written per run. What
	  remains on record is the macro's own Deleted Document row;
	* ``on_trash`` doc events do not fire per run, so a Jarvis Trigger someone
	  pointed at Jarvis Macro Run deletions is not run for them;
	* ``delete_doc``'s queued sweep of rows that point AT the deleted document
	  (Comment, Notification Log, ToDo, DocShare, Document Follow, View Log,
	  Communication and Activity Log references) does not run. Nothing in this app
	  writes any of those against a run: the Notification Log the engine sends its
	  owner (``macros.notify_owner``) names no document, the Macros pages offer
	  comments, assignment and attachments on the MACRO only, and a run row is
	  read-only to every role. One could exist only if somebody opened a run in
	  Desk and commented on or followed it by hand. Such a row is then an orphan
	  that points at a name which no longer resolves: it is shown nowhere (the
	  timeline it belonged to is gone) and nothing reads it. Sweeping eight tables
	  on every macro delete for that is not worth the queries."""
	from jarvis.chat import macros

	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("write")  # owner-gate before touching linked runs
	stopped = 0
	try:
		for _round in range(_DELETE_STOP_ROUNDS):
			stopped += macros.stop_runs_of_macro(
				doc.name, reason=macros._MACRO_DELETED_ERROR, by=frappe.session.user
			)
			if not macros._lock_macro_row(doc.name):
				frappe.db.commit()
				frappe.throw(_("This macro was already deleted."), frappe.DoesNotExistError)
			if macros.live_runs_of(doc.name):
				frappe.db.commit()  # let go of the macro row: a stop waits for a run lock
				continue
			macros.drop_runs_of_deleted_macro(doc.name)
			frappe.delete_doc(MACRO, doc.name)  # honors if_owner
			frappe.db.commit()
			return {"ok": True, "stopped_runs": stopped}
		_refuse_busy_delete(doc.name, stopped)
	except Exception as e:
		e.stopped_runs = stopped
		raise


# Why a bulk delete left a macro in place: the code the client keys on, and the
# sentence it shows after the macro's name.
_SKIP_MESSAGE = {
	"not owner": "It belongs to someone else.",
	"not found": "It was already deleted.",
	"not permitted": "You are not allowed to delete it.",
	"running": "Its run was stopped, but another run started before it could be deleted. Delete it again.",
	"busy": "It is busy right now. Try again in a moment.",
	"error": "It could not be deleted. Try again.",
}


def _skip_reason(e: Exception) -> str:
	from jarvis.chat import macros

	for exc, reason in (
		(frappe.DoesNotExistError, "not found"),
		(frappe.PermissionError, "not permitted"),
		(MacroBusyError, "running"),
		(macros.MacroRowBusyError, "busy"),
	):
		if isinstance(e, exc):
			return reason
	return "error"


@frappe.whitelist()
@require_jarvis_user
def delete_macros_bulk(names: str | list | None = None) -> dict:
	"""Bulk delete macros the caller OWNS (DESIGN-V3 §8.3 / D20). ``names`` is a
	JSON array of macro row-names. Reuses the ``delete_macro`` path per row so
	each macro's live runs are stopped and its Run history goes first
	(LinkExistsError otherwise). Per-row try/except: foreign rows skip with
	``not owner``, one bad row never aborts the batch. Returns
	``{deleted, skipped: [{name, title, reason, message}], stopped_runs}``:
	``title`` is the macro's own name and ``message`` a sentence for the user;
	``stopped_runs`` includes the runs of a macro that was then skipped.

	A row that fails is ROLLED BACK before the next one. Without that its macro row
	lock, and whatever its delete had half done, were carried into the next macro's
	stops: that macro waited for run locks while holding the first one's row (the
	lock order ``macros._lock_macro_row`` forbids), and its first commit then made
	the half-done delete permanent (run history gone, macro still there)."""
	raw = frappe.parse_json(names) if isinstance(names, str) else (names or [])
	items = [str(n) for n in raw if n] if isinstance(raw, list) else []
	me = frappe.session.user
	deleted = 0
	stopped_runs = 0
	skipped: list[dict] = []

	def skip(name: str, title: str, reason: str) -> None:
		skipped.append({"name": name, "title": title, "reason": reason, "message": _(_SKIP_MESSAGE[reason])})

	for n in items:
		title = n
		try:
			doc = frappe.get_doc(MACRO, n)
			title = doc.macro_name or n
			if doc.owner != me:
				skip(n, title, "not owner")
				continue
			# stops its live runs, clears run history, then the macro
			stopped_runs += delete_macro(n).get("stopped_runs") or 0
			deleted += 1
		except Exception as e:
			frappe.db.rollback()
			frappe.clear_messages()  # the thrown sentence is reported per row, below
			stopped_runs += getattr(e, "stopped_runs", 0)
			reason = _skip_reason(e)
			if reason == "error":
				# Never leak internal exception text to the client: log server-side.
				frappe.log_error(title="Jarvis: bulk macro delete failed", message=frappe.get_traceback())
				frappe.db.commit()  # a later row's rollback must not take the log with it
			skip(n, title, reason)
	frappe.db.commit()
	return {"deleted": deleted, "skipped": skipped, "stopped_runs": stopped_runs}


# --------------------------------------------------------------------------- #
# Run / stop
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def run_macro(name: str) -> dict:
	"""Start a macro now (manual trigger). Returns the run + conversation."""
	refuse_in_tool_dispatch()
	from jarvis.chat import macros

	return macros.run_macro(name, trigger="manual")


@frappe.whitelist()
@require_jarvis_user
def stop_macro_run(run: str) -> dict:
	"""Stop an in-progress run (owner-gated)."""
	from jarvis.chat import macros

	return macros.stop_macro_run(run)


@frappe.whitelist()
@require_jarvis_user
def get_macro_run(run: str) -> dict:
	"""Current state of a run (for polling as a socketio fallback)."""
	doc = frappe.get_doc(RUN, run)
	doc.check_permission("read")  # owner-gate
	if not doc.macro:
		# A row kept for the budget after its macro was deleted (``delete_macro``).
		# As a run it is gone, which is what this said when the row was deleted.
		frappe.throw(_("Jarvis Macro Run {0} not found").format(run), frappe.DoesNotExistError)
	return {
		"name": doc.name,
		"macro": doc.macro,
		"conversation": doc.conversation,
		"status": doc.status,
		"current_step": doc.current_step,
		"total_steps": doc.total_steps,
		"error": doc.error or "",
	}


# --------------------------------------------------------------------------- #
# Run history dashboard (settings → Macro runs)
# --------------------------------------------------------------------------- #
_RUN_STATUSES = {"queued", "running", "waiting_capacity", "completed", "failed", "stopped"}
# A run row with no macro is not run history: ``delete_macro`` keeps the month's
# scheduled rows of a deleted macro so the unattended budget still counts them, and
# clears their macro. Every query that shows or counts runs for a person carries this.
_HAS_MACRO = "macro IS NOT NULL"


@frappe.whitelist()
@require_jarvis_user
def list_macro_runs(status: str = "", macro: str = "", limit: int | str = 30, start: int | str = 0) -> dict:
	"""The current user's macro runs, newest-first, for the history dashboard.

	Joins the macro for its display name and computes each run's duration in
	seconds. Optional filters: ``status`` (a run status) and ``macro`` (a macro
	row-name). Owner-scoped in SQL so another user's runs are never returned.
	Fetches ``limit + 1`` rows to report ``has_more`` for the SPA's Load more.
	``total`` (COUNT under the same filters) rides along for the "N of M"
	footer (DESIGN-V3 D38 — additive)."""
	limit = max(1, min(int(limit or 30), 100))
	start = max(0, int(start or 0))
	conditions = ["r.owner = %(owner)s", f"r.{_HAS_MACRO}"]
	params = {"owner": frappe.session.user, "limit": limit + 1, "start": start}
	if status and status in _RUN_STATUSES:
		conditions.append("r.status = %(status)s")
		params["status"] = status
	if macro:
		conditions.append("r.macro = %(macro)s")
		params["macro"] = macro
	where = " AND ".join(conditions)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabJarvis Macro Run` r WHERE {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""
		SELECT r.name, r.macro, COALESCE(m.macro_name, r.macro) AS macro_name,
		       r.conversation, r.status, r.current_step, r.total_steps,
		       r.`trigger` AS `trigger`, r.creation, r.started_at, r.finished_at, r.error, r.run_mode,
		       CASE WHEN r.started_at IS NOT NULL AND r.finished_at IS NOT NULL
		            THEN TIMESTAMPDIFF(SECOND, r.started_at, r.finished_at)
		       END AS duration_s
		FROM `tabJarvis Macro Run` r
		LEFT JOIN `tabJarvis Macro` m ON m.name = r.macro
		WHERE {where}
		ORDER BY r.creation DESC
		LIMIT %(limit)s OFFSET %(start)s
		""",
		params,
		as_dict=True,
	)
	return {"runs": rows[:limit], "has_more": len(rows) > limit, "total": total}


@frappe.whitelist()
@require_jarvis_user
def macro_run_stats() -> dict:
	"""Summary tiles for the dashboard: counts per status, success rate, and the
	last run time — all owner-scoped.

	Success rate = completed / every run that ended with an outcome. A run the USER
	stopped is their cancellation, not an outcome, and stays out of the rate (it is
	still counted in ``total``). A run the ENGINE stopped is one: it carries a reason
	(a step is waiting on a confirmation, so its write was never applied) and counts
	against the rate like a failure. Left out, a macro that stops at the same card
	every day would read 100%."""
	owner = {"owner": frappe.session.user}
	rows = frappe.db.sql(
		f"""SELECT status, COUNT(*) AS n FROM `tabJarvis Macro Run`
		WHERE owner = %(owner)s AND {_HAS_MACRO} GROUP BY status""",
		owner,
		as_dict=True,
	)
	by = {r.status: r.n for r in rows}
	completed = by.get("completed", 0)
	failed = by.get("failed", 0)
	stopped_with_reason = frappe.db.sql(
		f"""SELECT COUNT(*) FROM `tabJarvis Macro Run`
		WHERE owner = %(owner)s AND {_HAS_MACRO} AND status = 'stopped' AND IFNULL(error, '') != ''""",
		owner,
	)[0][0]
	finished = completed + failed + stopped_with_reason
	last = frappe.db.sql(
		f"SELECT MAX(creation) FROM `tabJarvis Macro Run` WHERE owner = %(owner)s AND {_HAS_MACRO}", owner
	)[0][0]
	return {
		"total": sum(by.values()),
		"completed": completed,
		"failed": failed,
		"running": by.get("running", 0),
		"queued": by.get("queued", 0),
		"stopped": by.get("stopped", 0),
		"success_rate": round(completed * 100 / finished) if finished else None,
		"last_run_at": str(last) if last else "",
	}


# --------------------------------------------------------------------------- #
# Macro merge — summarize a 2+ step sequence into one prompt (spec:
# docs/superpowers/specs/2026-07-03-macro-merge-design.md). The LLM does the
# merging via the persona /macro-merge skill in a throwaway archived
# conversation; this endpoint kicks it off. The SPA reads the result straight
# off the Macro doc (merge_status/merged_prompt) once the worker's advance
# hook applies it — there is no separate poll/apply/discard surface.
# --------------------------------------------------------------------------- #
# NOTE: _MERGE_RE has no caller in THIS module — jarvis.chat.macros
# (_apply_merge_after_turn, off-limits here) lazily imports it to parse the
# assistant reply's ```jarvis-macro-merge``` block. Keep it even though it
# looks locally dead.
_MERGE_RE = re.compile(r"```jarvis-macro-merge[ \t]*\n([\s\S]*?)```")

_MERGE_INSTRUCTION = (
	"Summarize this macro's steps into ONE coherent self-contained prompt — a "
	"genuine rewrite that reads as a single ask, NOT the steps restated as a "
	"numbered list. Keep the execution order and weave every inter-step "
	'dependency into the prose ("...and from those results..."). Keep every '
	"concrete detail (filters, dates, names, quantities, formats). Reply with "
	"one short lead-in line and exactly one fenced ```jarvis-macro-merge``` "
	'block holding JSON: {"mergeable": bool, "reason": str, "merged_prompt": '
	'str, "dependencies": [{"step": int, "uses": [int]}]}. Set '
	"mergeable=false when merging would lose a user review checkpoint before "
	"a data change. Steps:\n\n"
)


def _summary_block_message(reason: str) -> str:
	"""What a refused summary tells the owner, by ``macros.entitlement_block`` reason.
	The run's own sentences (``macros._BLOCK_MESSAGE``) end in "this macro cannot
	run", which is not what was asked for here. Literal strings inside ``_()``: a
	sentence passed to it from a variable never reaches the translation files."""
	return {
		"usage_limit": _("You have reached your monthly usage limit, so this macro cannot be summarized."),
		"subscription_suspended": _(
			"Your subscription does not currently include chat, so this macro cannot be summarized."
		),
		"release_update_required": _(
			"A Jarvis update is rolling out. Try the summary again in a few minutes."
		),
		"workspace_resetting": _("Your workspace is being rebuilt. Try the summary again in a few minutes."),
		"maintenance": _("Jarvis is being upgraded. Try the summary again in a few minutes."),
		"llm_not_configured": _("Connect an AI model before summarizing a macro."),
	}.get(reason) or _("This macro cannot be summarized right now.")


def _abandon_summary(macro, conversation: str, *, replaced: str = "") -> None:
	"""No summary turn is coming: take back the "summarizing" mark and the throwaway
	chat made for it. Left in place, Run stays refused on a summary nothing will land.

	``replaced``: the chat of the summary this one was started to replace (a forced
	Re-summarize). The new one never started, so the old one is not given up after
	all: the mark goes back to it and it lands as it would have. Unless it has ended
	meanwhile (see ``_may_still_land``): then the mark is cleared like any other, and
	its chat removed. The stored summary text is not touched on any path here; only a
	summary that lands replaces it.

	The mark first, committed on its own: when it shared a transaction with the chat's
	delete, a delete that failed rolled the clearing back too and left "summarizing"
	for good. It is changed only while it still names THIS chat. Best-effort, and it
	never raises: the caller is already on its way out with the real answer."""
	try:
		put_back = bool(replaced) and _may_still_land(replaced)
		frappe.db.set_value(
			MACRO,
			{"name": macro.name, "merge_conversation": conversation},
			{"merge_status": "pending", "merge_conversation": replaced}
			if put_back
			else {"merge_status": "", "merge_conversation": ""},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		put_back = True  # unknown: leave the replaced chat alone
	_delete_summary_chat(conversation)
	if replaced and not put_back:
		_delete_summary_chat(replaced, owned_by=macro.owner)


def _may_still_land(conversation: str) -> bool:
	"""Whether a summary's chat can still produce a result: it exists, and its turn
	has not ended. A chat with no Turn row at all counts as "can": the legacy
	transport writes none, so there the answer is not knowable and the summary gets
	the benefit of the doubt."""
	if not frappe.db.exists("Jarvis Conversation", conversation):
		return False
	from jarvis.chat import turn_state

	return bool(turn_state.unfinished_turn_state(conversation)) or not frappe.db.exists(
		TURN, {"conversation": conversation}
	)


def _delete_summary_chat(conversation: str, *, owned_by: str | None = None) -> None:
	"""Remove a summary's throwaway chat. Best-effort, and it never raises.

	Never while a reply of it is in progress (``admission.reply_in_progress``, the rule
	"Delete all chat history" goes by too). Deleting a conversation deletes its Turn
	rows (``admission.on_conversation_trash``), and the pump reads a Turn row that is
	gone as a lost lease (``pump._epoch_lost``): its hop ends with no successor, and
	every live reply on the site stops until the lease runs out and something starts a
	new hop (30 seconds to 5 minutes). Such a chat is left to finish; it stays behind,
	archived, like the chat of a summary a step change gave up.

	``owned_by``: delete it only if it belongs to that user. The delete ignores
	permissions, so a chat named by a macro's stored link is removed only when it is
	the macro owner's, the rule the landing follows
	(``macros._drop_summary_link_unless_one_owner``)."""
	try:
		from jarvis.chat import admission

		if owned_by and frappe.db.get_value("Jarvis Conversation", conversation, "owner") != owned_by:
			return
		if admission.reply_in_progress(conversation):
			return
		frappe.db.delete("Jarvis Chat Message", {"conversation": conversation})
		frappe.delete_doc("Jarvis Conversation", conversation, ignore_permissions=True, force=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()


@frappe.whitelist()
@require_jarvis_user
def summarize_macro(name: str, force: int = 0) -> dict:
	"""Kick off the merge: throwaway archived conversation + one agent turn
	that invokes /macro-merge over the macro's steps. Returns the conversation
	the summary is written in. The macro's steps are untouched here.

	Asked again while a summary is already being written, it answers with that one
	and starts nothing: the first chat would be orphaned and its result thrown away,
	for a second model call. The macro's row is locked across "is one pending?" and
	"mark it pending", so two requests at once cannot both answer no.

	``force`` is the owner asking for a new summary by name (Re-summarize in the
	menu; the call the form makes after a save never passes it). The pending one is
	given up and a new one started. Without that, a "summarizing" mark nothing will
	ever land (the worker died mid-turn, or the request was cut off between the mark
	and the dispatch) answered "that one is pending" for good, with Run refused
	behind it and no way out but to change a step.

	Giving one up is moving the mark to the new chat: the old one's result then
	matches no macro and is ignored. Its chat is removed only once the new summary
	has started, and never while its own turn is live (``_delete_summary_chat``); a
	live one is left to run to its end, it is not cancelled. A new summary that could
	not start gives nothing up (``_abandon_summary``)."""
	from jarvis.chat import macros

	doc = frappe.get_doc(MACRO, name)
	# Gated on WRITE, not read: a summary lands on the macro (merged_prompt,
	# merge_status), and its turn runs in a chat created for whoever asks. On read
	# alone, anyone who could SEE a macro could overwrite its owner's summary, and
	# the throwaway chat belonged to the caller, not the macro's owner.
	doc.check_permission("write")
	macros.refuse_acting_for_barred_owner(doc.owner)
	# The gate every other macro turn passes (#468). A summary is one turn a person
	# asked for, so it is priced like a manual run of a single step: the token caps
	# apply, the scheduled-run budget does not. Asked BEFORE the row is locked: on a
	# cold cache it calls the control plane (seconds), it depends on the owner alone,
	# and under the lock a save of this macro and the landing of a summary waited on
	# it. Acted on below, before anything is created.
	blocked = macros.entitlement_block(doc.owner, steps=1, trigger="manual")
	doc = _locked_for_writing(doc)
	steps = doc.steps or []
	if len(steps) < 2:
		frappe.throw(_("Nothing to merge — the macro has fewer than 2 steps."))
	pending = doc.merge_conversation if (doc.merge_status or "") == "pending" else ""
	# A mark whose chat is gone has nothing coming for it: that one is replaced too.
	if pending and not frappe.utils.cint(force) and frappe.db.exists("Jarvis Conversation", pending):
		return {"ok": True, "conversation": pending}
	if blocked:
		frappe.throw(_summary_block_message(blocked))
	conv = frappe.get_doc(
		{
			"doctype": "Jarvis Conversation",
			"title": f"Merge: {doc.macro_name}"[:140],
			"status": "Active",  # enqueue against Active; hidden right after
			"agent_initiated": 1,  # a merge run log, not a chat the user started
		}
	)
	conv.flags.ignore_permissions = True
	conv.insert()
	# The throwaway chat belongs to the macro's OWNER, like every other row a macro
	# creates (see run_macro). The engine only lands a summary from a conversation the
	# macro's owner owns, so a chat left with Administrator would never land.
	if doc.owner != frappe.session.user:
		frappe.db.set_value("Jarvis Conversation", conv.name, "owner", doc.owner, update_modified=False)
	payload = [{"n": i + 1, "label": s.label or "", "prompt": s.prompt or ""} for i, s in enumerate(steps)]
	from jarvis.chat import api as chat_api

	prompt = _MERGE_INSTRUCTION + frappe.as_json(payload) + "\n\nApply these skills: /macro-merge"
	# Mark the macro "summarizing" BEFORE dispatch: run_macro refuses while pending, and
	# the worker's advance hook applies the summary when this turn finishes. Dispatch can
	# complete the turn inline (before _enqueue_turn even returns), so writing "pending"
	# after dispatch loses the race — the advance hook's lookup misses and silently no-ops.
	# Writing it first guarantees a completed turn always finds the macro already waiting.
	frappe.db.set_value(
		MACRO,
		doc.name,
		{
			"merge_status": "pending",
			"merge_conversation": conv.name,
		},
		update_modified=False,
	)
	frappe.db.commit()
	# A summary given up (``pending``, a forced call): the mark names the new chat as of
	# the commit above, so a late result from the old one finds no macro waiting on it
	# (``macros._apply_merge_after_turn`` looks the macro up by this link), whether or
	# not its chat is ever removed. That is done below, once the new one has started.
	try:
		out = chat_api._enqueue_turn(conv.name, prompt, origin="macro")
	except Exception:
		traceback = frappe.get_traceback()
		# The chat is new, so any step message with a Turn row is this dispatch's.
		if macros._went_out_since(conv.name, None):
			# The turn exists: the failure came after the dispatch. It is running, and
			# its end lands the summary, so "pending" is true and stays. No rollback:
			# the dispatch may have queued its send for after the commit, and a
			# rollback would drop it (same rule as the engine's step path).
			frappe.db.set_value("Jarvis Conversation", conv.name, "status", "Archived", update_modified=False)
			frappe.db.commit()
			frappe.log_error(title=f"jarvis macro summary bookkeeping failed: {doc.name}", message=traceback)
			if pending:
				_delete_summary_chat(pending, owned_by=doc.owner)
			return {"ok": True, "conversation": conv.name}
		# Nothing went out (``_ensure_session_key`` throws when the gateway is down;
		# ``_enqueue_turn`` commits the message, then dispatches). "pending" was
		# committed above and used to stay for good, with Run refused behind it.
		frappe.db.rollback()
		frappe.log_error(title=f"jarvis macro summary dispatch failed: {doc.name}", message=traceback)
		_abandon_summary(doc, conv.name, replaced=pending)
		frappe.throw(_("The summary could not be started. Please try again in a few minutes."))
	# CDX-19: the site's turn queue was momentarily full, so the merge turn was NOT dispatched
	# (its seed was cleaned up). Roll back the "pending" mark set above — there is no summary
	# turn coming for it to wait on (get_macro_merge would poll pending forever otherwise).
	# The merge is a one-shot user action (not a chained run), so the honest disposition is a
	# retryable rejection: tear down the throwaway conversation, clear the mark, and let the
	# user re-click.
	if isinstance(out, dict) and out.get("overloaded"):
		_abandon_summary(doc, conv.name, replaced=pending)
		return {
			"ok": False,
			"reason": out.get("reason") or _("The site is busy — please try again in a moment."),
		}
	# Hide from the sidebar (list_conversations skips Archived).
	frappe.db.set_value("Jarvis Conversation", conv.name, "status", "Archived", update_modified=False)
	frappe.db.commit()
	if pending:
		_delete_summary_chat(pending, owned_by=doc.owner)
	return {"ok": True, "conversation": conv.name}
