"""Tests for the dashboard/chart creation tools (need DB; run as the fixture
test context). Inserts are rolled back by FrappeTestCase."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.create_dashboard import create_dashboard
from jarvis.tools.create_dashboard_chart import create_dashboard_chart


def _h(prefix):
	return f"{prefix} {frappe.generate_hash(length=6)}"


class TestCreateDashboardChart(FrappeTestCase):
	def test_count_time_series(self):
		res = create_dashboard_chart(
			chart_name=_h("JT Count"),
			document_type="ToDo",
			chart_type="Count",
			based_on="creation",
			time_interval="Monthly",
			timespan="Last Year",
		)
		self.assertTrue(res["name"])
		self.assertEqual(res["chart_type"], "Count")
		self.assertIn("dashboard-chart", res["url"])
		self.assertEqual(frappe.db.get_value("Dashboard Chart", res["name"], "chart_type"), "Count")

	def test_group_by(self):
		res = create_dashboard_chart(
			chart_name=_h("JT GroupBy"),
			document_type="ToDo",
			chart_type="Group By",
			group_by_based_on="status",
			group_by_type="Count",
		)
		self.assertTrue(res["name"])

	def test_invalid_chart_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(chart_name="x", document_type="ToDo", chart_type="Bogus")

	def test_count_needs_based_on(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(chart_name="x", document_type="ToDo", chart_type="Count")

	def test_sum_needs_value_field(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name="x",
				document_type="ToDo",
				chart_type="Sum",
				based_on="creation",
			)

	def test_group_by_child_table_needs_parent_document_type(self):
		# Contact/Contact Email is a core Frappe parent/child pair - istable,
		# no ERPNext fixture needed. Without parent_document_type Frappe's own
		# Dashboard Chart.validate() would reject the insert; the tool should
		# catch this itself and name the valid parent in the message.
		with self.assertRaises(InvalidArgumentError) as ctx:
			create_dashboard_chart(
				chart_name=_h("JT Child NoParent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
			)
		self.assertIn("Contact", str(ctx.exception))

	def test_group_by_child_table_with_parent_document_type(self):
		res = create_dashboard_chart(
			chart_name=_h("JT Child Parent"),
			document_type="Contact Email",
			chart_type="Group By",
			group_by_based_on="email_id",
			parent_document_type="Contact",
		)
		self.assertTrue(res["name"])
		self.assertEqual(
			frappe.db.get_value("Dashboard Chart", res["name"], "parent_document_type"),
			"Contact",
		)

	def test_wrong_parent_document_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Wrong Parent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
				parent_document_type="ToDo",
			)

	def test_junk_parent_document_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Junk Parent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
				parent_document_type="No Such Doctype 123",
			)

	def test_parent_document_type_rejected_for_non_child_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Parent NonChild"),
				document_type="ToDo",
				chart_type="Count",
				based_on="creation",
				parent_document_type="ToDo",
			)

	def test_duplicate_chart_name_returns_clean_error(self):
		# Dashboard Chart autonames on chart_name -> a repeat raises Frappe's
		# DuplicateEntryError (a NameError). _run_tool must translate it to the
		# {ok: False, error} envelope, not let it escape as an HTTP 500.
		from jarvis.api import _run_tool

		name = _h("JT Dup")
		args = {"chart_name": name, "document_type": "ToDo", "chart_type": "Count", "based_on": "creation"}
		create_dashboard_chart(**args)
		res = _run_tool("create_dashboard_chart", dict(args))
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "InvalidArgumentError")


class TestCreateDashboardChartChildTablePermissions(FrappeTestCase):
	"""F1 regression: a plain ``has_permission(document_type, "read")`` on a
	child (istable) doctype returns False for every non-admin (child tables
	carry no permissions of their own - see query.py's step-3 child-table
	gate and get_list.py's ``_readable_child_parents``). All the tests above
	run as Administrator, which bypasses permission checks entirely and so
	cannot catch that. These use a dedicated custom parent/child doctype pair
	and two non-admin users to prove the parent-qualified check actually
	works: one user who can read the parent, one who cannot.
	"""

	PARENT_DT = "JT Chart Perm Parent"
	CHILD_DT = "JT Chart Perm Child"
	ROLE = "JT Chart Perm Reader"
	USER_WITH_ACCESS = "jt-chart-perm-reader@example.com"
	USER_WITHOUT_ACCESS = "jt-chart-perm-outsider@example.com"

	@classmethod
	def _ensure_role(cls):
		if not frappe.db.exists("Role", cls.ROLE):
			frappe.get_doc(
				{"doctype": "Role", "role_name": cls.ROLE, "desk_access": 1, "is_custom": 1}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_user(cls, email, roles):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		user = frappe.get_doc("User", email)
		if "System Manager" in frappe.get_roles(email):
			user.remove_roles("System Manager")
		missing = [r for r in roles if r not in frappe.get_roles(email)]
		if missing:
			user.add_roles(*missing)

	@classmethod
	def _ensure_doctypes(cls):
		from frappe.core.doctype.doctype.test_doctype import new_doctype

		for dt in (cls.PARENT_DT, cls.CHILD_DT):
			if frappe.db.exists("DocType", dt):
				frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
		# Child table (istable) - carries no permissions of its own; access is
		# derived entirely from whoever can read the parent below.
		new_doctype(
			name=cls.CHILD_DT,
			custom=1,
			istable=1,
			fields=[{"label": "Category", "fieldname": "category", "fieldtype": "Data"}],
			permissions=[],
		).insert()
		new_doctype(
			name=cls.PARENT_DT,
			custom=1,
			fields=[{"label": "Items", "fieldname": "items", "fieldtype": "Table", "options": cls.CHILD_DT}],
			permissions=[{"role": cls.ROLE, "permlevel": 0, "read": 1}],
		).insert()

	@classmethod
	def _grant_dashboard_chart_create(cls):
		"""Layer a Custom DocPerm onto the CORE "Dashboard Chart" doctype so
		``cls.ROLE`` can create one - needed for the happy-path user to reach
		``doc.insert()`` at all. ``setup_custom_perms`` first copies the
		existing standard rows (System Manager/Dashboard Manager/Desk User)
		into Custom DocPerm so this ADDS a rule rather than replacing them -
		once any Custom DocPerm row exists for a doctype, Frappe's permission
		engine uses only those, ignoring the standard permissions.json rows.
		"""
		from frappe.permissions import setup_custom_perms

		setup_custom_perms("Dashboard Chart")
		if not frappe.db.exists(
			"Custom DocPerm", {"parent": "Dashboard Chart", "role": cls.ROLE, "permlevel": 0}
		):
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": "Dashboard Chart",
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": cls.ROLE,
					"permlevel": 0,
					"read": 1,
					"create": 1,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_role()
		cls._ensure_doctypes()
		cls._grant_dashboard_chart_create()
		cls._ensure_user(cls.USER_WITH_ACCESS, (cls.ROLE,))
		cls._ensure_user(cls.USER_WITHOUT_ACCESS, ())

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_non_admin_with_parent_read_can_chart_child_table(self):
		frappe.set_user(self.USER_WITH_ACCESS)
		res = create_dashboard_chart(
			chart_name=_h("JT Perm OK"),
			document_type=self.CHILD_DT,
			chart_type="Group By",
			group_by_based_on="category",
			parent_document_type=self.PARENT_DT,
		)
		self.assertTrue(res["name"])

	def test_non_admin_without_parent_read_rejected(self):
		frappe.set_user(self.USER_WITHOUT_ACCESS)
		with self.assertRaises(PermissionDeniedError):
			create_dashboard_chart(
				chart_name="x",
				document_type=self.CHILD_DT,
				chart_type="Group By",
				group_by_based_on="category",
				parent_document_type=self.PARENT_DT,
			)


class TestCreateDashboard(FrappeTestCase):
	def test_dashboard_links_a_chart(self):
		ch = create_dashboard_chart(
			chart_name=_h("JT Dash Chart"),
			document_type="ToDo",
			chart_type="Count",
			based_on="creation",
		)
		dash = create_dashboard(dashboard_name=_h("JT Dash"), charts=[ch["name"]])
		self.assertTrue(dash["name"])
		self.assertIn("dashboard", dash["url"])
		linked = frappe.get_all("Dashboard Chart Link", filters={"parent": dash["name"]}, pluck="chart")
		self.assertIn(ch["name"], linked)

	def test_empty_charts_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard(dashboard_name="x", charts=[])
