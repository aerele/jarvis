"""A verified native skills root cannot be inferred from the media directory."""

import unittest
from unittest.mock import patch

import frappe

from jarvis.tests._collaboration_test_site import require_collaboration_test_site


class TestCollaborationRuntimePaths(unittest.TestCase):
	def setUp(self):
		require_collaboration_test_site(frappe.local.site)

	def test_attested_skills_root_is_used_verbatim(self):
		from jarvis.chat.collaboration_runtime import configured_skills_root

		root = "/srv/shared/skills"
		with patch.dict(frappe.conf, {"chat_collaboration_skills_root": root}):
			self.assertEqual(configured_skills_root(), root)

	def test_missing_or_malformed_root_refuses_instead_of_guessing(self):
		from jarvis.chat.collaboration_runtime import configured_skills_root

		for value in (None, "", True, {}, "relative/skills", "/workspace/../skills", "/workspace/private"):
			with self.subTest(value=value):
				with patch.dict(frappe.conf, {"chat_collaboration_skills_root": value}):
					with self.assertRaises(frappe.ValidationError):
						configured_skills_root()
