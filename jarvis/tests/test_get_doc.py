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

	def test_single_doctype_with_names_returns_the_batch_envelope(self):
		"""Review fix: the Single short-circuit used to return the plain
		document dict even when `names` was passed, silently ignoring that
		the caller asked for the batch shape. A non-empty `names` on a
		Single now gets the same {doctype, docs, count} envelope any other
		doctype's batch call gets, wrapping that one document - names'
		CONTENT is still meaningless (a Single has exactly one document,
		whose name IS the doctype), only its presence switches the shape."""
		result = get_doc(doctype="Stock Settings", names=["this-name-does-not-exist"])
		self.assertEqual(result["doctype"], "Stock Settings")
		self.assertEqual(result["count"], 1)
		self.assertEqual(len(result["docs"]), 1)
		self.assertEqual(result["docs"][0]["name"], "Stock Settings")

	def test_single_doctype_without_names_still_returns_the_flat_dict(self):
		"""The other half of the same fix: `name`/no-args at all must keep
		the pre-existing flat-dict shape - only an explicit non-empty
		`names` switches to the batch envelope."""
		result = get_doc(doctype="Stock Settings")
		self.assertNotIn("docs", result)
		self.assertEqual(result["name"], "Stock Settings")

		result = get_doc(doctype="Stock Settings", name="whatever")
		self.assertNotIn("docs", result)
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
			with self.assertRaises(PermissionDeniedError):
				get_doc(doctype="Customer", name="Jarvis Test Customer", names=[])
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


class TestGetDocOptionalArguments(FrappeTestCase):
	"""Exercise real reads using a built-in DocType, independent of ERPNext fixtures."""

	def setUp(self):
		super().setUp()
		self.document_name = (
			frappe.get_doc({"doctype": "ToDo", "description": "Lookup contract test"}).insert().name
		)

	def test_empty_optional_batch_keeps_single_record_shape(self):
		result = get_doc(doctype="ToDo", name=self.document_name, names=[])
		self.assertEqual(result["name"], self.document_name)
		self.assertNotIn("docs", result)

	def test_nonempty_batch_keeps_batch_shape(self):
		result = get_doc(doctype="ToDo", names=[self.document_name])
		self.assertEqual(result["count"], 1)
		self.assertEqual(result["docs"][0]["name"], self.document_name)

	def test_rejects_conflicting_single_and_batch_arguments(self):
		with self.assertRaisesRegex(InvalidArgumentError, "either name or names"):
			get_doc(doctype="ToDo", name=self.document_name, names=["Another ToDo"])

	def test_empty_batch_does_not_hide_missing_record(self):
		with self.assertRaisesRegex(InvalidArgumentError, "No ToDo named"):
			get_doc(doctype="ToDo", name="Definitely Not A Customer", names=[])

	def test_empty_batch_still_checks_record_permission(self):
		from unittest.mock import patch

		with patch("jarvis.tools.get_doc.frappe.has_permission", return_value=False) as permission:
			with self.assertRaises(PermissionDeniedError):
				get_doc(doctype="ToDo", name=self.document_name, names=[])
			self.assertEqual(permission.call_args.kwargs["ptype"], "read")
			self.assertEqual(permission.call_args.kwargs["doc"].name, self.document_name)

	def test_empty_batch_without_a_name_is_invalid(self):
		with self.assertRaises(InvalidArgumentError):
			get_doc(doctype="ToDo", names=[])

	def test_malformed_batch_with_a_name_is_invalid(self):
		with self.assertRaises(InvalidArgumentError):
			get_doc(doctype="ToDo", name=self.document_name, names="")

	def test_single_doctype_preserves_flat_and_batch_shapes(self):
		flat = get_doc(doctype="System Settings", name="ignored", names=[])
		self.assertEqual(flat["name"], "System Settings")
		self.assertNotIn("docs", flat)
		batch = get_doc(doctype="System Settings", names=["ignored"])
		self.assertEqual(batch["count"], 1)
		self.assertEqual(batch["docs"][0]["name"], "System Settings")
