"""Unit tests for the pure agent visibility classifier."""

import json
import unittest

from jarvis.chat.agent_visibility import (
	GL_SCOPED_DIMENSIONS,
	PAYROLL_SCOPED_DIMENSIONS,
	exact_bool,
	profile_scoped_visibility,
	scoped_dimensions,
)


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
