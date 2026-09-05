"""Jarvis Agent Installation DocType controller.

One row per (owner, agent) — a customer user installing a marketplace agent.
Owner-scoped (``if_owner`` permission), exactly like ``Jarvis Custom Skill``.
Enabling / scheduling is a pure DB write (no container restart); the bundle is
only pushed to the container on an explicit Apply
(``jarvis.chat.agents_api.apply_agents``).

All validation lives here so it runs on every insert/save regardless of whether
the write came from the SPA, the Desk, or a test — mirroring
``jarvis_custom_skill.py``.
"""

import frappe
from frappe import _
from frappe.model.document import Document

LISTING = "Jarvis Agent Listing"

# Per-owner install cap, mirroring jarvis_custom_skill._validate_owner_cap.
MAX_INSTALLS_PER_OWNER = 20

# GL-linked dimensions (OTHER than Company) whose User Permissions silently
# record-scope aggregates BEFORE they are summed — so a run_as_user carrying one
# would compute a numerically WRONG total (a false "TB out by X" blocker, wrong
# party balances, flipped YoY). Detected at mapping time -> scoped_visibility=1
# (A12). Company is deliberately excluded: the audit is already company-scoped,
# so a Company User Permission is a scoping consistency, not a slice.
_GL_SCOPED_DIMENSIONS = (
	"Account",
	"Cost Center",
	"Project",
	"Fiscal Year",
	"Finance Book",
	"Customer",
	"Supplier",
	"Employee",
	"Shareholder",
)


class JarvisAgentInstallation(Document):
	def validate(self):
		self._default_reviewer()
		self._guard_installability()
		self._validate_unique_per_owner()
		self._validate_owner_cap()
		self._validate_run_as_user()
		self._validate_schedule_time()
		self._validate_schedule_day_of_month()
		self._validate_schedule_budget()
		self._guard_activation_transition()

	def on_update(self):
		self._audit_cross_user_run_as()

	def _default_reviewer(self):
		"""PP-4: ``reviewer`` is reqd — the named human who sees this installation's
		SHADOW output (findings/dashboards) and whose explicit sign-off is the only
		path to ``live``. Default it to the run-as user (which itself defaults to the
		installer) so every write surface (SPA / Desk / data import / direct save /
		test) gets a sensible reviewer without the caller having to supply one; a
		Jarvis Admin may retarget it. Never leave it empty — a shadow install with no
		reviewer would have no one who can see its output or promote it. Runs before
		Frappe's mandatory check, so it satisfies ``reqd`` on every surface."""
		if not (self.reviewer or "").strip():
			self.reviewer = self.run_as_user or self.owner or frappe.session.user

	def _guard_installability(self):
		"""PP-3 / R5-J8: a capability is INSTALLABLE only when its listing's
		``min_apps`` are all installed AND its required DocTypes all exist. This is
		the authoritative install-time gate — it runs on EVERY insert/save regardless
		of surface (SPA / Desk / import / test), so an absent dependency can never
		enter through a path ``install_agent`` does not own.

		On a FRESH install an unmet dependency is refused (typed reason
		``app_absent_or_ineligible``) and the row is never created. For an EXISTING
		row the reconcile hook owns the ``installable`` flag (it marks a row 0 when an
		app disappears AFTER install, without deleting it); here we only refuse
		flipping such a row to ``enabled`` — the run/push gates cover the rest. A
		save that neither installs nor enables (e.g. a config edit on an already
		non-installable row) is left alone so it can still be repaired/uninstalled."""
		from jarvis.chat.agent_installability import evaluate_installability

		ok, reason, detail = evaluate_installability(self.agent)
		if self.is_new():
			if not ok:
				self.installable = 0
				self.not_installable_reason = reason
				frappe.throw(
					_("This agent requires {0}; it cannot be installed here.").format(detail),
					title=_("Agent not installable"),
				)
			self.installable = 1
			self.not_installable_reason = ""
			return
		# Existing row: never re-throw merely for being non-installable (reconcile
		# stamped it; the row must stay editable/uninstallable). But forbid ENABLING
		# a non-installable capability on ANY surface.
		if frappe.utils.cint(self.get("enabled")) and not ok:
			frappe.throw(
				_("This agent requires {0}; enable it once the dependency is present.").format(detail),
				title=_("Agent not installable"),
			)

	def _guard_activation_transition(self):
		"""PP-4 / PP-6: ``activation_state`` is THE flag. A fresh install is always
		born in ``shadow`` (it may not skip the reviewer-calibration stage), and the
		flip ``shadow -> live`` happens ONLY through the reviewer promotion path
		(``agents_api.promote_installation``, which stamps ``promoted_by``/
		``promoted_at``, checks the PP-6 global activation budget and writes a PP-5
		provenance event) and ``live -> shadow`` ONLY through the demotion/kill path —
		each sets its dedicated flag. A plain ORM save (a customer editing the row, a
		generic-REST PUT) may NOT flip it: that would bypass the sign-off, the who/when
		audit and the budget ceiling."""
		if self.is_new():
			if (self.activation_state or "shadow") != "shadow":
				frappe.throw(
					_("A new installation activates in shadow; promote it via reviewer sign-off (PP-4).")
				)
			return
		before = self.get_doc_before_save()
		if not before:
			return
		old = before.get("activation_state") or "shadow"
		new = self.activation_state or "shadow"
		if old == new:
			return
		if old == "shadow" and new == "live" and self.flags.get("promoting"):
			return
		if old == "live" and new == "shadow" and self.flags.get("demoting"):
			return
		frappe.throw(
			_("activation_state flips only via the reviewer promotion / demotion path (PP-4)."),
			frappe.PermissionError,
		)

	def _validate_schedule_time(self):
		"""#648: refuse a ``schedule_time`` that is not a time of day.

		The agent twin of ``JarvisMacro._validate_schedule_time`` (#472), and it has to
		live here for the same reason: Frappe runs the controller BEFORE its own Time
		field check (``Document.insert`` calls ``run_before_save_methods`` and only then
		``_validate``), so by the time the framework would object the value has already
		reached the schedule arithmetic. MariaDB's TIME column accepts up to 838:59:59,
		so the storage layer is not the guard either.

		``agents_api.set_schedule`` alone is not enough: it is one write surface among
		several, and a Desk edit, a data import, a bulk edit or a direct ``doc.save()``
		all bypass it. Since #472 made ``parse_schedule_seconds`` total, an out-of-range
		value no longer crashes the sweep, it silently schedules 09:00 instead — so
		without this check a customer who typed 99:00 would see runs at 09:00 with no
		explanation. Refusing at save is the honest answer.

		Checked whenever a value is PRESENT, not only when ``schedule_enabled`` is on:
		with the schedule off a bad value persists happily, and a later flip of
		``schedule_enabled`` would hand the stored garbage straight to the sweep.

		Runs on EVERY save, exactly like the macro sibling, and deliberately not scoped
		to "only when schedule_time changed". Review asked whether that strands a row
		holding a legacy bad value, since ``set_enabled`` and ``set_config`` both go
		through ``doc.save()``. It cannot, because such a row cannot be saved at all
		either way: the only values this check rejects that MariaDB will still STORE are
		``>= 24:00:00``, and those come back from a TIME column as a
		``datetime.timedelta`` with a days component, which Frappe writes back as
		``"4 days, 3:00:00"`` and MariaDB then refuses with error 1292. So there is no
		value that is simultaneously persistable, rejected here, and able to survive a
		read/write round trip. Scoping the check would add a branch for a state that
		cannot exist, and would split this rule from the macro one it shares."""
		from jarvis.chat.macro_scheduler import validate_schedule_time_or_throw

		validate_schedule_time_or_throw(self.schedule_time)

	def _validate_schedule_day_of_month(self):
		"""#653: the agent twin of ``JarvisMacro._validate_schedule_day_of_month`` -
		refuse a ``schedule_day_of_month`` outside 1-31 on every save surface, for the
		same reason ``_validate_schedule_time`` above gives (a Desk edit / data import /
		direct ``doc.save()`` all bypass ``agents_api.set_schedule``). The weekday Select
		is range-checked by the framework's own ``_validate_selects``; this Int field is
		not, so the check is explicit. Shared rule lives in ``macro_scheduler`` so this
		controller and ``JarvisMacro`` cannot drift apart."""
		from jarvis.chat.macro_scheduler import validate_schedule_day_of_month_or_throw

		validate_schedule_day_of_month_or_throw(self.schedule_day_of_month)

	def _validate_schedule_budget(self):
		"""A14: warn (do not hard-block) when an enabled schedule's expected monthly
		runs exceed the per-installation run budget — the run would be starved
		mid-month, so surface it at configuration time rather than as a silent
		month-long outage. A msgprint (not a throw) keeps daily/weekly saves working
		while flagging a budget that a bench admin set too low."""
		if not self.get("schedule_enabled"):
			return
		from jarvis.chat.agent_scheduler import (
			_agent_run_budget_monthly,
			_expected_monthly_runs,
		)

		expected = _expected_monthly_runs(self.get("schedule_frequency"))
		budget = _agent_run_budget_monthly()
		if expected > budget:
			frappe.msgprint(
				_(
					"This {0} schedule expects up to {1} runs/month but the agent run "
					"budget is {2}; later runs this month will be skipped until the "
					"budget is raised in Jarvis Settings."
				).format(self.get("schedule_frequency") or "daily", expected, budget),
				indicator="orange",
				alert=True,
			)

	def _audit_cross_user_run_as(self):
		"""FIX 10 / A4: audit EVERY cross-user run_as mapping, on ANY write surface —
		Desk form, data import, bulk edit, direct ``doc.save()``, not just the SPA
		``set_run_as_user`` endpoint. The escalation GUARD is in validate() (holds
		everywhere); this makes the AUDIT hold everywhere too, so the exact authority
		residual A4 requires to be traceable can never occur untraced. Fires only when
		run_as_user CHANGES to someone OTHER than the setter (a self-map is not an
		escalation); the Link-free Data activity row survives the uninstall cascade."""
		target = (self.run_as_user or "").strip()
		setter = frappe.session.user
		if not target or target == setter:
			return
		before = self.get_doc_before_save()
		old = ((before.run_as_user or "").strip()) if before else ""
		if old == target:
			return  # unchanged mapping — an incidental save, already audited when set
		from jarvis.chat.agent_activity import log_activity

		log_activity(
			agent=self.agent,
			agent_title=frappe.db.get_value(LISTING, self.agent, "title"),
			installation=self.name,
			action="run_as_changed",
			detail=f"run as {target} (set by {setter})"
			+ (" (scoped visibility)" if self.get("scoped_visibility") else ""),
			owner=self.owner,
		)

	def _validate_unique_per_owner(self):
		# One install of a given agent per owner. This is the FRIENDLY path only:
		# check-then-act cannot serialize two concurrent inserts (both see no clash
		# under REPEATABLE READ and both commit under distinct hash names — #460),
		# so the real guarantee is the composite unique index created in
		# ``on_doctype_update`` below. Keep this check first so the ordinary
		# double-install gets a readable message instead of a constraint error.
		owner = self.owner or frappe.session.user
		clash = frappe.db.exists(
			"Jarvis Agent Installation",
			{"owner": owner, "agent": self.agent, "name": ["!=", self.name or ""]},
		)
		if clash:
			frappe.throw(_("You have already installed this agent."))

	def _validate_owner_cap(self):
		if not self.is_new():
			return
		owner = self.owner or frappe.session.user
		count = frappe.db.count("Jarvis Agent Installation", {"owner": owner})
		if count >= MAX_INSTALLS_PER_OWNER:
			frappe.throw(_("You can have at most {0} installed agents.").format(MAX_INSTALLS_PER_OWNER))

	# ------------------------------------------------------------------ #
	# run_as_user — the moat's second half (A4 escalation + A12 perms).
	#
	# This runs on EVERY insert/save regardless of write surface (SPA / Desk /
	# test), so it — not the API layer — is the authoritative escalation guard:
	# the bench trusts the per-run Jarvis Chat Session row and enforces NOTHING
	# at call_tool, so install-time is the only place a low-priv installer can be
	# stopped from mapping the agent to run as a high-priv user (data exfil).
	#
	# A NON-EMPTY mapping is litigated regardless of ``enabled``: a disabled row
	# must never be usable to stage an unvetted run_as_user that a later enable
	# would wave through (the "unchanged mapping" short-circuit below would skip
	# it). Only the BLANK case is conditional on enabled.
	# ------------------------------------------------------------------ #
	def _validate_run_as_user(self):
		target = (self.run_as_user or "").strip()
		if not target:
			# An ENABLED install must name the identity its runs execute as, so a
			# blank one is still refused on that path (and the explicit throw keeps
			# the escalation block below from ever running against a None). A
			# DISABLED install runs nothing — the scheduler filters on enabled=1 —
			# so it needs no executing identity, and refusing a blank one there had
			# a real cost: legacy rows predate the field and carry NULL, and since
			# ``agents_api.set_enabled`` disables through ``doc.save()`` -> validate(),
			# an unconditional throw made such a row impossible to DISABLE through
			# any supported surface. That is the only escape from a bench whose
			# agent push a legacy row has broken, so it must stay open.
			#
			# NOTE: this is the authoritative check — the field's Desk-side
			# ``mandatory_depends_on: eval:doc.enabled`` is a form cue only
			# (frappe evaluates it in JS, never in _validate_mandatory).
			if frappe.utils.cint(self.get("enabled")):
				frappe.throw(_("Run-as user is required."))
			return

		# Only re-litigate the mapping when it is actually being (re)assigned.
		# An unrelated save (enable / schedule / config) on a row whose
		# run_as_user is unchanged must not be blocked just because, e.g., an
		# admin earlier mapped it cross-user — and perms drift is re-checked at
		# each run's session-mint, not on every incidental write.
		if not self.is_new():
			old = frappe.db.get_value("Jarvis Agent Installation", self.name, "run_as_user")
			if old and old == target:
				return

		# Existence + enabled + never Administrator/Guest — reuse the exact
		# fail-closed shape the scheduler applies at run time.
		from jarvis.chat.agent_scheduler import _valid_owner

		if not _valid_owner(target):
			frappe.throw(_("Run-as user must be an existing, enabled, non-system user."))

		self._validate_run_as_escalation(target)
		self._validate_run_as_permissions(target)

	def _validate_run_as_escalation(self, target: str) -> None:
		"""A4: a non-admin setter may map ONLY to themselves; any cross-user
		mapping needs Jarvis Admin (``has_jarvis_admin_access`` — SM or Jarvis
		Admin, never an invented permission); binding to a System Manager
		additionally requires the setter BE a System Manager."""
		from jarvis.permissions import has_jarvis_admin_access

		setter = frappe.session.user
		if target != setter and not has_jarvis_admin_access(setter):
			frappe.throw(
				_("You may only set this agent to run as yourself."),
				frappe.PermissionError,
			)
		target_is_sm = "System Manager" in frappe.get_roles(target)
		setter_is_sm = setter == "Administrator" or "System Manager" in frappe.get_roles(setter)
		if target_is_sm and not setter_is_sm:
			frappe.throw(
				_("Only a System Manager may map an agent to run as a System Manager."),
				frappe.PermissionError,
			)

	def _validate_run_as_permissions(self, target: str) -> None:
		"""A12: the EXECUTING identity must actually be able to read what the
		agent scans (a sliced aggregate is numerically wrong, not just narrower).
		Require ``target`` read on every declared ``doctypes_required``
		(fail-closed on a missing read); detect a GL-dimension User Permission and
		stamp ``scoped_visibility`` (a flag + message for now, not a hard refuse)."""
		for dt in self._required_doctypes():
			# R5-J8: an ABSENT required DocType is no longer silently skipped — a
			# missing app takes its DocTypes with it, and skipping them let a
			# capability install/run without its data. An absent required DocType
			# fails the preflight with the SAME typed reason as min_apps
			# (``app_absent_or_ineligible``), and guards has_permission from a bogus
			# doctype name.
			if not frappe.db.exists("DocType", dt):
				frappe.throw(
					_("This agent requires DocType {0}, which is not present on this site.").format(dt),
					title=_("Agent not installable"),
				)
			if not frappe.has_permission(dt, "read", user=target):
				frappe.throw(
					_("Run-as user {0} lacks read access to {1}, which this agent requires.").format(
						target, dt
					)
				)
		self.scoped_visibility = 1 if self._detect_scoped_visibility(target) else 0

	def _required_doctypes(self) -> list[str]:
		# R5-J8: return the FULL declared set (trimmed, non-empty strings) — no
		# longer silently drop DocTypes that do not exist. Absence is a gate signal
		# the preflight above acts on, not something to hide.
		raw = frappe.db.get_value(LISTING, self.agent, "doctypes_required")
		try:
			vals = frappe.parse_json(raw) if raw else []
		except Exception:
			vals = []
		if not isinstance(vals, list):
			return []
		return [d.strip() for d in vals if isinstance(d, str) and d.strip()]

	def _detect_scoped_visibility(self, target: str) -> bool:
		from frappe.permissions import get_user_permissions

		try:
			perms = get_user_permissions(target) or {}
		except Exception:
			return False
		return any(dt in perms for dt in _GL_SCOPED_DIMENSIONS)


def on_doctype_update():
	"""Create the composite unique index on (owner, agent) — #460.

	``_validate_unique_per_owner`` is check-then-act: ``frappe.db.exists`` then
	``frappe.throw``. The doctype is ``autoname: hash`` / ``naming_rule: Random``
	and declares no ``unique`` field, so nothing at the DB level serialized two
	concurrent installs of the same agent by one owner — both saw no clash under
	REPEATABLE READ and both committed under distinct names. The duplicate
	inflated ``install_count`` and made a single uninstall leave a phantom row, so
	the agent still rendered as installed.

	The constraint lives HERE and not only in the patch because
	``install_app`` runs with ``set_as_patched=True``, which marks every entry in
	patches.txt executed WITHOUT running it: on any fresh install (CI, a new
	customer bench, a rebuilt site) the patch is skipped, and a patch-only
	constraint would silently never exist. Frappe calls ``on_doctype_update()``
	whenever the DocType document is saved, fresh installs included.

	The converse gap is why ``v2_13_unique_agent_installation`` also exists:
	``bench migrate`` re-imports a DocType only when its ``.json`` changes, so a
	``.py``-only change like this one never fires the hook on an EXISTING site.
	That patch reloads the doctype (and de-dupes first, since ALTER TABLE would
	fail on live duplicates).

	``add_unique`` no-ops when the index is already present, so a site running
	both paths is harmless.
	"""
	frappe.db.add_unique(
		"Jarvis Agent Installation",
		["owner", "agent"],
		constraint_name="owner_agent",
	)
