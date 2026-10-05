"""Source revision reporting is exact, remembers only real answers, and fails soft."""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import git

from jarvis import source_version

_IDENTITY = {
	"GIT_AUTHOR_NAME": "Test",
	"GIT_AUTHOR_EMAIL": "test@example.com",
	"GIT_COMMITTER_NAME": "Test",
	"GIT_COMMITTER_EMAIL": "test@example.com",
}


class TestSourceDetails(TestCase):
	def setUp(self):
		source_version.reset_cache()

	def tearDown(self):
		source_version.reset_cache()

	def test_reports_branch_and_exact_release_tag(self):
		with patch.object(source_version, "_read_value", side_effect=["version-16", "v16.1.0"]):
			self.assertEqual(
				source_version.source_details(),
				{"branch": "version-16", "tag": "v16.1.0"},
			)

	def test_answers_are_cached_for_repeated_connection_polls(self):
		# An empty answer is still an answer (detached / untagged) and is cached like any other.
		with patch.object(source_version, "_read_value", side_effect=["develop", ""]) as read_value:
			source_version.source_details()
			self.assertEqual(source_version.source_details(), {"branch": "develop", "tag": ""})
		self.assertEqual(read_value.call_count, 2)

	def test_unknown_value_is_reported_as_none_and_asked_again(self):
		# An unreadable checkout must not be memoised as "no branch": the next poll retries it, and
		# in the meantime the caller sees None rather than a blank it would clear upstream with.
		with patch.object(
			source_version, "_read_value", side_effect=[None, "v16.1.0", "version-16"]
		) as read_value:
			self.assertEqual(source_version.source_details(), {"branch": None, "tag": "v16.1.0"})
			self.assertEqual(source_version.source_details(), {"branch": "version-16", "tag": "v16.1.0"})
		self.assertEqual(read_value.call_count, 3)


class TestReadValue(TestCase):
	def setUp(self):
		stack = contextlib.ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.dict(os.environ, _IDENTITY))
		self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
		self.repo = git.Repo.init(self.root, initial_branch="version-16")
		stack.enter_context(patch.object(source_version, "_REPOSITORY_ROOT", self.root))

	def commit(self):
		return self.repo.index.commit("change")

	def test_branch_and_highest_release_tag_on_head(self):
		self.repo.create_tag("v16.0.9", ref=self.commit())
		head = self.commit()
		self.repo.create_tag("v16.1.0", ref=head)
		self.repo.create_tag("v16.10.0", ref=head, message="annotated")
		self.repo.create_tag("v16.2.0-beta", ref=head)
		self.assertEqual(source_version._read_value("branch"), "version-16")
		self.assertEqual(source_version._read_value("tag"), "v16.10.0")

	def test_detached_head_and_untagged_commit_are_confirmed_blanks(self):
		self.repo.create_tag("v16.1.0", ref=self.commit())
		self.repo.head.reference = self.commit()
		self.assertEqual(source_version._read_value("branch"), "")
		self.assertEqual(source_version._read_value("tag"), "")

	def test_repository_without_commits_has_no_tag(self):
		self.assertEqual(source_version._read_value("branch"), "version-16")
		self.assertEqual(source_version._read_value("tag"), "")

	def test_not_a_checkout_is_a_confirmed_blank(self):
		with (
			tempfile.TemporaryDirectory() as plain,
			patch.object(source_version, "_REPOSITORY_ROOT", Path(plain)),
		):
			self.assertEqual(source_version._read_value("branch"), "")

	def test_unreadable_checkout_is_unknown(self):
		with patch.object(git, "Repo", side_effect=OSError("disk error")):
			self.assertIsNone(source_version._read_value("branch"))
			self.assertIsNone(source_version._read_value("tag"))
