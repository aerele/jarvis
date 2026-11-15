"""File Box intake row detail: who may open it, and what the page gets to map."""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import filebox
from jarvis.tests.test_intake_filebox import MANAGER, OTHER, UPLOADER, _ensure_user

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"


class TestIntakeDetail(FrappeTestCase):
	def setUp(self):
		_ensure_user(UPLOADER)
		_ensure_user(OTHER)
		_ensure_user(MANAGER, ("System Manager",))
		self._orig = frappe.session.user
		self._cleanup: list[tuple[str, str]] = []
		p = mock.patch("jarvis.permissions.require_jarvis_access")
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self):
		frappe.set_user(self._orig)
		for doctype, name in self._cleanup:
			frappe.db.delete(doctype, {"name": name})

	def _rule(self, spec=None, **over) -> str:
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"det-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps(spec or {"fields": ["description", "priority", "date"]}),
			"enabled": 1,
		}
		doc.update(over)
		name = frappe.get_doc(doc).insert(ignore_permissions=True).name
		self._cleanup.append((TRIGGER, name))
		return name

	def _shared_rule(self) -> str:
		if not getattr(self, "_default_rule", None):
			self._default_rule = self._rule()
		return self._default_rule

	def _file(self, name="bill.png") -> str:
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": name, "content": b"x", "is_private": 1, "owner": UPLOADER}
		).insert(ignore_permissions=True)
		self._cleanup.append(("File", doc.name))
		return doc.name

	def _draft(self, owner, description="Pay the bill") -> str:
		doc = frappe.get_doc({"doctype": "ToDo", "description": description, "priority": "High"})
		doc.insert(ignore_permissions=True)
		frappe.db.set_value("ToDo", doc.name, "owner", owner, update_modified=False)
		self._cleanup.append(("ToDo", doc.name))
		return doc.name

	def _row(self, owner=UPLOADER, trigger=None, reading=None, **fields) -> str:
		trigger = trigger or self._shared_rule()
		draft = {k: fields.pop(k) for k in ("draft_doctype", "draft_name") if k in fields}
		doc = frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"trigger": trigger,
				"trigger_label": "det",
				"action_type": "Intake",
				"status": "Success",
				"event_user": owner,
				"summary": "Draft ready",
				"source_file": self._file(),
				"reading_json": json.dumps(reading) if reading is not None else "",
				**fields,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(ACTIVITY, doc.name, {"owner": owner, **draft}, update_modified=False)
		self._cleanup.append((ACTIVITY, doc.name))
		return doc.name

	def _detail(self, user, activity):
		frappe.set_user(user)
		return filebox.get_intake_detail(activity)

	def test_owner_sees_row_file_spec_and_reading(self):
		a = self._row(reading={"description": "Pay", "priority": "Urgent", "date": ""})
		out = self._detail(UPLOADER, a)
		self.assertEqual(out["row"]["name"], a)
		self.assertTrue(out["row"]["file_name"].startswith("bill"))
		self.assertTrue(out["row"]["source_label"].startswith("Folder: Attachments"))
		self.assertTrue(out["file"]["is_image"])
		self.assertFalse(out["file"]["is_pdf"])
		self.assertTrue(out["file"]["url"])
		self.assertEqual([f["fieldname"] for f in out["spec"]["fields"]], ["description", "priority", "date"])
		self.assertEqual(out["spec"]["fields"][0]["label"], "Description")
		self.assertEqual(out["reading"]["values"]["description"], "Pay")
		self.assertIn("can_retry", out)
		self.assertIn("can_review", out)

	def test_another_user_gets_permission_error(self):
		a = self._row(UPLOADER)
		with self.assertRaises(frappe.PermissionError):
			self._detail(OTHER, a)

	def test_unknown_or_bad_activity(self):
		with self.assertRaises(frappe.PermissionError):
			self._detail(UPLOADER, "nope-nope")
		frappe.set_user(UPLOADER)
		with self.assertRaises(frappe.ValidationError):
			filebox.get_intake_detail(["x"])

	def test_manager_sees_someone_elses_row(self):
		a = self._row(UPLOADER, reading={"description": "Pay"})
		out = self._detail(MANAGER, a)
		self.assertEqual(out["row"]["name"], a)
		self.assertEqual(out["reading"]["values"]["description"], "Pay")

	def test_draft_values_only_when_readable(self):
		mine = self._draft(UPLOADER)
		theirs = self._draft(OTHER)
		a = self._row(UPLOADER, draft_doctype="ToDo", draft_name=mine, reading={"description": "x"})
		b = self._row(UPLOADER, draft_doctype="ToDo", draft_name=theirs, reading={"description": "x"})
		out = self._detail(UPLOADER, a)
		self.assertEqual(out["draft"]["doctype"], "ToDo")
		self.assertEqual(out["draft"]["name"], mine)
		self.assertEqual(out["draft"]["values"]["description"], "Pay the bill")
		self.assertEqual(out["draft"]["values"]["priority"], "High")
		self.assertIsNone(self._detail(UPLOADER, b)["draft"])
		# a manager can read every ToDo
		self.assertEqual(self._detail(MANAGER, b)["draft"]["name"], theirs)

	def test_no_draft_yet(self):
		a = self._row(UPLOADER, reading={"description": "x"})
		self.assertIsNone(self._detail(UPLOADER, a)["draft"])

	def test_missing_lists_fields_not_read(self):
		a = self._row(reading={"description": "Pay", "priority": "", "date": None})
		out = self._detail(UPLOADER, a)
		self.assertEqual(out["missing"], ["priority", "date"])

	def test_changes_explain_the_difference(self):
		draft = self._draft(UPLOADER)
		frappe.db.set_value("ToDo", draft, {"priority": "", "date": None})
		a = self._row(
			UPLOADER,
			draft_doctype="ToDo",
			draft_name=draft,
			reading={"description": "Pay the bill", "priority": "Urgent", "date": "soon"},
		)
		out = self._detail(UPLOADER, a)
		self.assertNotIn("description", out["changes"])
		self.assertIn("priority", out["changes"])
		self.assertIn("date", out["changes"])
		self.assertIn("option", out["changes"]["priority"])
		self.assertIn("date", out["changes"]["date"])

	def test_table_rows_in_column_order(self):
		spec = {"fields": ["title"], "table": {"fieldname": "seen_by", "columns": ["user"]}}
		rule = self._rule(spec=spec, intake_target_doctype="Note")
		a = self._row(
			trigger=rule,
			reading={"title": "x", "seen_by": [{"user": "a@example.com"}, {"user": "b@example.com"}]},
		)
		out = self._detail(UPLOADER, a)
		self.assertEqual(out["spec"]["table"]["label"], "Seen By Table")
		self.assertEqual(out["spec"]["table"]["columns"], [{"fieldname": "user", "label": "User"}])
		self.assertEqual(out["reading"]["table_rows"], [["a@example.com"], ["b@example.com"]])

	def test_reviewer_filled_fields_are_marked(self):
		draft = self._draft(UPLOADER)
		req = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": "Review x",
				"status": "Pending",
				"source": "Intake",
				"document_type": "ToDo",
				"question": "q",
				"intake_missing": json.dumps({"missing": [{"field": "description", "row": None}]}),
			}
		)
		req.flags.jarvis_server_write = True
		req.insert(ignore_permissions=True)
		self._cleanup.append(("Jarvis Approval Request", req.name))
		a = self._row(
			UPLOADER,
			draft_doctype="ToDo",
			draft_name=draft,
			reading={"description": "", "priority": "High"},
			approval_request=req.name,
		)
		# still pending: nothing is marked
		self.assertEqual(self._detail(UPLOADER, a)["reviewer_filled"], [])
		frappe.db.set_value("Jarvis Approval Request", req.name, "status", "Approved")
		self.assertEqual(self._detail(UPLOADER, a)["reviewer_filled"], ["description"])

	def test_can_retry_flag(self):
		a = self._row(UPLOADER, status="Failed", summary="Reading the file failed.")
		self.assertTrue(self._detail(UPLOADER, a)["can_retry"])
		b = self._row(UPLOADER, status="Success")
		self.assertFalse(self._detail(UPLOADER, b)["can_retry"])
