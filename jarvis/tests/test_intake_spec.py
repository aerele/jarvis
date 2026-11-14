"""Intake trigger kind: config validation (controller) and the spec helpers."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.intake import guards
from jarvis.intake import spec as intake_spec
from jarvis.intake.capture import INTAKE_MAP_CACHE_KEY, clear_intake_cache
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"
# A Record rule on ToDo that drafts Notes (the draft may not be the watched DocType).
_NOTE_DRAFTS = {
	"intake_target_doctype": "Note",
	"intake_spec": json.dumps({"fields": ["title"]}),
}


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
		# reference_docname is a Dynamic Link: a page cannot carry it, so it is no longer
		# a fillable column and the table can never be covered in strict mode.
		self.assertRaises(
			frappe.ValidationError, self._insert, intake_target_doctype="Event", intake_spec=table_ok
		)

	def test_fields_a_page_cannot_carry_are_refused(self):
		for fields in (["description", "no_such_field"],):
			with self.subTest(fields=fields):
				self.assertRaises(
					frappe.ValidationError,
					self._insert,
					intake_spec=json.dumps({"fields": fields}),
					allow_incomplete_drafts=1,
				)
		for df in (
			frappe._dict(fieldtype="Attach"),
			frappe._dict(fieldtype="Attach Image"),
			frappe._dict(fieldtype="Password"),
			frappe._dict(fieldtype="Code"),
			frappe._dict(fieldtype="JSON"),
			frappe._dict(fieldtype="Dynamic Link"),
			frappe._dict(fieldtype="Data", read_only=1),
			frappe._dict(fieldtype="Data", fetch_from="customer.name"),
			frappe._dict(fieldtype="Data", is_virtual=1),
		):
			with self.subTest(df=df):
				self.assertFalse(intake_spec._fillable(df))
		self.assertTrue(intake_spec._fillable(frappe._dict(fieldtype="Data")))

	def test_too_many_picked_fields_refused(self):
		meta = frappe.get_meta("Event")
		many = [df.fieldname for df in meta.fields if intake_spec._fillable(df)]
		spec = intake_spec.IntakeSpec(
			target="Event", fields=many * 5, table=None
		)  # over the cap whatever the real count
		with self.assertRaises(frappe.ValidationError):
			intake_spec.validate(spec)

	def test_mandatory_depends_on_is_evaluated_against_the_values(self):
		df = frappe._dict(
			fieldname="x", fieldtype="Data", read_only=0, mandatory_depends_on="eval:doc.kind=='A'"
		)
		self.assertTrue(intake_spec.depends_mandatory(df, {"kind": "A"}))
		self.assertFalse(intake_spec.depends_mandatory(df, {"kind": "B"}))
		broken = frappe._dict(fieldname="x", fieldtype="Data", mandatory_depends_on="eval:doc.(")
		self.assertFalse(intake_spec.depends_mandatory(broken, {}))

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
			**_NOTE_DRAFTS,
		}
		self.assertRaises(frappe.ValidationError, self._insert, **base, intake_status_field="no_such_field")
		self.assertRaises(frappe.ValidationError, self._insert, **{**base, "intake_record_doctype": "User"})
		self._insert(**base, intake_status_field="sender", intake_link_field="reference_name")

	def test_writeback_fields_must_be_able_to_hold_the_value(self):
		base = {
			"intake_source": "Record",
			"intake_folder": None,
			"intake_record_doctype": "ToDo",
			**_NOTE_DRAFTS,
		}
		# A Select would refuse "Needs Review"; a Link to another doctype would refuse a draft name.
		with self.assertRaises(frappe.ValidationError):
			self._insert(**base, intake_status_field="status")
		with self.assertRaises(frappe.ValidationError):
			self._insert(**base, intake_link_field="reference_type")
		with self.assertRaises(frappe.ValidationError):
			self._insert(**base, intake_link_field="date")
		self._insert(**base, intake_status_field="sender", intake_link_field="reference_name")

	def test_sensitive_doctypes_are_refused_as_target_and_as_record_source(self):
		for dt in (
			"Role",
			"User Permission",
			"DocPerm",
			"Custom DocPerm",
			"Custom Field",
			"Property Setter",
			"Webhook",
			"Workflow",
			"Print Format",
			"Server Script",
			"Client Script",
			"DocType",
			"Module Def",
			"System Settings",
			"Jarvis Settings",
		):
			with self.subTest(dt=dt):
				self.assertTrue(guards.refused_doctype(dt))
				with self.assertRaises(frappe.ValidationError):
					self._insert(intake_target_doctype=dt)
				with self.assertRaises(frappe.ValidationError):
					self._insert(
						intake_source="Record", intake_folder=None, intake_record_doctype=dt, **_NOTE_DRAFTS
					)
		for dt in ("ToDo", "Note", "Event"):
			self.assertFalse(guards.refused_doctype(dt))

	def test_a_record_rule_cannot_draft_the_doctype_it_watches(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(
				intake_source="Record",
				intake_folder=None,
				intake_record_doctype="ToDo",
				intake_target_doctype="ToDo",
			)

	def test_two_record_rules_cannot_feed_each_other(self):
		self._insert(intake_source="Record", intake_folder=None, intake_record_doctype="ToDo", **_NOTE_DRAFTS)
		back = {
			"intake_source": "Record",
			"intake_folder": None,
			"intake_record_doctype": "Note",
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps({"fields": ["description"]}),
		}
		with self.assertRaises(frappe.ValidationError):
			self._insert(**back)
		# Disabled it may be saved, but switching it on is refused.
		off = self._insert(**back, enabled=0)
		off.enabled = 1
		with self.assertRaises(frappe.ValidationError):
			off.save(ignore_permissions=True)

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
			for bad in ("nan", "inf", "-inf"):
				spec_json = json.dumps({"fields": ["item_name"], "defaults": {fieldname: bad}})
				with self.subTest(field=fieldname, value=bad):
					with self.assertRaisesRegex(frappe.ValidationError, "must be a number"):
						self._insert(
							intake_target_doctype="Item",
							intake_spec=spec_json,
							allow_incomplete_drafts=1,
						)

	def test_non_scalar_defaults_refused(self):
		filters = {"enabled": [">", 0]}
		for fieldname in ("allocated_to", "priority", "date"):
			for bad in (filters, ["a"], None):
				spec_json = json.dumps({"fields": ["description"], "defaults": {fieldname: bad}})
				with self.subTest(field=fieldname, value=bad):
					with self.assertRaisesRegex(frappe.ValidationError, "single text or number"):
						self._insert(intake_spec=spec_json)

	def test_link_default_needs_read_access(self):
		spec_json = json.dumps({"fields": ["description"], "defaults": {"allocated_to": "Administrator"}})
		# Guest cannot read User records: same message as a missing record.
		frappe.set_user("Guest")
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "does not exist"):
				self._insert(intake_spec=spec_json)
		finally:
			frappe.set_user("Administrator")
		self._insert(intake_spec=spec_json, enabled=0)

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
		rec = {
			"intake_source": "Record",
			"intake_folder": None,
			"intake_record_doctype": "ToDo",
			**_NOTE_DRAFTS,
		}
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

	def test_build_schema_select_lists_its_options(self):
		s = intake_spec.IntakeSpec(
			target="ToDo",
			fields=["status", "description"],
			table=None,
			match=[],
			defaults={},
			instructions="",
			allow_incomplete=True,
		)
		props = next(b for b in intake_spec.build_schema(s)["anyOf"] if "status" in b["properties"])[
			"properties"
		]
		options = [o for o in frappe.get_meta("ToDo").get_field("status").options.split("\n") if o.strip()]
		self.assertEqual(props["status"]["enum"], [*options, None])
		self.assertNotIn("enum", props["description"])

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
