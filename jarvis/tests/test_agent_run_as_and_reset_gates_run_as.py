"""Run-as mapping rule: only Administrator or a System Manager may set an agent's
Run As User to someone other than themselves (set time, item 2), and the same rule
is re-checked at every scheduled and manual run against whoever last set the
mapping, judged by that user's roles NOW (run time, item 3).

Fixtures are copied from ``test_agent_identity`` so the module stands alone.
"""

import unittest

import frappe

from jarvis.chat import agent_catalog, agents_api
from jarvis.tests._agent_access import allow_listing_for, clear_listing_access

INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
AGENT = "close-auditor"


def _ensure_user(email: str, extra_roles: tuple = (), jarvis_user: bool = True) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
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
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.set_value("User", email, {"enabled": 1, "user_type": "System User"}, update_modified=False)
	have = set(frappe.get_roles(email))
	want = set(extra_roles) | ({"Jarvis User"} if jarvis_user else set())
	missing = [r for r in want if r not in have]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	frappe.clear_cache(user=email)
	frappe.db.commit()
	return email


def _install_as(owner: str, agent_slug: str = AGENT) -> str:
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		return agents_api.install_agent(agent_slug)["data"]["name"]
	finally:
		frappe.set_user(original)


class _RunAsFixture(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from jarvis.permissions import ensure_jarvis_admin_role

		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		ensure_jarvis_admin_role()
		cls.owner = _ensure_user("rag-owner@example.com")
		cls.peer = _ensure_user("rag-peer@example.com")
		cls.jadmin = _ensure_user("rag-jadmin@example.com", extra_roles=("Jarvis Admin",))
		cls.sm = _ensure_user("rag-sm@example.com", extra_roles=("System Manager",))
		cls.sm2 = _ensure_user("rag-sm2@example.com", extra_roles=("System Manager",))
		cls.users = (cls.owner, cls.peer, cls.jadmin, cls.sm, cls.sm2)
		# close-auditor declares doctypes_required; the run-as user needs those reads.
		if frappe.db.exists("Role", "Accounts User"):
			for u in cls.users:
				frappe.get_doc("User", u).add_roles("Accounts User")
				frappe.clear_cache(user=u)
			frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self._cleanup()

	def _cleanup(self):
		clear_listing_access()
		for u in self.users:
			allow_listing_for(AGENT, user=u)
		for dt in (RUN, INSTALLATION):
			for u in self.users:
				for n in frappe.get_all(dt, filters={"owner": u}, pluck="name"):
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		for u in self.users:
			for n in frappe.get_all("Jarvis Chat Session", filters={"user": u}, pluck="name"):
				frappe.delete_doc("Jarvis Chat Session", n, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _set_as(self, user: str, inst: str, target: str):
		"""Save ``inst`` with run_as_user = ``target`` as ``user`` through the controller."""
		frappe.set_user(user)
		try:
			doc = frappe.get_doc(INSTALLATION, inst)
			doc.flags.ignore_permissions = True  # who may write the row is not under test
			doc.run_as_user = target
			# Under tests Frappe skips Version rows unless asked; production saves write them.
			doc.save(ignore_version=False)
			frappe.db.commit()
		finally:
			frappe.set_user("Administrator")


class TestRunAsSetTimeRule(_RunAsFixture):
	def test_jarvis_admin_who_is_not_system_manager_cannot_map_to_another_user(self):
		inst = _install_as(self.jadmin)
		with self.assertRaises(frappe.PermissionError):
			self._set_as(self.jadmin, inst, self.peer)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.jadmin)

	def test_set_run_as_user_endpoint_refuses_jarvis_admin_cross_user(self):
		inst = _install_as(self.jadmin)
		frappe.set_user(self.jadmin)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_run_as_user(inst, self.peer)
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.jadmin)

	def test_system_manager_may_map_cross_user_and_it_is_audited(self):
		inst = _install_as(self.owner)
		self._set_as(self.sm, inst, self.peer)
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.peer)
		detail = frappe.db.get_value(
			ACTIVITY, {"installation": inst, "action": "run_as_changed"}, "detail", order_by="creation desc"
		)
		self.assertIn(f"run as {self.peer} (set by {self.sm})", detail or "")

	def test_system_manager_may_map_to_self(self):
		inst = _install_as(self.owner)
		self._set_as(self.sm, inst, self.sm)
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.sm)

	def test_non_system_manager_may_not_map_to_a_system_manager(self):
		inst = _install_as(self.owner)
		with self.assertRaises(frappe.PermissionError):
			self._set_as(self.owner, inst, self.sm)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.owner)


class TestRunAsRunTimeRecheck(_RunAsFixture):
	"""Every run re-judges the mapping against the setter's roles now."""

	def _mapped_by(self, setter: str) -> str:
		"""An enabled install owned by ``owner`` and mapped to ``peer`` by ``setter``."""
		inst = _install_as(self.owner)
		self._set_as(setter, inst, self.peer)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		frappe.db.commit()
		return inst

	def _revoke_sm(self, user: str):
		frappe.get_doc("User", user).remove_roles("System Manager")
		frappe.clear_cache(user=user)

	def _restore_sm(self, user: str):
		frappe.get_doc("User", user).add_roles("System Manager")
		frappe.clear_cache(user=user)
		frappe.db.commit()

	def _refusal(self, inst: str):
		from jarvis.chat.agent_scheduler import run_as_mapping_refusal

		owner, run_as = frappe.db.get_value(INSTALLATION, inst, ["owner", "run_as_user"])
		return run_as_mapping_refusal(inst, owner, run_as)

	# -- the rule itself --------------------------------------------------- #
	def test_self_map_is_unaffected(self):
		inst = _install_as(self.owner)
		self.assertIsNone(self._refusal(inst))

	def test_mapping_set_by_a_system_manager_passes(self):
		inst = self._mapped_by(self.sm2)
		self.assertIsNone(self._refusal(inst))

	def test_mapping_set_by_administrator_passes(self):
		inst = self._mapped_by("Administrator")
		self.assertIsNone(self._refusal(inst))

	def test_mapping_is_refused_once_the_setter_loses_system_manager(self):
		inst = self._mapped_by(self.sm2)
		self._revoke_sm(self.sm2)
		try:
			reason = self._refusal(inst)
		finally:
			self._restore_sm(self.sm2)
		self.assertIn("no longer a System Manager", reason)
		self.assertIn(self.sm2, reason)

	def test_mapping_is_refused_when_the_setter_is_disabled(self):
		inst = self._mapped_by(self.sm2)
		frappe.db.set_value("User", self.sm2, "enabled", 0, update_modified=False)
		try:
			reason = self._refusal(inst)
		finally:
			frappe.db.set_value("User", self.sm2, "enabled", 1, update_modified=False)
		self.assertIn("is disabled", reason)

	def test_cross_user_mapping_with_no_record_fails_closed(self):
		inst = _install_as(self.owner)
		# A raw write leaves no Version row and no audit row behind.
		frappe.db.set_value(INSTALLATION, inst, "run_as_user", self.peer, update_modified=False)
		reason = self._refusal(inst)
		self.assertIn("nothing records who set the run-as user", reason)
		self.assertIn("System Manager", reason)

	def test_a_raw_write_after_a_recorded_mapping_leaves_it_unattributed(self):
		inst = self._mapped_by(self.sm2)
		frappe.db.set_value(INSTALLATION, inst, "run_as_user", self.jadmin, update_modified=False)
		self.assertIn("nothing records", self._refusal(inst))

	def test_a_self_map_by_the_run_as_user_needs_no_system_manager(self):
		inst = _install_as(self.owner)
		self._set_as(self.sm2, inst, self.peer)
		# A Jarvis Admin (not a System Manager) re-points the install at themselves.
		self._set_as(self.jadmin, inst, self.jadmin)
		self._revoke_sm(self.sm2)
		try:
			self.assertIsNone(self._refusal(inst))
		finally:
			self._restore_sm(self.sm2)

	def _pin_version_times(self, inst: str, times: list):
		"""Give the installation's run_as_user Version rows (oldest first) these creation times."""
		names = frappe.get_all(
			"Version",
			filters={"ref_doctype": INSTALLATION, "docname": inst, "data": ["like", '%"run_as_user"%']},
			order_by="creation asc",
			pluck="name",
		)
		self.assertEqual(len(names), len(times))
		for name, ts in zip(names, times, strict=True):
			frappe.db.set_value("Version", name, "creation", ts, update_modified=False)

	def test_attribution_does_not_depend_on_version_timestamps(self):
		for times in (
			["2026-01-01 10:00:00.000000", "2026-01-01 10:00:00.000000"],  # equal (frozen clock)
			["2026-01-01 10:00:05.000000", "2026-01-01 10:00:00.000000"],  # out of order
		):
			inst = _install_as(self.owner)
			self._set_as(self.sm2, inst, self.peer)
			self._set_as(self.jadmin, inst, self.jadmin)
			self._pin_version_times(inst, times)
			self._revoke_sm(self.sm2)
			try:
				# jadmin (not a System Manager) self-mapped last, so the earlier
				# sm2 -> peer change must not be mistaken for the setter.
				self.assertIsNone(self._refusal(inst))
			finally:
				self._restore_sm(self.sm2)
				# The owner may hold one install of the agent; free it for the next pass.
				self._cleanup()

	def test_insert_time_mapping_is_attributed_through_its_audit_row(self):
		from jarvis.chat.agent_activity import log_activity

		inst = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst, "run_as_user", self.peer, update_modified=False)
		log_activity(
			agent=AGENT,
			agent_title=None,
			installation=inst,
			action="run_as_changed",
			detail=f"run as {self.peer} (set by {self.sm2})",
			owner=self.owner,
		)
		self.assertIsNone(self._refusal(inst))
		self._revoke_sm(self.sm2)
		try:
			self.assertIn("no longer a System Manager", self._refusal(inst))
		finally:
			self._restore_sm(self.sm2)

	def test_audit_fallback_does_not_treat_underscore_or_percent_as_a_wildcard(self):
		from jarvis.chat.agent_activity import log_activity
		from jarvis.chat.agent_scheduler import _run_as_setter

		inst = _install_as(self.owner)
		log_activity(
			agent=AGENT,
			agent_title=None,
			installation=inst,
			action="run_as_changed",
			detail=f"run as ragYx@example.com (set by {self.sm2})",
			owner=self.owner,
		)
		# Only ragYx has a row; an address with "_" or "%" in the same place must not
		# pick it up as if those characters were LIKE wildcards.
		self.assertIsNone(_run_as_setter(inst, "rag_x@example.com"))
		self.assertIsNone(_run_as_setter(inst, "rag%@example.com"))
		self.assertEqual(_run_as_setter(inst, "ragYx@example.com"), self.sm2)

	# -- manual path -------------------------------------------------------- #
	def test_run_agent_now_refuses_a_mapping_whose_setter_lost_system_manager(self):
		inst = self._mapped_by(self.sm2)
		self._revoke_sm(self.sm2)
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.PermissionError) as ctx:
				agents_api.run_agent_now(inst)
		finally:
			frappe.set_user("Administrator")
			self._restore_sm(self.sm2)
		self.assertIn("no longer a System Manager", str(ctx.exception))
		self.assertEqual(frappe.db.count(RUN, {"installation": inst}), 0)

	def test_run_agent_now_refuses_a_cross_user_mapping_with_no_record(self):
		inst = _install_as(self.owner)
		frappe.db.set_value(
			INSTALLATION, inst, {"run_as_user": self.peer, "enabled": 1}, update_modified=False
		)
		frappe.db.commit()
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.PermissionError) as ctx:
				agents_api.run_agent_now(inst)
		finally:
			frappe.set_user("Administrator")
		self.assertIn("ask a System Manager to set it again", str(ctx.exception))

	# -- scheduled path ------------------------------------------------------ #
	def _due_row(self, inst: str):
		from frappe.utils import add_days, now_datetime

		frappe.db.set_value(
			INSTALLATION,
			inst,
			{
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"schedule_time": "01:00:00",
				"next_run_at": add_days(now_datetime(), -1),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return frappe.db.get_value(
			INSTALLATION,
			inst,
			[
				"name",
				"owner",
				"run_as_user",
				"agent",
				"schedule_frequency",
				"schedule_time",
				"schedule_weekday",
				"schedule_day_of_month",
				"installable",
				"activation_state",
				"source_apps_json",
			],
			as_dict=True,
		)

	def _sweep(self, row):
		from unittest import mock

		from frappe.utils import now_datetime

		from jarvis.chat import agent_scheduler

		with (
			mock.patch.object(agent_scheduler, "_dispatch") as dispatch,
			mock.patch.object(agent_scheduler, "_over_run_budget", return_value=(False, "")),
			mock.patch("jarvis.chat.agent_models.gate_run", return_value={"ok": True}),
		):
			frappe.set_user("Administrator")
			agent_scheduler._sweep_one(row, now_datetime(), set())
		return dispatch

	def test_scheduled_run_is_skipped_and_recorded_when_the_setter_lost_system_manager(self):
		inst = self._mapped_by(self.sm2)
		row = self._due_row(inst)
		self._revoke_sm(self.sm2)
		try:
			dispatch = self._sweep(row)
		finally:
			self._restore_sm(self.sm2)
		dispatch.assert_not_called()
		runs = frappe.get_all(RUN, filters={"installation": inst}, fields=["status", "error"])
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0]["status"], "failed")
		self.assertIn("no longer a System Manager", runs[0]["error"])
		self.assertIn("ask one to set it again", runs[0]["error"])  # survives the 140-char cap
		# The slot was consumed.
		self.assertIsNotNone(frappe.db.get_value(INSTALLATION, inst, "last_run_at"))

	def test_scheduled_run_with_an_unrecorded_cross_user_mapping_is_skipped(self):
		inst = _install_as(self.owner)
		frappe.db.set_value(
			INSTALLATION, inst, {"run_as_user": self.peer, "enabled": 1}, update_modified=False
		)
		row = self._due_row(inst)
		dispatch = self._sweep(row)
		dispatch.assert_not_called()
		error = frappe.db.get_value(RUN, {"installation": inst}, "error")
		self.assertIn("nothing records who set the run-as user", error)

	def test_scheduled_run_goes_ahead_while_the_setter_is_still_a_system_manager(self):
		inst = self._mapped_by(self.sm2)
		row = self._due_row(inst)
		dispatch = self._sweep(row)
		dispatch.assert_called_once()
		self.assertEqual(frappe.db.count(RUN, {"installation": inst, "status": "failed"}), 0)
