"""``jarvis.connectors.schema_view``: an over-large tool schema is shortened in
steps that keep field types, instead of collapsing to property names.

The fixture is shaped like a Webflow tool schema: many arguments, nested
``anyOf``, ``$defs`` with long descriptions, big enums, and examples. Pure, no
site needed."""

from __future__ import annotations

import json
import unittest

from jarvis.connectors import schema_view


def _size(obj) -> int:
	return len(json.dumps(obj))


def _fixture() -> dict:
	"""About 40 KB serialised."""
	statuses = {
		"type": "string",
		"description": "Publish state. " * 20,
		"enum": [f"state_{i}" for i in range(60)],
	}
	field = {
		"type": "object",
		"description": "One CMS field. " * 20,
		"title": "Field",
		"properties": {
			"slug": {"type": "string", "description": "Url part. " * 20, "examples": ["a", "b"]},
			"count": {"type": "integer", "default": 3, "description": "How many. " * 20},
			"tags": {"type": "array", "items": {"type": "string"}, "description": "Tags. " * 20},
			"nested": {
				"type": "object",
				"properties": {
					"deep": {
						"type": "object",
						"properties": {
							"deeper": {"type": "object", "properties": {"leaf": {"type": "boolean"}}},
							"rows": {"type": "array", "items": {"type": "number"}},
						},
					}
				},
			},
		},
		"required": ["slug"],
	}
	properties = {
		f"argument_{i:02d}": {
			"description": "Argument text. " * 25,
			"anyOf": [
				{"type": "string", "description": "As text. " * 10},
				{"$ref": "#/$defs/field"},
				{"type": "array", "items": {"$ref": "#/$defs/field"}},
			],
		}
		for i in range(45)
	}
	properties["status"] = statuses
	properties["description"] = {"type": "string", "description": "A real argument named description."}
	properties["default"] = {"type": "boolean"}
	return {
		"type": "object",
		"title": "Big tool",
		"description": "Top level text. " * 20,
		"properties": properties,
		"required": ["status", "argument_00"],
		"$defs": {f"field{i}": field for i in range(8)} | {"field": field},
	}


class TestSchemaViewSteps(unittest.TestCase):
	def setUp(self):
		self.schema = _fixture()
		self.assertGreater(_size(self.schema), 38_000)

	def test_a_schema_within_budget_is_returned_untouched(self):
		small = {"type": "object", "properties": {"a": {"type": "string", "examples": ["x"]}}}
		self.assertIs(schema_view.compact(small, 24_000), small)

	def test_every_step_is_smaller_and_wins_when_it_is_the_first_to_fit(self):
		last = _size(self.schema)
		for step in schema_view._STEPS:
			out = schema_view._marked(schema_view._shorten(self.schema, step))
			self.assertLess(_size(out), last)
			last = _size(out)
			self.assertEqual(schema_view.compact(self.schema, _size(out)), out)

	def test_step_one_drops_noise_and_clips_descriptions_but_keeps_everything_else(self):
		out = schema_view._shorten(self.schema, schema_view._STEPS[0])
		self.assertNotIn("title", out)
		arg = out["properties"]["argument_00"]
		self.assertEqual(len(arg["description"]), 160)
		self.assertEqual(len(out["properties"]["status"]["enum"]), 60)
		slug = out["$defs"]["field"]["properties"]["slug"]
		self.assertNotIn("examples", slug)
		self.assertNotIn("default", out["$defs"]["field"]["properties"]["count"])

	def test_step_two_cuts_enums_and_says_how_many_are_missing(self):
		out = schema_view._shorten(self.schema, schema_view._STEPS[1])
		status = out["properties"]["status"]
		self.assertEqual(len(status["enum"]), 20)
		self.assertEqual(status["enum_more"], 40)
		self.assertEqual(len(out["properties"]["argument_00"]["description"]), 60)

	def test_step_three_keeps_argument_descriptions_and_drops_deeper_ones(self):
		out = schema_view._shorten(self.schema, schema_view._STEPS[2])
		arg = out["properties"]["argument_00"]
		self.assertIn("description", arg)
		self.assertNotIn("description", arg["anyOf"][0])
		self.assertNotIn("description", out["$defs"]["field"]["properties"]["slug"])

	def test_step_four_collapses_deep_objects_and_arrays_but_keeps_item_types(self):
		out = schema_view._shorten(self.schema, schema_view._STEPS[3])
		nested = out["$defs"]["field"]["properties"]["nested"]
		deep = nested["properties"]["deep"]
		self.assertEqual(deep["properties"]["deeper"], {"type": "object", "properties": ["leaf"]})
		self.assertEqual(deep["properties"]["rows"], {"type": "array", "items": {"type": "number"}})
		self.assertEqual(out["$defs"]["field"]["properties"]["count"]["type"], "integer")

	def test_a_budget_the_steps_cannot_meet_falls_to_names_with_types(self):
		out = schema_view.compact(self.schema, 3_000)
		self.assertTrue(out["truncated"])
		self.assertEqual(out["required"], ["status", "argument_00"])
		self.assertEqual(out["properties"]["status"], "string")
		self.assertEqual(out["properties"]["argument_00"], "string|array")
		self.assertEqual(out["properties"]["default"], "boolean")
		self.assertLessEqual(_size(out), 3_000)


class TestSchemaViewContract(unittest.TestCase):
	def test_a_shortened_schema_is_marked_and_keeps_types_and_defs(self):
		schema = _fixture()
		out = schema_view.compact(schema, 24_000)
		self.assertLessEqual(_size(out), 24_000)
		self.assertIs(out["truncated"], True)
		self.assertEqual(out["note"], "Schema shortened to fit; the full schema still checks your arguments.")
		self.assertEqual(out["type"], "object")
		self.assertEqual(out["properties"]["status"]["type"], "string")
		self.assertEqual(out["properties"]["argument_00"]["anyOf"][1], {"$ref": "#/$defs/field"})
		self.assertIn("field", out["$defs"])
		self.assertEqual(out["required"], schema["required"])

	def test_the_input_schema_is_never_mutated(self):
		schema = _fixture()
		before = json.dumps(schema)
		schema_view.compact(schema, 5_000)
		self.assertEqual(json.dumps(schema), before)

	def test_arguments_named_like_keywords_are_not_stripped(self):
		out = schema_view.compact(_fixture(), 24_000)
		self.assertEqual(out["properties"]["default"], {"type": "boolean"})
		self.assertIn("description", out["properties"])

	def test_hostile_shapes_never_raise(self):
		huge = {"type": "object", "properties": {f"p{i}": 7 for i in range(500)}, "required": [1, None, "p1"]}
		for schema in (huge, ["x" * 30_000], "y" * 30_000, {"properties": ["a"] * 10, "enum": "z" * 30_000}):
			out = schema_view.compact(schema, 1_000)
			self.assertIsInstance(out, dict)
			self.assertTrue(out["truncated"])
			self.assertLessEqual(_size(out), 24_000)

	def test_last_resort_caps_the_property_list_and_says_so(self):
		props = {f"p{i}_" + "n" * 300: {"type": "string"} for i in range(500)}
		out = schema_view.last_resort({"properties": props, "required": list(props)})
		self.assertLessEqual(_size(out), schema_view.SCHEMA_MAX_CHARS)
		self.assertLess(len(out["properties"]), 100)
		self.assertEqual(len(out["properties"]) + out["properties_more"], 500)
		self.assertTrue(all(len(name) <= 128 for name in out["properties"]))

	def test_last_resort_keeps_100_ordinary_names(self):
		props = {f"p{i}": {"type": "string"} for i in range(500)}
		out = schema_view.last_resort({"properties": props, "required": ["p1"]})
		self.assertEqual(len(out["properties"]), 100)
		self.assertEqual(out["properties_more"], 400)

	def test_an_unserialisable_schema_raises_for_the_caller_to_handle(self):
		with self.assertRaises(TypeError):
			schema_view.compact({"x": object()}, 10)


if __name__ == "__main__":
	unittest.main()
