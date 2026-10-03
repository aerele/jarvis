"""AP evidence custody, fail-closed writeback and human-only review regressions."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import purchase_match as matching
from jarvis.jarvis.doctype.jarvis_approval_request.jarvis_approval_request import JarvisApprovalRequest


def config():
	return {
		"company": "Example",
		"from_date": "2026-03-01",
		"to_date": "2026-04-30",
		"report_date": "2026-04-15",
		"basis": "current_records",
		"review_scope": "document_matching_only",
		"policy_version": "AP-v1",
		"price_tolerance_percent": 0,
	}


def fiscal_document(name="2026", start="2026-01-01", end="2026-12-31", companies=(), disabled=0):
	document = MagicMock()
	values = {
		"name": name,
		"year_start_date": start,
		"year_end_date": end,
		"companies": [frappe._dict(company=company) for company in companies],
		"disabled": disabled,
	}
	for key, value in values.items():
		setattr(document, key, value)
	document.get.side_effect = values.get
	return document


def snapshot():
	policy = config()
	return matching._seal(
		{
			"scope": matching.scope_from_config(policy),
			"config": policy,
			"complete": True,
			"datasets": {
				"invoices": [{"name": "PI-1", "docstatus": 0, "posting_date": "2026-03-15"}],
				"orders": [],
				"receipts": [],
			},
		}
	)


def output(captured):
	findings = [
		{
			"token": matching.TOKEN,
			"ref_doctype": "Purchase Invoice",
			"ref_name": "PI-1",
			"amount": 0,
			"result_class": "derived_candidate",
			"severity": "note",
			"note": "Document review only.",
			"confidence": 0,
			"match_basis": "Exact links",
			"false_positive_path": "Manual review",
		}
	]
	coverage = {matching.TOKEN: {"state": "evaluated"}}
	return {
		"findings": findings,
		"coverage": coverage,
		"scope": captured["scope"],
		"integrity_digest": matching.digest(
			{
				"input_digest": captured["source_digest"],
				"scope": captured["scope"],
				"findings": findings,
				"coverage": coverage,
			}
		),
	}


class TestPurchaseMatch(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_configuration_requires_explicit_policy_and_cutoff(self):
		with (
			patch.object(matching.frappe, "get_doc") as get_doc,
			patch.object(matching.frappe.utils, "today", return_value="2026-09-30"),
		):
			self.assertEqual(matching.configuration(frappe._dict(config=json.dumps(config()))), config())
			get_doc.return_value.check_permission.assert_called_once_with("read")
			for missing in config():
				invalid = config()
				del invalid[missing]
				with self.subTest(missing=missing), self.assertRaises(frappe.ValidationError):
					matching.configuration(frappe._dict(config=json.dumps(invalid)))

	def test_invalid_dates_tolerances_and_scope_are_refused(self):
		for key, value in (
			("report_date", "9999-01-01"),
			("report_date", "2026-02-28"),
			("from_date", "2026-05-01"),
			("price_tolerance_percent", "NaN"),
			("price_tolerance_percent", -1),
			("price_tolerance_percent", 101),
			("policy_version", ""),
			("review_scope", "tax_certification"),
			("basis", "historical_books"),
		):
			policy = config()
			policy[key] = value
			with self.subTest(key=key, value=value), self.assertRaises(frappe.ValidationError):
				matching.configuration(frappe._dict(config=json.dumps(policy)))

	def test_configuration_guidance_names_only_the_invalid_fields(self):
		with patch.object(matching.frappe.utils, "today", return_value="2026-09-30"):
			self.assertEqual(matching.configuration_issues(config()), {})
			for field in config():
				policy = config()
				del policy[field]
				with self.subTest(field=field):
					self.assertEqual(set(matching.configuration_issues(json.dumps(policy))), {field})
			policy = {
				key: value for key, value in config().items() if key.endswith("date") or key == "company"
			}
			self.assertEqual(
				set(matching.configuration_issues(policy)),
				{"policy_version", "price_tolerance_percent", "basis", "review_scope"},
			)

	def test_configuration_guidance_keeps_the_launch_date_and_numeric_rules(self):
		with patch.object(matching.frappe.utils, "today", return_value="2026-09-30"):
			for field, value in (
				("from_date", "2026-02-30"),
				("to_date", "2026-02-28"),
				("report_date", "2026-10-01"),
				("report_date", "2026-02-28"),
				("price_tolerance_percent", "NaN"),
				("price_tolerance_percent", "Infinity"),
				("price_tolerance_percent", -1),
				("price_tolerance_percent", 101),
				("price_tolerance_percent", True),
				("policy_version", " " * 10),
				("policy_version", "policy" * 30),
			):
				policy = config()
				policy[field] = value
				with self.subTest(field=field, value=value):
					self.assertEqual(set(matching.configuration_issues(policy)), {field})

	def test_configuration_guidance_handles_invalid_json_without_touching_records(self):
		with patch.object(matching.frappe, "get_doc") as get_doc:
			for raw in ("{", "[]", "null", 10):
				with self.subTest(raw=raw):
					self.assertEqual(set(matching.configuration_issues(raw)), {"config"})
			with self.assertRaisesRegex(frappe.ValidationError, "AP matching policy reference"):
				matching.configuration(frappe._dict(config=json.dumps({**config(), "policy_version": ""})))
			get_doc.assert_not_called()

	def test_starter_defaults_are_visible_read_only_and_company_bounded(self):
		from jarvis.chat.agents_api import get_ap_review_defaults

		documents = {
			"Company": MagicMock(),
			"Shared": fiscal_document("Shared", "2026-04-01", "2027-03-31"),
			"Other": fiscal_document("Other", companies=("Other company",)),
			"Disabled": fiscal_document("Disabled", disabled=1),
		}
		with (
			patch.object(matching.frappe.defaults, "get_user_default", return_value="Example"),
			patch.object(matching.frappe.utils, "today", return_value="2026-09-30"),
			patch.object(
				matching.frappe, "get_list", return_value=["Shared", "Other", "Disabled"]
			) as get_list,
			patch.object(
				matching.frappe,
				"get_doc",
				side_effect=lambda doctype, name: documents["Company" if doctype == "Company" else name],
			),
			patch.object(matching.frappe.db, "set_value") as write,
		):
			result = get_ap_review_defaults()
		self.assertEqual(
			result["defaults"],
			{
				"company": "Example",
				"policy_version": "AP-MATCH-v1",
				"price_tolerance_percent": 0,
				"basis": "current_records",
				"review_scope": "document_matching_only",
			},
		)
		self.assertEqual(result["site_date"], "2026-09-30")
		self.assertEqual(
			result["fiscal_years"], [{"name": "Shared", "from_date": "2026-04-01", "to_date": "2027-03-31"}]
		)
		self.assertEqual(result["message"], "")
		get_list.assert_called_once()
		documents["Company"].check_permission.assert_called_once_with("read")
		write.assert_not_called()

	def test_defaults_do_not_choose_a_company_when_multiple_are_readable(self):
		with (
			patch.object(matching.frappe.defaults, "get_user_default", return_value=None),
			patch.object(matching.frappe, "get_list", return_value=["First", "Second"]),
			patch.object(matching.frappe, "get_doc") as get_doc,
		):
			result = matching.configuration_defaults()
		self.assertNotIn("company", result["defaults"])
		self.assertEqual(result["fiscal_years"], [])
		get_doc.assert_not_called()

	def test_defaults_endpoint_requires_jarvis_access_before_reading_erp_settings(self):
		from jarvis.chat.agents_api import get_ap_review_defaults

		with (
			patch("jarvis.permissions.has_jarvis_access", return_value=False),
			patch.object(matching, "configuration_defaults") as defaults,
		):
			with self.assertRaises(frappe.PermissionError):
				get_ap_review_defaults("Example")
			defaults.assert_not_called()

	def test_unreadable_default_company_is_not_exposed_or_used(self):
		document = MagicMock()
		document.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(matching.frappe.defaults, "get_user_default", return_value="Hidden"),
			patch.object(matching.frappe, "get_doc", return_value=document),
			patch.object(matching.frappe, "get_list") as get_list,
		):
			result = matching.configuration_defaults()
			self.assertNotIn("Hidden", json.dumps(result))
			with self.assertRaises(frappe.PermissionError):
				matching.configuration_defaults("Hidden")
		get_list.assert_not_called()

	def test_defaults_degrade_without_fiscal_year_read_access(self):
		with (
			patch.object(matching.frappe, "get_doc"),
			patch.object(matching.frappe, "get_list", side_effect=frappe.PermissionError),
		):
			result = matching.configuration_defaults("Example")
		self.assertEqual(result["fiscal_years"], [])
		self.assertEqual(result["defaults"]["price_tolerance_percent"], 0)
		self.assertIn("not readable", result["message"])

	def test_missing_ambiguous_and_truncated_fiscal_calendars_do_not_get_date_defaults(self):
		for names in ([], ["First", "Second"], list(map(str, range(201)))):
			with (
				patch.object(matching.frappe, "get_doc"),
				patch.object(matching.frappe, "get_list", return_value=names),
				patch.object(matching.frappe.utils, "today", return_value="2026-09-30"),
				patch.object(
					matching,
					"_fiscal_year",
					side_effect=lambda name, company: {
						"name": name,
						"from_date": "2026-01-01",
						"to_date": "2026-12-31",
					},
				),
				self.subTest(names=len(names)),
			):
				result = matching.configuration_defaults("Example")
				self.assertTrue(result["message"])
				self.assertNotIn("from_date", result["defaults"])

	def test_fiscal_year_validates_company_and_period_and_survives_snapshot_scope(self):
		policy = {**config(), "fiscal_year": "2026"}
		with (
			patch.object(
				matching.frappe,
				"get_doc",
				side_effect=lambda doctype, name: fiscal_document()
				if doctype == "Fiscal Year"
				else MagicMock(),
			),
			patch.object(matching.frappe.utils, "today", return_value="2026-09-30"),
		):
			validated = matching.configuration(frappe._dict(config=json.dumps(policy)))
			self.assertEqual(validated, policy)
			self.assertEqual(matching.scope_from_config(validated)["fiscal_year"], "2026")
			for field, value in (("from_date", "2025-12-31"), ("to_date", "2027-01-01")):
				self.assertEqual(set(matching.configuration_issues({**policy, field: value})), {field})
			self.assertNotIn("fiscal_year", matching.scope_from_config(config()))

	def test_wrong_company_disabled_unknown_and_unreadable_fiscal_years_are_invalid(self):
		for document in (
			fiscal_document(companies=("Other company",)),
			fiscal_document(disabled=1),
		):
			with patch.object(matching.frappe, "get_doc", return_value=document):
				self.assertEqual(
					set(matching.configuration_issues({**config(), "fiscal_year": "2026"})), {"fiscal_year"}
				)
		for error in (frappe.PermissionError, frappe.DoesNotExistError):
			with patch.object(matching.frappe, "get_doc", side_effect=error):
				self.assertEqual(
					set(matching.configuration_issues({**config(), "fiscal_year": "Hidden"})), {"fiscal_year"}
				)

	def test_leap_day_short_year_and_boundary_dates_use_stored_calendar(self):
		policy = {
			**config(),
			"fiscal_year": "Short",
			"from_date": "2024-02-01",
			"to_date": "2024-02-29",
			"report_date": "2024-02-29",
		}
		with (
			patch.object(
				matching.frappe, "get_doc", return_value=fiscal_document("Short", "2024-02-01", "2024-02-29")
			),
			patch.object(matching.frappe.utils, "today", return_value="2024-02-29"),
		):
			self.assertEqual(matching.configuration_issues(policy), {})
			self.assertEqual(
				set(matching.configuration_issues({**policy, "to_date": "2024-03-01"})), {"to_date"}
			)
			self.assertEqual(
				set(matching.configuration_issues({**policy, "report_date": "2024-03-01"})), {"report_date"}
			)

	def test_fiscal_year_field_level_restrictions_fail_closed(self):
		document = fiscal_document()
		document.apply_fieldlevel_read_permissions.side_effect = lambda: setattr(
			document.get, "side_effect", lambda key: None
		)
		with patch.object(matching.frappe, "get_doc", return_value=document):
			with self.assertRaises(frappe.PermissionError):
				matching._fiscal_year("2026", "Example")

	def test_malformed_optional_fiscal_year_is_not_silently_dropped(self):
		with patch.object(matching.frappe, "get_doc") as get_doc:
			for value in (False, 0, [], {}):
				with self.subTest(value=value):
					issues = matching.configuration_issues({**config(), "fiscal_year": value})
					self.assertEqual(set(issues), {"fiscal_year"})
			get_doc.assert_not_called()

	def test_hidden_rows_mean_incomplete_not_empty(self):
		with (
			patch.object(matching.frappe, "get_doc"),
			patch.object(matching.frappe, "has_permission", return_value=True),
			patch.object(matching.frappe, "get_list", return_value=[]),
			patch.object(matching.frappe.db, "count", return_value=1),
		):
			captured = matching.collect(matching.scope_from_config(config()), config())
		self.assertFalse(captured["complete"])
		self.assertEqual(captured["reason_code"], "permission_slice")
		self.assertEqual(captured["datasets"], {})

	def test_hidden_return_flag_is_permission_denial_not_an_ordinary_receipt(self):
		values = {"is_return": 1, "items": []}
		doc = MagicMock()
		doc.get.side_effect = values.get
		doc.apply_fieldlevel_read_permissions.side_effect = lambda: values.update(is_return=None)
		with self.assertRaises(frappe.PermissionError):
			matching._project(doc)

	def test_successful_empty_reads_require_all_three_datasets(self):
		with (
			patch.object(matching.frappe, "get_doc"),
			patch.object(matching.frappe, "has_permission", return_value=True),
			patch.object(matching.frappe, "get_list", return_value=[]) as get_list,
			patch.object(matching.frappe.db, "count", return_value=0),
		):
			captured = matching.collect(matching.scope_from_config(config()), config())
		self.assertTrue(captured["complete"])
		self.assertEqual(set(captured["datasets"]), {"invoices", "orders", "receipts"})
		self.assertEqual(get_list.call_count, 3)
		for call in get_list.call_args_list:
			self.assertEqual(call.kwargs["filters"], {"company": "Example"})

	def test_truncation_never_leaks_partial_matching_population(self):
		with (
			patch.object(matching.frappe, "get_doc"),
			patch.object(matching.frappe, "has_permission", return_value=True),
			patch.object(
				matching.frappe,
				"get_list",
				return_value=[str(index) for index in range(matching.MAX_DOCUMENTS + 1)],
			),
		):
			captured = matching.collect(matching.scope_from_config(config()), config())
		self.assertFalse(captured["complete"])
		self.assertEqual(captured["reason_code"], "run_truncated_watermark")

	def test_unchanged_results_are_bound_to_the_complete_snapshot(self):
		captured = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(captured))
		with patch.object(matching, "collect", return_value=captured):
			findings, coverage, ready = matching.validate_output(run, **output(captured))
		self.assertTrue(ready)
		self.assertEqual(len(findings), 1)
		self.assertEqual(coverage[matching.TOKEN]["state"], "evaluated")

	def test_stale_evidence_or_permission_loss_cannot_persist_findings(self):
		captured = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(captured))
		with patch.object(matching, "collect", return_value={"source_digest": "changed"}):
			findings, coverage, ready = matching.validate_output(run, **output(captured))
		self.assertFalse(ready)
		self.assertEqual(findings, [])
		self.assertEqual(coverage[matching.TOKEN]["state"], "not_evaluable")

	def test_digest_scope_and_omitted_drafts_are_refused(self):
		captured = snapshot()
		run = frappe._dict(input_snapshot_json=json.dumps(captured))
		for field, value in (("integrity_digest", "forged"), ("scope", {}), ("findings", [])):
			payload = output(captured)
			payload[field] = value
			with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
				matching.validate_output(run, **payload)
		payload = output(captured)
		payload["findings"] = []
		payload["integrity_digest"] = matching.digest(
			{
				"input_digest": captured["source_digest"],
				**{key: payload[key] for key in ("scope", "findings", "coverage")},
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "exactly one"):
			matching.validate_output(run, **payload)

	def test_incomplete_inputs_cannot_be_upgraded_by_claiming_evaluated(self):
		captured = snapshot()
		captured.update(complete=False, reason_code="permission_slice")
		run = frappe._dict(input_snapshot_json=json.dumps(captured))
		findings, coverage, ready = matching.validate_output(run, **output(captured))
		self.assertEqual(findings, [])
		self.assertFalse(ready)
		self.assertEqual(coverage[matching.TOKEN]["reason_code"], "permission_slice")

	def test_input_tool_refuses_other_agent_and_finalized_run(self):
		for agent, status in (("close-auditor", "running"), (matching.AGENT, "completed")):
			run = MagicMock(agent=agent, status=status)
			with (
				patch.object(
					matching.frappe.db, "get_value", return_value=frappe._dict(agent=agent, status=status)
				),
				self.assertRaises(frappe.PermissionError),
			):
				matching.get_inputs(run)

	def test_snapshot_is_set_once_and_retries_check_freshness(self):
		captured = snapshot()
		run = MagicMock(
			agent=matching.AGENT,
			status="running",
			assessment_config_json=json.dumps(config()),
			scope_json=json.dumps(captured["scope"]),
			input_snapshot_json=json.dumps(captured),
		)
		with (
			patch.object(
				matching.frappe.db,
				"get_value",
				return_value=frappe._dict(
					agent=matching.AGENT, status="running", input_snapshot_json=json.dumps(captured)
				),
			),
			patch.object(matching, "collect", return_value=captured),
			patch.object(matching.frappe.db, "set_value") as write,
		):
			self.assertEqual(matching.get_inputs(run), captured)
			write.assert_not_called()
		with (
			patch.object(
				matching.frappe.db,
				"get_value",
				return_value=frappe._dict(
					agent=matching.AGENT, status="running", input_snapshot_json=json.dumps(captured)
				),
			),
			patch.object(matching, "collect", return_value={"source_digest": "changed"}),
			self.assertRaisesRegex(frappe.ValidationError, "changed since capture"),
		):
			matching.get_inputs(run)

	def test_review_requires_named_human_and_fresh_evidence_but_allows_rejection(self):
		captured = snapshot()
		doc = frappe._dict(
			assessment_evidence=json.dumps(
				{
					"reviewer": "Administrator",
					"scope": captured["scope"],
					"policy": config(),
					"input_digest": captured["source_digest"],
				}
			)
		)
		with patch.object(matching, "collect", return_value=captured):
			matching.check_review(doc, True)
		with patch.object(matching, "collect", return_value={"source_digest": "changed"}):
			with self.assertRaisesRegex(frappe.ValidationError, "stale"):
				matching.check_review(doc, True)
			matching.check_review(doc, False)
		doc.assessment_evidence = json.dumps({"reviewer": "another@example.invalid"})
		with self.assertRaises(frappe.PermissionError):
			matching.check_review(doc, False)

	def test_review_artifact_evidence_cannot_be_forged_even_by_administrator(self):
		doc = frappe.new_doc("Jarvis Approval Request")
		doc.update({"source": "Agent Review", "assessment_key": "forged", "assessment_evidence": "{}"})
		with self.assertRaises(frappe.PermissionError):
			JarvisApprovalRequest._guard_server_fields(doc)
		doc.assessment_key = None
		doc.assessment_evidence = None
		before = frappe._dict(doc.as_dict())
		before.source = "File Box"
		with (
			patch.object(doc, "is_new", return_value=False),
			patch.object(doc, "get_doc_before_save", return_value=before),
			self.assertRaises(frappe.PermissionError),
		):
			JarvisApprovalRequest._guard_server_fields(doc)

	def test_review_output_does_not_depend_on_mutable_installation_policy(self):
		captured = snapshot()
		changed = copy.deepcopy(captured)
		changed["config"]["policy_version"] = "changed"
		self.assertNotEqual(matching._seal(changed)["source_digest"], captured["source_digest"])

	def test_real_writeback_validation_stages_only_live_review_candidates(self):
		from jarvis.tools import record_agent_run as writeback

		captured = snapshot()
		for mode in ("shadow", "live"):
			run = frappe._dict(
				name="RUN-AP",
				agent=matching.AGENT,
				installation="INST-AP",
				status="running",
				input_snapshot_json=json.dumps(captured),
				preparation_mode=mode,
				owner="Administrator",
				findings_count=1,
				blocker_count=0,
				coverage_note="",
				dashboard=None,
			)
			inst = frappe._dict(name="INST-AP", reviewer="Administrator")

			def get_value(doctype, *args, **kwargs):
				return (
					run
					if doctype == writeback.RUN
					else {
						"rule_tokens": json.dumps([matching.TOKEN]),
						"doctypes_required": '["Purchase Invoice"]',
					}
				)

			with (
				patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="test-session"),
				patch.object(frappe.db, "get_value", side_effect=get_value),
				patch.object(frappe.db, "set_value"),
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda doctype, name: run if doctype == writeback.RUN else inst,
				),
				patch.object(writeback, "_ref_verifiable", return_value=(True, "")),
				patch.object(matching, "collect", return_value=captured),
				patch.object(matching, "stage_reviews") as stage,
				patch("jarvis.chat.agent_runs.record_delegate_run", return_value=run) as persist,
				patch("jarvis.chat.agent_run_steps.record_step"),
			):
				result = writeback.record_agent_run(**output(captured))
				self.assertEqual(result["dropped"], [])
				self.assertEqual(len(persist.call_args.args[2]), 1)
				self.assertEqual(persist.call_args.args[1].activation_state, mode)
				self.assertEqual(stage.call_count, int(mode == "live"))

	def test_review_decision_never_resumes_the_delegate(self):
		from jarvis.chat import approvals_api

		doc = MagicMock(name="REVIEW", status="Pending", conversation="CONV", assessment_key="review-key")
		doc.get.side_effect = lambda field: "review-key" if field == "assessment_key" else None
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "sql", return_value=[(1,)]),
			patch.object(frappe.db, "commit"),
			patch.object(approvals_api, "_refuse_if_wiki_write"),
			patch.object(approvals_api, "_refuse_if_linked"),
			patch.object(approvals_api, "_unlinked", return_value=""),
			patch("jarvis.chat.operator_review.check_review") as check,
			patch.object(approvals_api, "_resume_conversation") as resume,
		):
			result = approvals_api.decide("REVIEW", "Review acknowledged; posting remains separate")
			check.assert_called_once_with(doc, True)
			self.assertFalse(result["resumed"])
			resume.assert_not_called()

	def test_ap_run_does_not_use_a_gl_watermark_or_gl_permission_slice(self):
		from jarvis.chat import agent_runs

		run = frappe._dict(
			agent=matching.AGENT,
			wm_row_count=100,
			permission_profile='{"user_permissions":{"Company":["Example"]}}',
		)
		with patch.object(frappe.db, "sql") as sql:
			self.assertFalse(agent_runs._watermark_drift(run, matching.scope_from_config(config())))
			self.assertFalse(agent_runs._rowcount_shortfall(run, 0))
			self.assertFalse(agent_runs._scoped_visibility(run, frappe._dict(scoped_visibility=1)))
			sql.assert_not_called()


class TestNativePurchaseMatch(unittest.TestCase):
	def test_native_po_receipt_draft_and_idempotent_human_review_never_post(self):
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest("ERPNext is required for native purchasing integration")
		from erpnext.accounts.utils import get_fiscal_year
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		from jarvis.chat import approvals_api
		from jarvis.tests.test_filebox_skills import E2E_CO, E2E_ITEM, E2E_SUPPLIER, _e2e_fixtures, _purge_e2e

		frappe.set_user("Administrator")
		roots, documents, reviews = [], [], []
		try:
			roots.extend(_e2e_fixtures())
			today = frappe.utils.today()
			order = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"company": E2E_CO,
					"supplier": E2E_SUPPLIER,
					"transaction_date": today,
					"schedule_date": today,
					"items": [{"item_code": E2E_ITEM, "qty": 10, "rate": 100, "schedule_date": today}],
				}
			).insert()
			documents.append((order.doctype, order.name))
			order.submit()
			receipt = make_purchase_receipt(order.name).insert()
			documents.append((receipt.doctype, receipt.name))
			receipt.submit()
			invoice = make_purchase_invoice(receipt.name)
			invoice.bill_no = "AP-MATCH-INTEGRATION"
			invoice.bill_date = today
			invoice.items[0].qty = 4
			invoice.insert()
			documents.append((invoice.doctype, invoice.name))
			policy = {
				**config(),
				"company": E2E_CO,
				"fiscal_year": get_fiscal_year(today, company=E2E_CO)[0],
				"from_date": today,
				"to_date": today,
				"report_date": today,
			}
			policy = matching.configuration(frappe._dict(config=json.dumps(policy)))
			captured = matching.collect(matching.scope_from_config(policy), policy)
			self.assertTrue(captured["complete"], captured)
			self.assertEqual(len(captured["datasets"]["invoices"]), 1)
			row = captured["datasets"]["invoices"][0]["items"][0]
			self.assertEqual(row["purchase_order"], order.name)
			self.assertEqual(row["pr_detail"], receipt.items[0].name)
			self.assertEqual(row["stock_qty"], "4.0")
			self.assertIsInstance(row["stock_qty_precision"], int)
			run = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Run",
					"agent": matching.AGENT,
					"trigger": "manual",
					"status": "running",
					"preparation_mode": "live",
					"assessment_config_json": frappe.as_json(policy),
					"scope_json": frappe.as_json(captured["scope"]),
					"session_key": "ap-native-test-session",
				}
			).insert(ignore_permissions=True)
			documents.append((run.doctype, run.name))
			from jarvis.tools.get_purchase_match_inputs import get_purchase_match_inputs

			with patch(
				"jarvis.tools.get_purchase_match_inputs.get_session_key", return_value=run.session_key
			):
				captured = get_purchase_match_inputs()
			run.reload()
			self.assertEqual(captured["captured_by"], "Administrator")
			self.assertTrue(captured["captured_at"])
			finding = output(captured)["findings"][0]
			finding["ref_name"] = invoice.name
			if assessment_path := os.environ.get("AP_ASSESSMENT_PATH"):
				with tempfile.TemporaryDirectory() as temporary:
					source = os.path.join(temporary, "input.json")
					result_path = os.path.join(temporary, "output.json")
					with open(source, "w", encoding="utf-8") as stream:
						json.dump(captured, stream)
					subprocess.run(
						[sys.executable, assessment_path, "--input", source, "--out", result_path],
						check=True,
						timeout=30,
					)
					with open(result_path, encoding="utf-8") as stream:
						assessed = json.load(stream)
					self.assertEqual(assessed["scope"], captured["scope"])
					self.assertEqual(assessed["coverage"][matching.TOKEN]["state"], "evaluated", assessed)
					self.assertEqual(assessed["findings"][0]["severity"], "note", assessed)
					self.assertIn("ready for human review", assessed["findings"][0]["note"])
					findings, coverage, ready = matching.validate_output(
						run,
						**{
							key: assessed[key]
							for key in ("findings", "coverage", "scope", "integrity_digest")
						},
					)
					self.assertTrue(ready)
					finding = findings[0]
			inst = frappe._dict(name="ap-native-test", reviewer="Administrator")
			reviews.extend(matching.stage_reviews(run, inst, [finding]))
			self.assertEqual(matching.stage_reviews(run, inst, [finding]), reviews)
			with patch.object(approvals_api, "_resume_conversation") as resume:
				result = approvals_api.decide(reviews[0], "Review acknowledged; no posting authorised")
				self.assertFalse(result["resumed"])
				resume.assert_not_called()
			self.assertEqual(frappe.db.get_value("Purchase Invoice", invoice.name, "docstatus"), 0)
			self.assertEqual(frappe.db.count("Purchase Invoice", {"company": E2E_CO}), 1)
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
			frappe.db.commit()
			_purge_e2e(roots)
