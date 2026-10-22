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
			doc.save()
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
