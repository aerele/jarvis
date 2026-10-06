"""Source revision reporting is exact, remembers only real answers, and fails soft."""

import subprocess
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jarvis import source_version


def _git(returncode: int, stdout: str) -> SimpleNamespace:
	return SimpleNamespace(returncode=returncode, stdout=stdout)


class TestSourceDetails(TestCase):
	def setUp(self):
		source_version.reset_cache()

	def tearDown(self):
		source_version.reset_cache()

	def test_reports_branch_and_exact_release_tag(self):
		with patch.object(source_version, "_git_value", side_effect=["version-16", "v16.1.0"]):
			self.assertEqual(
				source_version.source_details(),
				{"branch": "version-16", "tag": "v16.1.0"},
			)

	def test_answers_are_cached_for_repeated_connection_polls(self):
		# An empty answer is still an answer (detached / untagged) and is cached like any other.
		with patch.object(source_version, "_git_value", side_effect=["develop", ""]) as git_value:
			source_version.source_details()
			self.assertEqual(source_version.source_details(), {"branch": "develop", "tag": ""})
		self.assertEqual(git_value.call_count, 2)

	def test_unknown_value_is_reported_as_none_and_asked_again(self):
		# A timed-out git call must not be memoised as "no branch": the next poll retries it, and
		# in the meantime the caller sees None rather than a blank it would clear upstream with.
		with patch.object(
			source_version, "_git_value", side_effect=[None, "v16.1.0", "version-16"]
		) as git_value:
			self.assertEqual(source_version.source_details(), {"branch": None, "tag": "v16.1.0"})
			self.assertEqual(source_version.source_details(), {"branch": "version-16", "tag": "v16.1.0"})
		self.assertEqual(git_value.call_count, 3)


class TestGitValue(TestCase):
	def test_detached_head_and_missing_tag_are_confirmed_blanks(self):
		with patch.object(source_version.subprocess, "run", side_effect=[_git(0, "\n"), _git(128, "")]):
			self.assertEqual(source_version._git_value("branch", "--show-current"), "")
			self.assertEqual(source_version._git_value("describe", "--tags", "--exact-match"), "")

	def test_timeout_and_os_error_are_unknown(self):
		with patch.object(
			source_version.subprocess,
			"run",
			side_effect=[subprocess.TimeoutExpired("git", 2), OSError("fork failed")],
		):
			self.assertIsNone(source_version._git_value("branch", "--show-current"))
			self.assertIsNone(source_version._git_value("describe", "--tags", "--exact-match"))

	def test_only_a_release_shaped_tag_counts(self):
		with patch.object(
			source_version.subprocess, "run", side_effect=[_git(0, "v1-beta\n"), _git(0, "v16.2.0\n")]
		):
			self.assertEqual(source_version._git_value("describe", "--tags", "--exact-match"), "")
			self.assertEqual(source_version._git_value("describe", "--tags", "--exact-match"), "v16.2.0")
