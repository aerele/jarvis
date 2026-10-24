"""Real source/Redis/job/API permission flow under the test runner's transaction.

The dedicated snapshot suite proves physical read-view isolation. Here only that
transaction boundary is replaced so all fixtures/permission changes roll back.
Financial readers and every authorization/delivery gate are real.
"""

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import api, jobs, snapshot, store
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


@contextmanager
def test_read_view():
	yield {("get_value", "defaults::__default"), ("get_value", "document_cache::Company::fixture")}


class TestPaymentCalendarDeliveryScope(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_delivery_scope")
		self.fx = CalendarFixtures()
		self.invoice = self.fx.invoice((100,))
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_delivery_scope")
		for doctype in ("Journal Entry", "Payment Entry"):
			frappe.clear_cache(doctype=doctype)

	def deliver(self, hidden):
		frappe.set_user(self.user)
		scope = normalize_scope(self.fx.company, [])
		with (
			patch.object(frappe, "enqueue", return_value=object()),
			patch.object(snapshot, "consistent_read", test_read_view),
		):
			run = jobs.request_snapshot(scope)
			jobs.run_snapshot(actor=self.user, snapshot_id=run["snapshot_id"])
		data = api.get_snapshot_status(run["snapshot_id"])
		self.assertTrue(data["ok"], data)
		self.assertEqual(data["data"]["state"], "complete", data)
		self.assertFalse(data["data"]["projection"]["forecast_available"])
		cached = store.load_result(self.user, run["snapshot_id"])
		self.assertFalse(any(row["voucher_no"] == hidden for row in cached["sources"]["Payable"]["rows"]))
		self.assertEqual(cached["sources"]["Payable"]["canonical_total"], "100.00")

	def test_no_payment_entry_read_delivers_permitted_invoice_dues(self):
		self.fx.ple("Advance", "Payment Entry", self.fx.prefix + "Advance", -25)
		hidden = self.fx.prefix + "Advance"
		frappe.db.delete("Custom DocPerm", {"parent": "Payment Entry"})
		self.fx.insert(
			"Custom DocPerm",
			"PERestriction",
			parent="Payment Entry",
			role="System Manager",
			permlevel=0,
			read=1,
			report=1,
		)
		frappe.clear_cache(doctype="Payment Entry")
		self.deliver(hidden)

	def test_owner_only_journal_entry_delivers_permitted_invoice_dues(self):
		hidden = self.fx.insert(
			"Journal Entry", "Journal", company=self.fx.company, posting_date=self.fx.today, docstatus=1
		)
		self.fx.ple("JournalPLE", "Journal Entry", hidden, 30, voucher_no=hidden)
		frappe.db.set_value(
			"Payment Ledger Entry",
			self.fx.prefix + "JournalPLE",
			{"against_voucher_type": "Journal Entry", "against_voucher_no": hidden},
		)
		frappe.db.delete("Custom DocPerm", {"parent": "Journal Entry"})
		self.fx.insert(
			"Custom DocPerm",
			"JERestriction",
			parent="Journal Entry",
			role="Accounts Manager",
			permlevel=0,
			read=1,
			report=1,
			if_owner=1,
		)
		frappe.clear_cache(doctype="Journal Entry")
		self.deliver(hidden)

	def test_cost_center_restricted_payment_advance_does_not_block_delivery(self):
		a = self.fx.insert(
			"Cost Center",
			"AllowedCost",
			cost_center_name="Calendar allowed",
			company=self.fx.company,
			is_group=0,
		)
		b = self.fx.insert(
			"Cost Center",
			"HiddenCost",
			cost_center_name="Calendar hidden",
			company=self.fx.company,
			is_group=0,
		)
		self.fx.ple("Advance", "Payment Entry", self.fx.prefix + "Advance", -25)
		hidden = self.fx.prefix + "Advance"
		frappe.db.set_value("Payment Entry", hidden, "cost_center", b)
		frappe.db.set_value("Purchase Invoice", self.invoice, "cost_center", a)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Cost Center",
				"for_value": a,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.user)
		self.deliver(hidden)
