"""Bounded launcher scope for the statutory payroll deposit-status agent."""

import json
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import agent_scheduler, agent_scope
from jarvis.exceptions import InvalidArgumentError

AGENT = "statutory-payroll-deposit-status"
ACCOUNT_GROUP = "Payroll Liabilities - FC"
DEPOSIT_CONFIG = {
	"account_group": ACCOUNT_GROUP,
	"liability_accounts": {"PF Liability - FC": "pf", "ESI Liability - FC": "esi"},
	"vouchers": [
		{
			"doctype": "Journal Entry",
			"name": "JE-DEP-TEST",
			"liability_month": "2024-04",
		}
	],
	"settlement_as_of": "2024-05-31",
	"reviewer": "reviewer@example.test",
	"reviewer_capacity": True,
}
BASE_SCOPE = {
	"company": "Fake Company",
	"fiscal_year": "2024-2025",
	"from_date": "2024-04-01",
	"to_date": "2024-04-30",
	"report_date": "2024-05-31",
	"prior_fy_start": "2023-04-01",
	"prior_fy_end": "2024-03-31",
}


def _installation(
	agent=AGENT,
	account_group=ACCOUNT_GROUP,
	from_date="2024-04-01",
	to_date="2024-04-30",
	fiscal_year=None,
	settlement_as_of="2024-05-31",
):
	config = {
		"deposit": {
			**DEPOSIT_CONFIG,
			"account_group": account_group,
			"settlement_as_of": settlement_as_of,
		},
		"from_date": from_date,
		"to_date": to_date,
	}
	if fiscal_year:
		config["fiscal_year"] = fiscal_year
	return frappe._dict(
		{
			"name": "DEP-INST-1",
			"agent": agent,
			"config": json.dumps(config),
		}
	)


def _account(**overrides):
	account = frappe._dict(
		{
			"name": ACCOUNT_GROUP,
			"company": "Fake Company",
			"is_group": 1,
			"disabled": 0,
			**overrides,
		}
	)
	account.check_permission = Mock()
	return account


def _account_docs(root=None):
	root = root or _account()
	docs = {root.name: root}
	for name in DEPOSIT_CONFIG["liability_accounts"]:
		docs[name] = _account(name=name, is_group=0, parent_account=root.name)
	return docs


class TestPayrollDepositScope(TestCase):
	def test_exact_agent_delivers_only_a_readable_account_group_scope(self):
		account = _account()
		docs = _account_docs(account)
		with (
			patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			patch.object(
				agent_scope.frappe,
				"get_doc",
				side_effect=lambda doctype, name: docs[name],
			) as get_doc,
		):
			scope = agent_scope.resolve_scope(_installation())
		self.assertEqual(scope["record_scope"], {"doctype": "Account", "name": ACCOUNT_GROUP})
		self.assertEqual(get_doc.call_count, len(docs))
		for doc in docs.values():
			doc.check_permission.assert_called_once_with("read")

	def test_other_agent_cannot_gain_a_record_scope_from_deposit_configuration(self):
		with (
			patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			patch.object(agent_scope.frappe, "get_doc") as get_doc,
		):
			scope = agent_scope.resolve_scope(_installation(agent="close-auditor"))
		self.assertNotIn("record_scope", scope)
		get_doc.assert_not_called()

	def test_deposit_scope_rejects_fiscal_year_and_an_implicit_period(self):
		for kwargs in ({"fiscal_year": "2024-2025"}, {"from_date": None}, {"to_date": None}):
			with self.subTest(kwargs=kwargs):
				with (
					patch.object(agent_scope, "_resolve") as resolve,
					patch.object(agent_scope.frappe, "get_doc") as get_doc,
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope.resolve_scope(_installation(**kwargs))
				resolve.assert_not_called()
				get_doc.assert_not_called()

	def test_deposit_scope_requires_a_completed_month_and_cutoff_before_account_reads(self):
		for scope, installation in (
			(
				{**BASE_SCOPE, "from_date": "2024-04-02"},
				_installation(from_date="2024-04-02"),
			),
			(
				{**BASE_SCOPE, "to_date": "2024-04-29"},
				_installation(to_date="2024-04-29"),
			),
			(
				dict(BASE_SCOPE),
				_installation(settlement_as_of="2024-04-29"),
			),
			(
				{**BASE_SCOPE, "report_date": "2024-04-29"},
				_installation(),
			),
		):
			with self.subTest(scope=scope, installation=installation.config):
				with (
					patch.object(agent_scope, "_resolve", return_value=scope),
					patch.object(agent_scope.frappe, "get_doc") as get_doc,
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope.resolve_scope(installation)
				get_doc.assert_not_called()

	def test_unreadable_or_forged_account_group_is_refused_before_bundle_delivery(self):
		for account in (
			_account(company="Other Company"),
			_account(is_group=0),
			_account(disabled=1),
		):
			with self.subTest(account=account):
				with (
					patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
					patch.object(agent_scope.frappe, "get_doc", return_value=account),
					self.assertRaises(InvalidArgumentError),
				):
					agent_scope.resolve_scope(_installation())
		with (
			patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			patch.object(
				agent_scope.frappe,
				"get_doc",
				side_effect=frappe.PermissionError("Account is not readable"),
			),
			self.assertRaises(frappe.PermissionError),
		):
			agent_scope.resolve_scope(_installation())

	def test_liability_mapping_cannot_expand_the_configured_account_group(self):
		root = _account()
		docs = _account_docs(root)
		docs["PF Liability - FC"].parent_account = "Other Liability Group"
		with (
			patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			patch.object(
				agent_scope.frappe,
				"get_doc",
				side_effect=lambda doctype, name: docs[name],
			),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scope.resolve_scope(_installation())

	def test_prompt_carries_the_bounded_scope_and_only_the_declared_namespace(self):
		listing = frappe._dict({"name": AGENT, "agent_slug": AGENT, "config_keys": ["deposit"]})
		inst = frappe._dict(
			{
				"name": "DEP-INST-1",
				"config": frappe.as_json(
					{
						"deposit": DEPOSIT_CONFIG,
						"unrelated": {"must_not_reach_delegate": True},
					}
				),
			}
		)
		self.assertEqual(agent_scheduler._explicit_config(listing, inst), {"deposit": DEPOSIT_CONFIG})
		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			message = agent_scheduler._audit_prompt(
				listing,
				inst,
				trigger="manual",
				scope={
					**BASE_SCOPE,
					"record_scope": {
						"doctype": "Account",
						"name": ACCOUNT_GROUP,
						"private_extra": "must_not_reach_delegate",
					},
				},
			)
		self.assertIn('record_scope={"doctype": "Account", "name": "Payroll Liabilities - FC"}', message)
		self.assertIn(json.dumps({"deposit": DEPOSIT_CONFIG}, sort_keys=True), message)
		self.assertNotIn("private_extra", message)
		self.assertNotIn("unrelated", message)

	def test_invalid_deposit_scope_aborts_before_an_unscoped_delegate_run(self):
		inst = frappe._dict({"agent": AGENT})
		with (
			patch(
				"jarvis.chat.agent_scope.resolve_scope",
				side_effect=InvalidArgumentError("deposit account group is required"),
			),
			self.assertRaises(InvalidArgumentError),
		):
			agent_scheduler._stamp_scope_and_watermark("DEP-RUN-1", inst, "owner@example.test")
