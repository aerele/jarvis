"""AR custody, completeness, human-only disposition and native-ledger integration."""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import operator_review
from jarvis.chat import receivables_review as review


def config():
	return {
		"company": "Example",
		"from_date": "2026-04-01",
		"to_date": "2026-09-30",
		"report_date": "2026-09-30",
		"basis": "current_records",
		"policy_version": "AR-REVIEW-v1",
		"review_scope": "review_and_drafts_only",
		"settlement_hold_days": 3,
	}


def snapshot():
	policy = config()
	result = {
		"scope": review.scope_from_config(policy),
		"config": policy,
		"complete": True,
		"site_date": "2026-09-30",
		"datasets": {
			"invoices": [{"name": "SI-1", "docstatus": 1, "is_return": 0, "posting_date": "2025-03-01"}],
			"ledger": [],
			"payments": [],
			"journals": [],
			"customers": [],
		},
	}
	result["source_digest"] = review.digest(result)
	return result


def output(source):
	result = {
		"scope": source["scope"],
		"coverage": {review.TOKEN: {"state": "evaluated"}},
		"findings": [
			{
				"token": review.TOKEN,
				"ref_doctype": "Sales Invoice",
				"ref_name": "SI-1",
				"amount": 0,
				"severity": "note",
				"result_class": "derived_candidate",
				"confidence": 0,
				"match_basis": "Recorded evidence",
				"false_positive_path": "Human review",
				"note": "Unsent review draft",
				"review_state": "reminder_candidate",
				"reminder_draft": "Please confirm.",
			}
		],
	}
	return seal_output(source, result)


def seal_output(source, result):
	result["integrity_digest"] = review.digest(
		{
			"input_digest": source["source_digest"],
			**{key: result[key] for key in ("scope", "findings", "coverage")},
		}
	)
	return result


class TestReceivablesReview(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_configuration_required_and_explicit(self):
		with patch.object(frappe.utils, "today", return_value="2026-09-30"), patch.object(frappe, "get_doc"):
			self.assertEqual(review.configuration_issues(config()), {})
			for field in config():
				policy = config()
				del policy[field]
				self.assertIn(field, review.configuration_issues(policy))
			self.assertEqual(review.configuration(frappe._dict(config=json.dumps(config()))), config())

	def test_invalid_policy_dates_and_hold_refused(self):
		for field, value in (
			("settlement_hold_days", True),
			("settlement_hold_days", 1.5),
			("settlement_hold_days", -1),
			("settlement_hold_days", 31),
			("report_date", "2026-10-01"),
			("report_date", "2026-03-31"),
			("review_scope", "send"),
			("basis", "historical"),
			("policy_version", ""),
		):
			with (
				self.subTest(field=field, value=value),
				patch.object(frappe.utils, "today", return_value="2026-09-30"),
			):
				self.assertIn(field, review.configuration_issues({**config(), field: value}))
		for raw in ("{", "[]", None):
			self.assertIn("config", review.configuration_issues(raw))

	def test_defaults_reuse_company_fiscal_years_without_saving(self):
		with (
			patch.object(
				review.purchase_match,
				"configuration_defaults",
				return_value={
					"defaults": {
						"company": "Example",
						"basis": "current_records",
						"price_tolerance_percent": 0,
					},
					"fiscal_years": [{"name": "FY"}],
					"site_date": "2026-09-30",
				},
			) as fiscal,
			patch.object(frappe.db, "set_value") as write,
		):
			result = review.configuration_defaults("Example")
			fiscal.assert_called_once_with("Example")
			self.assertEqual(result["defaults"]["settlement_hold_days"], 3)
			self.assertEqual(result["defaults"]["review_scope"], "review_and_drafts_only")
			self.assertNotIn("price_tolerance_percent", result["defaults"])
			write.assert_not_called()

	def test_scope_retains_fiscal_year_cutoff_and_opening_balances(self):
		scope = review.scope_from_config({**config(), "fiscal_year": "2026-2027"})
		self.assertEqual(scope["fiscal_year"], "2026-2027")
		self.assertEqual(scope["report_date"], "2026-09-30")
		self.assertTrue(scope["include_opening_receivables"])

	def test_fiscal_calendar_is_captured_and_changes_invalidate_evidence(self):
		policy = {**config(), "fiscal_year": "2026-2027"}
		with (
			patch.object(review, "_project", return_value={"country": "India"}),
			patch.object(frappe, "get_doc"),
			patch.object(frappe, "get_installed_apps", return_value=["erpnext"]),
			patch.object(review, "_names", return_value=[]),
			patch.object(
				review.purchase_match,
				"_fiscal_year",
				return_value={"name": "2026-2027", "from_date": "2026-04-01", "to_date": "2027-03-31"},
			) as fiscal,
		):
			captured = review.collect(review.scope_from_config(policy), policy)
			self.assertTrue(captured["complete"])
			self.assertEqual(captured["fiscal_year"]["from_date"], "2026-04-01")
			fiscal.return_value = None
			changed = review.collect(review.scope_from_config(policy), policy)
			self.assertFalse(changed["complete"])
			self.assertNotEqual(changed["source_digest"], captured["source_digest"])

	def test_hidden_rows_and_capacity_limits_fail_closed(self):
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_list", return_value=["SI-1"]),
			patch.object(frappe.db, "count", return_value=2),
		):
			with self.assertRaises(frappe.PermissionError):
				review._names("Sales Invoice", {}, 10)
			with self.assertRaises(OverflowError):
				review._names("Sales Invoice", {}, 0)

	def test_field_level_permissions_are_checked(self):
		document = MagicMock()
		with patch.object(review, "_projection", side_effect=[{"amount": 100}, {"amount": None}]):
			with self.assertRaises(frappe.PermissionError):
				review._project(document)
			document.check_permission.assert_called_once_with("read")

	def test_india_projection_uses_installed_apps_actual_field_names(self):
		document = MagicMock()
		document.doctype = "Sales Invoice"
		document.precision.return_value = 2
		fields = {"irn": "recorded-irn", "einvoice_status": "Cancelled"}
		document.get.side_effect = fields.get
		document.meta.has_field.side_effect = fields.__contains__
		self.assertNotIn("india", review._projection(document, False))
		self.assertEqual(review._projection(document, True)["india"], fields)

	def test_writeback_stages_reviews_only_in_live_mode(self):
		from jarvis.tools import record_agent_run as writeback

		source = snapshot()
		for mode in ("shadow", "live"):
			run = frappe._dict(
				name="RUN-AR",
				agent=review.AGENT,
				installation="INST-AR",
				status="running",
				input_snapshot_json=json.dumps(source),
				preparation_mode=mode,
				owner="Administrator",
				findings_count=1,
				blocker_count=0,
				coverage_note="",
				dashboard=None,
			)
			inst = frappe._dict(name="INST-AR", reviewer="Administrator")
			with (
				patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="session"),
				patch.object(
					frappe.db,
					"get_value",
					side_effect=lambda doctype, *args, **kwargs: run
					if doctype == writeback.RUN
					else {
						"rule_tokens": json.dumps([review.TOKEN]),
						"doctypes_required": '["Sales Invoice"]',
					},
				),
				patch.object(frappe.db, "set_value"),
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda doctype, name: run if doctype == writeback.RUN else inst,
				),
				patch.object(writeback, "_ref_verifiable", return_value=(True, "")),
				patch.object(review, "collect", return_value=source),
				patch.object(review, "stage_reviews") as stage,
				patch("jarvis.chat.agent_runs.record_delegate_run", return_value=run) as persist,
				patch("jarvis.chat.agent_run_steps.record_step"),
			):
				result = writeback.record_agent_run(**output(source))
				self.assertEqual(result["dropped"], [])
				self.assertEqual(stage.call_count, int(mode == "live"))
				self.assertEqual(persist.call_args.args[1].activation_state, mode)

	def test_collector_does_not_filter_out_opening_or_future_evidence(self):
		with (
			patch.object(review, "_project", return_value={"country": "India"}),
			patch.object(frappe, "get_doc"),
			patch.object(frappe, "get_installed_apps", return_value=["erpnext", "india_compliance"]),
			patch.object(review, "_names", return_value=[]) as names,
		):
			result = review.collect(review.scope_from_config(config()), config())
			self.assertTrue(result["complete"])
			self.assertTrue(result["jurisdiction"]["india_compliance_installed"])
			self.assertEqual(result["jurisdiction"]["accounting_framework"], "not_determined")
			for call in names.call_args_list:
				self.assertNotIn("posting_date", call.args[1])
				self.assertEqual(call.args[1]["company"], "Example")

	def test_collector_permission_and_overflow_propagate_typed_failure(self):
		for error, reason in (
			(frappe.PermissionError, "permission_slice"),
			(OverflowError, "run_truncated_watermark"),
		):
			with patch.object(frappe, "get_doc"), patch.object(review, "_project", side_effect=error):
				result = review.collect(review.scope_from_config(config()), config())
				self.assertFalse(result["complete"])
				self.assertEqual(result["datasets"], {})
				self.assertEqual(result["reason_code"], reason)

	def test_output_must_include_prior_year_invoice_once(self):
		source = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(source))
		with patch.object(review, "collect", return_value=source):
			self.assertTrue(review.validate_output(run, **output(source))[2])
			for findings in ([], output(source)["findings"] * 2):
				result = output(source)
				result["findings"] = findings
				with self.assertRaises(frappe.ValidationError):
					review.validate_output(run, **seal_output(source, result))

	def test_changed_scope_or_digest_rejected(self):
		source = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(source))
		for key, value in (("scope", {}), ("integrity_digest", "wrong")):
			result = output(source)
			result[key] = value
			with self.assertRaises(frappe.ValidationError):
				review.validate_output(run, **result)

	def test_stale_or_incomplete_evidence_never_stages(self):
		source = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(source))
		with patch.object(review, "collect", return_value={"source_digest": "changed"}):
			findings, coverage, ready = review.validate_output(run, **output(source))
			self.assertEqual(findings, [])
			self.assertFalse(ready)
			self.assertEqual(coverage[review.TOKEN]["state"], "not_evaluable")
		source.update(complete=False, reason_code="permission_slice", detail="Hidden")
		run.input_snapshot_json = json.dumps(source)
		self.assertFalse(review.validate_output(run, **output(source))[2])

	def test_historical_or_held_output_cannot_carry_a_reminder(self):
		source = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(source))
		result = output(source)
		result["findings"][0]["review_state"] = "hold"
		with self.assertRaises(frappe.ValidationError):
			review.validate_output(run, **seal_output(source, result))
		source["site_date"] = "2026-10-01"
		run.input_snapshot_json = json.dumps(source)
		with self.assertRaises(frappe.ValidationError):
			review.validate_output(run, **output(source))

	def test_get_inputs_is_bound_to_active_ar_delegate(self):
		for agent, status in ((review.AGENT, "completed"), ("ap-3way-match-operator", "running")):
			with patch.object(frappe.db, "get_value", return_value=frappe._dict(agent=agent, status=status)):
				with self.assertRaises(frappe.PermissionError):
					review.get_inputs(frappe._dict(name="RUN"))

	def test_review_dispatches_ar_backend_and_requires_named_reviewer(self):
		source = snapshot()
		doc = frappe._dict(
			agent=review.AGENT,
			assessment_evidence=json.dumps(
				{
					"reviewer": "Administrator",
					"scope": source["scope"],
					"policy": source["config"],
					"input_digest": source["source_digest"],
				}
			),
		)
		with (
			patch.object(review, "collect", return_value=source),
			patch.object(review.purchase_match, "collect") as ap_collect,
		):
			operator_review.check_review(doc, True)
			ap_collect.assert_not_called()
		with patch.object(review, "collect", return_value={"source_digest": "changed"}):
			with self.assertRaises(frappe.ValidationError):
				operator_review.check_review(doc, True)
			operator_review.check_review(doc, False)
		doc.assessment_evidence = json.dumps({"reviewer": "someone-else@example.invalid"})
		with self.assertRaises(frappe.PermissionError):
			operator_review.check_review(doc, False)

	def test_non_actionable_balances_do_not_create_review_requests(self):
		findings = [
			{"review_state": state} for state in ("settled", "within_terms", "hold", "reminder_candidate")
		]
		with patch.object(operator_review, "stage_reviews") as stage:
			review.stage_reviews(None, None, findings)
			self.assertEqual(stage.call_args.args[2], findings[2:])

	def test_finding_refs_cannot_request_accounting_outcomes(self):
		source = snapshot()
		for field, value in (
			("ref_doctype", "Payment Entry"),
			("amount", 100),
			("result_class", "confirmed_outcome"),
			("token", "other"),
		):
			result = output(source)
			result["findings"][0][field] = value
			with self.assertRaises(frappe.ValidationError):
				review.validate_output(
					frappe._dict(input_snapshot_json=json.dumps(source)), **seal_output(source, result)
				)

	def test_ar_uses_snapshot_completeness_not_gl_shortcuts(self):
		from jarvis.chat import agent_runs

		run = frappe._dict(agent=review.AGENT, wm_row_count=100)
		with patch.object(frappe.db, "sql") as query:
			self.assertFalse(agent_runs._watermark_drift(run, review.scope_from_config(config())))
			self.assertFalse(agent_runs._rowcount_shortfall(run, 0))
			self.assertFalse(agent_runs._scoped_visibility(run, frappe._dict(scoped_visibility=1)))
			query.assert_not_called()

	def test_review_artifact_escapes_untrusted_evidence_and_is_idempotent(self):
		source = snapshot()
		finding = output(source)["findings"][0]
		finding["note"] = "<script>send()</script>"
		run = frappe._dict(
			name="RUN",
			agent=review.AGENT,
			conversation=None,
			preparation_mode="live",
			input_snapshot_json=json.dumps(source),
		)
		artifact = MagicMock()
		artifact.name = "REVIEW"
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe.db, "get_value", side_effect=[1, None, 1, "REVIEW"]),
			patch.object(frappe, "get_doc", return_value=artifact) as create,
			patch.object(review, "collect", return_value=source),
			patch.object(frappe.share, "add"),
		):
			inst = frappe._dict(name="INST", reviewer="Administrator")
			self.assertEqual(review.stage_reviews(run, inst, [finding]), ["REVIEW"])
			self.assertEqual(review.stage_reviews(run, inst, [finding]), ["REVIEW"])
			self.assertEqual(create.call_count, 1)
			payload = create.call_args.args[0]
			self.assertEqual(payload["doctype"], "Jarvis Approval Request")
			self.assertEqual(payload["ref_doctype"], "Sales Invoice")
			self.assertIn("&lt;script&gt;", payload["context_md"])
			self.assertNotIn("<script>", payload["context_md"])


def assess_native(source, assessment):
	with tempfile.TemporaryDirectory() as directory:
		input_path = os.path.join(directory, "input.json")
		output_path = os.path.join(directory, "output.json")
		with open(input_path, "w", encoding="utf-8") as stream:
			json.dump(source, stream)
		subprocess.run(
			[sys.executable, assessment, "--input", input_path, "--out", output_path], check=True, timeout=30
		)
		with open(output_path, encoding="utf-8") as stream:
			return json.load(stream)


class TestNativeReceivablesReview(FrappeTestCase):
	def test_real_invoice_payment_snapshot_and_review_are_read_only(self):
		if not (assessment := os.environ.get("AR_ASSESSMENT_PATH")):
			self.skipTest("Set AR_ASSESSMENT_PATH to exercise the paired private runtime")
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		from jarvis.chat import approvals_api
		from jarvis.tests.test_filebox_skills import (
			E2E_ABBR,
			E2E_CO,
			E2E_ITEM,
			_e2e_fixtures,
			_insert,
			_purge_e2e,
		)

		frappe.set_user("Administrator")
		roots, documents, reviews = [], [], []
		try:
			roots.extend(_e2e_fixtures())
			for doctype in ("GL Entry", "Payment Ledger Entry"):
				frappe.db.delete(doctype, {"company": E2E_CO})
			if not frappe.db.exists("Party Type", "Customer"):
				roots.append(
					(
						"Party Type",
						_insert("Party Type", {"party_type": "Customer", "account_type": "Receivable"}),
					)
				)
			for root, kind in (("Assets", "Asset"), ("Income", "Income")):
				_insert(
					"Account",
					{"account_name": f"AR {root}", "company": E2E_CO, "is_group": 1, "root_type": kind},
					True,
				)
			debtors = _insert(
				"Account",
				{
					"account_name": "AR Debtors",
					"company": E2E_CO,
					"parent_account": f"AR Assets - {E2E_ABBR}",
					"account_type": "Receivable",
				},
			)
			bank = _insert(
				"Account",
				{
					"account_name": "AR Bank",
					"company": E2E_CO,
					"parent_account": f"AR Assets - {E2E_ABBR}",
					"account_type": "Bank",
				},
			)
			income = _insert(
				"Account",
				{"account_name": "AR Sales", "company": E2E_CO, "parent_account": f"AR Income - {E2E_ABBR}"},
			)
			frappe.db.set_value(
				"Company",
				E2E_CO,
				{
					"default_receivable_account": debtors,
					"round_off_cost_center": f"zz-fbk Main - {E2E_ABBR}",
					"round_off_account": f"zz-fbk Purchases - {E2E_ABBR}",
				},
			)
			frappe.clear_document_cache("Company", E2E_CO)
			if frappe.get_meta("Company").has_field("reporting_currency"):
				frappe.db.set_value("Company", E2E_CO, "reporting_currency", "INR")
				frappe.clear_document_cache("Company", E2E_CO)
			price_list = _insert(
				"Price List", {"price_list_name": "zz-AR Test Prices", "currency": "INR", "selling": 1}
			)
			roots.append(("Price List", price_list))
			customer = frappe.get_doc(
				{"doctype": "Customer", "customer_name": "zz-AR Review Customer", "customer_type": "Company"}
			)
			customer.flags.ignore_mandatory = True
			customer.insert(ignore_permissions=True)
			documents.append((customer.doctype, customer.name))
			today = frappe.utils.today()
			invoice = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": E2E_CO,
					"customer": customer.name,
					"posting_date": frappe.utils.add_days(today, -60),
					"set_posting_time": 1,
					"due_date": frappe.utils.add_days(today, -30),
					"currency": "INR",
					"conversion_rate": 1,
					"debit_to": debtors,
					"selling_price_list": price_list,
					"price_list_currency": "INR",
					"plc_conversion_rate": 1,
					"items": [
						{
							"item_code": E2E_ITEM,
							"qty": 1,
							"rate": 100,
							"income_account": income,
							"cost_center": f"zz-fbk Main - {E2E_ABBR}",
						}
					],
				}
			).insert()
			documents.append((invoice.doctype, invoice.name))
			invoice.submit()
			policy = {
				**config(),
				"company": E2E_CO,
				"from_date": frappe.utils.add_days(today, -10),
				"to_date": today,
				"report_date": today,
			}
			run = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Run",
					"agent": review.AGENT,
					"trigger": "manual",
					"status": "running",
					"preparation_mode": "live",
					"scope_json": frappe.as_json(review.scope_from_config(policy)),
					"assessment_config_json": frappe.as_json(policy),
					"session_key": "ar-native-test-session",
				}
			).insert(ignore_permissions=True)
			documents.append((run.doctype, run.name))
			captured = review.get_inputs(run)
			self.assertTrue(captured["complete"], captured)
			assessed = assess_native(captured, assessment)
			self.assertEqual(assessed["findings"][0]["review_state"], "reminder_candidate", assessed)
			self.assertIn("Opening/prior-period", assessed["findings"][0]["note"])
			self.assertTrue(
				review.validate_output(
					run,
					**{key: assessed[key] for key in ("findings", "coverage", "scope", "integrity_digest")},
				)[2]
			)
			inst = frappe._dict(name="ar-native-test", reviewer="Administrator")
			before = {
				doctype: frappe.db.count(doctype)
				for doctype in ("Dunning", "Communication", "GL Entry", "Payment Entry")
			}
			reviews.extend(review.stage_reviews(run, inst, assessed["findings"]))
			self.assertEqual(review.stage_reviews(run, inst, assessed["findings"]), reviews)
			with patch.object(approvals_api, "_resume_conversation") as resume:
				self.assertFalse(
					approvals_api.decide(reviews[0], "Review acknowledged; no sending authorised")["resumed"]
				)
				resume.assert_not_called()
			for doctype, count in before.items():
				self.assertEqual(frappe.db.count(doctype), count)
			payment = get_payment_entry("Sales Invoice", invoice.name, bank_account=bank, party_amount=40)
			payment.reference_no, payment.reference_date = "AR-TEST", today
			payment.insert()
			documents.append((payment.doctype, payment.name))
			payment.submit()
			with self.assertRaisesRegex(frappe.ValidationError, "stale"):
				operator_review.check_review(frappe.get_doc("Jarvis Approval Request", reviews[0]), True)
			fresh = review.collect(captured["scope"], policy)
			self.assertNotEqual(fresh["source_digest"], captured["source_digest"])
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"), 60)
			paid = assess_native(fresh, assessment)["findings"][0]
			self.assertEqual(paid["review_state"], "hold", paid)
			self.assertIsNone(paid["reminder_draft"])
			payment.cancel()
			reversed_payment = assess_native(review.collect(captured["scope"], policy), assessment)[
				"findings"
			][0]
			self.assertEqual(reversed_payment["review_state"], "reminder_candidate", reversed_payment)
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"), 100)
		finally:
			frappe.set_user("Administrator")
			for name in reviews:
				frappe.delete_doc("Jarvis Approval Request", name, force=True, ignore_permissions=True)
			for doctype, name in reversed(documents):
				if frappe.db.exists(doctype, name):
					document = frappe.get_doc(doctype, name)
					if document.docstatus == 1:
						document.cancel()
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			for doctype in ("GL Entry", "Payment Ledger Entry"):
				frappe.db.delete(doctype, {"company": E2E_CO})
			frappe.db.commit()
			_purge_e2e(roots)
