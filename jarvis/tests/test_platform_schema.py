"""Phase-A schema checks for the platform prerequisites (PP-1..PP-6).

Asserts the doctype fields, Select enums, the new provenance-event doctype, the
Settings ceiling, and the shared ``coverage_reasons`` enum module all exist with
the EXACT contract names later phases build on. Schema-only — the runtime
enforcement (set-once, writeback drops, strong-verb gating, append-only writes)
is covered by the per-PP behaviour tests.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import coverage_reasons as cr


def _field(doctype: str, fieldname: str):
	return frappe.get_meta(doctype).get_field(fieldname)


def _options(doctype: str, fieldname: str) -> list:
	f = _field(doctype, fieldname)
	return [o for o in (f.options or "").split("\n") if o]


class TestPlatformSchemaEnums(FrappeTestCase):
	def test_result_class_enum(self):
		self.assertEqual(
			cr.RESULT_CLASSES,
			("observed_fact", "derived_candidate", "legal_scenario", "confirmed_outcome"),
		)
		self.assertEqual(cr.RESERVED_RESULT_CLASS, "confirmed_outcome")
		self.assertEqual(cr.DEFAULT_RESULT_CLASS, "observed_fact")

	def test_run_state_enum(self):
		self.assertEqual(
			cr.RUN_STATES,
			("evaluated_clean", "partial", "not_evaluable", "failed"),
		)
		self.assertEqual(cr.CLEAN_RUN_STATE, "evaluated_clean")

	def test_reason_code_enum_is_the_closed_nine(self):
		self.assertEqual(
			set(cr.REASON_CODES),
			{
				"app_absent_or_ineligible",
				"permission_slice",
				"configuration_missing",
				"record_coverage_insufficient",
				"source_stale",
				"rule_expired",
				"external_evidence_absent",
				"run_truncated_watermark",
				"unsupported_customisation",
			},
		)
		# every code carries the full metadata contract
		for code, meta in cr.REASON_CODES.items():
			self.assertIn("remediation", meta, code)
			self.assertIn("retryable", meta, code)
			self.assertIn("routing", meta, code)
			self.assertIsInstance(meta["retryable"], bool, code)

	def test_reason_code_coercion_fail_safe(self):
		self.assertEqual(cr.coerce_reason_code("source_stale"), ("source_stale", ""))
		# an unknown string is coerced, never dropped; raw preserved in detail
		code, detail = cr.coerce_reason_code("some novel gibberish")
		self.assertEqual(code, "unsupported_customisation")
		self.assertEqual(detail, "some novel gibberish")

	def test_result_class_required_metadata(self):
		self.assertEqual(cr.required_metadata_for("observed_fact"), ())
		self.assertEqual(
			cr.required_metadata_for("derived_candidate"),
			("confidence", "match_basis", "false_positive_path"),
		)
		self.assertEqual(
			cr.required_metadata_for("legal_scenario"),
			("rule_version", "source", "reviewer"),
		)
		# a derived_candidate row missing metadata is reported missing
		self.assertEqual(
			set(cr.missing_metadata("derived_candidate", {"confidence": 90})),
			{"match_basis", "false_positive_path"},
		)


class TestPlatformSchemaFindingFields(FrappeTestCase):
	DT = "Jarvis Agent Finding"

	def test_result_class_field(self):
		f = _field(self.DT, "result_class")
		self.assertIsNotNone(f)
		self.assertEqual(f.fieldtype, "Select")
		self.assertEqual(f.reqd, 1)
		self.assertEqual(_options(self.DT, "result_class"), list(cr.RESULT_CLASSES))

	def test_derived_candidate_metadata_fields(self):
		for fn, ft in (
			("confidence", "Percent"),
			("match_basis", "Small Text"),
			("false_positive_path", "Small Text"),
			("confirmation_status", "Select"),
		):
			f = _field(self.DT, fn)
			self.assertIsNotNone(f, fn)
			self.assertEqual(f.fieldtype, ft, fn)
		self.assertEqual(_options(self.DT, "confirmation_status"), list(cr.CONFIRMATION_STATUSES))

	def test_legal_scenario_metadata_fields(self):
		for fn in ("rule_version", "assumptions", "known_exceptions", "source", "reviewer"):
			self.assertIsNotNone(_field(self.DT, fn), fn)

	def test_provenance_fields(self):
		for fn in (
			"outcome_provenance",
			"resolved_by",
			"resolved_at",
			"resolution_kind",
			"result_link_doctype",
			"result_link_name",
		):
			self.assertIsNotNone(_field(self.DT, fn), fn)
		self.assertEqual(
			_options(self.DT, "resolution_kind"),
			["human", "auto_coverage", "auto_watermark"],
		)


class TestPlatformSchemaRunFields(FrappeTestCase):
	DT = "Jarvis Agent Run"

	def test_result_state_field(self):
		f = _field(self.DT, "result_state")
		self.assertIsNotNone(f)
		self.assertEqual(f.fieldtype, "Select")
		self.assertEqual(f.read_only, 1)
		self.assertEqual(_options(self.DT, "result_state"), list(cr.RUN_STATES))

	def test_pp5_launch_fields(self):
		for fn in ("bundle_version", "preparation_mode", "initiating_human"):
			f = _field(self.DT, fn)
			self.assertIsNotNone(f, fn)
			self.assertEqual(f.read_only, 1, fn)
		self.assertEqual(_options(self.DT, "preparation_mode"), ["shadow", "live"])


class TestPlatformSchemaApprovalFields(FrappeTestCase):
	DT = "Jarvis Approval Request"

	def test_pp5_and_pp1_fields(self):
		for fn in ("agent", "run", "preparation_mode", "result_class"):
			self.assertIsNotNone(_field(self.DT, fn), fn)
		# result_class is NOT reqd on approvals (human requests leave it blank)
		self.assertFalse(_field(self.DT, "result_class").reqd)
		self.assertEqual(_options(self.DT, "result_class"), list(cr.RESULT_CLASSES))


class TestPlatformSchemaInstallationFields(FrappeTestCase):
	DT = "Jarvis Agent Installation"

	def test_pp4_activation_fields(self):
		f = _field(self.DT, "activation_state")
		self.assertIsNotNone(f)
		self.assertEqual(_options(self.DT, "activation_state"), ["shadow", "live"])
		self.assertEqual(f.default, "shadow")
		reviewer = _field(self.DT, "reviewer")
		self.assertIsNotNone(reviewer)
		self.assertEqual(reviewer.reqd, 1)
		for fn in ("promoted_by", "promoted_at"):
			self.assertIsNotNone(_field(self.DT, fn), fn)

	def test_pp3_installable_fields(self):
		f = _field(self.DT, "installable")
		self.assertIsNotNone(f)
		self.assertEqual(f.fieldtype, "Check")
		nir = _field(self.DT, "not_installable_reason")
		self.assertIsNotNone(nir)
		# the closed reason enum is a subset of its options
		self.assertTrue(set(cr.REASON_CODES).issubset(set(_options(self.DT, "not_installable_reason"))))


class TestPlatformSchemaSettingsAndProvenance(FrappeTestCase):
	def test_activation_module_ceiling(self):
		f = _field("Jarvis Settings", "activation_module_ceiling")
		self.assertIsNotNone(f)
		self.assertEqual(f.fieldtype, "Int")
		self.assertEqual(str(f.default), "1")

	def test_provenance_event_doctype_and_fields(self):
		self.assertTrue(frappe.db.exists("DocType", "Jarvis Agent Provenance Event"))
		meta = frappe.get_meta("Jarvis Agent Provenance Event")
		for fn in (
			"event_type",
			"agent",
			"bundle_version",
			"run",
			"installation",
			"preparation_mode",
			"initiating_human",
			"reviewing_human",
			"outcome",
			"finding",
			"approval",
			"result_link_doctype",
			"result_link_name",
			"occurred_at",
		):
			self.assertIsNotNone(meta.get_field(fn), fn)
		event_opts = [o for o in (meta.get_field("event_type").options or "").split("\n") if o]
		for required in (
			"run_launched",
			"finding_raised",
			"approval_requested",
			"approval_decided",
			"draft_created",
			"transaction_posted",
			"finding_resolved",
			"agent_promoted_to_live",
			"incident_raised",
		):
			self.assertIn(required, event_opts, required)

	def test_provenance_event_is_append_only(self):
		ev = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Provenance Event",
				"event_type": "run_launched",
			}
		).insert(ignore_permissions=True)
		self.assertTrue(ev.occurred_at)
		# a persisted event cannot be modified...
		ev.detail = "tampered"
		with self.assertRaises(frappe.PermissionError):
			ev.save(ignore_permissions=True)
		# ...nor deleted (even with permissions ignored)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Jarvis Agent Provenance Event", ev.name, ignore_permissions=True, force=True)
