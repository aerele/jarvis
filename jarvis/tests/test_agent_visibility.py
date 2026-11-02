"""Unit tests for the pure agent visibility classifier."""

import hashlib
import json
import unittest

from jarvis.chat.agent_visibility import (
	GL_SCOPED_DIMENSIONS,
	PAYROLL_SCOPED_DIMENSIONS,
	exact_bool,
	make_visibility_receipt,
	parse_visibility_receipt,
	profile_hash,
	profile_scoped_visibility,
	scoped_dimensions,
)


def _profile(*, roles=None, permissions=None):
	"""Build the exact scheduler profile shape used by receipt tests."""
	summary = {
		"roles": roles or ["HR User"],
		"user_permissions": permissions if permissions is not None else {},
	}
	digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
	return {"hash": digest, **summary}


class TestAgentVisibility(unittest.TestCase):
	def test_payroll_uses_hr_dimensions_and_finance_keeps_gl_dimensions(self):
		self.assertEqual(
			PAYROLL_SCOPED_DIMENSIONS,
			(
				"Employee",
				"Department",
				"Branch",
				"Designation",
				"Grade",
				"Leave Type",
				"Holiday List",
				"Salary Structure",
				"Salary Component",
			),
		)
		self.assertEqual(
			GL_SCOPED_DIMENSIONS,
			(
				"Account",
				"Cost Center",
				"Project",
				"Fiscal Year",
				"Finance Book",
				"Customer",
				"Supplier",
				"Employee",
				"Shareholder",
			),
		)
		self.assertEqual(scoped_dimensions("hrms-payroll"), PAYROLL_SCOPED_DIMENSIONS)
		self.assertEqual(scoped_dimensions("accounting"), GL_SCOPED_DIMENSIONS)
		self.assertEqual(scoped_dimensions("unknown-domain"), GL_SCOPED_DIMENSIONS)

		department_profile = json.dumps({"user_permissions": {"Department": ["Finance"]}})
		cost_center_profile = json.dumps({"user_permissions": {"Cost Center": ["Main"]}})
		self.assertIs(profile_scoped_visibility(department_profile, "hrms-payroll"), True)
		self.assertIs(profile_scoped_visibility(department_profile, "accounting"), False)
		self.assertIs(profile_scoped_visibility(cost_center_profile, "hrms-payroll"), False)
		self.assertIs(profile_scoped_visibility(cost_center_profile, "accounting"), True)

	def test_profile_classifier_is_tristate_and_rejects_malformed_values(self):
		self.assertIs(profile_scoped_visibility('{"user_permissions": {}}', "hrms-payroll"), False)
		self.assertIs(profile_scoped_visibility(None, "hrms-payroll"), None)
		self.assertIs(profile_scoped_visibility("not json", "hrms-payroll"), None)
		self.assertIs(profile_scoped_visibility('{"user_permissions": []}', "hrms-payroll"), None)
		self.assertIs(
			profile_scoped_visibility('{"user_permissions": {"Department": "Finance"}}', "hrms-payroll"),
			None,
		)

	def test_exact_bool_rejects_integer_and_string_lookalikes(self):
		self.assertIs(exact_bool(True), True)
		self.assertIs(exact_bool(False), False)
		for malformed in (None, 0, 1, "false", "true", [], {}):
			with self.subTest(malformed=malformed):
				self.assertIsNone(exact_bool(malformed))

	def test_hr_visibility_receipt_is_independent_of_catalog_domain(self):
		"""A Measurement agent can still use the reviewed HR policy selector."""
		profile = _profile(permissions={"Department": ["Finance"]})
		self.assertIs(profile_scoped_visibility(profile, "measurement"), False)
		receipt = make_visibility_receipt(profile, "hrms-payroll")
		self.assertEqual(receipt["state"], "sliced")
		self.assertEqual(receipt["visibility_domain"], "hrms-payroll")
		self.assertTrue(receipt["profile_hash"])

	def test_receipt_fails_closed_for_missing_malformed_or_changed_profiles(self):
		complete_profile = _profile()
		complete = make_visibility_receipt(complete_profile, "hrms-payroll")
		self.assertEqual(complete["state"], "complete")
		self.assertEqual(profile_hash(complete_profile), complete["profile_hash"])

		for malformed in (
			None,
			"not json",
			{"roles": [], "user_permissions": {}},
			{"hash": "0" * 64, "roles": [], "user_permissions": {}},
			_profile(permissions={"Department": "Finance"}),
			{**_profile(), "unreviewed_new_key": True},
		):
			with self.subTest(malformed=malformed):
				self.assertEqual(make_visibility_receipt(malformed, "hrms-payroll")["state"], "unknown")

		changed = _profile(permissions={"Department": ["Finance"]})
		self.assertNotEqual(profile_hash(changed), complete["profile_hash"])

	def test_persisted_receipt_rejects_wrong_policy_or_proof(self):
		receipt = make_visibility_receipt(_profile(), "hrms-payroll")
		self.assertEqual(parse_visibility_receipt(receipt, "hrms-payroll")["state"], "complete")
		for bad in (
			None,
			"not json",
			{**receipt, "visibility_domain": "measurement"},
			{**receipt, "profile_hash": "bad"},
			{**receipt, "state": "clean"},
		):
			with self.subTest(bad=bad):
				self.assertIsNone(parse_visibility_receipt(bad, "hrms-payroll"))
