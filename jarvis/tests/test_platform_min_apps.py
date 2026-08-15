"""R5-P0-05 / R5-J8 — min_apps + required-DocType installability gate.

A marketplace capability is INSTALLABLE only when every app in its listing's
``min_apps`` is installed AND every required DocType exists. Absence is an
install-time NOT-INSTALLABLE state (typed reason ``app_absent_or_ineligible``),
never a run coverage result. This module proves:

  * INSTALL refuses an unmet min_apps subset (initial absence) and an absent
    required DocType — with the typed reason, and no install row is created.
  * The ``_required_doctypes`` preflight STOPS silently filtering absent
    DocTypes (an absent required DocType now fails, where it used to install).
  * A RECONCILE (the migrate/app-change hook) marks an EXISTING install
    ``installable=0`` + reason when a dependency disappears AFTER install, never
    deleting the row, and clears it again when the dependency returns.
  * ENABLE / SCHEDULE / RUN-NOW refuse a non-installable capability.
  * The container push payload EXCLUDES, and the scheduler due-loop SKIPS, a
    non-installable enabled install (a dependency that vanished after install).
  * A non-installable install is still repairable/uninstallable.

App absence/removal is simulated by patching the module-local
``agent_installability.installed_apps`` seam (patching ``frappe.get_installed_apps``
globally breaks Frappe's own hook resolution). A phantom app id
(``PHANTOM_APP``) that is never really installed keeps every reconcile scoped to
THIS module's install — reconcile is a whole-site sweep, so it must never flip a
sibling agent whose real dependency is unchanged.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_min_apps
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_installability, agent_scheduler, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"

# A min_app id that is never really installed on any site — the scoped lever for
# simulating an app that was present at install then removed.
PHANTOM_APP = "min_apps_test_phantom_app"
PHANTOM_DOCTYPE = "Min Apps Test Phantom DocType"

SLUG_MISS_APP = "min-apps-test-missing-app"
SLUG_MISS_DT = "min-apps-test-missing-doctype"
SLUG_REMOVABLE = "min-apps-test-removable"  # min_apps includes PHANTOM_APP

_ALL_SLUGS = (SLUG_MISS_APP, SLUG_MISS_DT, SLUG_REMOVABLE)


def _mk_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
		u.add_roles("Jarvis User")
	return email


def _mk_listing(slug: str, *, min_apps=None, doctypes_required=None) -> str:
	if frappe.db.exists(LISTING, slug):
		frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": slug,
			"title": slug.replace("-", " ").title(),
			"nature": "Auditor",
			"status": "Published",
			"delivery": "delegate",
			"version": "1.0.0",
			"min_apps": json.dumps(min_apps or []),
			"doctypes_required": json.dumps(doctypes_required or []),
			"rule_tokens": json.dumps([]),
		}
	).insert(ignore_permissions=True)
	return slug


def _installed_plus_phantom():
	"""The real installed apps PLUS the phantom — the 'app present' world."""
	return set(frappe.get_installed_apps()) | {PHANTOM_APP}


def _patch_apps(app_set):
	return patch.object(agent_installability, "installed_apps", return_value=set(app_set))


def _install_as(owner: str, slug: str) -> str:
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		res = agents_api.install_agent(slug)
		return res["data"]["name"]
	finally:
		frappe.set_user(original)


def _wipe():
	for dt in (RUN, INSTALLATION):
		for n in frappe.get_all(
			dt, filters={"agent": ["in", _ALL_SLUGS]}, pluck="name", ignore_permissions=True
		):
			frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
	for slug in _ALL_SLUGS:
		if frappe.db.exists(LISTING, slug):
			frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestMinAppsGate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("min-apps-owner@example.com")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	# ------------------------------------------------------------------ #
	# unit predicate
	# ------------------------------------------------------------------ #
	def test_predicate_and_reason(self):
		_mk_listing(SLUG_MISS_APP, min_apps=[PHANTOM_APP])
		ok, reason, detail = agent_installability.evaluate_installability(SLUG_MISS_APP)
		self.assertFalse(ok)
		self.assertEqual(reason, "app_absent_or_ineligible")
		self.assertIn(PHANTOM_APP, detail)

		_mk_listing(SLUG_MISS_DT, doctypes_required=[PHANTOM_DOCTYPE])
		ok2, reason2, detail2 = agent_installability.evaluate_installability(SLUG_MISS_DT)
		self.assertFalse(ok2)
		self.assertEqual(reason2, "app_absent_or_ineligible")
		self.assertIn(PHANTOM_DOCTYPE, detail2)

		# erpnext is installed on the bench; an erpnext-only agent is installable.
		_mk_listing(SLUG_REMOVABLE, min_apps=["erpnext"])
		ok3, reason3, _ = agent_installability.evaluate_installability(SLUG_REMOVABLE)
		self.assertTrue(ok3)
		self.assertIsNone(reason3)

	def test_required_doctypes_no_longer_filters_absent(self):
		"""R5-J8: the controller's _required_doctypes returns the FULL declared
		set — absent DocTypes are no longer silently dropped."""
		_mk_listing(SLUG_MISS_DT, doctypes_required=["Company", PHANTOM_DOCTYPE])
		# required_doctypes_for is the unfiltered reader the controller mirrors.
		got = agent_installability.required_doctypes_for(SLUG_MISS_DT)
		self.assertIn("Company", got)
		self.assertIn(PHANTOM_DOCTYPE, got)

	# ------------------------------------------------------------------ #
	# initial absence — install refuses
	# ------------------------------------------------------------------ #
	def test_install_refused_when_min_app_absent(self):
		_mk_listing(SLUG_MISS_APP, min_apps=[PHANTOM_APP])
		with self.assertRaises(frappe.ValidationError):
			_install_as(self.owner, SLUG_MISS_APP)
		# No install row is created for a non-installable capability.
		self.assertFalse(frappe.db.exists(INSTALLATION, {"owner": self.owner, "agent": SLUG_MISS_APP}))

	def test_install_refused_when_required_doctype_absent(self):
		"""Previously this installed (the absent DocType was filtered out); now it
		fails the preflight with the same typed reason."""
		_mk_listing(SLUG_MISS_DT, doctypes_required=[PHANTOM_DOCTYPE])
		with self.assertRaises(frappe.ValidationError):
			_install_as(self.owner, SLUG_MISS_DT)
		self.assertFalse(frappe.db.exists(INSTALLATION, {"owner": self.owner, "agent": SLUG_MISS_DT}))

	def test_controller_enforces_on_direct_insert(self):
		"""The gate lives in validate(), so a Desk/import/direct insert is refused
		too, not only the install_agent endpoint."""
		_mk_listing(SLUG_MISS_APP, min_apps=[PHANTOM_APP])
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": SLUG_MISS_APP,
				"run_as_user": self.owner,
				"reviewer": self.owner,
				"activation_state": "shadow",
			}
		)
		doc.owner = self.owner
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	# ------------------------------------------------------------------ #
	# app-removal-after-install — reconcile marks installable=0, never deletes
	# ------------------------------------------------------------------ #
	def test_reconcile_marks_non_installable_after_app_removed(self):
		_mk_listing(SLUG_REMOVABLE, min_apps=["erpnext", PHANTOM_APP])
		# Install while the phantom app is "present".
		with _patch_apps(_installed_plus_phantom()):
			inst = _install_as(self.owner, SLUG_REMOVABLE)
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "installable"), 1)

		# The phantom app is "removed": reconcile against the real site (no phantom).
		res = agent_installability.reconcile_installations()
		self.assertGreaterEqual(res["changed"], 1)
		row = frappe.db.get_value(INSTALLATION, inst, ["installable", "not_installable_reason"], as_dict=True)
		self.assertEqual(row.installable, 0)
		self.assertEqual(row.not_installable_reason, "app_absent_or_ineligible")
		# NEVER deleted — the row survives so a reinstall can restore it.
		self.assertTrue(frappe.db.exists(INSTALLATION, inst))

		# The app returns: reconcile clears the flag and the reason.
		with _patch_apps(_installed_plus_phantom()):
			agent_installability.reconcile_installations()
		row2 = frappe.db.get_value(
			INSTALLATION, inst, ["installable", "not_installable_reason"], as_dict=True
		)
		self.assertEqual(row2.installable, 1)
		self.assertIn(row2.not_installable_reason, (None, ""))

	def test_reconcile_leaves_installable_siblings_untouched(self):
		"""A whole-site reconcile driven by the phantom's absence must not flip an
		agent whose real dependency (erpnext) is unchanged."""
		_mk_listing(SLUG_REMOVABLE, min_apps=["erpnext", PHANTOM_APP])
		_mk_listing(SLUG_MISS_APP, min_apps=["erpnext"])  # reuse slug as an erpnext sibling
		with _patch_apps(_installed_plus_phantom()):
			removable = _install_as(self.owner, SLUG_REMOVABLE)
			sibling = _install_as(self.owner, SLUG_MISS_APP)
		agent_installability.reconcile_installations()
		self.assertEqual(frappe.db.get_value(INSTALLATION, removable, "installable"), 0)
		self.assertEqual(frappe.db.get_value(INSTALLATION, sibling, "installable"), 1)

	# ------------------------------------------------------------------ #
	# enable / schedule / run-now refuse a non-installable capability
	# ------------------------------------------------------------------ #
	def _install_removable_enabled_off(self) -> str:
		_mk_listing(SLUG_REMOVABLE, min_apps=["erpnext", PHANTOM_APP])
		with _patch_apps(_installed_plus_phantom()):
			return _install_as(self.owner, SLUG_REMOVABLE)

	def test_enable_refused_when_not_installable(self):
		inst = self._install_removable_enabled_off()
		frappe.set_user(self.owner)
		try:
			# Phantom absent (no patch) -> the live predicate refuses enabling.
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_enabled(inst, 1)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "enabled"), 0)

	def test_schedule_enable_refused_when_not_installable(self):
		inst = self._install_removable_enabled_off()
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_schedule(inst, schedule_enabled=1, schedule_frequency="daily")
		finally:
			frappe.set_user("Administrator")

	def test_run_now_refused_when_not_installable(self):
		inst = self._install_removable_enabled_off()
		# Simulate 'was enabled while installable, app later removed'.
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError):
				agents_api.run_agent_now(inst)
		finally:
			frappe.set_user("Administrator")

	def test_enable_still_allowed_when_installable(self):
		inst = self._install_removable_enabled_off()
		frappe.set_user(self.owner)
		try:
			with _patch_apps(_installed_plus_phantom()):
				agents_api.set_enabled(inst, 1)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "enabled"), 1)

	# ------------------------------------------------------------------ #
	# push payload + scheduler exclude a reconciled non-installable row
	# ------------------------------------------------------------------ #
	def test_push_payload_excludes_non_installable(self):
		inst = self._install_removable_enabled_off()
		frappe.db.set_value(
			INSTALLATION,
			inst,
			{"enabled": 1, "installable": 0, "not_installable_reason": "app_absent_or_ineligible"},
			update_modified=False,
		)
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		slugs = {p["slug"] for p in payload}
		self.assertNotIn(f"agent-{SLUG_REMOVABLE}", slugs)

	def test_scheduler_skips_non_installable(self):
		inst = self._install_removable_enabled_off()
		past = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
		frappe.db.set_value(
			INSTALLATION,
			inst,
			{
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"schedule_time": "09:00:00",
				"next_run_at": past,
				"installable": 0,
				"not_installable_reason": "app_absent_or_ineligible",
			},
			update_modified=False,
		)
		frappe.db.commit()
		agent_scheduler.run_due_agent_audits()
		# Skipped: no dispatched (running/complete) run; a failed record explains why.
		live = frappe.get_all(
			RUN, filters={"installation": inst, "status": ["in", ("running", "complete")]}, pluck="name"
		)
		self.assertEqual(live, [])
		failed = frappe.get_all(RUN, filters={"installation": inst, "status": "failed"}, fields=["error"])
		self.assertTrue(any("app_absent_or_ineligible" in (f.error or "") for f in failed))
		# The slot was consumed — next_run_at advanced into the future.
		nxt = frappe.db.get_value(INSTALLATION, inst, "next_run_at")
		self.assertGreater(frappe.utils.get_datetime(nxt), frappe.utils.now_datetime())

	# ------------------------------------------------------------------ #
	# a non-installable install is still repairable / uninstallable
	# ------------------------------------------------------------------ #
	def test_non_installable_install_can_be_uninstalled(self):
		inst = self._install_removable_enabled_off()
		frappe.db.set_value(
			INSTALLATION,
			inst,
			{"installable": 0, "not_installable_reason": "app_absent_or_ineligible"},
			update_modified=False,
		)
		frappe.set_user(self.owner)
		try:
			agents_api.uninstall_agent(inst)
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists(INSTALLATION, inst))
