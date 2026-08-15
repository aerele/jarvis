"""Tests for the @google/gemini-cli OAuth client_secret extractor and the
env-var precedence baked into hooks.get_oauth_client_secret.

The extractor only runs for ``Google Gemini`` and only when the operator
hasn't supplied the env-var override - both gates are pinned here so a
future refactor can't silently turn the node_modules walk into a
hot-path cost.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from jarvis import hooks
from jarvis.oauth.gemini_cli_secret import (
	PACKAGE_REL_PATH,
	_app_root,
	extract_gemini_cli_secret,
)


def _seed_pkg(app_root: str, file_name: str, contents: bytes) -> str:
	"""Lay out the expected ``node_modules/@google/gemini-cli/`` tree
	under app_root and drop ``contents`` into ``file_name`` inside it.
	Returns the absolute file path so tests can assert on it.
	"""
	pkg_root = os.path.join(app_root, *PACKAGE_REL_PATH)
	os.makedirs(pkg_root, exist_ok=True)
	path = os.path.join(pkg_root, file_name)
	with open(path, "wb") as f:
		f.write(contents)
	return path


class TestExtractGeminiCliSecret(unittest.TestCase):
	def test_returns_empty_when_package_missing(self):
		with tempfile.TemporaryDirectory() as tmp:
			# No node_modules tree at all.
			self.assertEqual(extract_gemini_cli_secret(tmp), "")

	def test_extracts_secret_from_js_file(self):
		with tempfile.TemporaryDirectory() as tmp:
			_seed_pkg(
				tmp,
				"bundle.js",
				b'var x = 1;\nvar OAUTH_CLIENT_SECRET = "GOCSPX-abc123_xyz-DEF";\n',
			)
			self.assertEqual(extract_gemini_cli_secret(tmp), "GOCSPX-abc123_xyz-DEF")

	def test_extracts_secret_from_json_file(self):
		# Some gemini-cli builds carry the literal in a config JSON
		# alongside the bundle.
		with tempfile.TemporaryDirectory() as tmp:
			_seed_pkg(
				tmp,
				"config.json",
				b'{"client_id": "x", "client_secret": "GOCSPX-fromjson"}',
			)
			self.assertEqual(extract_gemini_cli_secret(tmp), "GOCSPX-fromjson")

	def test_skips_non_bundle_files(self):
		# README / LICENSE / .md / .ts files shouldn't be walked.
		with tempfile.TemporaryDirectory() as tmp:
			_seed_pkg(tmp, "README.md", b'"GOCSPX-shouldnotmatch"')
			_seed_pkg(tmp, "types.d.ts", b'"GOCSPX-alsoshouldnot"')
			self.assertEqual(extract_gemini_cli_secret(tmp), "")

	def test_returns_empty_when_no_match(self):
		# Package installed but no GOCSPX-... literal in it (e.g. a
		# future build wraps the secret in a getter, or strips it).
		with tempfile.TemporaryDirectory() as tmp:
			_seed_pkg(tmp, "bundle.js", b'export const CLIENT_ID = "x";\n')
			self.assertEqual(extract_gemini_cli_secret(tmp), "")

	def test_walks_nested_directories(self):
		# gemini-cli has a chunk/ subdir; walk should descend into it.
		with tempfile.TemporaryDirectory() as tmp:
			nested_dir = os.path.join(tmp, *PACKAGE_REL_PATH, "dist", "chunks")
			os.makedirs(nested_dir, exist_ok=True)
			nested_file = os.path.join(nested_dir, "chunk-A.mjs")
			with open(nested_file, "wb") as f:
				f.write(b'k="GOCSPX-nested-find";')
			self.assertEqual(extract_gemini_cli_secret(tmp), "GOCSPX-nested-find")

	def test_continues_past_unreadable_file(self):
		# A per-file OSError shouldn't abort the whole walk.
		with tempfile.TemporaryDirectory() as tmp:
			bad = _seed_pkg(tmp, "bundle.cjs", b'"GOCSPX-survives"')
			# Make the bundle unreadable, but seed a sibling that does
			# have the literal.
			os.chmod(bad, 0)
			_seed_pkg(tmp, "fallback.js", b'k="GOCSPX-fromfallback";')
			try:
				self.assertEqual(extract_gemini_cli_secret(tmp), "GOCSPX-fromfallback")
			finally:
				os.chmod(bad, 0o644)


class TestAppRootResolution(unittest.TestCase):
	def test_app_root_is_app_root_not_module_dir(self):
		# _app_root() must resolve to apps/jarvis (the app root holding the npm
		# package.json + node_modules + the inner jarvis module dir), NOT the
		# inner apps/jarvis/jarvis module dir. The regression returned the
		# module dir, so pkg_root pointed at a non-existent
		# apps/jarvis/jarvis/node_modules and extraction silently returned "",
		# surfacing as Google's "client_secret is missing" token-exchange error.
		root = _app_root()
		self.assertTrue(
			os.path.isfile(os.path.join(root, "package.json")),
			f"_app_root() {root!r} should contain apps/jarvis/package.json",
		)
		self.assertTrue(
			os.path.isdir(os.path.join(root, "jarvis", "oauth")),
			f"_app_root() {root!r} should contain the inner jarvis/oauth module",
		)


class TestGetOauthClientSecretPrecedence(unittest.TestCase):
	def test_env_var_wins_over_node_modules(self):
		# When the env-var is supplied, the node_modules walk should
		# never run - guards the perf hazard on every Google OAuth.
		with (
			patch.dict(
				hooks.OAUTH_CLIENT_SECRETS,
				{"Google Gemini": "GOCSPX-from-env"},
			),
			patch(
				"jarvis.oauth.gemini_cli_secret.extract_gemini_cli_secret",
			) as walker,
		):
			out = hooks.get_oauth_client_secret("Google Gemini")
		self.assertEqual(out, "GOCSPX-from-env")
		walker.assert_not_called()

	def test_falls_back_to_node_modules_when_env_unset(self):
		with (
			patch.dict(
				hooks.OAUTH_CLIENT_SECRETS,
				{"Google Gemini": ""},
			),
			patch(
				"jarvis.oauth.gemini_cli_secret.extract_gemini_cli_secret",
				return_value="GOCSPX-from-bundle",
			) as walker,
		):
			out = hooks.get_oauth_client_secret("Google Gemini")
		self.assertEqual(out, "GOCSPX-from-bundle")
		walker.assert_called_once()

	def test_other_providers_never_walk_node_modules(self):
		# Only Google Gemini falls back to the bundle scan; everything
		# else returns the env/empty value directly.
		with (
			patch.dict(
				hooks.OAUTH_CLIENT_SECRETS,
				{"OpenAI": ""},
			),
			patch(
				"jarvis.oauth.gemini_cli_secret.extract_gemini_cli_secret",
			) as walker,
		):
			out = hooks.get_oauth_client_secret("OpenAI")
		self.assertEqual(out, "")
		walker.assert_not_called()
