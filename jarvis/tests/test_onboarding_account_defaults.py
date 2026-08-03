"""onboarding.get_account_defaults — company prefill for the SPA Account step.

Ports the desk auto-fetch (commit 1507495) to a backend endpoint: the SPA has no
`frappe.defaults`, so the server resolves a default company (user/global default
→ sole Company, with a datalist list for several). Silent no-op on sites without
the Company doctype / read permission.

Email is not part of the payload — see the endpoint's docstring for why.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


class TestAccountDefaults(FrappeTestCase):
	def test_email_is_never_returned(self):
		"""Regression guard: the caller's email used to be prefilled into Work
		email, which on a fresh site is admin@example.com."""
		self.assertNotIn("email", onboarding.get_account_defaults())

	def test_user_default_company_wins(self):
		with (
			patch("frappe.defaults.get_user_default", return_value="Aerele"),
			patch("frappe.defaults.get_global_default", return_value="Other"),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "Aerele")

	def test_sole_company_autofills_when_no_default(self):
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			patch("frappe.get_all", return_value=[frappe._dict(name="Only Co")]),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "Only Co")
		self.assertEqual(out["companies"], ["Only Co"])

	def test_several_companies_no_autofill_but_listed(self):
		rows = [frappe._dict(name="A"), frappe._dict(name="B")]
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			patch("frappe.get_all", return_value=rows),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "")
		self.assertEqual(out["companies"], ["A", "B"])

	def test_no_company_doctype_is_silent_noop(self):
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			patch("frappe.get_all", side_effect=Exception("no Company doctype")),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "")
		self.assertEqual(out["companies"], [])
