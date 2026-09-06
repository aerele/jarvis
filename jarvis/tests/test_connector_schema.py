"""Unit tests for ``jarvis.connectors.schema.validate_arguments`` - the pre-call
argument check against a tool's cached MCP ``inputSchema``. Plain ``unittest``.
Exercises the light structural path explicitly (it is the fallback when
``jsonschema`` is not installed, which is the case on this bench today).
"""

from __future__ import annotations

import unittest

from jarvis.connectors import schema


class TestValidateArguments(unittest.TestCase):
	def test_empty_or_missing_schema_is_no_constraint(self):
		self.assertIsNone(schema.validate_arguments(None, {"a": 1}))
		self.assertIsNone(schema.validate_arguments({}, {"a": 1}))

	def test_non_object_schema_is_skipped(self):
		self.assertIsNone(schema.validate_arguments({"type": "string"}, "whatever"))

	def test_args_must_be_object(self):
		self.assertIsNotNone(schema.validate_arguments({"type": "object"}, ["not", "a", "dict"]))

	def test_missing_required(self):
		err = schema.validate_arguments({"type": "object", "required": ["repo"]}, {})
		self.assertIn("repo", err)

	def test_required_present_passes(self):
		self.assertIsNone(schema.validate_arguments({"type": "object", "required": ["repo"]}, {"repo": "x"}))

	def test_additional_properties_false_rejects_unknown(self):
		s = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
		err = schema.validate_arguments(s, {"a": 1, "b": 2})
		self.assertIn("b", err)

	def test_type_mismatch_string(self):
		s = {"type": "object", "properties": {"n": {"type": "string"}}}
		self.assertIsNotNone(schema.validate_arguments(s, {"n": 5}))

	def test_type_match_number_and_integer(self):
		s = {"type": "object", "properties": {"n": {"type": "number"}, "i": {"type": "integer"}}}
		self.assertIsNone(schema.validate_arguments(s, {"n": 1.5, "i": 3}))

	def test_boolean_is_not_an_integer(self):
		# Python bools are ints; the guard must not accept True for an integer field.
		s = {"type": "object", "properties": {"i": {"type": "integer"}}}
		self.assertIsNotNone(schema.validate_arguments(s, {"i": True}))

	def test_union_type_list(self):
		s = {"type": "object", "properties": {"x": {"type": ["string", "null"]}}}
		self.assertIsNone(schema.validate_arguments(s, {"x": None}))
		self.assertIsNone(schema.validate_arguments(s, {"x": "hi"}))
		self.assertIsNotNone(schema.validate_arguments(s, {"x": 5}))


if __name__ == "__main__":
	unittest.main()
