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
		self._recompute_next_run()

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
			or self.has_value_changed("schedule_time")
			or not self.next_run_at
		)
		if changed:
			from jarvis.chat.macro_scheduler import compute_next_run

			self.next_run_at = compute_next_run(self.schedule_frequency, self.schedule_time)


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
