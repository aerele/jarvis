"""Bank reconciliation review: config, custody, completeness and proposals-only staging."""

import importlib.util
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

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

	def test_configuration_checks_the_linked_gl_account(self):
		inst = frappe._dict(config=frappe.as_json(config()))
		checked = []

		def get_doc(doctype, name=None):
			doc = MagicMock()
			doc.account = BANK.account
			doc.check_permission.side_effect = lambda *a: checked.append((doctype, name))
			if doctype == "Account":
				doc.check_permission.side_effect = frappe.PermissionError
			return doc

		with (
			patch.object(review, "_bank_account", return_value=BANK),
			patch.object(frappe.utils, "today", return_value="2026-10-05"),
			patch.object(frappe, "get_doc", side_effect=get_doc),
		):
			self.assertRaises(frappe.PermissionError, review.configuration, inst)
		self.assertIn(("Bank Account", "Example Current - Example Bank"), checked)

	def test_scope_with_missing_bank_account_throws_cleanly(self):
		with patch.object(review, "_bank_account", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "no longer available"):
				review.scope_from_config(config())

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


E2E_CO = "_Test BRR Company"


def _ensure_masters():
	if not frappe.db.exists("Company", E2E_CO):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": E2E_CO,
				"abbr": "TBRR",
				"default_currency": "INR",
				"country": "India",
			}
		).insert()
	if not frappe.db.exists("Fiscal Year", "_Test BRR FY 2026-2027"):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "_Test BRR FY 2026-2027",
				"year_start_date": "2026-04-01",
				"year_end_date": "2027-03-31",
				"companies": [{"company": E2E_CO}],
			}
		).insert()
	abbr = "TBRR"
	gl = f"_Test BRR Current - {abbr}"
	if not frappe.db.exists("Account", gl):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test BRR Current",
				"parent_account": f"Bank Accounts - {abbr}",
				"company": E2E_CO,
				"account_type": "Bank",
				"root_type": "Asset",
				"is_group": 0,
			}
		).insert()
	other = f"_Test BRR Other Current - {abbr}"
	if not frappe.db.exists("Account", other):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test BRR Other Current",
				"parent_account": f"Bank Accounts - {abbr}",
				"company": E2E_CO,
				"account_type": "Bank",
				"root_type": "Asset",
				"is_group": 0,
			}
		).insert()
	if not frappe.db.exists("Bank", "_Test BRR Bank"):
		frappe.get_doc({"doctype": "Bank", "bank_name": "_Test BRR Bank"}).insert()
	name = frappe.db.get_value("Bank Account", {"account": gl})
	if not name:
		name = (
			frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": "_Test BRR Current",
					"bank": "_Test BRR Bank",
					"account": gl,
					"company": E2E_CO,
					"is_company_account": 1,
				}
			)
			.insert()
			.name
		)
	if not frappe.db.exists("Customer", "_Test BRR Customer"):
		frappe.get_doc(
			{"doctype": "Customer", "customer_name": "_Test BRR Customer", "customer_type": "Company"}
		).insert()
	return gl, other, name


def _pe(kind, amount, posting, ref, paid_from, paid_to, party=True):
	doc = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": kind,
			"company": E2E_CO,
			"posting_date": posting,
			"paid_from": paid_from,
			"paid_to": paid_to,
			"paid_amount": amount,
			"received_amount": amount,
			"reference_no": ref,
			"reference_date": posting,
			**({"party_type": "Customer", "party": "_Test BRR Customer"} if party else {}),
		}
	)
	doc.setup_party_account_field()
	doc.set_missing_values()
	doc.set_exchange_rate()
	doc.set_amounts()
	doc.insert()
	doc.submit()
	return doc.name


def _je(posting, lines, ref):
	doc = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Bank Entry",
			"company": E2E_CO,
			"posting_date": posting,
			"cheque_no": ref,
			"cheque_date": posting,
			"accounts": [
				{
					"account": a,
					"debit_in_account_currency": d,
					"credit_in_account_currency": c,
					"cost_center": frappe.db.get_value("Company", E2E_CO, "cost_center"),
				}
				for a, d, c in lines
			],
		}
	)
	doc.insert()
	doc.submit()
	return doc.name


def _bt(bank_account, day, deposit=0, withdrawal=0, ref=None, submit=True, allocate=None):
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": day,
			"company": E2E_CO,
			"bank_account": bank_account,
			"currency": "INR",
			"deposit": deposit,
			"withdrawal": withdrawal,
			"description": f"line {ref}",
			"reference_number": ref,
			"transaction_id": ref,
		}
	)
	if allocate:
		doc.append(
			"payment_entries",
			{
				"payment_document": "Payment Entry",
				"payment_entry": allocate,
				"allocated_amount": deposit or withdrawal,
			},
		)
	doc.insert()
	if submit:
		doc.submit()
	return doc


class TestNativeBankReconEvidence(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.gl, cls.other, cls.bank_account = _ensure_masters()
		receivable = frappe.db.get_value("Company", E2E_CO, "default_receivable_account")
		cls.receipt = _pe("Receive", 1000, "2026-09-02", "BRR-REF-1", receivable, cls.gl)
		cls.linked = _pe("Receive", 2000, "2026-09-03", "BRR-REF-2", receivable, cls.gl)
		cls.transfer = _pe("Internal Transfer", 3000, "2026-09-04", "BRR-TRF", cls.gl, cls.other, party=False)
		cls.zero_je = _je("2026-09-05", [(cls.gl, 500, 0), (cls.gl, 0, 500)], "BRR-ZERO")
		cls.je = _je("2026-09-06", [(cls.other, 700, 0), (cls.gl, 0, 700)], "BRR-JE")
		cls.lines = [
			_bt(cls.bank_account, "2026-09-03", deposit=1000, ref="BRR-REF-1").name,
			_bt(cls.bank_account, "2026-09-03", deposit=2000, ref="BRR-REF-2", allocate=cls.linked).name,
			_bt(cls.bank_account, "2026-09-07", deposit=50, ref="BRR-DRAFT", submit=False).name,
		]
		cancelled = _bt(cls.bank_account, "2026-09-08", deposit=1000, ref="BRR-CXL", allocate=cls.receipt)
		cancelled.cancel()

	def snapshot(self):
		cfg = config(company=E2E_CO, bank_account=self.bank_account)
		return review.collect(review.scope_from_config(cfg), cfg)

	def voucher(self, snap, name):
		return next(v for v in snap["datasets"]["vouchers"] if v["name"] == name)

	def test_snapshot_is_complete_and_sealed(self):
		snap = self.snapshot()
		self.assertTrue(snap["complete"], snap.get("detail"))
		body = {k: v for k, v in snap.items() if k != "source_digest"}
		self.assertEqual(snap["source_digest"], review.digest(body))
		self.assertEqual(
			{row["name"] for row in snap["datasets"]["bank_lines"]} & set(self.lines), set(self.lines)
		)

	def test_internal_transfer_direction_follows_selected_bank_side(self):
		self.assertEqual(self.voucher(self.snapshot(), self.transfer)["direction"], "out")

	def test_payment_amount_is_taken_on_the_selected_bank_side(self):
		scope = {"company": E2E_CO, "bank_gl": "GL", "voucher_from": "2026-08-01", "to_date": "2026-09-30"}
		incoming = frappe._dict(
			name="PE-IN", paid_from="X", paid_to="GL", paid_amount=100, received_amount=120
		)
		outgoing = frappe._dict(
			name="PE-OUT", paid_from="GL", paid_to="X", paid_amount=90, received_amount=75
		)
		docs = {"PE-IN": incoming, "PE-OUT": outgoing}
		with (
			patch.object(review, "_names", side_effect=[["PE-OUT"], ["PE-IN"]]),
			patch.object(review, "_read", side_effect=lambda dt, name: docs[name]),
		):
			rows = {row["name"]: row for row in review._payment_vouchers(scope)}
		self.assertEqual((rows["PE-IN"]["direction"], rows["PE-IN"]["amount"]), ("in", "120"))
		self.assertEqual((rows["PE-OUT"]["direction"], rows["PE-OUT"]["amount"]), ("out", "90"))

	def test_zero_net_journal_is_not_a_candidate(self):
		names = {v["name"] for v in self.snapshot()["datasets"]["vouchers"]}
		self.assertNotIn(self.zero_je, names)
		self.assertEqual(self.voucher(self.snapshot(), self.je)["direction"], "out")
		self.assertEqual(Decimal(self.voucher(self.snapshot(), self.je)["amount"]), Decimal("700"))

	def test_only_submitted_bank_links_lock_a_voucher(self):
		snap = self.snapshot()
		self.assertEqual(self.voucher(snap, self.linked)["linked_bank_lines"], [self.lines[1]])
		self.assertEqual(self.voucher(snap, self.receipt)["linked_bank_lines"], [])

	def test_hidden_rows_fail_closed(self):
		with patch.object(review, "_count", return_value=10**6):
			snap = self.snapshot()
		self.assertFalse(snap["complete"])
		self.assertEqual(snap["reason_code"], "permission_slice")

	def hide_from_list(self, doctype, name):
		real = frappe.get_list

		def get_list(dt, *args, **kwargs):
			rows = real(dt, *args, **kwargs)
			if dt != doctype or kwargs.get("ignore_permissions"):
				return rows
			return [r for r in rows if (r if isinstance(r, str) else r["name"]) != name]

		return patch.object(frappe, "get_list", side_effect=get_list)

	def test_hidden_journal_entry_fails_closed(self):
		with self.hide_from_list("Journal Entry", self.je):
			snap = self.snapshot()
		self.assertEqual((snap["complete"], snap["reason_code"]), (False, "permission_slice"))

	def test_hidden_charge_account_fails_closed(self):
		with self.hide_from_list("Account", "Bank Charges - TBRR"):
			snap = self.snapshot()
		self.assertEqual((snap["complete"], snap["reason_code"]), (False, "permission_slice"))

	def test_payment_union_is_capped_before_any_read(self):
		read = review._read
		seen = []
		with (
			patch.object(review, "MAX_VOUCHERS", 2),
			patch.object(review, "_read", side_effect=lambda dt, n: seen.append(dt) or read(dt, n)),
		):
			snap = self.snapshot()
		self.assertEqual(snap["reason_code"], "run_truncated_watermark")
		self.assertNotIn("Payment Entry", seen)

	def test_journal_cap_counts_entries_not_bank_rows(self):
		scope = review.scope_from_config(config(company=E2E_CO, bank_account=self.bank_account))
		with patch.object(review, "MAX_VOUCHERS", 1):
			self.assertRaises(OverflowError, review._journal_vouchers, scope)

	def test_capacity_limit_is_typed(self):
		with patch.object(review, "MAX_VOUCHERS", 1):
			snap = self.snapshot()
		self.assertEqual((snap["complete"], snap["reason_code"]), (False, "run_truncated_watermark"))

	def test_unreadable_doctype_is_permission_slice(self):
		real = frappe.has_permission
		with patch.object(
			frappe,
			"has_permission",
			side_effect=lambda dt, *a, **k: dt != "Journal Entry" and real(dt, *a, **k),
		):
			snap = self.snapshot()
		self.assertEqual(snap["reason_code"], "permission_slice")


def stored_snapshot():
	snap = {
		"scope": {
			"company": "Example",
			"bank_account": "BA",
			"bank_gl": "GL",
			"from_date": "2026-09-01",
			"to_date": "2026-09-30",
			"voucher_from": "2026-08-02",
		},
		"config": config(bank_account="BA"),
		"site_date": "2026-10-05",
		"complete": True,
		"datasets": {
			"bank_lines": [
				{
					"name": "BT-1",
					"date": "2026-09-03",
					"docstatus": 1,
					"status": "Unreconciled",
					"deposit": "100",
					"withdrawal": "0",
					"unallocated_amount": "100",
				},
				{
					"name": "BT-2",
					"date": "2026-09-04",
					"docstatus": 1,
					"status": "Unreconciled",
					"deposit": "0",
					"withdrawal": "50",
					"unallocated_amount": "50",
				},
				{
					"name": "BT-3",
					"date": "2026-09-05",
					"docstatus": 1,
					"status": "Unreconciled",
					"deposit": "50",
					"withdrawal": "0",
					"unallocated_amount": "50",
				},
				{
					"name": "BT-4",
					"date": "2026-09-06",
					"docstatus": 0,
					"status": "Pending",
					"deposit": "9",
					"withdrawal": "0",
					"unallocated_amount": "9",
				},
			],
			"vouchers": [
				{
					"doctype": "Payment Entry",
					"name": "PE-1",
					"direction": "in",
					"amount": "100",
					"linked_bank_lines": [],
				},
				{
					"doctype": "Payment Entry",
					"name": "PE-2",
					"direction": "out",
					"amount": "100",
					"linked_bank_lines": [],
				},
				{
					"doctype": "Payment Entry",
					"name": "PE-3",
					"direction": "in",
					"amount": "100",
					"linked_bank_lines": ["BT-9"],
				},
			],
			"accounts": [
				{"name": "Bank Charges - EX", "account_name": "Bank Charges", "root_type": "Expense"}
			],
		},
	}
	snap["source_digest"] = review.digest(snap)
	return snap


def finding(ref, state, **extra):
	row = {
		"token": review.TOKEN,
		"ref_doctype": "Bank Transaction",
		"ref_name": ref,
		"amount": 0,
		"result_class": "derived_candidate",
		"note": "n",
		"review_state": state,
		"candidates": [],
		"pair": None,
		"proposed_entry": None,
		"match_confidence": None,
	}
	row.update(extra)
	return row


def good_findings():
	return [
		finding(
			"BT-1",
			"match_proposed",
			match_confidence="high",
			candidates=[{"doctype": "Payment Entry", "name": "PE-1"}],
		),
		finding("BT-2", "reversal_pair", pair="BT-3"),
		finding("BT-3", "reversal_pair", pair="BT-2"),
	]


class TestBankReconOutput(FrappeTestCase):
	def run_doc(self, snap=None):
		return frappe._dict(input_snapshot_json=frappe.as_json(snap or stored_snapshot()), agent=review.AGENT)

	def check(self, findings, snap=None, coverage=None, scope=None):
		snap = snap or stored_snapshot()
		coverage = coverage or {review.TOKEN: {"state": "evaluated", "drafts_awaiting_submission": 1}}
		scope = scope or snap["scope"]
		sealed = review.digest(
			{
				"input_digest": snap["source_digest"],
				"scope": scope,
				"findings": findings,
				"coverage": coverage,
			}
		)
		with patch.object(review, "collect", return_value=snap):
			return review.validate_output(self.run_doc(snap), findings, coverage, scope, sealed)

	def assertRejected(self, findings, **kw):
		self.assertRaises(frappe.ValidationError, self.check, findings, **kw)

	def test_valid_output_is_ready(self):
		self.assertTrue(self.check(good_findings())[2])

	def test_missing_extra_and_duplicate_lines_rejected(self):
		self.assertRejected(good_findings()[:2])
		self.assertRejected(good_findings() + [finding("BT-4", "needs_decision")])
		self.assertRejected(good_findings() + [good_findings()[0]])

	def test_unknown_linked_and_wrong_direction_vouchers_rejected(self):
		for name in ("PE-404", "PE-3", "PE-2"):
			rows = good_findings()
			rows[0]["candidates"] = [{"doctype": "Payment Entry", "name": name}]
			self.assertRejected(rows)

	def test_voucher_proposed_for_two_lines_rejected(self):
		snap = stored_snapshot()
		snap["datasets"]["bank_lines"][2].update(deposit="100", unallocated_amount="100")
		snap["source_digest"] = review.digest({k: v for k, v in snap.items() if k != "source_digest"})
		rows = good_findings()
		rows[1:] = [
			finding("BT-2", "needs_decision"),
			finding(
				"BT-3",
				"match_proposed",
				match_confidence="low",
				candidates=[{"doctype": "Payment Entry", "name": "PE-1"}],
			),
		]
		self.assertRejected(rows, snap=snap)

	def test_unpaired_reversal_rejected(self):
		rows = good_findings()
		rows[2] = finding("BT-3", "needs_decision")
		self.assertRejected(rows)
		snap = stored_snapshot()
		snap["datasets"]["bank_lines"].append({**snap["datasets"]["bank_lines"][1], "name": "BT-5"})
		snap["source_digest"] = review.digest({k: v for k, v in snap.items() if k != "source_digest"})
		rows = good_findings()
		rows[2]["pair"] = "BT-5"
		self.assertRejected(rows + [finding("BT-5", "reversal_pair", pair="BT-3")], snap=snap)

	def test_draft_must_match_line_direction_amount_and_known_account(self):
		base = {
			"payment_type": "Pay",
			"amount": "50",
			"account": "Bank Charges - EX",
			"party_type": None,
			"party": None,
			"basis": "bank_charges",
		}
		rows = good_findings()
		rows[1] = finding("BT-2", "draft_proposal", proposed_entry=base)
		rows[2] = finding("BT-3", "needs_decision")
		self.assertTrue(self.check(rows)[2])
		for bad in (
			{"payment_type": "Receive"},
			{"amount": "49"},
			{"account": "Other - EX"},
			{"party": "Somebody"},
		):
			rows = good_findings()
			rows[1] = finding("BT-2", "draft_proposal", proposed_entry={**base, **bad})
			rows[2] = finding("BT-3", "needs_decision")
			self.assertRejected(rows)

	def test_states_and_confidence_are_closed_sets(self):
		for bad in (
			{"review_state": "reconciled"},
			{"match_confidence": "certain"},
			{"amount": 1},
			{"result_class": "confirmed_outcome"},
		):
			rows = good_findings()
			rows[0].update(bad)
			self.assertRejected(rows)

	def test_tampered_digest_and_scope_rejected(self):
		snap = stored_snapshot()
		self.assertRaises(
			frappe.ValidationError,
			review.validate_output,
			self.run_doc(snap),
			good_findings(),
			{review.TOKEN: {"state": "evaluated"}},
			snap["scope"],
			"0" * 64,
		)
		self.assertRejected(good_findings(), scope={**snap["scope"], "to_date": "2026-09-29"})

	def test_incomplete_snapshot_returns_not_evaluable(self):
		snap = stored_snapshot()
		snap.update(complete=False, reason_code="permission_slice", detail="x")
		snap["source_digest"] = review.digest({k: v for k, v in snap.items() if k != "source_digest"})
		findings, coverage, ready = self.check(
			[],
			snap=snap,
			coverage={review.TOKEN: {"state": "not_evaluable", "reason_code": "permission_slice"}},
		)
		self.assertEqual((findings, ready), ([], False))
		self.assertEqual(coverage[review.TOKEN]["reason_code"], "permission_slice")

	def test_drift_after_capture_is_not_ready(self):
		snap = stored_snapshot()
		drifted = {**snap, "source_digest": "f" * 64}
		coverage = {review.TOKEN: {"state": "evaluated", "drafts_awaiting_submission": 1}}
		sealed = review.digest(
			{
				"input_digest": snap["source_digest"],
				"scope": snap["scope"],
				"findings": good_findings(),
				"coverage": coverage,
			}
		)
		with patch.object(review, "collect", return_value=drifted):
			_f, cov, ready = review.validate_output(
				self.run_doc(snap), good_findings(), coverage, snap["scope"], sealed
			)
		self.assertFalse(ready)
		self.assertEqual(cov[review.TOKEN]["reason_code"], "run_truncated_watermark")

	def test_review_context_states_scope_and_no_authority(self):
		text = review.review_context(stored_snapshot(), finding("BT-1", "needs_decision"))
		for needle in (
			"BA",
			"2026-09-01",
			"2026-09-30",
			"30 calendar days",
			"BANK-RECON-v1",
			"does not reconcile",
		):
			self.assertIn(needle, text)

	def test_stage_reviews_forwards_to_shared_custody(self):
		with patch.object(operator_review, "stage_reviews", return_value=["AR-1"]) as staged:
			self.assertEqual(review.stage_reviews("run", "inst", ["f"]), ["AR-1"])
		self.assertIs(staged.call_args.args[3], review)


_STORE = os.path.join(
	os.path.dirname(frappe.utils.get_bench_path()), "jarvis", "jarvis-agents", "agents", "bank-recon-operator"
)
_STORE = os.environ.get("JARVIS_BANK_RECON_STORE", os.path.normpath(_STORE))


@unittest.skipUnless(os.path.isfile(os.path.join(_STORE, "assessment.py")), "bank-recon store bundle absent")
class TestBankReconStoreContract(FrappeTestCase):
	"""The private matcher's output passes the bench gate unchanged."""

	def test_store_output_is_accepted(self):
		spec = importlib.util.spec_from_file_location(
			"bank_recon_store", os.path.join(_STORE, "assessment.py")
		)
		assessment = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(assessment)
		snap = stored_snapshot()
		data = snap["datasets"]
		data["bank_lines"][0]["reference_number"] = "BRR-REF-100"
		data["bank_lines"][1]["description"] = "RVSL BRR-REF-777"
		data["bank_lines"][2]["reference_number"] = "BRR-REF-777"
		data["bank_lines"].append(
			{
				"name": "BT-5",
				"date": "2026-09-07",
				"docstatus": 1,
				"status": "Unreconciled",
				"deposit": "0",
				"withdrawal": "5",
				"unallocated_amount": "5",
				"description": "SMS ALERT CHG",
			}
		)
		for voucher in data["vouchers"]:
			voucher["posting_date"] = "2026-09-02"
		data["vouchers"][0]["reference"] = "BRR-REF-100"
		snap.pop("source_digest")
		snap["source_digest"] = review.digest(snap)
		out = assessment.evaluate(snap)
		states = {row["ref_name"]: row["review_state"] for row in out["findings"]}
		self.assertEqual(
			states,
			{
				"BT-1": "match_proposed",
				"BT-2": "reversal_pair",
				"BT-3": "reversal_pair",
				"BT-5": "draft_proposal",
			},
		)
		with patch.object(review, "collect", return_value=snap):
			findings, _coverage, ready = review.validate_output(
				self.run_doc(snap), out["findings"], out["coverage"], out["scope"], out["integrity_digest"]
			)
		self.assertTrue(ready)
		self.assertEqual(len(findings), 4)

	def run_doc(self, snap):
		return frappe._dict(input_snapshot_json=frappe.as_json(snap), agent=review.AGENT)
