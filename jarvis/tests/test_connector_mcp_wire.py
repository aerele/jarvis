"""Unit tests for ``jarvis.connectors.mcp_wire`` - header value encoding, modern
error recognition and ``x-mcp-header`` mirroring (spec 2026-07-28, Streamable
HTTP "Value Encoding" + "Custom Headers from Tool Parameters"). No bench."""

from __future__ import annotations

import unittest

from jarvis.connectors import mcp_wire


class TestHeaderValueEncoding(unittest.TestCase):
	def test_examples_from_the_spec_table(self):
		enc = mcp_wire.encode_header_value
		self.assertEqual(enc("us-west1"), "us-west1")
		self.assertEqual(enc("Hello, 世界"), "=?base64?SGVsbG8sIOS4lueVjA==?=")
		self.assertEqual(enc(" padded "), "=?base64?IHBhZGRlZCA=?=")
		self.assertEqual(enc("line1\nline2"), "=?base64?bGluZTEKbGluZTI=?=")
		self.assertEqual(enc("=?base64?literal?="), "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=")

	def test_integer_and_boolean_forms(self):
		self.assertEqual(mcp_wire.encode_header_value(42), "42")
		self.assertEqual(mcp_wire.encode_header_value(-7), "-7")
		self.assertEqual(mcp_wire.encode_header_value(True), "true")
		self.assertEqual(mcp_wire.encode_header_value(False), "false")


class TestModernErrors(unittest.TestCase):
	def test_parse_rpc_error_shapes(self):
		self.assertIsNone(mcp_wire.parse_rpc_error(b""))
		self.assertIsNone(mcp_wire.parse_rpc_error(b"not json"))
		self.assertIsNone(mcp_wire.parse_rpc_error(b'{"jsonrpc":"2.0","id":1,"result":{}}'))
		err = mcp_wire.parse_rpc_error(
			b'{"jsonrpc":"2.0","id":1,"error":{"code":-32022,"message":"x","data":{"supported":["2026-07-28"]}}}'
		)
		self.assertEqual(err["code"], -32022)
		self.assertEqual(mcp_wire.supported_versions(err), ["2026-07-28"])

	def test_probe_error_recognition(self):
		self.assertTrue(mcp_wire.is_modern_probe_error({"code": -32022}))
		self.assertTrue(mcp_wire.is_modern_probe_error({"code": -32020}))
		# Method-not-found on server/discover is a LEGACY answer (modern servers MUST implement it).
		self.assertFalse(mcp_wire.is_modern_probe_error({"code": -32601}))
		self.assertFalse(mcp_wire.is_modern_probe_error(None))

	def test_pick_legacy_version_prefers_newest_common(self):
		accepted = {"2025-11-25", "2025-06-18"}
		self.assertEqual(
			mcp_wire.pick_legacy_version(["2025-06-18", "2025-11-25", "2030-01-01"], accepted), "2025-11-25"
		)
		self.assertIsNone(mcp_wire.pick_legacy_version(["2030-01-01"], accepted))


_SCHEMA = {
	"type": "object",
	"properties": {
		"region": {"type": "string", "x-mcp-header": "Region"},
		"retries": {"type": "integer", "x-mcp-header": "Retries"},
		"dry": {"type": "boolean", "x-mcp-header": "Dry-Run"},
		"nested": {"type": "object", "properties": {"tenant": {"type": "string", "x-mcp-header": "Tenant"}}},
		"sql": {"type": "string"},
	},
}


class TestHeaderParams(unittest.TestCase):
	def test_values_at_exact_paths_are_mirrored_and_missing_ones_omitted(self):
		headers = mcp_wire.header_params(
			_SCHEMA,
			{"region": "us-west1", "retries": 3, "dry": False, "nested": {"tenant": "acme"}, "sql": "x"},
		)
		self.assertEqual(
			headers,
			{
				"Mcp-Param-Region": "us-west1",
				"Mcp-Param-Retries": "3",
				"Mcp-Param-Dry-Run": "false",
				"Mcp-Param-Tenant": "acme",
			},
		)
		self.assertEqual(mcp_wire.header_params(_SCHEMA, {"sql": "x"}), {})

	def test_unvetted_header_name_is_never_sent(self):
		# Defense in depth for a pre-sanitize cache: a name that is not a header token
		# never becomes a header, even if it reached the cache.
		schema = {"properties": {"a": {"type": "string", "x-mcp-header": "bad name"}}}
		self.assertEqual(mcp_wire.header_params(schema, {"a": "v"}), {})

	def test_valid_schema_has_no_annotation_error(self):
		self.assertIsNone(mcp_wire.header_annotation_error(_SCHEMA))
		self.assertIsNone(mcp_wire.header_annotation_error({"type": "object"}))
		self.assertIsNone(mcp_wire.header_annotation_error(None))

	def test_invalid_annotations_are_rejected(self):
		bad_token = {"properties": {"a": {"type": "string", "x-mcp-header": "Re gion"}}}
		self.assertIn("header token", mcp_wire.header_annotation_error(bad_token))
		bad_type = {"properties": {"a": {"type": "number", "x-mcp-header": "N"}}}
		self.assertIn("string, integer or boolean", mcp_wire.header_annotation_error(bad_type))
		dup = {
			"properties": {
				"a": {"type": "string", "x-mcp-header": "Region"},
				"b": {"type": "string", "x-mcp-header": "region"},
			}
		}
		self.assertIn("twice", mcp_wire.header_annotation_error(dup))
		under_items = {
			"properties": {"a": {"type": "array", "items": {"type": "string", "x-mcp-header": "X"}}}
		}
		self.assertIn("outside", mcp_wire.header_annotation_error(under_items))
		under_one_of = {"oneOf": [{"properties": {"a": {"type": "string", "x-mcp-header": "X"}}}]}
		self.assertIn("outside", mcp_wire.header_annotation_error(under_one_of))


class TestToolNames(unittest.TestCase):
	def test_spec_examples(self):
		for ok in ("getUser", "DATA_EXPORT_v2", "admin.tools.list", "a-b"):
			self.assertTrue(mcp_wire.TOOL_NAME_RE.match(ok), ok)
		for bad in ("", "has space", "comma,name", "x" * 129, "ünïcode", "slash/name"):
			self.assertFalse(mcp_wire.TOOL_NAME_RE.match(bad), bad)
