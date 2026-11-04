"""Collaboration fixtures only run on the dedicated local site or GitHub CI site."""

import os
import unittest
from unittest.mock import patch

from jarvis.tests._collaboration_test_site import require_collaboration_test_site


class TestCollaborationTestSite(unittest.TestCase):
	def test_local_dedicated_site_is_allowed(self):
		with patch.dict(os.environ, {}, clear=True):
			require_collaboration_test_site("test_jarvis")

	def test_exact_ci_site_is_allowed_in_github_actions(self):
		with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
			require_collaboration_test_site("jarvis-ci.test")

	def test_ci_site_refuses_outside_github_actions(self):
		for value in (None, "", "false", "1", "TRUE"):
			with self.subTest(value=value):
				with patch.dict(os.environ, {}, clear=True):
					if value is not None:
						os.environ["GITHUB_ACTIONS"] = value
					with self.assertRaises(AssertionError):
						require_collaboration_test_site("jarvis-ci.test")

	def test_live_sites_and_near_matches_always_refuse(self):
		for actions in ("false", "true"):
			with patch.dict(os.environ, {"GITHUB_ACTIONS": actions}, clear=True):
				for site in (
					"jarvis.localhost",
					"jarvis_admin.localhost",
					"jarvis_helpdesk.localhost",
					"other.test",
					"test_jarvis_backup",
					"jarvis-ci.test.backup",
					"JARVIS-CI.TEST",
					None,
					"",
				):
					with self.subTest(actions=actions, site=site):
						with self.assertRaises(AssertionError):
							require_collaboration_test_site(site)
