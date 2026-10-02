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
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"


def _parse_steps(steps) -> list[dict]:
	"""Normalize the steps array (JSON string or list) into child-row dicts,
	dropping wholly empty steps. Order is preserved (child ``idx``).

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
				"skills": frappe.as_json(_clean_step_skills(s.get("skills"))),
			}
		)
	return rows


# --------------------------------------------------------------------------- #
# CRUD (owner-scoped)
# --------------------------------------------------------------------------- #
def _clean_step_skills(skills) -> list[str]:
	"""Normalize one step's tagged-skills value (JSON string or list of Jarvis
	Custom Skill row-names) into a validated list. Only skills the current user
	OWNS or was SHARED are accepted — same visibility rule as invoking /slug in
	chat. Stored as a JSON list on the step row (child tables can't nest a
	child table, so a Table field is not an option here)."""
	if skills is None:
		return []
	if isinstance(skills, str):
		try:
			skills = frappe.parse_json(skills)
		except Exception:
			return []
	if not isinstance(skills, list):
		return []
	from jarvis.chat.custom_skills_api import _skill_names_shared_with

	me = frappe.session.user
	shared = set(_skill_names_shared_with(me))
	clean, seen = [], set()
	for name in skills:
		name = (name or "").strip() if isinstance(name, str) else ""
		if not name or name in seen:
			continue
		owner = frappe.db.get_value("Jarvis Custom Skill", name, "owner")
		if not owner:
			continue
		if owner != me and name not in shared:
			continue
		seen.add(name)
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
	someone overseeing the macro may know that a run failed, not read the chat."""
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


def _step_skills(step) -> list[str]:
	"""Parse a step row's ``skills`` JSON into a list (tolerant of legacy rows)."""
	try:
		v = frappe.parse_json(step.skills) if step.skills else []
		return v if isinstance(v, list) else []
	except Exception:
		return []


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
	return {"ok": True, "data": {"name": doc.name, "macro_name": doc.macro_name}}


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
) -> dict:
	"""Update provided fields of a macro (owner-gated). When ``steps`` is given it
	replaces the whole ordered list (per-step skills ride inside each step dict) —
	and, unless ``merged_prompt`` is sent in the same call, clears any stored
	summary (it's stale once the steps change; the save flow regenerates it)."""
	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("write")  # owner-gate (save enforces too; explicit for clarity)
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
		doc.set("steps", _parse_steps(steps))
		if merged_prompt is None:
			# steps changed → the stored summary is stale; the save flow's
			# background re-summarize repopulates it (merge_status → pending).
			doc.merged_prompt = ""
			doc.merge_status = ""
	if merged_prompt is not None:
		doc.merged_prompt = (merged_prompt or "").strip()
		doc.merge_status = "ready" if doc.merged_prompt else ""
	_refuse_schedule_for_barred_owner(doc.owner, doc.schedule_enabled)
	doc.save()
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "modified": str(doc.modified)}}


@frappe.whitelist()
@require_jarvis_user
def delete_macro(name: str) -> dict:
	"""Delete a macro row (owner-gated). Its Macro Run history rows link the
	macro and would block the delete (LinkExistsError), so they go first — they
	are just execution history; the run conversations themselves stay.

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
	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("write")  # owner-gate before touching linked runs
	frappe.db.delete(RUN, {"macro": doc.name})
	frappe.delete_doc(MACRO, doc.name)  # honors if_owner
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
@require_jarvis_user
def delete_macros_bulk(names: str | list | None = None) -> dict:
	"""Bulk delete macros the caller OWNS (DESIGN-V3 §8.3 / D20). ``names`` is a
	JSON array of macro row-names. Reuses the ``delete_macro`` path per row so
	each macro's Run history goes first (LinkExistsError otherwise). Per-row
	try/except: foreign rows skip with ``not owner``, one bad row never aborts
	the batch. Returns ``{deleted, skipped: [{name, reason}]}``."""
	raw = frappe.parse_json(names) if isinstance(names, str) else (names or [])
	items = [str(n) for n in raw if n] if isinstance(raw, list) else []
	me = frappe.session.user
	deleted = 0
	skipped: list[dict] = []
	for n in items:
		try:
			doc = frappe.get_doc(MACRO, n)
			if doc.owner != me:
				skipped.append({"name": n, "reason": "not owner"})
				continue
			delete_macro(n)  # clears run history first, then the macro
			deleted += 1
		except frappe.DoesNotExistError:
			skipped.append({"name": n, "reason": "not found"})
		except frappe.PermissionError:
			skipped.append({"name": n, "reason": "not permitted"})
		except Exception:
			# Never leak internal exception text to the client — log server-side.
			frappe.log_error(title="Jarvis: bulk macro delete failed", message=frappe.get_traceback())
			skipped.append({"name": n, "reason": "error"})
	frappe.db.commit()
	return {"deleted": deleted, "skipped": skipped}


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
	conditions = ["r.owner = %(owner)s"]
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
		"SELECT status, COUNT(*) AS n FROM `tabJarvis Macro Run` WHERE owner = %(owner)s GROUP BY status",
		owner,
		as_dict=True,
	)
	by = {r.status: r.n for r in rows}
	completed = by.get("completed", 0)
	failed = by.get("failed", 0)
	stopped_with_reason = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabJarvis Macro Run`
		WHERE owner = %(owner)s AND status = 'stopped' AND IFNULL(error, '') != ''""",
		owner,
	)[0][0]
	finished = completed + failed + stopped_with_reason
	last = frappe.db.sql("SELECT MAX(creation) FROM `tabJarvis Macro Run` WHERE owner = %(owner)s", owner)[0][
		0
	]
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


@frappe.whitelist()
@require_jarvis_user
def summarize_macro(name: str) -> dict:
	"""Kick off the merge: throwaway archived conversation + one agent turn
	that invokes /macro-merge over the macro's steps. Returns the conversation
	for the SPA to poll. The macro itself is untouched here."""
	doc = frappe.get_doc(MACRO, name)
	# Gated on WRITE, not read: a summary lands on the macro (merged_prompt,
	# merge_status), and its turn runs in a chat created for whoever asks. On read
	# alone, anyone who could SEE a macro could overwrite its owner's summary, and
	# the throwaway chat belonged to the caller, not the macro's owner.
	doc.check_permission("write")
	from jarvis.chat import macros

	macros.refuse_acting_for_barred_owner(doc.owner)
	steps = doc.steps or []
	if len(steps) < 2:
		frappe.throw(_("Nothing to merge — the macro has fewer than 2 steps."))
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
		name,
		{
			"merge_status": "pending",
			"merge_conversation": conv.name,
		},
		update_modified=False,
	)
	frappe.db.commit()
	out = chat_api._enqueue_turn(conv.name, prompt, origin="macro")
	# CDX-19: the site's turn queue was momentarily full, so the merge turn was NOT dispatched
	# (its seed was cleaned up). Roll back the "pending" mark set above — there is no summary
	# turn coming for it to wait on (get_macro_merge would poll pending forever otherwise).
	# The merge is a one-shot user action (not a chained run), so the honest disposition is a
	# retryable rejection: tear down the throwaway conversation, clear the mark, and let the
	# user re-click.
	if isinstance(out, dict) and out.get("overloaded"):
		try:
			frappe.delete_doc("Jarvis Conversation", conv.name, ignore_permissions=True, force=True)
			frappe.db.set_value(
				MACRO,
				name,
				{
					"merge_status": "",
					"merge_conversation": "",
				},
				update_modified=False,
			)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
		return {
			"ok": False,
			"reason": out.get("reason") or _("The site is busy — please try again in a moment."),
		}
	# Hide from the sidebar (list_conversations skips Archived).
	frappe.db.set_value("Jarvis Conversation", conv.name, "status", "Archived", update_modified=False)
	frappe.db.commit()
	return {"ok": True, "conversation": conv.name}
