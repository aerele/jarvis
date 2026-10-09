"""Jarvis Macro DocType controller.

A macro is an ordered list of prompts (child table ``Jarvis Macro Step``) that a
customer runs to perform a repetitive multi-step task (e.g. a month-end audit).
Running one spins up a fresh conversation and executes each prompt as its own
agent turn, chained server-side (see ``jarvis.chat.macros``). Rows are owned by
the Frappe user who created them (``if_owner`` permission).

All user-facing validation lives here so it runs on every insert/save whether the
write came from the SPA, the Desk, or a test — mirroring ``Jarvis Custom Skill``.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import NotRenamable, has_jarvis_admin_access

MAX_NAME_LEN = 80
MAX_DESC_LEN = 500
MAX_STEPS = 25
MAX_PROMPT_LEN = 5000
MAX_SUMMARY_LEN = MAX_STEPS * MAX_PROMPT_LEN
MAX_MACROS_PER_OWNER = 25

_STEP_TEXT_FIELDS = ("label", "prompt", "model_override", "thinking_override")

# An admin's hold (``macros_admin_api.admin_hold``). The two columns arrive with a
# migrate; until it has run the doctype does not have them and nothing is held.
HOLD_FIELD = "admin_hold"
HOLD_REASON_FIELD = "admin_hold_reason"
# What a held macro may not be switched to. Switching any of them OFF stays free.
_HELD_OFF_FIELDS = ("enabled", "schedule_enabled", "skip_confirmation")


# Set on the document by the macro form's own two endpoints and by nothing else
# (``macros_api.create_macro`` / ``update_macro``). A payload cannot carry it:
# ``flags`` is a reserved key that ``Document.update`` and ``frappe.get_doc(dict)``
# skip, so REST, a tool's values, Data Import and bulk update never set it.
FORM_FLAG = "jarvis_macro_form"


class MacroOnHoldError(frappe.ValidationError):
	"""A Jarvis Admin has put the macro on hold."""


def hold_fields_exist() -> bool:
	"""Whether this site's ``Jarvis Macro`` has the hold fields (the migrate ran).

	The doctype's meta, not ``frappe.db.has_column``: on Frappe 16 that reads
	``information_schema`` without a schema filter, so it can see the migrated table of
	another site on the same database server; on Frappe 15 its answer can be cached
	from before the migrate. The meta is this site's own doctype."""
	return bool(frappe.get_meta("Jarvis Macro").has_field(HOLD_FIELD))


def arm_on_form_only() -> str:
	return _("Skip confirmation can be switched on only on the macro's own page in Jarvis, by its owner.")


def _arm_refusal(owner: str) -> str:
	if frappe.session.user != owner:
		return _("Only the macro's owner can switch on Skip confirmation.")
	return _("Arming a macro to skip confirmation requires a Jarvis Admin or System Manager role.")


def join_words(items: list[str]) -> str:
	"""``a``, ``a and b``, ``a, b and c``: one list in a sentence, translatable."""
	return items[0] if len(items) == 1 else _("{0} and {1}").format(", ".join(items[:-1]), items[-1])


def held_message(reason: str | None) -> str:
	"""The one sentence for "on hold": the form's banner says the same.

	A message, so HTML: Frappe 15's Desk dialog inserts a thrown message as it is, and
	the reason is an admin's free text. It is escaped here; the SPA decodes a message
	once before showing it as text, and reads the reason itself raw (``get_macro``)."""
	reason = (reason or "").strip()
	if reason:
		return _(
			"An admin has put this macro on hold: {0}. It will not run until an admin releases it."
		).format(frappe.utils.escape_html(reason.rstrip(".")))
	return _("An admin has put this macro on hold. It will not run until an admin releases it.")


def owner_may_arm(owner: str, user: str | None = None) -> bool:
	"""Whether ``user`` (the session user by default) may arm a macro ``owner`` owns,
	the hold aside: only its owner, and only once the admin's hold exists on this
	site. Before the migrate there is no hold to stop an armed macro with, and the
	rule from before applies: the owner must also be a Jarvis Admin."""
	user = user or frappe.session.user
	if user != owner:
		return False
	return hold_fields_exist() or has_jarvis_admin_access(user)


def step_skills(step) -> list[str]:
	"""Parse a step row's ``skills`` JSON into a list (tolerant of legacy rows)."""
	try:
		skills = step.get("skills")
		v = frappe.parse_json(skills) if skills else []
		return v if isinstance(v, list) else []
	except Exception:
		return []


def step_values(steps) -> list[tuple]:
	"""What each step SAYS, in order, for telling whether a save changed the steps.

	Takes step dicts or the stored child rows and reads both the same way, so the
	comparison is on values: never on row identity (a save resends the rows) and
	never on how a value happens to be spelled (padding, ``None`` for an unset
	override, the layout of the skills JSON, ``\r\n`` where a textarea posts ``\n``)."""
	return [
		(
			*((s.get(f) or "").replace("\r\n", "\n").strip() for f in _STEP_TEXT_FIELDS),
			tuple(step_skills(s)),
		)
		for s in steps or []
	]


class JarvisMacro(NotRenamable, Document):
	def validate(self):
		self._validate_name()
		self._validate_steps()
		self._validate_unique_per_owner()
		self._validate_owner_cap()
		self._validate_schedule_time()
		self._validate_schedule_day_of_month()
		self._guard_admin_hold()
		self._guard_summary_state()
		self._guard_arming()
		self._clear_summary_of_changed_steps()
		self._settle_summary_written_in_this_save()
		self._validate_summary_length()
		self._recompute_next_run()

	def _guard_summary_state(self):
		"""``merge_conversation`` is engine state, not a user field: it names the
		throwaway chat a summary is being generated in. When a turn in that chat ends,
		the engine reads its reply into this macro and deletes the chat with permissions
		ignored (``macros._apply_merge_after_turn``). A user who could set it could name
		any conversation. Every legitimate writer is server-side and uses a raw
		``db.set_value``, which never reaches ``validate``; so whatever a save carries
		in this field is never the engine's, and the stored value always wins.

		Restored silently rather than refused. A refusal would also hit an honest save
		whose document was loaded a moment before the engine set or cleared the link
		(a form left open while a summary starts or lands), and a Desk "Duplicate" of a
		macro that is being summarized.

		"Summarizing" is engine state for the same reason: only ``summarize_macro``
		starts a summary, with the same raw write. A save that would move the macro
		INTO ``pending`` was loaded while a summary was running and saved after it
		ended (a form left open), so nothing is coming to clear it and a manual Run
		would be refused for good. The stored status and summary text stay instead. A
		save that clears the summary (steps changed) is untouched."""
		before = self.get_doc_before_save()
		stored_status = (before.get("merge_status") if before else "") or ""
		if (self.merge_status or "") == "pending" and stored_status != "pending":
			self.merge_status = stored_status
			if before:
				self.merged_prompt = before.get("merged_prompt")
		# The stored value as it is (NULL stays NULL): this doctype tracks changes, and
		# NULL -> "" on a Data field would add a Version line to an unrelated save.
		stored = before.get("merge_conversation") if before else None
		self.merge_conversation = stored

	def _guard_admin_hold(self):
		"""The hold is an admin's, never the owner's. Only the admin verbs write it, with
		a raw write that never reaches ``validate``; so, as for ``merge_conversation``,
		whatever a save carries in the two fields is not theirs and the stored value
		always wins: on a form save, REST ``set_value``, a Data Import, a Desk Duplicate
		(an insert, where "stored" is "not held"). The fields' permlevel 1 with no writer
		does the same for a plain user's save, but not for one that ignores permissions
		or comes from Administrator; this does.

		While held, the save may not switch the macro on, schedule it or arm it. Each of
		those is judged against the STORED value, so a form loaded before the hold and
		saved after it is refused too. Switching any of them off stays free."""
		if not hold_fields_exist():
			return
		before = self.get_doc_before_save()
		# As stored, NULL included: this doctype tracks changes (see _guard_summary_state).
		self.set(HOLD_FIELD, before.get(HOLD_FIELD) if before else 0)
		self.set(HOLD_REASON_FIELD, before.get(HOLD_REASON_FIELD) if before else None)
		if not frappe.utils.cint(self.get(HOLD_FIELD)):
			return
		for field in _HELD_OFF_FIELDS:
			if frappe.utils.cint(self.get(field)) and not frappe.utils.cint(before.get(field)):
				frappe.throw(held_message(self.get(HOLD_REASON_FIELD)), MacroOnHoldError)

	def _clear_summary_of_changed_steps(self):
		"""A stored summary runs INSTEAD of the steps (``macros.run_macro``), so one
		made from steps the macro no longer has must go, and with it the "summarizing"
		mark of one still being written from them: without the mark its result does not
		land. Unless this same save wrote the summary: then it is the writer's text for
		the new steps, and it stays.

		Here, and not in ``macros_api.update_macro`` where it used to be: that covered
		the Macros form only. Steps changed by the assistant's ``update_doc``, the Desk
		form, ``frappe.client.save`` or a Data Import kept the old summary, and the
		next run did what the old steps said.

		``flags.steps_changed`` tells ``update_macro`` whether a new summary is due.
		Run after ``_guard_summary_state``, so a summary this save only carries because
		its document was loaded a while ago is not mistaken for one it wrote."""
		before = self.get_doc_before_save()
		self.flags.steps_changed = bool(before) and step_values(self.steps) != step_values(before.steps)
		if not self.flags.steps_changed or self._summary_written_in_this_save():
			return
		self.merged_prompt = ""
		self.merge_status = ""

	def _summary_written_in_this_save(self) -> bool:
		before = self.get_doc_before_save()
		stored = (before.get("merged_prompt") if before else "") or ""
		return (self.merged_prompt or "").strip() != stored.strip()

	def _settle_summary_written_in_this_save(self):
		"""A save that changes the summary text is someone writing it by hand, and that
		text is the summary from then on: "ready" (no status once emptied), whatever
		the status was. One being written by the model at that moment is given up: the
		"summarizing" mark and its link go, so its late result finds no macro waiting
		and is ignored (``macros._apply_merge_after_turn``).

		The rule lived in ``macros_api.update_macro`` alone. A summary written through
		the Desk form, REST or the assistant's ``update_doc`` kept "pending" and the
		link, and the model's text landed over it a moment later; written on a macro
		whose summary had failed, it ran under a Failed badge.

		The link is cleared only here and only with the mark, which is the one thing a
		save may do to engine state (see ``_guard_summary_state``): dropping one's own
		summary in flight, as changing a step already does. Not on insert: a new macro
		keeps what it is created with."""
		before = self.get_doc_before_save()
		if not before or not self._summary_written_in_this_save():
			return
		self.merge_status = "ready" if (self.merged_prompt or "").strip() else ""
		if (before.get("merge_status") or "") == "pending":
			self.merge_conversation = ""

	def _validate_summary_length(self):
		"""A summary runs in place of the steps, so one written by hand may be as long
		as all the steps together (``MAX_SUMMARY_LEN``), not as long as one of them:
		nothing limits a summary the model writes (a raw write that never comes through
		here), and with the limit of one step a typo could not be fixed in a longer one.

		Only a summary this save writes is measured, and one that is over the limit is
		refused only when it is also longer than the stored text: a summary that is
		already over (the model's) can still be corrected or shortened, and never made
		longer."""
		if not self._summary_written_in_this_save():
			return
		before = self.get_doc_before_save()
		stored = len(((before.get("merged_prompt") if before else "") or "").strip())
		written = len((self.merged_prompt or "").strip())
		if written > MAX_SUMMARY_LEN and written > stored:
			frappe.throw(_("The summarized prompt must be at most {0} characters.").format(MAX_SUMMARY_LEN))

	def _guard_arming(self):
		"""ARM the macro = its runs make the covered writes without a card (the broad
		set, incl. run_method, send_email and run_import; delete, cancel, amend,
		creating or changing a skill and a connector call still park and stop the run). It runs as
		the owner, so the owner may arm it (owner decision 2026-10-03), and nobody else,
		an admin included (``owner_may_arm``).

		Only on the macro's own form, by a person: ``FORM_FLAG`` is set by its two
		endpoints alone, and they refuse to run inside a tool call. Everything else
		that saves a macro (REST, the Desk form and its Duplicate, Data Import, bulk
		update and its background job, ``run_method`` reaching ``frappe.client``, the
		assistant's ``update_doc`` / ``create_doc``, a card the user approved) is
		refused taking ``skip_confirmation`` from 0 to 1, insert included.

		On a macro that is armed and stays armed, the same holds for what changes what
		it does: the steps, the summary, switching it on, switching Stop on error off,
		and a schedule change that leaves it scheduled. Each is judged against the
		stored row; a summary carried by a save from elsewhere is refused, unless that
		save is a safe one (``_keep_the_stored_summary``). Switching it off, taking it off its schedule and disarming
		it stay free from any route: the safe direction is never blocked, and an armed
		macro is never edited from where arming is refused. A held macro is refused
		before this (``_guard_admin_hold``).

		Its steps apply only skills its owner controls, on arming and on every change
		to the steps or the summary (``_refuse_skills_outside_owners_control``).

		Enabling the macro flag is what later drives ``run_macro``'s stamp of the run
		conversation's own ``skip_confirmation`` (a raw write)."""
		before = self.get_doc_before_save()
		from_form = bool(self.flags.get(FORM_FLAG))
		was_armed = bool(before) and frappe.utils.cint(before.get("skip_confirmation"))
		if not frappe.utils.cint(self.skip_confirmation):
			if was_armed and not from_form:
				self._keep_the_stored_summary(before)
			return
		owner = self.owner or frappe.session.user
		if not was_armed:
			if not from_form:
				frappe.throw(arm_on_form_only(), frappe.PermissionError)
			if not owner_may_arm(owner):
				frappe.throw(_arm_refusal(owner), frappe.PermissionError)
			self._refuse_skills_outside_owners_control(owner)
			return
		if not from_form and self._switched_off_or_unscheduled(before):
			self._keep_the_stored_summary(before)
		changes = self._armed_changes(before)
		if changes and not (from_form and frappe.session.user == owner):
			frappe.throw(
				_(
					"This macro runs without asking for confirmation, so a change to its {0} can be "
					"made only on its own page in Jarvis, by its owner. Switching it off, taking it "
					"off its schedule or switching off Skip confirmation works from anywhere."
				).format(join_words(changes)),
				frappe.PermissionError,
			)
		if step_values(self.steps) != step_values(before.steps) or self._summary_written_in_this_save():
			self._refuse_skills_outside_owners_control(owner)

	def _switched_off_or_unscheduled(self, before) -> bool:
		return any(
			frappe.utils.cint(before.get(field)) and not frappe.utils.cint(self.get(field))
			for field in ("enabled", "schedule_enabled")
		)

	def _keep_the_stored_summary(self, before):
		"""An armed macro's summary changes on its own page only. A save from elsewhere
		that carries another one is refused (``_armed_changes``), unless it also switches
		the macro off, unschedules it or disarms it: then the stored summary is kept and
		the safe change goes through. The summary landing is a raw write that leaves
		``modified`` alone, so a Desk or REST copy loaded before it carries the old text,
		and switching the macro off from there must work."""
		self.merged_prompt = before.get("merged_prompt")

	def _refuse_skills_outside_owners_control(self, owner: str):
		"""An armed macro's steps apply only skills whose words its owner controls: their
		own, or a Role or Org skill, whose content only a reviewer can change
		(``skill_permissions.controlled_by``). Another user's private skill shared with
		the owner can be rewritten by its author at any time, and the next run would
		follow the new words without asking. The tagged skills and every ``/slug`` typed
		into a step or the summary count. Checked when the macro is armed and when an
		armed macro's steps or summary change; the run checks again, because sharing and
		authorship change later (``macros.check_a_fetched_skill``)."""
		from jarvis.chat.skill_permissions import prompt_slugs, skills_outside_control

		names = [name for step in self.steps or [] for name in step_skills(step)]
		texts = [step.get("prompt") or "" for step in self.steps or []] + [self.merged_prompt or ""]
		slugs = set().union(*(prompt_slugs(text) for text in texts))
		foreign = skills_outside_control(owner, names=names, slugs=slugs)
		if foreign:
			frappe.throw(
				_(
					"Skip confirmation works only with skills you own, or skills only a reviewer "
					"can change. Take {0} off the steps, or leave Skip confirmation off."
				).format(join_words([f"/{slug}" for slug in foreign])),
				frappe.PermissionError,
			)

	def _armed_changes(self, before) -> list[str]:
		"""What this save changes, of what an armed macro does or when it runs."""
		changes = []
		if step_values(self.steps) != step_values(before.steps):
			changes.append(_("steps"))
		if self._summary_written_in_this_save():
			changes.append(_("summary"))
		if frappe.utils.cint(self.enabled) and not frappe.utils.cint(before.get("enabled")):
			changes.append(_("Enabled switch"))
		# Off, a run carries on past a failed step and keeps writing on top of it.
		if frappe.utils.cint(before.get("stop_on_error")) and not frappe.utils.cint(self.stop_on_error):
			changes.append(_("Stop on error switch"))
		if frappe.utils.cint(self.schedule_enabled) and (
			not frappe.utils.cint(before.get("schedule_enabled"))
			or (before.get("schedule_frequency") or "") != (self.schedule_frequency or "")
			or self._time_changed()
			or self._anchor_changed("schedule_weekday")
			or self._anchor_changed("schedule_day_of_month")
		):
			changes.append(_("schedule"))
		return changes

	def _validate_name(self):
		self.macro_name = (self.macro_name or "").strip()
		if not self.macro_name:
			frappe.throw(_("Macro name is required."))
		if len(self.macro_name) > MAX_NAME_LEN:
			frappe.throw(_("Macro name must be at most {0} characters.").format(MAX_NAME_LEN))
		self.description = (self.description or "").strip()
		if len(self.description) > MAX_DESC_LEN:
			frappe.throw(_("Description must be at most {0} characters.").format(MAX_DESC_LEN))

	def _validate_steps(self):
		steps = self.steps or []
		if not steps:
			frappe.throw(_("A macro needs at least one step."))
		if len(steps) > MAX_STEPS:
			frappe.throw(_("A macro can have at most {0} steps.").format(MAX_STEPS))
		for i, s in enumerate(steps, start=1):
			prompt = (s.prompt or "").strip()
			if not prompt:
				frappe.throw(_("Step {0} has an empty prompt.").format(i))
			if len(prompt) > MAX_PROMPT_LEN:
				frappe.throw(_("Step {0} prompt must be at most {1} characters.").format(i, MAX_PROMPT_LEN))
			s.prompt = prompt
			s.label = (s.label or "").strip()

	def _validate_unique_per_owner(self):
		# Frappe's field-level ``unique`` is global; enforce (owner, macro_name)
		# uniqueness here so two customers can both have a "Month-end audit".
		owner = self.owner or frappe.session.user
		clash = frappe.db.exists(
			"Jarvis Macro",
			{"owner": owner, "macro_name": self.macro_name, "name": ["!=", self.name or ""]},
		)
		if clash:
			frappe.throw(_("You already have a macro named '{0}'.").format(self.macro_name))

	def _validate_owner_cap(self):
		if not self.is_new():
			return
		owner = self.owner or frappe.session.user
		if frappe.db.count("Jarvis Macro", {"owner": owner}) >= MAX_MACROS_PER_OWNER:
			frappe.throw(_("You can have at most {0} macros.").format(MAX_MACROS_PER_OWNER))

	def _validate_schedule_time(self):
		"""#472: refuse a ``schedule_time`` that is not a time of day.

		Frappe does not coerce or range-check a Time field before the controller runs
		(``Document.insert`` runs ``validate`` first and ``_validate`` after), so the raw
		value reached ``_recompute_next_run`` -> ``compute_next_run`` ->
		``datetime.replace(hour=...)`` and surfaced as an unhandled ``ValueError``, i.e.
		an HTTP 500 with a traceback instead of a field error.

		Checked whenever a value is PRESENT, not only when ``schedule_enabled`` is on.
		With the schedule off the bad value used to persist happily (MariaDB TIME holds
		up to 838:59:59), which is what armed the cron-wide abort: a later flip of
		``schedule_enabled`` handed the stored garbage straight to the sweep.

		The rule itself lives in ``macro_scheduler.validate_schedule_time_or_throw`` so
		this controller and ``JarvisAgentInstallation`` (#648) cannot drift apart."""
		from jarvis.chat.macro_scheduler import validate_schedule_time_or_throw

		validate_schedule_time_or_throw(self.schedule_time)

	def _validate_schedule_day_of_month(self):
		"""#653: refuse a ``schedule_day_of_month`` outside 1-31, same reasoning and
		timing as ``_validate_schedule_time`` above (a Select-field weekday is
		range-checked by the framework's own ``_validate_selects``; this Int field is
		not, so the check is explicit here). Shared with ``JarvisAgentInstallation``
		via ``macro_scheduler.validate_schedule_day_of_month_or_throw``."""
		from jarvis.chat.macro_scheduler import validate_schedule_day_of_month_or_throw

		validate_schedule_day_of_month_or_throw(self.schedule_day_of_month)

	def _recompute_next_run(self):
		"""Keep ``next_run_at`` in sync with the schedule fields. The scheduler
		(``jarvis.chat.macro_scheduler``) advances it after each run via a raw
		``db.set_value`` (no re-validate), so here we only (re)compute it when the
		schedule is turned on/changed or it hasn't been set yet."""
		if not self.schedule_enabled:
			self.next_run_at = None
			return
		changed = (
			self.is_new()
			or self.has_value_changed("schedule_enabled")
			or self.has_value_changed("schedule_frequency")
			or self._time_changed()
			or self._anchor_changed("schedule_weekday")
			or self._anchor_changed("schedule_day_of_month")
			or not self.next_run_at
		)
		if changed:
			from jarvis.chat.macro_scheduler import compute_next_run

			self.next_run_at = compute_next_run(
				self.schedule_frequency,
				self.schedule_time,
				weekday=self.schedule_weekday,
				day_of_month=self.schedule_day_of_month,
			)

	def _anchor_changed(self, fieldname: str) -> bool:
		"""``has_value_changed`` for the two optional anchors, treating every spelling of
		"not set" as the same value.

		The row holds ``0`` / ``""`` for an unset anchor, while a save from the form
		arrives as ``None``. Compared raw, every save of a daily macro looked like a
		schedule change and recomputed ``next_run_at`` from now, so a run that was due
		but not swept yet silently moved to the next day (admin-v2#675)."""
		before = self.get_doc_before_save()
		if not before:
			return True
		return (before.get(fieldname) or None) != (self.get(fieldname) or None)

	def _time_changed(self) -> bool:
		"""Whether the time of day the macro RUNS at changed, compared as the scheduler
		reads it (``_time_to_seconds``): a row with no stored time runs at the 09:00
		default, so the form posting "09:00" for it is the same schedule, not a change.
		It also makes "10:15" and a stored ``10:15:00`` equal without leaning on how the
		framework happens to coerce a Time field."""
		from jarvis.chat.macro_scheduler import _time_to_seconds

		before = self.get_doc_before_save()
		if not before:
			return True
		return _time_to_seconds(before.get("schedule_time")) != _time_to_seconds(self.schedule_time)


def on_doctype_update():
	"""Composite (owner, macro_name) index.

	No index touches ``owner`` on this table today, so ``_validate_unique_per_owner``
	(the {"owner", "macro_name", "name": ["!=", ...]} exists() check above) and
	``_validate_owner_cap`` (a plain ``{"owner": owner}`` count) both scan the
	WHOLE multi-tenant table on EVERY macro save, not just one owner's rows.
	``MAX_MACROS_PER_OWNER`` (25) caps a single owner's slice permanently, so
	this index buys little for any one tenant; its real value is skipping
	every OTHER tenant's rows, which the per-owner cap does nothing to bound
	as the number of tenants grows. Low priority relative to Jarvis Macro Run,
	which has no such cap.

	``frappe.db.add_index`` no-ops when the index already exists, so repeated
	migrates are harmless.
	"""
	frappe.db.add_index(
		"Jarvis Macro",
		["owner", "macro_name"],
		index_name="owner_macro_name_index",
	)
