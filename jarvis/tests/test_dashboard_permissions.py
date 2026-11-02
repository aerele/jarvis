"""Tests for jarvis.chat.dashboard_permissions + the Jarvis Dashboard
controller's scope gate.

Fixture users: a Jarvis Admin, two plain jarvis users, one plain user holding
a custom test role, plus Administrator as the SM tier. Hermetic: every
dashboard created here is tracked and deleted in tearDown.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.dashboard_permissions import (
	can_edit_dashboard,
	can_read_dashboard,
	creatable_scopes,
	delete_deposit_dashboard_for_retention,
	delete_deposit_dashboard_for_uninstall,
	has_dashboard_permission,
	is_immutable_deposit_dashboard,
	manageable_roles,
)
from jarvis.exceptions import PermissionDeniedError
from jarvis.jarvis.doctype.jarvis_agent_run.jarvis_agent_run import trusted_deposit_run_removal
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)
from jarvis.tools.get_doc import get_doc as tool_get_doc

DASHBOARD = "Jarvis Dashboard"
RUN = "Jarvis Agent Run"
DEPOSIT_AGENT = "statutory-payroll-deposit-status"

ADMIN_USER = "jarvis-dash-admin@example.com"
SYSTEM_MANAGER_USER = "jarvis-dash-system-manager@example.com"
PLAIN_A = "jarvis-dash-user-a@example.com"
PLAIN_B = "jarvis-dash-user-b@example.com"
ROLE_USER = "jarvis-dash-roleholder@example.com"
CUSTOM_ROLE = "Jarvis Dash Test Role"


def _ensure_user(email: str, roles: list[str]) -> None:
	"""Create the fixture user if missing; idempotent."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Dash",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(*roles)
	frappe.db.commit()


def _ensure_custom_role() -> None:
	if not frappe.db.exists("Role", CUSTOM_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": CUSTOM_ROLE,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


class _DashboardPermTestCase(FrappeTestCase):
	def setUp(self):
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_custom_role()
		_ensure_user(ADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, CUSTOM_ROLE])
		_ensure_user(SYSTEM_MANAGER_USER, ["System Manager", JARVIS_USER_ROLE])
		_ensure_user(PLAIN_A, [JARVIS_USER_ROLE])
		_ensure_user(PLAIN_B, [JARVIS_USER_ROLE])
		_ensure_user(ROLE_USER, [JARVIS_USER_ROLE, CUSTOM_ROLE])
		self._orig_user = frappe.session.user
		self._dashboards: list[str] = []
		self._runs: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._runs:
			if frappe.db.exists(RUN, name):
				delete_deposit_dashboard_for_retention(name)
				with trusted_deposit_run_removal(None):
					frappe.delete_doc(RUN, name, ignore_permissions=True, force=True)
		for name in self._dashboards:
			if frappe.db.exists(DASHBOARD, name):
				frappe.delete_doc(DASHBOARD, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _mk(self, scope: str = "User", owner: str | None = None, **kw) -> str:
		"""Create a dashboard as Administrator (passes every gate); optionally
		force the owner afterwards (set_user_and_timestamp always stamps the
		session user on insert, so a foreign owner can only be simulated with a
		direct db write — validate is not the subject there)."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": DASHBOARD,
					"dashboard_title": kw.pop("dashboard_title", f"perm-{frappe.generate_hash(length=8)}"),
					"scope": scope,
					"html": "<h1>t</h1>",
					**kw,
				}
			).insert()
			self._dashboards.append(doc.name)
			if owner:
				frappe.db.set_value(DASHBOARD, doc.name, "owner", owner, update_modified=False)
			frappe.db.commit()
			return doc.name
		finally:
			frappe.set_user(prev)

	def _doc(self, name: str):
		return frappe.get_doc(DASHBOARD, name)

	def _link_deposit_report(self, dashboard: str, owner: str = ROLE_USER) -> str:
		"""Make a real linked run. Raw rehome is intentionally server-only."""
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": DEPOSIT_AGENT,
				"status": "completed",
				"dashboard": dashboard,
				"owner": owner,
			}
		).insert(ignore_permissions=True, ignore_links=True)
		self._runs.append(run.name)
		frappe.db.commit()
		return run.name

	def _mk_run(self, agent: str, dashboard: str | None = None) -> str:
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": agent,
				"status": "completed",
				"dashboard": dashboard,
			}
		).insert(ignore_permissions=True, ignore_links=True)
		self._runs.append(run.name)
		frappe.db.commit()
		return run.name

	def _visible_names(self, user: str, names: list[str]) -> set:
		frappe.set_user(user)
		rows = frappe.get_list(DASHBOARD, filters={"name": ["in", names]}, pluck="name")
		return set(rows)


class TestReadMatrix(_DashboardPermTestCase):
	def test_org_visible_to_all_jarvis_users(self):
		name = self._mk("Org")
		doc = self._doc(name)
		for user in (PLAIN_A, PLAIN_B, ROLE_USER, ADMIN_USER):
			self.assertTrue(can_read_dashboard(doc, user), user)

	def test_role_visible_only_to_role_holder_and_admins(self):
		name = self._mk("Role", target_role=CUSTOM_ROLE)
		doc = self._doc(name)
		self.assertTrue(can_read_dashboard(doc, ROLE_USER))
		self.assertTrue(can_read_dashboard(doc, ADMIN_USER))
		self.assertFalse(can_read_dashboard(doc, PLAIN_A))
		self.assertFalse(can_read_dashboard(doc, PLAIN_B))

	def test_user_scope_visible_only_to_target_and_admins(self):
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		doc = self._doc(name)
		self.assertTrue(can_read_dashboard(doc, PLAIN_A))
		self.assertTrue(can_read_dashboard(doc, ADMIN_USER))
		self.assertFalse(can_read_dashboard(doc, PLAIN_B))
		self.assertFalse(can_read_dashboard(doc, ROLE_USER))

	def test_owner_reads_own_role_scoped_without_holding_role(self):
		# PLAIN_A does NOT hold CUSTOM_ROLE but owns the dashboard.
		name = self._mk("Role", target_role=CUSTOM_ROLE, owner=PLAIN_A)
		doc = self._doc(name)
		self.assertTrue(can_read_dashboard(doc, PLAIN_A))
		self.assertIn(name, self._visible_names(PLAIN_A, [name]))
		self.assertFalse(can_read_dashboard(doc, PLAIN_B))

	def test_blank_scope_is_private_not_org(self):
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		# Simulate a row that lost its scope (validate would never produce it).
		frappe.db.set_value(DASHBOARD, name, "scope", "", update_modified=False)
		frappe.db.commit()
		doc = self._doc(name)
		self.assertTrue(can_read_dashboard(doc, PLAIN_A))  # owner + target_user
		self.assertFalse(can_read_dashboard(doc, PLAIN_B))
		self.assertFalse(can_read_dashboard(doc, ROLE_USER))
		self.assertNotIn(name, self._visible_names(PLAIN_B, [name]))

	def test_sm_reads_all(self):
		names = [
			self._mk("Org"),
			self._mk("Role", target_role=CUSTOM_ROLE),
			self._mk("User", target_user=PLAIN_A, owner=PLAIN_A),
		]
		for n in names:
			self.assertTrue(can_read_dashboard(self._doc(n), "Administrator"))
		self.assertEqual(self._visible_names("Administrator", names), set(names))

	def test_get_list_hook_parity(self):
		org = self._mk("Org")
		role = self._mk("Role", target_role=CUSTOM_ROLE)
		private_a = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		names = [org, role, private_a]
		self.assertEqual(self._visible_names(PLAIN_A, names), {org, private_a})
		self.assertEqual(self._visible_names(ROLE_USER, names), {org, role})
		self.assertEqual(self._visible_names(PLAIN_B, names), {org})
		self.assertEqual(self._visible_names(ADMIN_USER, names), set(names))


class TestWriteMatrix(_DashboardPermTestCase):
	def test_write_matrix(self):
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		doc = self._doc(name)
		self.assertTrue(can_edit_dashboard(doc, PLAIN_A))
		self.assertFalse(can_edit_dashboard(doc, PLAIN_B))
		self.assertTrue(can_edit_dashboard(doc, ADMIN_USER))
		# ORM enforcement: a non-owner write is denied by the hook.
		frappe.set_user(PLAIN_B)
		foreign = frappe.get_doc(DASHBOARD, name)
		foreign.description = "hijack"
		self.assertRaises(frappe.PermissionError, foreign.save)
		# The owner's write goes through.
		frappe.set_user(PLAIN_A)
		mine = frappe.get_doc(DASHBOARD, name)
		mine.description = "mine"
		mine.save()
		self.assertEqual(frappe.db.get_value(DASHBOARD, name, "description"), "mine")

	def test_delete_matrix(self):
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		doc = self._doc(name)
		self.assertFalse(has_dashboard_permission(doc, "delete", PLAIN_B))
		self.assertTrue(has_dashboard_permission(doc, "delete", PLAIN_A))
		self.assertTrue(has_dashboard_permission(doc, "delete", ADMIN_USER))
		frappe.set_user(PLAIN_B)
		self.assertRaises(frappe.PermissionError, frappe.delete_doc, DASHBOARD, name)
		frappe.set_user(PLAIN_A)
		frappe.delete_doc(DASHBOARD, name)
		self.assertFalse(frappe.db.exists(DASHBOARD, name))

	def test_tool_get_doc_denied_on_private_dashboard(self):
		# The chat-referral path: another user asking the agent to open a
		# private dashboard must hit the permission wall in jarvis.tools.
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		frappe.set_user(PLAIN_B)
		self.assertRaises(PermissionDeniedError, tool_get_doc, DASHBOARD, name)
		frappe.clear_messages()

	def test_linked_deposit_report_is_immutable_for_reviewer_owner_and_admin(self):
		# A shadow report is readable by its reviewer, then trusted raw rehome makes
		# the same artifact visible to the live owner. Neither party can mutate it.
		from jarvis.chat import dashboards_api

		name = self._mk("User", target_user=ROLE_USER, owner=ROLE_USER)
		run = self._link_deposit_report(name)
		doc = self._doc(name)
		self.assertTrue(can_read_dashboard(doc, ROLE_USER))
		self.assertTrue(is_immutable_deposit_dashboard(doc))
		for user in (ROLE_USER, ADMIN_USER, SYSTEM_MANAGER_USER, "Administrator"):
			with self.subTest(user=user):
				self.assertFalse(can_edit_dashboard(doc, user))
				self.assertFalse(has_dashboard_permission(doc, "write", user))
				self.assertFalse(has_dashboard_permission(doc, "delete", user))
				frappe.set_user(user)
				candidate = frappe.get_doc(DASHBOARD, name)
				candidate.description = "must not save"
				self.assertRaises(frappe.PermissionError, candidate.save, ignore_permissions=True)

		# Server-only shadow -> live rehome bypasses the ORM lock, as intended.
		frappe.db.set_value(
			DASHBOARD, name, {"owner": PLAIN_A, "target_user": PLAIN_A}, update_modified=False
		)
		frappe.db.commit()
		rehomed = self._doc(name)
		self.assertTrue(can_read_dashboard(rehomed, PLAIN_A))
		self.assertFalse(can_edit_dashboard(rehomed, PLAIN_A))
		for user in (PLAIN_A, SYSTEM_MANAGER_USER, "Administrator"):
			with self.subTest(user=user, route="client-set-value"):
				frappe.set_user(user)
				self.assertRaises(
					frappe.PermissionError,
					frappe.client.set_value,
					DASHBOARD,
					name,
					"description",
					"must not set",
				)
			with self.subTest(user=user, route="dashboard-api"):
				self.assertRaises(
					frappe.PermissionError,
					dashboards_api.save_dashboard,
					frappe.as_json({"name": name, "description": "must not save"}),
				)
			with self.subTest(user=user, route="dashboard-api-delete"):
				self.assertRaises(frappe.PermissionError, dashboards_api.delete_dashboard, name)
			with self.subTest(user=user, route="force-delete"):
				self.assertRaises(
					frappe.PermissionError,
					frappe.delete_doc,
					DASHBOARD,
					name,
					ignore_permissions=True,
					force=True,
				)

		# Retention has one explicit server route: it removes the report while
		# holding the Run lock. This is not an external permission bypass.
		delete_deposit_dashboard_for_retention(run)
		self.assertFalse(frappe.db.exists(DASHBOARD, name))

	def test_deposit_run_dashboard_link_and_deletion_need_server_lifecycle(self):
		"""No Desk/REST/ORM path can clear, repoint, relabel, or delete the Run."""
		report = self._mk("User", target_user=ROLE_USER, owner=ROLE_USER)
		other = self._mk("User", target_user=ROLE_USER, owner=ROLE_USER)
		run = self._link_deposit_report(report)
		for user in (ROLE_USER, ADMIN_USER, SYSTEM_MANAGER_USER, "Administrator"):
			for field, value in (("dashboard", None), ("dashboard", other), ("agent", "generic-agent")):
				with self.subTest(user=user, field=field, value=value):
					frappe.set_user(user)
					candidate = frappe.get_doc(RUN, run)
					candidate.set(field, value)
					# The test slug is not a Listing fixture. Ignore that unrelated
					# Link check so this exercises the controller guard that also
					# protects an otherwise valid relabel.
					candidate.flags.ignore_links = True
					self.assertRaises(frappe.PermissionError, candidate.save, ignore_permissions=True)
		for user in (SYSTEM_MANAGER_USER, "Administrator"):
			for dashboard in (None, other):
				with self.subTest(user=user, route="client-set-value", dashboard=dashboard):
					frappe.set_user(user)
					self.assertRaises(
						frappe.PermissionError,
						frappe.client.set_value,
						RUN,
						run,
						"dashboard",
						dashboard,
					)
			with self.subTest(user=user, route="delete-doc"):
				self.assertRaises(
					frappe.PermissionError,
					frappe.delete_doc,
					RUN,
					run,
					ignore_permissions=True,
					force=True,
				)
		# Each failed mutation leaves the original report link in place, so the
		# Dashboard remains locked even when a caller cannot mutate the Run.
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), report)
		self.assertTrue(is_immutable_deposit_dashboard(self._doc(report)))
		locked = self._doc(report)
		locked.description = "must remain immutable"
		self.assertRaises(frappe.PermissionError, locked.save, ignore_permissions=True)

		# A generic run cannot relabel itself as Deposit to bypass the old/new
		# identity guard either.
		generic = self._mk_run("generic-agent", other)
		candidate = frappe.get_doc(RUN, generic)
		candidate.agent = DEPOSIT_AGENT
		candidate.flags.ignore_links = True
		self.assertRaises(frappe.PermissionError, candidate.save, ignore_permissions=True)

		# The trusted retention path removes the report first, then uses a
		# process-local capability for the detached Run deletion.
		delete_deposit_dashboard_for_retention(run)
		self.assertFalse(frappe.db.exists(DASHBOARD, report))
		with trusted_deposit_run_removal(None):
			frappe.delete_doc(RUN, run, ignore_permissions=True, force=True)
		self._runs.remove(run)
		self.assertFalse(frappe.db.exists(RUN, run))

	def test_trusted_rehome_and_uninstall_lifecycle_preserve_the_report_lock(self):
		"""Only the private rehome/uninstall routes may move or remove the receipt."""
		from jarvis.chat import agents_api

		report = self._mk("User", target_user=ROLE_USER, owner=ROLE_USER)
		run = self._link_deposit_report(report)
		installation = f"deposit-lifecycle-{frappe.generate_hash(length=8)}"
		frappe.db.set_value(RUN, run, "installation", installation, update_modified=False)
		frappe.db.commit()
		try:
			# Promotion/demotion uses this narrow raw server route. It changes only
			# visibility ownership, so the linked report stays immutable.
			agents_api._rehome_installation_outputs(frappe._dict(name=installation), PLAIN_A)
			self.assertEqual(frappe.db.get_value(DASHBOARD, report, "owner"), PLAIN_A)
			self.assertTrue(is_immutable_deposit_dashboard(self._doc(report)))
			locked = self._doc(report)
			locked.description = "still immutable after rehome"
			self.assertRaises(frappe.PermissionError, locked.save, ignore_permissions=True)

			# The uninstall route binds both the Run and its installation. A caller
			# cannot use it to free another installation's linked report.
			self.assertRaises(
				frappe.PermissionError,
				delete_deposit_dashboard_for_uninstall,
				run,
				"wrong-installation",
			)
			self.assertTrue(frappe.db.exists(DASHBOARD, report))
			delete_deposit_dashboard_for_uninstall(run, installation)
			self.assertFalse(frappe.db.exists(DASHBOARD, report))
			with trusted_deposit_run_removal(installation):
				frappe.delete_doc(RUN, run, ignore_permissions=True, force=True)
			self._runs.remove(run)
			self.assertFalse(frappe.db.exists(RUN, run))
		finally:
			# Leave the shared fake site clean if an assertion failed before the
			# official lifecycle completed.
			if frappe.db.exists(RUN, run):
				if frappe.db.get_value(RUN, run, "dashboard"):
					delete_deposit_dashboard_for_uninstall(run, installation)
				with trusted_deposit_run_removal(installation):
					frappe.delete_doc(RUN, run, ignore_permissions=True, force=True)
				self._runs.remove(run)

	def test_generic_dashboard_remains_editable(self):
		name = self._mk("User", target_user=PLAIN_A, owner=PLAIN_A)
		frappe.set_user(PLAIN_A)
		doc = self._doc(name)
		doc.description = "generic owner edit"
		doc.save()
		self.assertEqual(frappe.db.get_value(DASHBOARD, name, "description"), "generic owner edit")


class TestScopeHelpers(_DashboardPermTestCase):
	def test_creatable_scopes_matrix(self):
		self.assertEqual(creatable_scopes(PLAIN_A), ["User"])
		self.assertEqual(creatable_scopes(ADMIN_USER), ["Org", "Role", "User"])
		self.assertEqual(creatable_scopes("Administrator"), ["Org", "Role", "User"])

	def test_manageable_roles(self):
		sm_roles = manageable_roles("Administrator")
		self.assertIn(CUSTOM_ROLE, sm_roles)
		for blocked in ("System Manager", "All", "Guest", JARVIS_USER_ROLE, JARVIS_ADMIN_ROLE):
			self.assertNotIn(blocked, sm_roles)
		admin_roles = manageable_roles(ADMIN_USER)
		self.assertIn(CUSTOM_ROLE, admin_roles)
		self.assertNotIn(JARVIS_ADMIN_ROLE, admin_roles)
		self.assertNotIn(JARVIS_USER_ROLE, admin_roles)
		self.assertEqual(manageable_roles(PLAIN_A), [])


class TestControllerScopeGate(_DashboardPermTestCase):
	def test_plain_user_cannot_create_org_dashboard(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"gate-{frappe.generate_hash(length=8)}",
				"scope": "Org",
				"html": "<h1>x</h1>",
			}
		)
		self.assertRaises(frappe.PermissionError, doc.insert)

	def test_plain_user_cannot_widen_own_to_role(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"gate-{frappe.generate_hash(length=8)}",
				"scope": "User",
				"html": "<h1>x</h1>",
			}
		).insert()
		self._dashboards.append(doc.name)
		doc.scope = "Role"
		doc.target_role = CUSTOM_ROLE
		self.assertRaises(frappe.PermissionError, doc.save)

	def test_admin_role_scope_with_untargetable_role_throws(self):
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"gate-{frappe.generate_hash(length=8)}",
				"scope": "Role",
				"target_role": "System Manager",  # exists, but never targetable
				"html": "<h1>x</h1>",
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_user_scope_autofill_and_scope_switch_clears_counterpart(self):
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"gate-{frappe.generate_hash(length=8)}",
				"scope": "Role",
				"target_role": CUSTOM_ROLE,
				"html": "<h1>x</h1>",
			}
		).insert()
		self._dashboards.append(doc.name)
		self.assertFalse(doc.target_user)
		doc.scope = "User"
		doc.save()
		self.assertEqual(doc.target_user, ADMIN_USER)  # auto-filled from owner
		self.assertFalse(doc.target_role)  # cleared on the switch
		doc.scope = "Org"
		doc.save()
		self.assertFalse(doc.target_user)
		self.assertFalse(doc.target_role)

	def test_user_scope_pins_target_user_to_owner_ignoring_client_value(self):
		"""A direct REST/Desk write cannot push a User-scoped dashboard into an
		arbitrary victim's list — target_user is always forced to the owner."""
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"pin-{frappe.generate_hash(length=8)}",
				"scope": "User",
				# attacker-supplied victim — must be ignored
				"target_user": PLAIN_B,
				"html": "<h1>x</h1>",
			}
		).insert()
		self._dashboards.append(doc.name)
		self.assertEqual(doc.target_user, PLAIN_A)  # pinned to owner, not PLAIN_B
		# and it is NOT visible to the intended victim
		from jarvis.chat.dashboard_permissions import can_read_dashboard

		self.assertFalse(can_read_dashboard(doc, PLAIN_B))
