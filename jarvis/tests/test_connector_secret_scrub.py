"""Unit tests for ``jarvis.connectors.secret_scrub`` - value scrubbing by shape and
the key-name test. No bench."""

from __future__ import annotations

import json
import unittest

from jarvis.connectors.secret_scrub import REDACTION, is_secret_key, scrub_counted, scrub_text, scrub_value

AWS_ID = "AKIAIOSFODNN7EXAMPLE"
PRESIGNED = (
	"https://bucket.s3.amazonaws.com/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
	f"&X-Amz-Credential={AWS_ID}%2F20261123%2Fap-south-1%2Fs3%2Faws4_request"
	"&X-Amz-Date=20261123T101010Z&X-Amz-Expires=900"
	"&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEJr%2F%2F%2F%2F%2F%2F%2F%2F%2F%2F"
	"&X-Amz-Signature=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


class TestScrubText(unittest.TestCase):
	def test_presigned_url_loses_credential_signature_and_token(self):
		out, count = scrub_text(f"Download: {PRESIGNED}")
		self.assertNotIn(AWS_ID, out)
		self.assertNotIn("0123456789abcdef", out)
		self.assertNotIn("IQoJb3JpZ2luX2VjEJr", out)
		self.assertGreaterEqual(count, 3)
		# The harmless parts of the link stay, so it is still readable.
		self.assertIn("bucket.s3.amazonaws.com/report.csv", out)
		self.assertIn("X-Amz-Expires=900", out)
		self.assertIn(f"X-Amz-Signature={REDACTION}", out)

	def test_json_escaped_ampersand_in_a_url(self):
		out, _count = scrub_text('{"url":"https://x.test/f?a=1\\u0026access_token=abc123def456"}')
		self.assertNotIn("abc123def456", out)
		self.assertIn("a=1", out)

	def test_other_signed_url_params(self):
		for url in (
			"https://x.test/f?X-Goog-Signature=deadbeef01&X-Goog-Credential=svc%40p.iam",
			"https://acct.blob.core.windows.net/c/b?sv=2024&sig=Zm9vYmFyYmF6%3D&se=2026",
			"https://x.test/cb?id_token=abc.def&refresh_token=rrr111",
		):
			out, count = scrub_text(url)
			self.assertGreaterEqual(count, 1, url)
			for leaked in ("deadbeef01", "svc%40p.iam", "Zm9vYmFyYmF6", "abc.def", "rrr111"):
				self.assertNotIn(leaked, out)

	def test_well_known_token_shapes(self):
		secrets = [
			"ghp_" + "a1B2c3D4e5" * 4,
			"github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1",
			"sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
			"sk_test_" + "4eC39HqLyjWDarjtT1zdp7dc",
			"xoxb-1234567890-abcdefghij",
			"AIza" + "SyA-1234567890abcdefghijklmnopqrstu",
			"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
			AWS_ID,
		]
		for secret in secrets:
			out, count = scrub_text(f"id is {secret} ok")
			self.assertNotIn(secret, out)
			self.assertGreaterEqual(count, 1, secret)

	def test_bearer_and_quoted_assignment(self):
		out, _count = scrub_text(
			'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 {"password": "hunter2hunter2hunter2"}'
		)
		self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", out)
		self.assertNotIn("hunter2hunter2hunter2", out)

	def test_json_value_under_a_secret_named_key(self):
		text = '{"access_token":"short-but-real","client_secret": "s3cr3t-value","key":"user-42"}'
		out, count = scrub_text(text)
		self.assertNotIn("short-but-real", out)
		self.assertNotIn("s3cr3t-value", out)
		self.assertIn('"key":"user-42"', out)
		self.assertEqual(count, 2)

	def test_cursor_tokens_stay_readable(self):
		text = (
			'{"next_page_token":"CAESEAoOc29tZS1jdXJzb3I","pageToken":"abcdefgh1234","cursor":"abcdefgh1234"}'
		)
		self.assertEqual(scrub_text(text), (text, 0))

	def test_ordinary_text_and_ids_are_untouched(self):
		text = (
			'{"id":"c9d3e5a0-1b2c-4d5e-8f90-123456789abc","issue_key":"PROJ-123",'
			'"key":"user-42","url":"https://github.com/o/r/issues/7","number":7}'
		)
		self.assertEqual(scrub_text(text), (text, 0))

	def test_empty_and_none(self):
		self.assertEqual(scrub_text(""), ("", 0))
		self.assertEqual(scrub_text(None), ("", 0))


class TestScrubValue(unittest.TestCase):
	def test_walks_dicts_and_lists(self):
		out = scrub_value({"a": [f"x {AWS_ID}", {"b": "ghp_" + "z" * 36}], "n": 3, "ok": True})
		self.assertNotIn(AWS_ID, str(out))
		self.assertNotIn("ghp_" + "z" * 36, str(out))
		self.assertEqual(out["n"], 3)
		self.assertIs(out["ok"], True)


class TestScrubStructured(unittest.TestCase):
	"""A structured answer and its text copy are masked alike."""

	def test_a_value_under_a_credential_named_field_is_masked(self):
		out, count = scrub_counted({"client_secret": "plain-words-here", "nested": [{"apiKey": "k1"}]})
		self.assertEqual(out["client_secret"], REDACTION)
		self.assertEqual(out["nested"][0]["apiKey"], REDACTION)
		self.assertEqual(count, 2)

	def test_ids_cursors_and_locators_are_left_alone(self):
		answer = {
			"key": "user-42",
			"issue_key": "ABC-1",
			"idempotency_key": "req-9",
			"next_page_token": "cursor-77",
			"authorization_url": "https://idp.example/authorize",
			"api_key_id": "k_123",
			"description": "a long sentence about a ticket " * 5,
		}
		out, count = scrub_counted(answer)
		self.assertEqual(out, answer)
		self.assertEqual(count, 0)

	def test_counts_replacements_and_leaves_numbers_and_bools(self):
		out, count = scrub_counted({"n": 1, "ok": True, "u": f"id {AWS_ID}"})
		self.assertEqual((out["n"], out["ok"], count), (1, True, 1))


class TestScrubIsLinear(unittest.TestCase):
	def test_many_key_headers_without_a_footer_are_not_quadratic(self):
		import time

		text = "-----BEGIN PRIVATE KEY----- abc " * 60000
		started = time.monotonic()
		out, count = scrub_text(text)
		self.assertLess(time.monotonic() - started, 2.0)
		self.assertEqual(out, REDACTION)
		self.assertEqual(count, 1)

	def test_blocks_with_footers_are_masked_one_by_one(self):
		block = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----"
		out, count = scrub_text(f"a {block} b {block} c")
		self.assertEqual(out, f"a {REDACTION} b {REDACTION} c")
		self.assertEqual(count, 2)


class TestIsSecretKey(unittest.TestCase):
	def test_credential_names_are_secret(self):
		for name in (
			"password",
			"Password",
			"passwd",
			"pwd",
			"secret",
			"token",
			"access_token",
			"accessToken",
			"refresh-token",
			"credentials",
			"authorization",
			"bearer",
			"apikey",
			"api_key",
			"apiKey",
			"API_KEY",
			"APIKey",
			"access_key",
			"secret_key",
			"private_key",
			"client_secret",
			"clientSecret",
			"session_token",
			"encryption_key",
			"signing_key",
			"cookie",
		):
			self.assertTrue(is_secret_key(name), name)

	def test_ids_that_merely_contain_key_are_not(self):
		for name in (
			"key",
			"Key",
			"issue_key",
			"issueKey",
			"teamKey",
			"idempotency_key",
			"primary_key",
			"keyword",
			"max_tokens",
			"name",
			"",
		):
			self.assertFalse(is_secret_key(name), name)
