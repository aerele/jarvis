"""Regressions for the independent review of owner-only chat exports."""

from __future__ import annotations

import html
import json
import re
import sys
import unittest
from contextlib import contextmanager

from jarvis.chat.export import _render_blocks, _render_chart
from jarvis.chat.scrub import scrub_for_share


def _rows(markdown: str) -> list[list[str]]:
	return [
		[
			html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|>~\-])", r"\1", cell.strip()))
			for cell in line.strip().strip("|").split("|")
		]
		for line in markdown.splitlines()
		if line.strip().startswith("|") and not re.fullmatch(r"[\s|:\-]+", line)
	]


def _strings(value):
	"""Inspect arbitrarily nested output without recursing in the assertion."""
	stack = [value]
	while stack:
		item = stack.pop()
		if isinstance(item, dict):
			stack.extend(item.values())
		elif isinstance(item, list):
			stack.extend(item)
		elif isinstance(item, str):
			yield item


@contextmanager
def _normal_recursion_limit():
	"""Exercise Python's normal limit even when a test runner changes it."""
	previous = sys.getrecursionlimit()
	try:
		sys.setrecursionlimit(1000)
		yield
	finally:
		sys.setrecursionlimit(previous)


class TestChatExportReviewScrub(unittest.TestCase):
	def test_explicit_authorization_headers_scrub_short_case_insensitive_bearers(self):
		for scheme in ("bearer", "bEaReR", "Bearer"):
			for header in (f"Authorization: {scheme} S2", f"authorization = '{scheme}\nS2'"):
				with self.subTest(header=header):
					self.assertNotIn("S2", scrub_for_share(header))

	def test_deep_json_strings_do_not_crash_or_leak_nested_credentials(self):
		for depth in (600, 950):
			for opener, closer in (("[", "]"), ('{"wrap":', "}")):
				with self.subTest(depth=depth, opener=opener):
					text = opener * depth + '{"access_token":"DEEP-JSON-SENTINEL"}' + closer * depth
					with _normal_recursion_limit():
						out = scrub_for_share(text)
					self.assertIsInstance(out, str)
					self.assertNotIn("DEEP-JSON-SENTINEL", out)

	def test_deep_python_containers_do_not_crash_or_leak_nested_credentials(self):
		for depth in (600, 950):
			with self.subTest(depth=depth):
				value = {"access_token": "DEEP-CONTAINER-SENTINEL"}
				for _ in range(depth):
					value = {"wrapper": [value]}
				with _normal_recursion_limit():
					out = scrub_for_share(value)
				self.assertFalse(any("DEEP-CONTAINER-SENTINEL" in text for text in _strings(out)))

	def test_nested_json_string_at_depth_does_not_leak(self):
		value = '{"access_token":"NESTED-STRING-SENTINEL"}'
		for _ in range(600):
			value = {"wrapper": value}
		with _normal_recursion_limit():
			out = scrub_for_share(value)
		self.assertFalse(any("NESTED-STRING-SENTINEL" in text for text in _strings(out)))

	def test_deep_structured_safe_leaf_is_preserved_while_secret_is_removed(self):
		value = {"author": "KEPT-DEEP-SAFE-LEAF", "access_token": "DEEP-SECRET-SENTINEL"}
		for _ in range(600):
			value = {"wrapper": [value]}
		with _normal_recursion_limit():
			out = scrub_for_share(value)
		leaves = list(_strings(out))
		self.assertIn("KEPT-DEEP-SAFE-LEAF", leaves)
		self.assertFalse(any("DEEP-SECRET-SENTINEL" in text for text in leaves))

	def test_parseable_deep_json_keeps_safe_leaf_and_removes_secret(self):
		text = (
			'{"wrapper":' * 600
			+ '{"author":"KEPT-JSON-SAFE-LEAF","access_token":"JSON-SECRET-SENTINEL"}'
			+ "}" * 600
		)
		with _normal_recursion_limit():
			out = scrub_for_share(text)
		self.assertIn("KEPT-JSON-SAFE-LEAF", out)
		self.assertNotIn("JSON-SECRET-SENTINEL", out)

	def test_pem_private_key_blocks_are_redacted(self):
		payload = "UEVNLVNFQ1JFVC1TRU5USU5FTA=="
		for kind in ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"):
			with self.subTest(kind=kind):
				out = scrub_for_share(
					f"Before\n-----BEGIN {kind}-----\n{payload}\n-----END {kind}-----\nAfter"
				)
				self.assertNotIn(payload, out)
				self.assertNotIn(f"BEGIN {kind}", out)
				self.assertIn("Before", out)
				self.assertIn("After", out)

	def test_aws_temporary_and_long_term_access_keys_are_redacted(self):
		for key in ("ASIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"):
			with self.subTest(key=key):
				self.assertNotIn(key, scrub_for_share(f"Before {key} after"))

	def test_lowercase_bearer_cheque_prose_survives(self):
		text = "The bearer cheque was deposited yesterday."
		self.assertEqual(scrub_for_share(text), text)

	def test_ordinary_xoxo_signoff_survives(self):
		text = "Thanks for your help, xoxo."
		self.assertEqual(scrub_for_share(text), text)

	def test_short_uppercase_stock_identifier_survives(self):
		text = "Stock item SK-1001 has ten units available."
		self.assertEqual(scrub_for_share(text), text)

	def test_company_secretary_prose_survives(self):
		for text in (
			"Company Secretary: Mr. Rao reviewed the minutes.",
			"CompanySecretary: Priya reviewed the minutes.",
		):
			with self.subTest(text=text):
				self.assertEqual(scrub_for_share(text), text)

	def test_bare_compound_credential_keys_keep_whole_key_matching(self):
		for key in ("dbpassword", "dbpassword_raw", "clientsecret", "clientsecret_raw"):
			with self.subTest(key=key):
				self.assertNotIn(
					"BARE-CREDENTIAL-SENTINEL", scrub_for_share(f"{key}: BARE-CREDENTIAL-SENTINEL")
				)

	def test_secretary_exception_is_prose_only_and_preserves_other_secret_fragments(self):
		self.assertNotIn(
			"STRUCTURED-SECRET", json.dumps(scrub_for_share({"CompanySecretary": "STRUCTURED-SECRET"}))
		)
		self.assertNotIn(
			"BARE-CREDENTIAL-SENTINEL", scrub_for_share("ClientSecretSecretary: BARE-CREDENTIAL-SENTINEL")
		)

	def test_markdown_key_label_keeps_its_formatting(self):
		text = "**Key:** Keep the invoice reference."
		self.assertEqual(scrub_for_share(text), text)

	def test_existing_bearer_jwt_api_key_and_multiline_rules_remain_effective(self):
		secrets = (
			("Authorization: Bearer S2", "S2"),
			("Authorization: Bearer\nMULTILINE-BEARER-SENTINEL", "MULTILINE-BEARER-SENTINEL"),
			('"access_token":\n"MULTILINE-KEY-SENTINEL"', "MULTILINE-KEY-SENTINEL"),
			("sk-APIKEYSENTINEL1234567890", "sk-APIKEYSENTINEL1234567890"),
			("xoxb-SLACKSENTINEL12345", "xoxb-SLACKSENTINEL12345"),
			("ghp_GITHUBSENTINEL1234567890", "ghp_GITHUBSENTINEL1234567890"),
			("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTRUNSRVQifQ.c2lnbmF0dXJl", "eyJhbGciOiJIUzI1NiJ9"),
		)
		for text, sentinel in secrets:
			with self.subTest(text=text):
				self.assertNotIn(sentinel, scrub_for_share(text))


class TestChatExportReviewCharts(unittest.TestCase):
	def test_malformed_records_series_fields_fall_back_without_crashing(self):
		for field in (["amount"], {"name": "amount"}):
			with self.subTest(field=field):
				spec = {
					"type": "bar",
					"title": "Malformed records",
					"x": "label",
					"data": [{"label": "A", "amount": 10}],
					"series": [{"name": "Amount", "field": field}],
				}
				out = _render_blocks("Before\n```jarvis-chart\n" + json.dumps(spec) + "\n```\nAfter")
				self.assertIn("[Chart: Malformed records]", out)
				self.assertIn("Before", out)
				self.assertIn("After", out)

	def test_deep_chart_json_falls_back_without_crashing(self):
		body = (
			'{"type":"bar","title":"Deep chart","extra":'
			+ "[" * 950
			+ "0"
			+ "]" * 950
			+ ',"x":["A"],"series":[{"name":"Amount","data":[10]}]}'
		)
		out = _render_chart(body)
		self.assertIsInstance(out, str)
		self.assertTrue("[Chart:" in out or "10" in out)

	def test_empty_series_does_not_discard_another_series_values(self):
		for missing in ({"name": "Empty"}, {"name": "Empty", "data": None}, {"name": "Empty", "data": []}):
			with self.subTest(series=missing):
				spec = {
					"type": "bar",
					"x": ["Alpha", "Beta"],
					"series": [missing, {"name": "Sales", "data": [13, 21]}],
				}
				rows = _rows(_render_chart(json.dumps(spec)))
				self.assertTrue(rows)
				self.assertIn("Sales", rows[0])
				self.assertTrue(any("Alpha" in row and "13" in row for row in rows))
				self.assertTrue(any("Beta" in row and "21" in row for row in rows))

	def test_funnel_without_labels_uses_frontend_numbered_stages(self):
		spec = {"type": "funnel", "series": [{"name": "Pipeline", "data": [90, 45, 12]}]}
		rows = _rows(_render_chart(json.dumps(spec)))
		self.assertTrue(rows)
		for index, value in enumerate((90, 45, 12), 1):
			self.assertTrue(any(f"#{index}" in row and str(value) in row for row in rows))

	def test_records_with_existing_label_array_are_normalized(self):
		spec = {
			"type": "bar",
			"x": ["Alpha", "Beta"],
			"data": [{"amount": 13}, {"amount": 21}],
			"series": [{"name": "Sales", "field": "amount"}],
		}
		rows = _rows(_render_chart(json.dumps(spec)))
		self.assertTrue(any("Alpha" in row and "13" in row for row in rows))
		self.assertTrue(any("Beta" in row and "21" in row for row in rows))

	def test_records_numeric_values_match_frontend_number_conversion(self):
		spec = {
			"type": "bar",
			"x": "label",
			"data": [
				{"label": "Alpha", "amount": "17"},
				{"label": "Beta", "amount": "invalid"},
				{"label": "Gamma", "amount": None},
			],
			"series": [{"name": "Sales", "field": "amount"}],
		}
		rows = _rows(_render_chart(json.dumps(spec)))
		for label, value in (("Alpha", "17"), ("Beta", "0"), ("Gamma", "0")):
			self.assertTrue(any(label in row and value in row for row in rows))

	def test_data_beyond_the_label_array_is_preserved(self):
		spec = {
			"type": "bar",
			"x": ["Only label"],
			"series": [{"name": "Sales", "data": [13, 21, 34]}, {"name": "Costs", "data": [5, 8]}],
		}
		rows = _rows(_render_chart(json.dumps(spec)))
		self.assertTrue(any("Only label" in row and "13" in row and "5" in row for row in rows))
		for value in ("21", "34", "8"):
			self.assertTrue(any(value in row for row in rows), f"lost value {value}: {rows}")
