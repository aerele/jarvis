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

from jarvis.permissions import has_jarvis_admin_access

MAX_NAME_LEN = 80
MAX_DESC_LEN = 500
MAX_STEPS = 25
MAX_PROMPT_LEN = 5000
MAX_MACROS_PER_OWNER = 25


class JarvisMacro(Document):
	def validate(self):
		self._validate_name()
		self._validate_steps()
		self._validate_unique_per_owner()
		self._validate_owner_cap()
		self._validate_schedule_time()
		self._validate_schedule_day_of_month()
		self._guard_skip_confirmation_enable()
		self._guard_summary_link()
		self._recompute_next_run()

	def _guard_summary_link(self):
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
		macro that is being summarized."""
		before = self.get_doc_before_save()
		self.merge_conversation = (before.get("merge_conversation") if before else "") or ""

	def _guard_skip_confirmation_enable(self):
		"""ARM the macro = run its writes uncarded (the broad covered set, incl.
		run_method/send_email/run_import; the irreversible trio still parks + stops
		the run). Only a Jarvis Admin / System Manager may enable it - a plain owner
		may RUN a macro (``if_owner`` write) but must not self-grant the bypass.

		Only the 0/unset -> 1 transition is gated; disabling and every other edit
		(including editing steps while it stays on - the arm persists across step
		edits by design, D6) are free for the owner. Enabling the macro flag is what
		later drives ``run_macro``'s stamp of the run conversation's own
		``skip_confirmation`` (guarded there too)."""
		if not self.skip_confirmation:
			return
		previous = self.get_doc_before_save()
		if previous and bool(previous.skip_confirmation):
			return
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Arming a macro to skip confirmation requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)

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
