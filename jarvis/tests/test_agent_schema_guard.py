"""Exercise the writeback tool boundary with a forged model conclusion."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs, derived_config
from jarvis.tools import record_agent_run as writeback


class TestAgentSchemaGuard(FrappeTestCase):
	def _write(self, provider, *, findings=None, coverage=None, keys='["tax_withholding_model"]'):
		run = frappe._dict(name="FAKE-RUN", status="running", installation="FAKE-INST", agent="fake-auditor")
		inst = frappe._dict(name="FAKE-INST", agent="fake-auditor", owner="Administrator")
		listing = {
			"rule_tokens": '["check", "external"]',
			"doctypes_required": "[]",
			"config_keys": keys,
		}
		result = frappe._dict(
			name=run.name, owner="Administrator", status="partial", findings_count=0, blocker_count=0
		)
		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="FAKE-SESSION"),
			patch.object(writeback.frappe.db, "get_value", side_effect=[run, listing]),
			patch.object(writeback.frappe, "get_doc", side_effect=[inst, run]),
			patch.dict(derived_config.PROVIDERS, {"tax_withholding_model": (provider, False)}),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(writeback, "_ref_verifiable", return_value=(True, "")),
			patch.object(agent_runs, "record_delegate_run", return_value=result) as persist,
			patch("jarvis.chat.agent_run_steps.record_step"),
		):
			writeback.record_agent_run(
				findings=findings or [],
				coverage=coverage or {"check": {"state": "evaluated"}},
				dashboard="MODEL-PAGE",
				integrity_digest="MODEL-DIGEST",
			)
		return persist.call_args

	def test_v16_cannot_record_a_forged_finding_or_clean_coverage(self):
		finding = {
			"token": "check",
			"ref_doctype": "Company",
			"ref_name": "Fake Company",
			"severity": "note",
			"amount": 1,
			"result_class": "observed_fact",
		}
		call = self._write(lambda _: "entry_table_v16", findings=[finding])
		self.assertEqual(call.args[2], [])
		self.assertIn("unsupported_customisation", call.kwargs["dropped"][0]["reason"])
		self.assertTrue(
			all(row["reason_code"] == "unsupported_customisation" for row in call.kwargs["coverage"].values())
		)
		self.assertIsNone(call.kwargs["dashboard"])
		self.assertIsNone(call.kwargs["integrity_digest"])

	def test_legacy_schema_keeps_the_existing_writeback(self):
		coverage = {"check": {"state": "evaluated"}}
		call = self._write(lambda _: "legacy_supplier", coverage=coverage)
		self.assertEqual(call.kwargs["coverage"], coverage)
		self.assertEqual(call.kwargs["dashboard"], "MODEL-PAGE")
		self.assertEqual(call.kwargs["integrity_digest"], "MODEL-DIGEST")

	def test_provider_error_cannot_record_clean_coverage(self):
		call = self._write(lambda _: 1 / 0)
		self.assertEqual(call.kwargs["coverage"]["check"]["reason_code"], "unsupported_customisation")
		self.assertIsNone(call.kwargs["dashboard"])

	def test_external_evidence_gap_is_preserved(self):
		external = {
			"state": "not_evaluable",
			"reason_code": "external_evidence_absent",
			"detail": "portal facts",
		}
		call = self._write(lambda _: "entry_table_v16", coverage={"external": external})
		self.assertEqual(call.kwargs["coverage"]["external"], external)
		self.assertEqual(call.kwargs["coverage"]["check"]["reason_code"], "unsupported_customisation")

	def test_unverified_legacy_runtime_is_also_blocked(self):
		call = self._write(lambda _: "legacy_supplier_unverified")
		self.assertEqual(call.kwargs["coverage"]["check"]["reason_code"], "unsupported_customisation")

	def test_honest_guarded_page_is_kept_and_extra_tokens_are_removed(self):
		coverage = {
			token: {"state": "not_evaluable", "reason_code": "unsupported_customisation"}
			for token in ("check", "external", "invented")
		}
		call = self._write(lambda _: "entry_table_v16", coverage=coverage)
		self.assertEqual(set(call.kwargs["coverage"]), {"check", "external"})
		self.assertEqual(call.kwargs["dashboard"], "MODEL-PAGE")

	def test_undeclared_guard_does_not_affect_other_agents(self):
		call = self._write(lambda _: "entry_table_v16", keys="[]")
		self.assertEqual(call.kwargs["coverage"], {"check": {"state": "evaluated"}})
