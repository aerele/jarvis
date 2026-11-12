"""The desktop shell tells the SPA whether to show the Payment Calendar entry points.

The flag is computed by ``visibility.can_view_payment_calendar``. Boot must survive any
failure of that check and answer False.
"""

import builtins
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import visibility
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.www import jarvis as www_desktop

HELPER = "jarvis.payment_calendar.visibility.can_view_payment_calendar"
REPORTER = "jarvis.holiday_touch.report.report"
APPS = "jarvis.chat.agent_installability.installed_apps"


def _boot() -> dict:
	ctx = frappe._dict()
	with (
		patch.object(www_desktop, "has_jarvis_access", return_value=True),
		patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
		patch.object(www_desktop, "is_skill_reviewer", return_value=False),
		patch.object(www_desktop, "support_scope", return_value=None),
		patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
	):
		www_desktop.get_context(ctx)
	return ctx.boot


class TestWwwPaymentCalendarBoot(TestCase):
	def test_flag_follows_the_helper(self):
		for answer in (True, False):
			with patch(HELPER, return_value=answer):
				self.assertIs(_boot()["can_view_payment_calendar"], answer)

	def test_helper_error_gives_false_and_boot_returns(self):
		with patch(HELPER, side_effect=RuntimeError("boom")), patch(REPORTER) as reporter:
			boot = _boot()
		self.assertIs(boot["can_view_payment_calendar"], False)
		reporter.assert_called_once()
		self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_visibility:boot")
		self.assertIn("holiday_touch", boot)
		self.assertIn("jarvis_worker_warning", boot)

	def test_import_error_of_the_package_gives_false_and_boot_returns(self):
		real_import = builtins.__import__

		def fail(name, *args, **kwargs):
			if name == "jarvis.payment_calendar" or name.startswith("jarvis.payment_calendar."):
				raise ImportError(name)
			return real_import(name, *args, **kwargs)

		# `from jarvis.payment_calendar import visibility` imports the package first.
		with patch.object(builtins, "__import__", side_effect=fail):
			boot = _boot()
		self.assertIs(boot["can_view_payment_calendar"], False)
		self.assertIn("jarvis_worker_warning", boot)


class TestPaymentCalendarFlagQueries(TestCase):
	"""The flag costs a handful of queries on ERPNext and none elsewhere."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_boot_queries")
		visibility.reset_contract_cache()
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_boot_queries")
		frappe.clear_document_cache("Report", "Accounts Payable")
		visibility.reset_contract_cache()

	def count_queries(self) -> tuple[bool, int]:
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			answer = visibility.can_view_payment_calendar()
		return answer, sql.call_count

	def test_supported_erpnext_site_stays_within_the_query_budget(self):
		user = self.fx.user(role="Accounts User")
		frappe.get_doc("User", user).add_roles("Jarvis User")
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		self.assertIs(visibility.can_view_payment_calendar(), True)  # warm the caches
		answer, queries = self.count_queries()
		self.assertIs(answer, True)
		self.assertLessEqual(queries, 5, queries)

	def test_site_without_erpnext_runs_no_query(self):
		with patch(APPS, return_value={"frappe", "jarvis"}):
			answer, queries = self.count_queries()
		self.assertIs(answer, False)
		self.assertEqual(queries, 0)
