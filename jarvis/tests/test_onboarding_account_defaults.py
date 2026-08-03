"""onboarding.get_account_defaults — prefill for the SPA Account step.

Ports the desk auto-fetch (commit 1507495) to a backend endpoint: the SPA has no
`frappe.defaults`, so the server resolves the caller's email + a default company
(user/global default → sole Company, with a datalist list for several). Silent
no-op on sites without the Company doctype / read permission.

A reserved-domain email is dropped, so the field keeps its placeholder instead of
offering an address that cannot receive the receipts the step promises.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


class TestUndeliverable(FrappeTestCase):
	def test_reserved_domains(self):
		for addr in (
			"admin@example.com",  # Frappe's Administrator
			"guest@example.com",  # Frappe's Guest
			"x@example.org",
			"x@mail.example.com",  # subdomain of a reserved domain
			"cust-abc@jarvis.invalid",  # admin_v2's synthetic customer logins
			"x@foo.test",
			"x@localhost",
		):
			self.assertTrue(onboarding._is_undeliverable(addr), addr)

	def test_real_addresses_survive(self):
		# acme.com, not example.com: these must NOT match, so they cannot come
		# from a reserved domain. Same convention as the rest of the suite.
		for addr in (
			"manager@acme.com",
			"a.b+c@mail.acme.com",
			"x@examplex.com",  # not a reserved domain despite the prefix
			"x@example.co.in",
		):
			self.assertFalse(onboarding._is_undeliverable(addr), addr)

	def test_blank_is_not_undeliverable(self):
		# Nothing to reject; the caller already treats "" as "no prefill".
		self.assertFalse(onboarding._is_undeliverable(""))


class TestAccountDefaults(FrappeTestCase):
	def test_caller_email_is_prefilled(self):
		with patch("frappe.db.get_value", return_value="manager@acme.com"):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["email"], "manager@acme.com")

	def test_reserved_caller_email_is_dropped(self):
		# Mocked, not read off the live site: a bench whose Administrator has a
		# real address would otherwise fail this while the code is correct.
		with patch("frappe.db.get_value", return_value="admin@example.com"):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["email"], "")

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
