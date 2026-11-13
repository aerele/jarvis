"""The shared row check: stored rows must still be visible to the actor now."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import access
from jarvis.payment_calendar.access import ScopeChanged
from jarvis.payment_calendar.permissions import visible_names
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

DENIED = set()
CALLS = []
RAISE = []
VISIBLE_NAMES = "jarvis.payment_calendar.access.visible_names"
HOOK = "jarvis.tests.test_payment_calendar_row_check.site_hook"


def site_hook(doc, ptype=None, user=None, debug=False):
	"""A stateful has_permission hook as a site could register it."""
	CALLS.append((doc.doctype, doc.name))
	if RAISE:
		raise RAISE[0]
	return doc.name not in DENIED


class RowCheckCase(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_row_check")
		DENIED.clear()
		CALLS.clear()
		RAISE.clear()
		self.fx = CalendarFixtures()
		self.pi = [self.fx.invoice((100,)), self.fx.invoice((50,))]
		self.source = read_obligations(
			self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)
		self.rows = self.source["rows"]
		self.columns = self.source["permission_columns"]
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.clear_cache(user=self.user)
		frappe.set_user(self.user)
		self.scope = {"actor": self.user, "company": self.fx.company}

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_row_check")
		for doctype in ("Purchase Invoice", "Supplier", "Journal Entry"):
			frappe.clear_cache(doctype=doctype)

	def hook(self, *doctypes):
		real = frappe.get_hooks

		def get_hooks(*args, **kwargs):
			value = real(*args, **kwargs)
			if args and args[0] == "has_permission":
				return {**value, **{doctype: [HOOK] for doctype in doctypes}}
			return value

		return patch("frappe.get_hooks", side_effect=get_hooks)

	def check(self, rows=None, parties=(), columns=None):
		columns = self.columns if columns is None else columns
		rows = self.rows if rows is None else rows
		access._check_rows(self.scope, "Payable", columns, rows, parties)

	def supplier(self, suffix):
		return self.fx.insert("Supplier", suffix, supplier_name=suffix, supplier_type="Company", disabled=0)

	def journal_entry(self, suffix, *parties):
		"""One voucher, one stored row per party, each with its own copy of a real proof."""
		voucher = self.fx.insert(
			"Journal Entry",
			suffix,
			company=self.fx.company,
			posting_date=self.fx.today,
			docstatus=1,
			bill_no=suffix,
			bill_date=self.fx.today,
		)
		rows = []
		for party in parties:
			row = deepcopy(self.rows[0])
			row.update(voucher_type="Journal Entry", voucher_no=voucher, party_type="Supplier", party=party)
			row["_permission"].update(
				voucher_type="Journal Entry", voucher_no=voucher, party_type="Supplier", party=party
			)
			rows.append(row)
		return rows


class TestCheckRows(RowCheckCase):
	def test_visible_rows_pass(self):
		self.assertEqual(len(self.rows), 2)
		self.assertIsNone(self.check())

	def test_no_rows_and_no_parties_make_no_call_at_all(self):
		with (
			patch(VISIBLE_NAMES) as names,
			patch("jarvis.payment_calendar.access.require_report") as report,
			patch("frappe.desk.query_report.get_filtered_data") as proof,
		):
			self.assertIsNone(self.check(rows=[], parties=()))
		names.assert_not_called()
		report.assert_not_called()
		proof.assert_not_called()

	def test_only_the_given_vouchers_and_parties_are_looked_up(self):
		with patch(VISIBLE_NAMES, wraps=access.visible_names) as names:
			self.check(rows=self.rows[:1], parties=[("Supplier", self.fx.supplier)])
		asked = {call.args[0]: set(call.args[1]) for call in names.call_args_list}
		voucher = self.rows[0]["voucher_no"]
		self.assertEqual(asked, {"Purchase Invoice": {voucher}, "Supplier": {self.fx.supplier}})

	def test_party_pairs_may_be_lists(self):
		other = self.supplier("ListPair")
		DENIED.add(other)
		self.check(rows=[], parties=[["Supplier", other]])
		with self.hook("Supplier"), self.assertRaises(ScopeChanged):
			self.check(rows=[], parties=[["Supplier", other]])

	def test_a_party_only_check_skips_the_proof(self):
		with patch("frappe.desk.query_report.get_filtered_data") as proof:
			self.check(rows=[], parties=[("Supplier", self.fx.supplier)])
		proof.assert_not_called()

	def test_a_hidden_voucher_refuses_the_whole_check(self):
		DENIED.add(self.rows[1]["voucher_no"])
		with self.hook("Purchase Invoice"), self.assertRaises(ScopeChanged):
			self.check()

	def test_the_hook_runs_once_per_returned_voucher(self):
		with self.hook("Purchase Invoice"):
			self.check()
		self.assertEqual(sorted(CALLS), sorted(("Purchase Invoice", row["voucher_no"]) for row in self.rows))

	def test_a_cancelled_voucher_refuses(self):
		frappe.db.set_value("Purchase Invoice", self.rows[0]["voucher_no"], "docstatus", 2)
		with self.assertRaises(ScopeChanged):
			self.check()

	def test_a_voucher_of_another_company_refuses(self):
		frappe.db.set_value("Purchase Invoice", self.rows[0]["voucher_no"], "company", "Another Co")
		with self.assertRaises(ScopeChanged):
			self.check()

	def test_a_hidden_party_in_the_party_list_refuses(self):
		other = self.supplier("Other")
		DENIED.add(other)
		with self.hook("Supplier"), self.assertRaises(ScopeChanged):
			self.check(rows=self.rows[:1], parties=[("Supplier", other)])

	def test_a_hidden_party_of_a_returned_row_refuses(self):
		DENIED.add(self.rows[0]["party"])
		with self.hook("Supplier"), self.assertRaises(ScopeChanged):
			self.check(rows=self.rows[:1])

	def test_every_party_of_a_multi_party_voucher_is_checked(self):
		second = self.supplier("Second")
		rows = self.journal_entry("JE1", self.fx.supplier, second)
		with self.hook("Supplier"):
			self.check(rows=rows)
			DENIED.add(second)
			with self.assertRaises(ScopeChanged):
				self.check(rows=rows)

	def test_a_damaged_proof_refuses(self):
		rows = deepcopy(self.rows)
		del rows[0]["_permission"]
		with self.assertRaises(ScopeChanged):
			self.check(rows=rows)

	def test_a_proof_the_report_filter_drops_refuses(self):
		with (
			patch("frappe.desk.query_report.get_filtered_data", return_value=[]),
			self.assertRaises(ScopeChanged),
		):
			self.check()

	def test_a_proof_without_columns_refuses(self):
		with self.assertRaises(ScopeChanged):
			self.check(columns=[{}])

	def test_an_attribute_error_from_a_hook_is_not_a_scope_change(self):
		RAISE.append(AttributeError("hook bug"))
		with self.hook("Purchase Invoice"), self.assertRaises(AttributeError):
			self.check()

	def test_a_runtime_error_from_a_hook_is_not_a_scope_change(self):
		RAISE.append(RuntimeError("hook bug"))
		with self.hook("Purchase Invoice"), self.assertRaises(RuntimeError):
			self.check()

	def test_a_validation_error_from_a_hook_is_not_a_scope_change(self):
		RAISE.append(frappe.ValidationError("hook says no"))
		with self.hook("Purchase Invoice"), self.assertRaises(frappe.ValidationError):
			self.check()

	def test_an_error_from_the_report_filter_is_not_a_scope_change(self):
		for error in (RuntimeError("filter bug"), frappe.ValidationError("filter says no")):
			with self.subTest(type(error).__name__):
				with (
					patch("frappe.desk.query_report.get_filtered_data", side_effect=error),
					self.assertRaises(type(error)) as caught,
				):
					self.check()
				self.assertNotIsInstance(caught.exception, ScopeChanged)

	def test_a_permission_error_from_a_hook_hides_the_voucher(self):
		RAISE.append(frappe.PermissionError("hidden"))
		with self.hook("Purchase Invoice"), self.assertRaises(ScopeChanged):
			self.check()

	def test_an_attribute_error_in_the_proof_rebuild_is_a_scope_change(self):
		rows = deepcopy(self.rows)
		rows[0]["_permission"] = None
		with self.assertRaises(ScopeChanged):
			self.check(rows=rows)

	def test_a_value_error_in_the_proof_rebuild_is_a_scope_change(self):
		with (
			patch("frappe.desk.query_report.get_filtered_data", side_effect=ValueError("bad shape")),
			self.assertRaises(ScopeChanged),
		):
			self.check()

	def test_a_name_list_that_the_lookup_cannot_find_is_not_visible(self):
		for error in (frappe.DoesNotExistError("gone"), frappe.PermissionError("hidden")):
			with self.subTest(type(error).__name__), patch.object(frappe, "get_list", side_effect=error):
				self.assertEqual(visible_names("Purchase Invoice", self.pi), set())

	def test_the_public_entry_point_is_the_same_check(self):
		DENIED.add(self.rows[0]["voucher_no"])
		with self.hook("Purchase Invoice"), self.assertRaises(ScopeChanged):
			access.verify_returned_rows(self.scope, "Payable", self.columns, self.rows)
		self.assertIsNone(access.verify_returned_rows(self.scope, "Payable", self.columns, []))


class TestValidateResultVisibility(RowCheckCase):
	def result(self, **source):
		return {"sources": {"Payable": {**self.source, **source}, "Receivable": None}}

	def test_visible_rows_pass_and_an_unavailable_side_is_skipped(self):
		self.assertIsNone(access.validate_result_visibility(self.scope, self.result()))

	def test_a_hidden_row_refuses(self):
		DENIED.add(self.rows[1]["voucher_no"])
		with self.hook("Purchase Invoice"), self.assertRaises(ScopeChanged):
			access.validate_result_visibility(self.scope, self.result())

	def test_excluded_rows_are_checked_with_the_rows(self):
		excluded = deepcopy(self.rows[0])
		excluded["party"] = excluded["_permission"]["party"] = self.supplier("Hidden")
		DENIED.add(excluded["party"])
		with self.hook("Supplier"), self.assertRaises(ScopeChanged):
			access.validate_result_visibility(self.scope, self.result(excluded_rows=[excluded]))

	def test_the_rows_and_the_excluded_rows_go_through_one_shared_check_per_side(self):
		with patch.object(access, "_check_rows", wraps=access._check_rows) as shared:
			access.validate_result_visibility(self.scope, self.result(excluded_rows=[]))
		shared.assert_called_once()
		self.assertEqual(shared.call_args.args[1], "Payable")
		self.assertEqual(len(shared.call_args.args[3]), len(self.rows))
