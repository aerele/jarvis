import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_doc import get_doc


def _ensure_master_data():
	"""Make sure root + leaf Customer Group and Territory exist so Customer inserts succeed.

	ERPNext requires Customer.customer_group / territory to point at non-group records.
	"""
	if not frappe.db.exists("Customer Group", "All Customer Groups"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "All Customer Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer Group", "_Test Customer Group"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "_Test Customer Group",
				"is_group": 0,
				"parent_customer_group": "All Customer Groups",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Territory", "All Territories"):
		frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "All Territories",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Territory", "_Test Territory"):
		frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "_Test Territory",
				"is_group": 0,
				"parent_territory": "All Territories",
			}
		).insert(ignore_permissions=True)


class TestGetDoc(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_master_data()
		if not frappe.db.exists("Customer", "Jarvis Test Customer"):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "Jarvis Test Customer",
					"customer_type": "Company",
					"customer_group": "_Test Customer Group",
					"territory": "_Test Territory",
				}
			).insert(ignore_permissions=True)

	def test_returns_doc_by_name(self):
		result = get_doc(doctype="Customer", name="Jarvis Test Customer")
		self.assertEqual(result["name"], "Jarvis Test Customer")
		self.assertEqual(result["customer_name"], "Jarvis Test Customer")

	def test_rejects_missing_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_doc(doctype="", name="Jarvis Test Customer")

	def test_rejects_missing_name(self):
		with self.assertRaises(InvalidArgumentError):
			get_doc(doctype="Customer", name="")

	def test_rejects_unknown_doc(self):
		with self.assertRaises(InvalidArgumentError):
			get_doc(doctype="Customer", name="Definitely Not A Customer")

	def test_permission_check_blocks_unauthorized_user(self):
		user_email = "docless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Docless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				get_doc(doctype="Customer", name="Jarvis Test Customer")
		finally:
			frappe.set_user("Administrator")
