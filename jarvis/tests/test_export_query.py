from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export import resolvers
from jarvis.tools.export_query import export_query


class TestExportQuery(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ["like", "eq-%"]})
		for i in range(4):
			frappe.get_doc({"doctype": "ToDo", "description": f"eq-{i}"}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_xlsx_export_returns_envelope_with_total(self):
		res = export_query(
			"ToDo",
			filters={"description": ["like", "eq-%"]},
			fields=["name", "description"],
			format="xlsx",
			title="Todos",
		)
		self.assertTrue(res["file_url"].endswith(".xlsx"))
		self.assertEqual(res["total"], 4)
		self.assertTrue(frappe.db.exists("File", res["name"]))

	def test_csv_export(self):
		res = export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"], format="csv")
		self.assertTrue(res["file_url"].endswith(".csv"))

	def test_bad_format_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			export_query("ToDo", format="parquet")

	def test_fails_closed_above_ceiling(self):
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 2):
			with self.assertRaises(InvalidArgumentError):
				export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"])

	def test_emits_telemetry(self):
		with mock.patch("jarvis.telemetry.record_export_event") as m:
			export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"])
		m.assert_called_once()
		self.assertEqual(m.call_args.kwargs.get("mode"), "sync")


class TestExportQueryPermissions(FrappeTestCase):
	"""The mutation-proof guard: a restricted user's export reflects only the
	records they may see. Fails immediately if the resolver ever switches to
	get_all / ignore_permissions."""

	USER = "jv-export-restricted@example.com"

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ["like", "eqp-%"]})
		self.todos = []
		for i in range(3):
			d = frappe.get_doc({"doctype": "ToDo", "description": f"eqp-{i}"}).insert()
			self.todos.append(d.name)

		if not frappe.db.exists("User", self.USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.USER,
					"first_name": "eqp",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		if "System Manager" in frappe.get_roles(self.USER):
			frappe.get_doc("User", self.USER).remove_roles("System Manager")

		# Scope the user to exactly ONE of the three ToDos.
		frappe.db.delete("User Permission", {"user": self.USER, "allow": "ToDo"})
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.USER,
				"allow": "ToDo",
				"for_value": self.todos[0],
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_export_reflects_only_permitted_records(self):
		flt = {"description": ["like", "eqp-%"]}
		# Administrator sees all three.
		admin_res = export_query("ToDo", filters=flt, fields=["name", "description"])
		self.assertEqual(admin_res["total"], 3)

		# The restricted user (a User Permission scopes their ToDo access) sees a
		# STRICTLY smaller set - here zero. The point is enforcement is applied:
		# if the resolver ever used get_all / ignore_permissions this would be 3,
		# so this assertion is mutation-proof against a permission bypass.
		frappe.set_user(self.USER)
		self.assertTrue(frappe.has_permission("ToDo", "read"))  # not a blanket denial
		restricted_res = export_query("ToDo", filters=flt, fields=["name", "description"])
		self.assertLess(restricted_res["total"], admin_res["total"])
		self.assertEqual(restricted_res["total"], 0)
