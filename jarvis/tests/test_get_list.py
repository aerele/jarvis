import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
	ResultTooLargeError,
)
from jarvis.tools.get_list import ROW_GUARD, get_list


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


class TestGetList(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_master_data()
		for name in ("Jarvis List A", "Jarvis List B"):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": name,
						"customer_type": "Company",
						"customer_group": "_Test Customer Group",
						"territory": "_Test Territory",
					}
				).insert(ignore_permissions=True)

		# Child-table (istable) fixture: a Contact with an email row. Contact
		# (parent) owns "Contact Email" (child) via its ``email_ids`` Table
		# field - a core Frappe pair, so the child-table tests need no ERPNext
		# Timesheet setup while exercising the same istable mechanism.
		contact_fn = "Jarvis TS Contact"
		existing = frappe.db.get_value("Contact", {"first_name": contact_fn})
		if existing:
			cls.contact_name = existing
		else:
			cls.contact_name = (
				frappe.get_doc(
					{
						"doctype": "Contact",
						"first_name": contact_fn,
						"email_ids": [{"email_id": "jarvis-ts@example.com", "is_primary": 1}],
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

	def test_returns_rows(self):
		rows = get_list(
			doctype="Customer",
			fields=["name", "customer_name"],
			filters={"customer_name": ["like", "Jarvis List%"]},
		)
		names = {r["name"] for r in rows}
		self.assertEqual(names, {"Jarvis List A", "Jarvis List B"})

	def test_respects_limit(self):
		rows = get_list(
			doctype="Customer",
			fields=["name"],
			filters={"customer_name": ["like", "Jarvis List%"]},
			limit=1,
		)
		self.assertEqual(len(rows), 1)

	def test_rejects_excessive_limit(self):
		with self.assertRaises(InvalidArgumentError):
			get_list(doctype="Customer", fields=["name"], limit=5000)

	def test_rejects_missing_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_list(doctype="", fields=["name"])

	def test_row_guard_fires_above_threshold(self):
		"""A get_list call that returns more than ROW_GUARD rows must
		raise ResultTooLargeError when confirm_large is not set. DocType
		is a stable, populated system table on any Frappe site - hundreds
		of rows guaranteed - so we use it as the fixture and don't need
		to seed."""
		with self.assertRaises(ResultTooLargeError) as cm:
			get_list(
				doctype="DocType",
				fields=["name"],
				limit=ROW_GUARD + 50,
			)
		err = cm.exception
		self.assertGreater(err.row_count, ROW_GUARD)
		self.assertEqual(err.limit, ROW_GUARD)
		self.assertEqual(err.tool, "get_list")
		self.assertIn("confirm_large", str(err))

	def test_row_guard_silent_under_threshold(self):
		rows = get_list(
			doctype="DocType",
			fields=["name"],
			limit=ROW_GUARD - 50,
		)
		self.assertLessEqual(len(rows), ROW_GUARD - 50)

	def test_row_guard_opt_in_bypasses(self):
		rows = get_list(
			doctype="DocType",
			fields=["name"],
			limit=ROW_GUARD + 50,
			confirm_large=True,
		)
		self.assertGreater(len(rows), ROW_GUARD)

	def test_permission_check_blocks_unauthorized_user(self):
		user_email = "listless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Listless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				get_list(doctype="Customer", fields=["name"])
		finally:
			frappe.set_user("Administrator")

	def test_child_table_without_parent_doctype_errors(self):
		"""A direct get_list on a child (istable) DocType with no parent_doctype
		must raise a clear InvalidArgumentError that names the parent - not the
		opaque 'no read permission' the old code produced."""
		with self.assertRaises(InvalidArgumentError) as cm:
			get_list(doctype="Contact Email", fields=["email_id"])
		msg = str(cm.exception)
		self.assertIn("child table", msg)
		self.assertIn("parent_doctype", msg)
		self.assertIn("Contact", msg)  # the discovered owning DocType

	def test_child_table_with_parent_doctype_reads_rows(self):
		"""With parent_doctype supplied the child rows are readable (here as
		Administrator - proves the arg is forwarded and the query runs)."""
		rows = get_list(
			doctype="Contact Email",
			fields=["parent", "email_id"],
			filters={"parent": self.contact_name},
			parent_doctype="Contact",
		)
		self.assertIn("jarvis-ts@example.com", {r["email_id"] for r in rows})

	def test_child_table_permission_derived_from_parent(self):
		"""A user who cannot read the parent DocType is denied the child rows
		even with parent_doctype set - access derives from the parent, so this
		is not a permission bypass. Uses the reported Timesheet Detail / Timesheet
		pair: Timesheet is role-restricted, and the perm check fires before any
		rows are fetched so no Timesheet needs to exist."""
		user_email = "childless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Childless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				get_list(
					doctype="Timesheet Detail",
					fields=["activity_type"],
					parent_doctype="Timesheet",
				)
		finally:
			frappe.set_user("Administrator")

	def test_parent_doctype_harmless_for_non_child(self):
		"""Passing parent_doctype on a normal (non-child) DocType is ignored."""
		rows = get_list(
			doctype="Customer",
			fields=["name"],
			filters={"customer_name": ["like", "Jarvis List%"]},
			parent_doctype="Anything",
		)
		self.assertEqual({r["name"] for r in rows}, {"Jarvis List A", "Jarvis List B"})
