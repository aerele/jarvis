"""AC-B17: the hrms `EmployeePaymentEntry` controller keeps Payment Entry on the batch path.

hrms replaces the Payment Entry controller (`override_doctype_class`). The test skips on a site where that
class is not the one in force, so it never passes for the wrong reason.
"""

from unittest import SkipTest

import frappe

from jarvis.payment_calendar import batch_permission
from jarvis.tests.batch_permission_fixtures import PE, BatchCase, oracle, session_as

HRMS_CONTROLLER = "EmployeePaymentEntry"


def hrms_controller_in_force() -> bool:
	if "hrms" not in frappe.get_installed_apps():
		return False
	return any(base.__name__ == HRMS_CONTROLLER for base in frappe.model.document.get_controller(PE).__mro__)


class TestHrmsEmployeePaymentEntry(BatchCase):
	@classmethod
	def setUpClass(cls):
		if not hrms_controller_in_force():
			raise SkipTest("hrms EmployeePaymentEntry is not the Payment Entry controller on this site")
		super().setUpClass()

	def users(self):
		f = self.f
		return {"company": f.u1, "if_owner": f.u4, "select_only": f.u5, "no_up": f.u3}

	def test_the_plan_is_batch_with_the_hrms_controller(self):
		controller = frappe.model.document.get_controller(PE)
		self.assertIn(HRMS_CONTROLLER, [base.__name__ for base in controller.__mro__])
		for label, user in self.users().items():
			with self.subTest(user=label), session_as(user):
				plan = batch_permission.batch_plan(PE)
				self.assertEqual((plan.mode, plan.reason), ("batch", None))

	def test_the_batch_equals_the_oracle_for_every_user(self):
		names = self.f.names(PE)
		for ptype in ("read", "select"):
			for label, user in self.users().items():
				with self.subTest(user=label, ptype=ptype):
					plan, result = self.evaluate(user, PE, ptype)
					self.assertEqual(plan.mode, "batch")
					with session_as(user):
						expected = oracle(PE, names, ptype)
					self.assertEqual(result.visible, expected)
