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

	def test_unknown_doc_error_is_instructive(self):
		"""#1062: was "unknown Customer: <name>" - a delegate reading this had no
		next move. Names the DocType/name and points at the tool that finds
		valid ones."""
		with self.assertRaises(InvalidArgumentError) as ctx:
			get_doc(doctype="Customer", name="Definitely Not A Customer")
		self.assertEqual(
			str(ctx.exception),
			"No Customer named 'Definitely Not A Customer'. Use get_list to find valid names first.",
		)

	def test_missing_name_error_is_instructive(self):
		"""#1062: a non-single doctype with no name/names gets told the actual
		calling convention, including the single-doctype escape hatch."""
		with self.assertRaises(InvalidArgumentError) as ctx:
			get_doc(doctype="Customer")
		self.assertEqual(
			str(ctx.exception),
			"Pass name (one document) or names (a non-empty list). For a single "
			"record like Stock Settings call get_doc with only the doctype.",
		)

	def test_empty_names_list_error_is_instructive(self):
		"""names=[] is the other shape of "nothing to identify a document with" -
		same instructive message, not the old "must be a non-empty list"."""
		with self.assertRaises(InvalidArgumentError) as ctx:
			get_doc(doctype="Customer", names=[])
		self.assertIn("Pass name (one document) or names", str(ctx.exception))

	# ------------------------------------------------------------------ #
	# #1062 live evidence: a delegate calling get_doc on a Single (Stock
	# Settings) with names=[] or a bogus name got "names must be a non-empty
	# list of document names" / "unknown Stock Settings: x" - wasted tool
	# calls on a doctype that has exactly one document and needs no name at
	# all.
	# ------------------------------------------------------------------ #
	def test_single_doctype_returns_its_one_document_without_a_name(self):
		result = get_doc(doctype="Stock Settings")
		self.assertEqual(result["name"], "Stock Settings")
		self.assertEqual(result["doctype"], "Stock Settings")

	def test_single_doctype_ignores_a_bogus_name(self):
		"""name/names are meaningless for a Single - a delegate with nothing
		sensible to pass must still get the one document that exists, not a
		404 on a name it invented."""
		result = get_doc(doctype="Stock Settings", name="this-name-does-not-exist")
		self.assertEqual(result["name"], "Stock Settings")

	def test_single_doctype_ignores_an_empty_names_list(self):
		result = get_doc(doctype="Stock Settings", names=[])
		self.assertEqual(result["name"], "Stock Settings")

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

	def test_single_doctype_still_enforces_read_permission(self):
		"""#1062: frappe.get_single skips no permission check of its own - a
		Single carries real DocPerms (Stock Settings: Stock Manager / Sales
		User only), so a user holding neither must still be refused, not
		waved through because reading a Single takes no name to gate on."""
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
				get_doc(doctype="Stock Settings")
		finally:
			frappe.set_user("Administrator")
