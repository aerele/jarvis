"""Intake trigger kind: config validation (controller) and the spec helpers."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.intake import spec as intake_spec
from jarvis.intake.capture import INTAKE_MAP_CACHE_KEY, clear_intake_cache
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"


class TestIntakeSpec(FrappeTestCase):
	def setUp(self):
		self._made: list[str] = []

	def tearDown(self):
		for name in self._made:
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		engine.clear_cache()
		clear_intake_cache()

	def _trigger(self, **over):
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps({"fields": ["description"]}),
		}
		doc.update(over)
		return frappe.get_doc(doc)

	def _insert(self, **over):
		doc = self._trigger(**over)
		doc.insert(ignore_permissions=True)
		self._made.append(doc.name)
		return doc

	def test_folder_must_exist(self):
		self.assertRaises(frappe.ValidationError, self._insert, intake_folder="Home/No Such Folder")
		# A plain file (not a folder) is not a valid source either.
		self._insert()  # the happy path passes with a real folder

	def test_unknown_field_rejected(self):
		bad = json.dumps({"fields": ["description", "no_such_field"]})
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=bad)
		bad_table = json.dumps(
			{"fields": ["subject"], "table": {"fieldname": "event_participants", "columns": ["nope"]}}
		)
		self.assertRaises(
			frappe.ValidationError,
			self._insert,
			intake_target_doctype="Event",
			intake_spec=bad_table,
		)

	def test_strict_requires_mandatory_coverage(self):
		# ToDo.description is mandatory; leaving it out must fail in strict mode.
		spec_json = json.dumps({"fields": ["priority"]})
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=spec_json)
		# Covered by a default instead of a field: passes.
		ok = json.dumps({"fields": ["priority"], "defaults": {"description": "From intake"}})
		self._insert(intake_spec=ok)
		# Child mandatory columns (Event Participants) must be in table.columns.
		header = ["subject", "starts_on", "event_type"]
		table_missing = json.dumps(
			{"fields": header, "table": {"fieldname": "event_participants", "columns": []}}
		)
		self.assertRaises(
			frappe.ValidationError, self._insert, intake_target_doctype="Event", intake_spec=table_missing
		)
		cols = ["reference_doctype", "reference_docname"]
		table_ok = json.dumps(
			{"fields": header, "table": {"fieldname": "event_participants", "columns": cols}}
		)
		self._insert(intake_target_doctype="Event", intake_spec=table_ok)

	def test_incomplete_allowed_skips_coverage(self):
		spec_json = json.dumps({"fields": ["priority"]})
		self._insert(intake_spec=spec_json, allow_incomplete_drafts=1)

	def test_match_must_be_listed_link_fields(self):
		not_listed = json.dumps({"fields": ["description"], "match": ["allocated_to"]})
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=not_listed)
		not_link = json.dumps({"fields": ["description"], "match": ["description"]})
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=not_link)
		ok = json.dumps({"fields": ["description", "allocated_to"], "match": ["allocated_to"]})
		self._insert(intake_spec=ok)

	def test_defaults_keys_must_exist(self):
		bad = json.dumps({"fields": ["description"], "defaults": {"nope": 1}})
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=bad)

	def test_record_source_checks_doctype_and_writeback_fields(self):
		base = {
			"intake_source": "Record",
			"intake_folder": None,
			"intake_record_doctype": "ToDo",
		}
		self.assertRaises(frappe.ValidationError, self._insert, **base, intake_status_field="no_such_field")
		self.assertRaises(frappe.ValidationError, self._insert, **{**base, "intake_record_doctype": "User"})
		self._insert(**base, intake_status_field="status", intake_link_field="reference_name")

	def test_target_doctype_gated(self):
		# User is denylisted, Website Settings is Single, Note Seen By is a child table.
		for dt in ("User", "Website Settings", "Note Seen By", "No Such DocType"):
			spec_json = json.dumps({"fields": ["description"]})
			with self.subTest(target=dt):
				self.assertRaises(
					frappe.ValidationError, self._insert, intake_target_doctype=dt, intake_spec=spec_json
				)

	def test_record_doctype_gated(self):
		for dt in ("User", "Website Settings", "Note Seen By"):
			with self.subTest(record=dt):
				self.assertRaises(
					frappe.ValidationError,
					self._insert,
					intake_source="Record",
					intake_folder=None,
					intake_record_doctype=dt,
				)

	def test_defaults_are_validated(self):
		def spec_with(**defaults):
			return json.dumps({"fields": ["description"], "defaults": defaults})

		# Blank defaults are refused as coverage of a mandatory field.
		for blank in (None, "", "   "):
			bad = json.dumps({"fields": ["priority"], "defaults": {"description": blank}})
			with self.subTest(blank=blank):
				self.assertRaises(frappe.ValidationError, self._insert, intake_spec=bad)
		# Select must be one of its options.
		self.assertRaises(frappe.ValidationError, self._insert, intake_spec=spec_with(priority="Urgent"))
		self._insert(intake_spec=spec_with(priority="High"), enabled=0)
		# Link must exist.
		self.assertRaises(
			frappe.ValidationError, self._insert, intake_spec=spec_with(allocated_to="nobody@nowhere.invalid")
		)
		self._insert(intake_spec=spec_with(allocated_to="Administrator"), enabled=0)
		# Numbers must parse.
		item = {"intake_target_doctype": "Item"}
		fields = ["item_name", "item_group"]
		bad_num = json.dumps({"fields": fields, "defaults": {"shelf_life_in_days": "soon"}})
		self.assertRaises(
			frappe.ValidationError, self._insert, **item, intake_spec=bad_num, allow_incomplete_drafts=1
		)
		ok_num = json.dumps({"fields": fields, "defaults": {"shelf_life_in_days": "30"}})
		self._insert(**item, intake_spec=ok_num, allow_incomplete_drafts=1)

	def test_non_finite_numeric_defaults_refused(self):
		for fieldname in ("shelf_life_in_days", "weight_per_unit", "valuation_rate"):
			for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
				spec_json = json.dumps(
					{"fields": ["item_name"], "defaults": {fieldname: bad}}, allow_nan=True
				)
				with self.subTest(field=fieldname, value=bad):
					self.assertRaises(
						frappe.ValidationError,
						self._insert,
						intake_target_doctype="Item",
						intake_spec=spec_json,
						allow_incomplete_drafts=1,
					)

	def test_duplicate_intake_rule_refused(self):
		self._insert()
		self.assertRaises(frappe.ValidationError, self._insert)
		# Different target, same folder: allowed.
		self._insert(
			intake_target_doctype="Note",
			intake_spec=json.dumps({"fields": ["title"]}),
		)
		# Disabled duplicate: allowed; but re-enabling it is refused.
		dup = self._insert(enabled=0)
		dup.enabled = 1
		self.assertRaises(frappe.ValidationError, dup.save, ignore_permissions=True)
		# Same record doctype + target is a duplicate too.
		rec = {"intake_source": "Record", "intake_folder": None, "intake_record_doctype": "ToDo"}
		self._insert(**rec)
		self.assertRaises(frappe.ValidationError, self._insert, **rec)

	def test_load_reads_row_and_accepts_dict_or_json(self):
		row = self._insert(allow_incomplete_drafts=1, intake_instructions="Read the totals.")
		s = intake_spec.load(row)
		self.assertEqual(s.target, "ToDo")
		self.assertEqual(s.fields, ["description"])
		self.assertIsNone(s.table)
		self.assertEqual((s.match, s.defaults), ([], {}))
		self.assertEqual(s.instructions, "Read the totals.")
		self.assertTrue(s.allow_incomplete)
		as_dict = intake_spec.load(
			{
				"intake_target_doctype": "ToDo",
				"intake_spec": {"fields": ["priority"]},
				"intake_instructions": None,
				"allow_incomplete_drafts": 0,
			}
		)
		self.assertEqual(as_dict.fields, ["priority"])
		self.assertFalse(as_dict.allow_incomplete)

	def test_mandatory_fields(self):
		s = intake_spec.IntakeSpec(
			target="Event",
			fields=["subject"],
			table={"fieldname": "event_participants", "columns": []},
			match=[],
			defaults={},
			instructions="",
			allow_incomplete=False,
		)
		header, columns = intake_spec.mandatory_fields(s)
		self.assertEqual(sorted(header), ["event_type", "starts_on", "subject"])
		self.assertEqual(sorted(columns), ["reference_docname", "reference_doctype"])

	def test_build_schema_types(self):
		s = intake_spec.IntakeSpec(
			target="Item",
			fields=[
				"item_name",
				"shelf_life_in_days",
				"valuation_rate",
				"weight_per_unit",
				"end_of_life",
				"item_group",
			],
			table=None,
			match=[],
			defaults={},
			instructions="",
			allow_incomplete=True,
		)
		schema = intake_spec.build_schema(s)
		# Task 6 relies on this reply shape for "no usable image".
		self.assertIn(
			{
				"type": "object",
				"properties": {"error": {"const": "NO_IMAGE"}},
				"required": ["error"],
			},
			schema["anyOf"],
		)
		data = next(b for b in schema["anyOf"] if "item_name" in b["properties"])
		props = data["properties"]
		self.assertEqual(props["item_name"]["type"], ["string", "null"])
		self.assertEqual(props["item_group"]["type"], ["string", "null"])
		self.assertEqual(props["shelf_life_in_days"]["type"], ["integer", "null"])
		self.assertEqual(props["valuation_rate"]["type"], ["number", "null"])
		self.assertEqual(props["weight_per_unit"]["type"], ["number", "null"])
		self.assertEqual(props["end_of_life"]["type"], ["string", "null"])
		self.assertEqual(props["end_of_life"]["format"], "date")
		# Unreadable values come back null, so every data property allows null.
		for p in props.values():
			self.assertIn("null", p["type"])

	def test_build_schema_table_rows(self):
		s = intake_spec.IntakeSpec(
			target="Event",
			fields=["subject"],
			table={"fieldname": "event_participants", "columns": ["reference_doctype"]},
			match=[],
			defaults={},
			instructions="",
			allow_incomplete=False,
		)
		data = next(b for b in intake_spec.build_schema(s)["anyOf"] if "subject" in b["properties"])
		rows = data["properties"]["event_participants"]
		self.assertEqual(rows["type"], ["array", "null"])
		self.assertEqual(rows["items"]["properties"]["reference_doctype"]["type"], ["string", "null"])

	def test_intake_rows_not_in_event_map(self):
		row = self._insert()
		# Even with a stray target/event set, an Intake row never enters the map.
		frappe.db.set_value(TRIGGER, row.name, {"target_doctype": "ToDo", "doc_event": "on_update"})
		mapped = engine._build_triggers_map()
		names = [r["name"] for events in mapped.values() for rows in events.values() for r in rows]
		self.assertNotIn(row.name, names)

	def test_saving_intake_trigger_clears_intake_cache(self):
		frappe.cache().set_value(INTAKE_MAP_CACHE_KEY, {"stale": True})
		self._insert()
		self.assertIsNone(frappe.cache().get_value(INTAKE_MAP_CACHE_KEY))

	def test_script_and_llm_still_require_target_and_event(self):
		for action, extra in (
			("LLM", {"llm_instruction": "Check it."}),
			("Script", {"script_body": "x = 1"}),
		):
			doc = frappe.get_doc(
				{"doctype": TRIGGER, "trigger_name": f"x-{action}", "action_type": action, **extra}
			)
			self.assertRaises(frappe.ValidationError, doc.insert, ignore_permissions=True)
