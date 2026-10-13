"""The File Box source file reaches the draft it produced.

A dropped invoice used to stay attached only to its Jarvis Conversation: a record
can take attachments only once it exists, and nothing attached the file after the
draft was created. ``filebox.attach_source_to_draft`` runs right after the
auto-applied create, and the File Box list says whether the file got there.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import filebox
from jarvis.tests import test_filebox_held as fbh
from jarvis.tests._pending_action_helpers import as_user, draft_doctype

OWNER = fbh.OWNER
OTHER = fbh.OTHER


def _todo(allocated_to: str) -> str:
	doc = frappe.get_doc({"doctype": "ToDo", "description": "fba draft", "allocated_to": allocated_to})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _source(conv: str):
	name = frappe.db.get_value(fbh.CONV, conv, "filebox_source_file")
	return frappe.db.get_value("File", name, ["name", "file_url", "file_name"], as_dict=True)


def _attachments(doctype: str, name: str) -> list:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=["name", "file_url"],
	)


class TestAttachSourceToDraft(fbh._Base):
	def setUp(self):
		super().setUp()
		self.addCleanup(lambda: frappe.db.delete("ToDo", {"description": "fba draft"}))

	def test_attaches_the_dropped_file_to_the_draft(self):
		conv = self.conv()
		todo = _todo(OWNER)
		with as_user(OWNER):
			out = filebox.attach_source_to_draft(conv, "ToDo", todo)
		src = _source(conv)
		[attached] = _attachments("ToDo", todo)
		self.assertEqual(out, {"attached_file": attached.name})
		self.assertEqual(attached.file_url, src.file_url)
		self.assertNotEqual(attached.name, src.name)
		# The conversation keeps its own link: resumes and duplicate checks use it.
		self.assertEqual(frappe.db.get_value("File", src.name, "attached_to_name"), conv)

	def test_is_idempotent(self):
		conv = self.conv()
		todo = _todo(OWNER)
		with as_user(OWNER):
			first = filebox.attach_source_to_draft(conv, "ToDo", todo)
			second = filebox.attach_source_to_draft(conv, "ToDo", todo)
		self.assertEqual(first, second)
		self.assertEqual(len(_attachments("ToDo", todo)), 1)

	def test_no_source_file_is_a_no_op(self):
		with as_user(OWNER):
			conv = frappe.get_doc({"doctype": fbh.CONV, "title": "File: none"}).insert(
				ignore_permissions=True
			)
		todo = _todo(OWNER)
		with as_user(OWNER):
			self.assertEqual(filebox.attach_source_to_draft(conv.name, "ToDo", todo), {})
		self.assertEqual(_attachments("ToDo", todo), [])

	def test_failure_reports_without_undoing_anything_else(self):
		conv = self.conv()
		todo = _todo(OTHER)  # OWNER cannot write it
		frappe.local.message_log = []
		with as_user(OWNER):
			out = filebox.attach_source_to_draft(conv, "ToDo", todo)
		self.assertIn("could not be attached", out["attach_error"])
		self.assertIn(_source(conv).file_name, out["attach_error"])
		self.assertEqual(_attachments("ToDo", todo), [])
		self.assertTrue(frappe.db.exists("ToDo", todo))
		self.assertEqual(frappe.local.message_log, [])  # the throw stays out of the reply
		self.assertTrue(frappe.db.exists("Error Log", {"method": "jarvis.file_box.attach_failed"}))

	def test_clearing_the_file_box_row_keeps_the_drafts_copy(self):
		conv = self.conv()
		todo = _todo(OWNER)
		with as_user(OWNER):
			attached = filebox.attach_source_to_draft(conv, "ToDo", todo)["attached_file"]
		frappe.delete_doc("File", _source(conv).name, ignore_permissions=True, force=True)
		content = frappe.get_doc("File", attached).get_content()
		self.assertIn(b"fbh invoice", content.encode() if isinstance(content, str) else content)


class TestAutoApplyAttaches(fbh._Base):
	def test_an_auto_applied_draft_gets_the_file_and_the_reply_says_so(self):
		dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		conv = self.conv()
		made = {"ok": True, "data": {"doctype": dt, "name": "D-1"}}
		with (
			patch("jarvis.api.dispatch_confirmed", return_value=made),
			patch.object(filebox, "attach_source_to_draft", return_value={"attached_file": "F-1"}) as attach,
		):
			res = self.call("create_doc", {"doctype": dt, "values": {"remark": "x"}}, conv)
		self.assertTrue(res["ok"], res)
		attach.assert_called_once_with(conv, dt, "D-1")
		self.assertEqual(res["data"]["attached_file"], "F-1")
		self.assertEqual(frappe.db.get_value(fbh.CONV, conv, "filebox_result_name"), "D-1")

	def test_a_non_document_create_is_not_attached(self):
		conv = self.conv()
		made = {"ok": True, "data": {"doctype": "ToDo", "name": "T-1"}}
		with (
			patch("jarvis.api.dispatch_confirmed", return_value=made),
			patch.object(filebox, "attach_source_to_draft") as attach,
			patch("jarvis.chat.held_writes._classify", return_value=("apply", None)),
		):
			self.call("create_doc", {"doctype": "ToDo", "values": {"description": "x"}}, conv)
		attach.assert_not_called()


class TestListSaysWhetherTheFileIsAttached(fbh._Base):
	def setUp(self):
		super().setUp()
		self.addCleanup(lambda: frappe.db.delete("ToDo", {"description": "fba draft"}))

	def _stamp(self, conv: str, todo: str) -> None:
		frappe.db.set_value(
			fbh.CONV,
			conv,
			{"filebox_result_doctype": "ToDo", "filebox_result_name": todo, "filebox_result_count": 1},
			update_modified=False,
		)
		frappe.db.commit()

	def test_attached_and_not_attached(self):
		with_file, without = self.conv(), self.conv()
		todo_a, todo_b = _todo(OWNER), _todo(OWNER)
		self._stamp(with_file, todo_a)
		self._stamp(without, todo_b)
		with as_user(OWNER):
			filebox.attach_source_to_draft(with_file, "ToDo", todo_a)
		frappe.db.commit()
		ladder = self.ladder()
		self.assertIn(f"Draft ToDo {todo_a} created · file attached", ladder[with_file]["result"])
		self.assertIn(f"Draft ToDo {todo_b} created · file not attached", ladder[without]["result"])
		self.assertNotIn("source_attached", ladder[with_file])
		self.assertNotIn("filebox_source_file", ladder[with_file])

	def test_deleted_draft_says_nothing_about_the_file(self):
		conv = self.conv()
		todo = _todo(OWNER)
		self._stamp(conv, todo)
		frappe.delete_doc("ToDo", todo, ignore_permissions=True, force=True)
		frappe.db.commit()
		result = self.ladder()[conv]["result"]
		self.assertIn("deleted", result)
		self.assertNotIn("attached", result)
