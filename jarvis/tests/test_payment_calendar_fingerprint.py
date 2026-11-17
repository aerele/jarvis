"""Every input of the policy proof must change the hash, so no read trusts a stale proof."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import access
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

SAFE_EXEC = "jarvis.payment_calendar.access.is_safe_exec_enabled"
# The doctypes the Accounts Payable / Receivable columns link to (Link `options`), plus the two the plan names.
REPORT_LINK_DOCTYPES = (
	"Contact",
	"Currency",
	"Customer Group",
	"DocType",
	"Payment Term",
	"Project",
	"Sales Person",
	"Supplier Group",
	"Territory",
)


class TestPaymentCalendarFingerprint(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_fingerprint")
		self.fx = CalendarFixtures()
		self.fx.invoice((100,))
		self.scope = normalize_scope(self.fx.company, [self.fx.bank])

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_fingerprint")
		frappe.clear_cache(doctype="Purchase Invoice")

	def fingerprint(self):
		return access.policy_fingerprint(self.scope)

	def add_query_script(self, doctype="Purchase Invoice", **fields):
		doc = frappe.get_doc(
			{
				"doctype": "Server Script",
				"name": self.fx.prefix + "QueryScript",
				"script_type": "Permission Query",
				"reference_doctype": doctype,
				"script": "conditions = ''",
				**fields,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_the_declared_inputs_are_exactly_the_hashed_inputs(self):
		self.assertEqual(set(access.FINGERPRINT_INPUTS), set(access.fingerprint_data(self.scope)))
		self.assertEqual(len(access.FINGERPRINT_INPUTS), len(set(access.FINGERPRINT_INPUTS)))

	def test_the_new_inputs_are_declared(self):
		for name in ("server_scripts", "server_script_enabled", "domains"):
			self.assertIn(name, access.FINGERPRINT_INPUTS)

	def test_the_doctypes_the_report_columns_link_to_are_hashed(self):
		self.assertLessEqual(set(REPORT_LINK_DOCTYPES), set(access._doctypes()))

	def test_a_property_change_on_a_report_link_doctype_changes_the_hash(self):
		for doctype in REPORT_LINK_DOCTYPES:
			with self.subTest(doctype):
				before = self.fingerprint()
				frappe.get_doc(
					{
						"doctype": "Property Setter",
						"doctype_or_field": "DocType",
						"doc_type": doctype,
						"property": "track_changes",
						"property_type": "Check",
						"value": "1",
					}
				).insert(ignore_permissions=True)
				self.assertNotEqual(self.fingerprint(), before)

	def test_the_report_link_columns_still_name_the_hashed_doctypes(self):
		"""A new Link column in the report would need its doctype here: read them from the report source."""
		import re

		from erpnext.accounts.report.accounts_receivable import accounts_receivable

		with open(accounts_receivable.__file__) as source:
			text = source.read()
		linked = set(re.findall(r'options="([A-Z][A-Za-z ]+)"', text))
		self.assertLessEqual(linked, set(access._doctypes()))

	def test_every_declared_input_changes_the_hash(self):
		data = access.fingerprint_data(self.scope)
		base = access.fingerprint_hash(data)
		self.assertEqual(base, self.fingerprint())
		for name in access.FINGERPRINT_INPUTS:
			with self.subTest(name):
				changed = {**data, name: ["changed", data[name]]}
				self.assertNotEqual(access.fingerprint_hash(changed), base)

	def test_a_permission_query_script_for_a_calendar_doctype_changes_the_hash(self):
		before = self.fingerprint()
		self.add_query_script()
		self.assertNotEqual(self.fingerprint(), before)

	def test_editing_the_query_script_changes_the_hash_even_without_a_new_modified(self):
		doc = self.add_query_script()
		before = self.fingerprint()
		frappe.db.set_value("Server Script", doc.name, "script", "conditions = '1=0'", update_modified=False)
		self.assertNotEqual(self.fingerprint(), before)

	def test_disabling_the_query_script_changes_the_hash(self):
		doc = self.add_query_script()
		before = self.fingerprint()
		frappe.db.set_value("Server Script", doc.name, "disabled", 1, update_modified=False)
		self.assertNotEqual(self.fingerprint(), before)

	def test_a_query_script_for_another_doctype_or_type_does_not_change_the_hash(self):
		before = self.fingerprint()
		self.add_query_script(doctype="ToDo")
		self.assertEqual(self.fingerprint(), before)

	def test_toggling_server_script_execution_changes_the_hash(self):
		with patch(SAFE_EXEC, return_value=False):
			off = self.fingerprint()
		with patch(SAFE_EXEC, return_value=True):
			on = self.fingerprint()
		self.assertNotEqual(on, off)

	def test_saving_domain_settings_with_a_toggled_domain_changes_the_hash(self):
		before = self.fingerprint()
		settings = frappe.get_doc("Domain Settings")
		self.assertNotIn("Retail", [row.domain for row in settings.active_domains])
		settings.append("active_domains", {"domain": "Retail"})
		settings.save(ignore_permissions=True)
		self.assertNotEqual(self.fingerprint(), before)
