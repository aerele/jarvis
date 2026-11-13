"""Intake trigger form helpers: the folder list and the field picker."""

import frappe

from jarvis.chat.triggers_api import get_intake_field_options, get_trigger, list_intake_folders
from jarvis.intake import spec as intake_spec
from jarvis.tests.test_triggers_api import ADMIN_USER, PLAIN_USER, _TriggersApiTestCase

_FOLDER = "Home/Intake Form Test"


class TestIntakeFormApi(_TriggersApiTestCase):
	def _folder(self, name: str):
		if frappe.db.exists("File", name):
			return
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": name.split("/")[-1],
				"is_folder": 1,
				"folder": name.rsplit("/", 1)[0],
			}
		).insert(ignore_permissions=True)

	def test_manager_only(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.PermissionError, list_intake_folders)
		self.assertRaises(frappe.PermissionError, get_intake_field_options, "ToDo")

	def test_folders_listed_sorted_without_home_and_attachments(self):
		self._folder(_FOLDER)
		frappe.set_user(ADMIN_USER)
		folders = list_intake_folders()
		self.assertIn(_FOLDER, folders)
		self.assertNotIn("Home", folders)
		self.assertNotIn("Home/Attachments", folders)
		self.assertEqual(folders, sorted(folders))

	def test_field_options_flags_match_spec_helper(self):
		frappe.set_user(ADMIN_USER)
		out = get_intake_field_options("ToDo")
		by_name = {f["fieldname"]: f for f in out["fields"]}
		meta = frappe.get_meta("ToDo")
		self.assertTrue(by_name["description"]["mandatory"])
		for name, row in by_name.items():
			self.assertEqual(row["mandatory"], intake_spec._is_mandatory(meta.get_field(name)))
		self.assertEqual(set(by_name["status"]), {"fieldname", "label", "fieldtype", "options", "mandatory"})
		# Standard, layout and computed fields never appear.
		self.assertNotIn("owner", by_name)
		self.assertNotIn("creation", by_name)
		self.assertFalse({f["fieldtype"] for f in out["fields"]} & {"Section Break", "Column Break", "Table"})

	def test_child_tables_and_columns(self):
		frappe.set_user(ADMIN_USER)
		out = get_intake_field_options("Event")
		table = next(t for t in out["tables"] if t["fieldname"] == "event_participants")
		self.assertEqual(table["child_doctype"], "Event Participants")
		columns = {c["fieldname"]: c for c in table["columns"]}
		self.assertTrue(columns["reference_doctype"]["mandatory"])
		self.assertNotIn("event_participants", {f["fieldname"] for f in out["fields"]})

	def test_refuses_what_the_controller_refuses(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError) as ctx:
			get_intake_field_options("Error Log")
		self.assertIn("internal DocType", str(ctx.exception))
		self.assertRaises(frappe.ValidationError, get_intake_field_options, "Event Participants")
		self.assertRaises(frappe.ValidationError, get_intake_field_options, "No Such DocType")

	def test_trigger_detail_carries_intake_fields(self):
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Trigger",
				"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
				"action_type": "Intake",
				"intake_source": "Folder",
				"intake_folder": "Home/Attachments",
				"intake_target_doctype": "ToDo",
				"intake_spec": frappe.as_json({"fields": ["description"]}),
				"allow_incomplete_drafts": 1,
			}
		).insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		frappe.set_user(ADMIN_USER)
		data = get_trigger(doc.name)["data"]
		self.assertEqual(data["intake_target_doctype"], "ToDo")
		self.assertEqual(data["allow_incomplete_drafts"], 1)
		self.assertIn("description", data["intake_spec"])
