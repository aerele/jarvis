from unittest import TestCase

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarIntegrity(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_integrity")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.db.rollback(save_point="calendar_integrity")

	def test_malformed_schedule_cannot_redefine_the_ledger_outstanding(self):
		self.fx.invoice((40, 30), gross_total=100)
		with self.assertRaises(CalendarSourceUnavailable):
			read_obligations(
				self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
			)
