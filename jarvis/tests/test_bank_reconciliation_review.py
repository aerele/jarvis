"""Bank reconciliation review: config, custody, completeness and proposals-only staging."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import bank_reconciliation_review as review
from jarvis.chat import operator_review


def config(**over):
	value = {
		"company": "Example",
		"bank_account": "Example Current - Example Bank",
		"from_date": "2026-09-01",
		"to_date": "2026-09-30",
		"voucher_lookback_days": 30,
		"policy_version": "BANK-RECON-v1",
		"review_scope": "proposals_only",
	}
	value.update(over)
	return value


BANK = frappe._dict(company="Example", is_company_account=1, account="Example Current - EX", disabled=0)


class TestBankReconConfiguration(FrappeTestCase):
	def issues(self, **over):
		with (
			patch.object(review, "_bank_account", return_value=BANK),
			patch.object(frappe.utils, "today", return_value="2026-10-05"),
		):
			return review.configuration_issues(config(**over))

	def test_registered_as_operator_backend(self):
		self.assertIn(review.AGENT, operator_review.AGENTS)
		self.assertIs(operator_review.backend(review.AGENT), review)

	def test_valid_configuration_has_no_issues(self):
		self.assertEqual(self.issues(), {})

	def test_each_field_rule(self):
		for over, field in (
			({"company": ""}, "company"),
			({"bank_account": ""}, "bank_account"),
			({"from_date": "2026-9-1"}, "from_date"),
			({"to_date": "2026-08-31"}, "to_date"),
			({"to_date": "2026-10-06"}, "to_date"),
			({"voucher_lookback_days": 91}, "voucher_lookback_days"),
			({"voucher_lookback_days": "30"}, "voucher_lookback_days"),
			({"policy_version": ""}, "policy_version"),
			({"review_scope": "reconcile"}, "review_scope"),
		):
			self.assertIn(field, self.issues(**over), over)

	def test_bank_account_must_be_a_company_account_of_the_company(self):
		for bad in (
			frappe._dict(BANK, company="Other"),
			frappe._dict(BANK, is_company_account=0),
			frappe._dict(BANK, account=None),
			frappe._dict(BANK, disabled=1),
			None,
		):
			with (
				patch.object(review, "_bank_account", return_value=bad),
				patch.object(frappe.utils, "today", return_value="2026-10-05"),
			):
				self.assertIn("bank_account", review.configuration_issues(config()), bad)

	def test_scope_resolves_gl_and_voucher_window(self):
		with patch.object(review, "_bank_account", return_value=BANK):
			self.assertEqual(
				review.scope_from_config(config()),
				{
					"company": "Example",
					"bank_account": "Example Current - Example Bank",
					"bank_gl": "Example Current - EX",
					"from_date": "2026-09-01",
					"to_date": "2026-09-30",
					"voucher_from": "2026-08-02",
				},
			)

	def test_configuration_rechecks_bank_account_at_launch(self):
		inst = frappe._dict(config=frappe.as_json(config()))
		with (
			patch.object(review, "_bank_account", return_value=frappe._dict(BANK, disabled=1)),
			patch.object(frappe.utils, "today", return_value="2026-10-05"),
		):
			self.assertRaises(frappe.ValidationError, review.configuration, inst)

	def test_defaults_suggest_previous_month_and_never_save(self):
		with (
			patch.object(
				review.purchase_match,
				"configuration_defaults",
				return_value={
					"defaults": {
						"company": "Example",
						"policy_version": "AP-MATCH-v1",
						"basis": "current_records",
						"price_tolerance_percent": 0,
						"review_scope": "document_matching_only",
					},
					"site_date": "2026-10-05",
					"fiscal_years": [],
					"message": "",
				},
			),
			patch.object(
				frappe,
				"get_list",
				return_value=[{"name": "Example Current - Example Bank", "account": "Example Current - EX"}],
			),
		):
			result = review.configuration_defaults("Example")
		self.assertEqual(
			result["defaults"],
			{
				"company": "Example",
				"policy_version": "BANK-RECON-v1",
				"review_scope": "proposals_only",
				"voucher_lookback_days": 30,
				"from_date": "2026-09-01",
				"to_date": "2026-09-30",
				"bank_account": "Example Current - Example Bank",
			},
		)
