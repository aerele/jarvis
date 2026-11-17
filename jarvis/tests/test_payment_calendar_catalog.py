"""The account catalog gate: one request gate, chunked account validation with a single-account fallback."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import catalog
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestCatalog(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_catalog")
		self.fx = CalendarFixtures()
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.calls = []
		self.denied = set()
		real = frappe.get_list

		def get_list(doctype, *args, **kwargs):
			if doctype == "Account":
				return [frappe._dict(name=f"Fake {i}", account_name=f"Fake {i}") for i in range(120)]
			return real(doctype, *args, **kwargs)

		patch("frappe.get_list", side_effect=get_list).start()
		patch.object(catalog, "validate_bank_accounts", side_effect=self.validate).start()

	def tearDown(self):
		patch.stopall()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_catalog")

	def validate(self, company, names):
		self.calls.append(list(names))
		if self.denied & set(names):
			raise PermissionDeniedError("PRIVATE")
		return names

	def test_every_account_is_validated_in_one_pass_of_chunks_of_fifty(self):
		accounts = catalog.permitted_accounts(self.fx.company)
		self.assertEqual([len(call) for call in self.calls], [50, 50, 20])
		self.assertEqual(len(accounts), 120)

	def test_a_failing_chunk_falls_back_to_single_accounts_for_that_chunk_only(self):
		self.denied = {"Fake 7"}
		accounts = catalog.permitted_accounts(self.fx.company)
		self.assertEqual([len(call) for call in self.calls], [50] + [1] * 50 + [50, 20])
		self.assertEqual(len(accounts), 119)
		self.assertNotIn("Fake 7", [account.name for account in accounts])
		self.assertEqual(
			[account.name for account in accounts][:8], [f"Fake {i}" for i in (0, 1, 2, 3, 4, 5, 6, 8)]
		)

	def test_the_request_gate_reads_no_accounts(self):
		with (
			patch.object(catalog, "require_report") as report,
			patch.object(catalog, "require_company") as company,
		):
			catalog.request_gate(self.fx.company)
		company.assert_called_once_with(self.fx.company)
		report.assert_called_once_with(self.fx.company, "Payable")
		self.assertEqual(self.calls, [])

	def test_validate_in_chunks_stops_at_the_first_failure(self):
		self.denied = {"Fake 7"}
		names = [f"Fake {i}" for i in range(120)]
		with self.assertRaises(PermissionDeniedError):
			catalog.validate_in_chunks(self.fx.company, names)
		self.assertEqual([len(call) for call in self.calls], [50])
