"""Intake capture: File listener queues, skips and dedupes uploads."""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.intake import capture
from jarvis.intake.capture import clear_intake_cache
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"
FOLDER = "Home/Attachments"


def _bytes_for(name: str, seed: str) -> bytes:
	"""Distinct bytes per seed; PDFs and images must parse, or Frappe refuses the upload."""
	if name.lower().endswith(".pdf"):
		import io

		from pypdf import PdfWriter

		writer = PdfWriter()
		writer.add_blank_page(width=100, height=100)
		writer.add_metadata({"/Title": seed})
		buf = io.BytesIO()
		writer.write(buf)
		return buf.getvalue()
	if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
		import hashlib
		import io

		from PIL import Image

		colour = tuple(hashlib.sha256(seed.encode()).digest()[:3])
		buf = io.BytesIO()
		Image.new("RGB", (4, 4), colour).save(buf, format="PNG")
		return buf.getvalue()
	return seed.encode()


class TestIntakeCapture(FrappeTestCase):
	def setUp(self):
		self._triggers: list[str] = []
		self._files: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self._files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, ignore_permissions=True, force=True)
		for name in self._triggers:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		engine.clear_cache()
		clear_intake_cache()

	def _rule(self, **over):
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": FOLDER,
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps({"fields": ["description"]}),
			"enabled": 1,
		}
		doc.update(over)
		rule = frappe.get_doc(doc).insert(ignore_permissions=True)
		self._triggers.append(rule.name)
		clear_intake_cache()
		return rule

	def _upload(self, name="sheet.jpg", content=None, **over):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": name,
				"content": _bytes_for(name, content or frappe.generate_hash(length=20)),
				"folder": FOLDER,
				"is_private": 1,
				**over,
			}
		)
		with mock.patch("frappe.enqueue") as enq:
			doc.insert(ignore_permissions=True)
		self._files.append(doc.name)
		return doc, enq

	def _rows(self, rule):
		return frappe.get_all(ACTIVITY, filters={"trigger": rule.name}, fields=["*"], order_by="creation asc")

	def test_no_intake_rules_no_query(self):
		clear_intake_cache()
		capture.intake_map()  # warm: the rest must be pure cache reads
		doc = frappe.get_doc({"doctype": "File", "file_name": "a.jpg", "content": b"x1", "folder": FOLDER})
		with (
			mock.patch("frappe.db.sql") as sql,
			mock.patch("frappe.db.get_value") as gv,
			mock.patch("frappe.get_all") as ga,
		):
			capture.on_file_insert(doc)
		sql.assert_not_called()
		gv.assert_not_called()
		ga.assert_not_called()

	def test_folder_match_queues(self):
		rule = self._rule()
		doc, enq = self._upload("sheet-0913.JPG")
		rows = self._rows(rule)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual((row.status, row.action_type, row.source_file), ("Queued", "Intake", doc.name))
		self.assertEqual(row.file_hash, doc.content_hash)
		self.assertEqual(row.event_user, "Administrator")
		self.assertEqual(row.target_doctype, "ToDo")
		kw = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.intake.worker.run_job")
		self.assertEqual(kw["activity"], row.name)
		self.assertEqual(kw["job_id"], f"intake:{row.name}")
		self.assertTrue(kw["enqueue_after_commit"] and kw["deduplicate"])
		self.assertEqual(kw["queue"], "default")

	def test_folder_row_itself_ignored(self):
		rule = self._rule()
		doc = frappe.get_doc({"doctype": "File", "file_name": "sub", "is_folder": 1, "folder": FOLDER})
		with mock.patch("frappe.enqueue") as enq:
			doc.insert(ignore_permissions=True)
		self._files.append(doc.name)
		self.assertEqual(self._rows(rule), [])
		enq.assert_not_called()

	def test_unsupported_type_skipped(self):
		rule = self._rule()
		_doc, enq = self._upload("notes.txt")
		rows = self._rows(rule)
		self.assertEqual(rows[0].status, "Skipped")
		self.assertEqual(rows[0].summary, "This file type can't be read for intake")
		enq.assert_not_called()

	def test_duplicate_hash_skipped(self):
		rule = self._rule()
		self._upload("a.png", content="same-bytes-1")
		_doc, enq = self._upload("b.png", content="same-bytes-1")
		first, second = self._rows(rule)
		self.assertEqual(second.status, "Skipped")
		self.assertEqual(second.duplicate_of, first.name)
		self.assertEqual(second.summary, "Same file as an earlier upload")
		enq.assert_not_called()

	def test_reupload_after_the_draft_was_deleted_is_captured_again(self):
		rule = self._rule()
		self._upload("a.png", content="same-bytes-2")
		(first,) = self._rows(rule)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "d"}).insert(ignore_permissions=True)
		frappe.db.set_value(
			ACTIVITY,
			first.name,
			{"status": "Success", "draft_doctype": "ToDo", "draft_name": todo.name},
		)
		# While the draft exists the second upload is still a duplicate.
		self._upload("b.png", content="same-bytes-2")
		self.assertEqual(self._rows(rule)[1].status, "Skipped")
		frappe.delete_doc("ToDo", todo.name, ignore_permissions=True)
		_doc, enq = self._upload("c.png", content="same-bytes-2")
		third = self._rows(rule)[2]
		self.assertEqual((third.status, third.duplicate_of), ("Queued", None))
		enq.assert_called_once()

	def test_record_attach_waits_for_saved_parent(self):
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype="Note",
			intake_target_doctype="ToDo",
		)
		note = frappe.get_doc({"doctype": "Note", "title": "n-" + frappe.generate_hash(length=6)}).insert()
		self.addCleanup(frappe.delete_doc, "Note", note.name, force=True)
		# Attached to an unsaved form: ignored.
		self._upload("a.pdf", attached_to_doctype="Note", attached_to_name="new-note-abc")
		self.assertEqual(self._rows(rule), [])
		# Uploaded loose, then attached to the saved record: captured once on update.
		doc, _ = self._upload("b.pdf")
		self.assertEqual(self._rows(rule), [])
		doc.reload()
		doc.attached_to_doctype, doc.attached_to_name = "Note", note.name
		with mock.patch("frappe.enqueue") as enq:
			doc.save(ignore_permissions=True)
			doc.save(ignore_permissions=True)
		rows = self._rows(rule)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].target_docname, rows[0].doc_event), (note.name, "on_update"))
		self.assertEqual(enq.call_count, 1)

	def test_insert_already_attached_queues_once(self):
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype="Note",
			intake_target_doctype="ToDo",
		)
		note = frappe.get_doc({"doctype": "Note", "title": "n-" + frappe.generate_hash(length=6)}).insert()
		self.addCleanup(frappe.delete_doc, "Note", note.name, force=True)
		self._upload("a.pdf", attached_to_doctype="Note", attached_to_name=note.name)
		rows = self._rows(rule)
		self.assertEqual([r.status for r in rows], ["Queued"])

	def _move(self, doc, folder):
		doc.reload()
		doc.folder = folder
		with mock.patch("frappe.enqueue") as enq:
			doc.save(ignore_permissions=True)
		return enq

	def test_move_into_watched_folder_captured(self):
		rule = self._rule()
		doc, _ = self._upload("m.png", folder="Home")
		self.assertEqual(self._rows(rule), [])
		enq = self._move(doc, FOLDER)
		rows = self._rows(rule)
		self.assertEqual(
			[(r.status, r.doc_event, r.source_file) for r in rows], [("Queued", "on_update", doc.name)]
		)
		self.assertEqual(enq.call_count, 1)
		# Saving again with no folder change adds nothing.
		self._move(doc, FOLDER)
		self.assertEqual(len(self._rows(rule)), 1)

	def test_move_between_unwatched_folders_ignored(self):
		rule = self._rule()
		doc, _ = self._upload("n.png", folder="Home")
		other = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "Other-" + frappe.generate_hash(length=5),
				"is_folder": 1,
				"folder": "Home",
			}
		)
		other.insert(ignore_permissions=True)
		self._files.append(other.name)
		self._move(doc, other.name)
		self.assertEqual(self._rows(rule), [])

	def test_listener_swallows_errors(self):
		self._rule()
		with mock.patch.object(capture, "_queue_one", side_effect=RuntimeError("boom")):
			doc, _ = self._upload("c.jpg")
		self.assertTrue(frappe.db.exists("File", doc.name))
