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
			# The reserved names standing alone, not just as suffixes. Only
			# localhost used to be caught here; the other three slipped through
			# because "test" does not end with ".test".
			"x@test",
			"x@example",
			"x@invalid",
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

	def test_root_anchored_form_is_reserved(self):
		# "example.com." names the same domain; the trailing dot must not smuggle
		# it past the suffix match.
		self.assertTrue(onboarding._is_undeliverable("admin@example.com."))

	def test_case_and_whitespace_are_normalised(self):
		self.assertTrue(onboarding._is_undeliverable("Admin@EXAMPLE.CoM"))
		self.assertTrue(onboarding._is_undeliverable("admin@  example.com  "))

	def test_last_at_wins(self):
		# rpartition: the domain is what follows the FINAL "@".
		self.assertTrue(onboarding._is_undeliverable("a@b@example.com"))
		self.assertFalse(onboarding._is_undeliverable("a@example.com@acme.com"))

	def test_blank_is_not_undeliverable(self):
		# Nothing to reject; the caller already treats "" as "no prefill".
		for addr in ("", "noatsign", "x@", "x@."):
			self.assertFalse(onboarding._is_undeliverable(addr), addr)


def _caller_email(value):
	"""Mock ONLY the endpoint's caller-email lookup — ``frappe.db.get_value("User",
	user, "email")`` — and delegate every other ``get_value`` to the real function.

	A blanket ``return_value`` would answer any get_value the endpoint grows later,
	which would then silently receive an email address. A blanket ``side_effect`` is
	worse: on a cold cache Frappe's own first read of the User doctype meta goes
	through this same ``frappe.db.get_value``, so intercepting it broadly poisons the
	meta cache (``DocType None not found``) for every later test in the process.
	Matching the exact ``("User", …, "email")`` shape keeps the mock to the one call
	the endpoint makes."""
	real_get_value = frappe.db.get_value

	def side_effect(doctype, *args, **kwargs):
		fieldname = kwargs.get("fieldname", args[1] if len(args) >= 2 else None)
		if doctype == "User" and fieldname == "email":
			return value
		return real_get_value(doctype, *args, **kwargs)

	return patch("frappe.db.get_value", side_effect=side_effect)


def _company_rows(rows=(), *, raises=False):
	"""Mock ONLY the endpoint's ``frappe.get_all("Company", …)`` query and delegate
	every other ``get_all`` to the real function.

	Patching ``frappe.get_all`` broadly intercepts Frappe's own internal ``get_all``
	calls (metadata, permissions) whenever the cache is cold, which fails those reads
	and poisons the process. Narrowing to ``doctype == "Company"`` leaves Frappe's
	machinery untouched while still standing in for the single query under test."""
	real_get_all = frappe.get_all

	def side_effect(doctype, *args, **kwargs):
		if doctype == "Company":
			if raises:
				raise Exception("no Company doctype")
			return list(rows)
		return real_get_all(doctype, *args, **kwargs)

	return patch("frappe.get_all", side_effect=side_effect)


class TestAccountDefaults(FrappeTestCase):
	def test_caller_email_is_prefilled(self):
		with _caller_email("manager@acme.com"):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["email"], "manager@acme.com")

	def test_reserved_caller_email_is_dropped(self):
		# Mocked, not read off the live site: a bench whose Administrator has a
		# real address would otherwise fail this while the code is correct.
		with _caller_email("admin@example.com"):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["email"], "")

	def test_user_default_company_wins(self):
		# _company_rows([]) stands in for the Company query so the test states its
		# point -- user default beats global default -- on a frappe-only site too,
		# where the real "Company" doctype (ERPNext) does not exist and the endpoint
		# would otherwise fall into its silent no-op and blank the company out.
		with (
			patch("frappe.defaults.get_user_default", return_value="Aerele"),
			patch("frappe.defaults.get_global_default", return_value="Other"),
			_company_rows([]),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "Aerele")

	def test_sole_company_autofills_when_no_default(self):
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			_company_rows([frappe._dict(name="Only Co")]),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "Only Co")
		self.assertEqual(out["companies"], ["Only Co"])

	def test_several_companies_no_autofill_but_listed(self):
		rows = [frappe._dict(name="A"), frappe._dict(name="B")]
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			_company_rows(rows),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "")
		self.assertEqual(out["companies"], ["A", "B"])

	def test_no_company_doctype_is_silent_noop(self):
		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			_company_rows(raises=True),
		):
			out = onboarding.get_account_defaults()
		self.assertEqual(out["company"], "")
		self.assertEqual(out["companies"], [])

	def test_erpnext_installed_true_when_present(self):
		# Uses the onboarding._installed_apps seam (see jarvis#812), never
		# frappe.get_installed_apps directly.
		with (
			patch.object(onboarding, "_installed_apps", return_value={"frappe", "erpnext"}),
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			_company_rows([frappe._dict(name="Acme")]),
		):
			out = onboarding.get_account_defaults()
		self.assertTrue(out["erpnext_installed"])

	def test_erpnext_installed_false_when_absent(self):
		with (
			patch.object(onboarding, "_installed_apps", return_value={"frappe"}),
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.defaults.get_global_default", return_value=None),
			_company_rows([]),
		):
			out = onboarding.get_account_defaults()
		self.assertFalse(out["erpnext_installed"])
