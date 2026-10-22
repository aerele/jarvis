"""Personalise note attachments: which File rows a note may take, and which it
may re-parent. A File already attached to a record is never moved; only the
caller's own bare upload is attached to the note."""

from __future__ import annotations

from unittest import mock

import frappe

from jarvis.chat import personalise_api
from jarvis.tests.test_personalise_api import NOTE, USER_A, USER_B, PersonaliseApiTestCase, _as

CONV = "Jarvis Conversation"
_FILE_FIELDS = ["attached_to_doctype", "attached_to_name"]


class TestPersonaliseAttachmentScope(PersonaliseApiTestCase):
	def setUp(self):
		super().setUp()
		self._files: list[str] = []
		self._convs: list[str] = []
		self._todos: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self._files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, ignore_permissions=True, force=True)
		for name in self._convs:
			if frappe.db.exists(CONV, name):
				frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
		for name in self._todos:
			if frappe.db.exists("ToDo", name):
				frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def _upload(self, user: str, name: str = "scope.txt", content: str = "scope body", **extra):
		fields = {"doctype": "File", "file_name": name, "is_private": 1, "content": content}
		fields.update(extra)
		with _as(user):
			fdoc = frappe.get_doc(fields).insert(ignore_permissions=True)
		self._files.append(fdoc.name)
		frappe.db.commit()
		return fdoc

	def _conversation(self, user: str) -> str:
		with _as(user):
			conv = frappe.get_doc({"doctype": CONV, "title": "persattach"}).insert(ignore_permissions=True)
		self._convs.append(conv.name)
		frappe.db.commit()
		return conv.name

	def test_own_bare_upload_is_attached_to_the_note(self):
		fdoc = self._upload(USER_A)
		with _as(USER_A):
			out = personalise_api.save_note(attachment=fdoc.file_url)
		row = frappe.db.get_value("File", fdoc.name, _FILE_FIELDS, as_dict=True)
		self.assertEqual(row.attached_to_doctype, NOTE)
		self.assertEqual(row.attached_to_name, out["note"])

	def test_file_in_own_chat_is_used_and_its_row_is_unchanged(self):
		conv = self._conversation(USER_A)
		fdoc = self._upload(USER_A, name="chat.txt", attached_to_doctype=CONV, attached_to_name=conv)
		with _as(USER_A):
			out = personalise_api.save_note(attachment=fdoc.file_url)
		note = frappe.db.get_value(NOTE, out["note"], ["kind", "attachment", "extracted_text"], as_dict=True)
		self.assertEqual(note.kind, "Attachment")
		self.assertEqual(note.attachment, fdoc.file_url)
		self.assertEqual(note.extracted_text, "scope body")
		row = frappe.db.get_value("File", fdoc.name, _FILE_FIELDS, as_dict=True)
		self.assertEqual(row.attached_to_doctype, CONV)
		self.assertEqual(row.attached_to_name, conv)

	def test_other_users_bare_upload_is_not_attached(self):
		# A bare row the caller does not own is never claimed for the note. The
		# validator is stubbed to resolve B's row so only the re-parent step is
		# under test (the validator itself is covered apart).
		fdoc = self._upload(USER_B, name="theirs.txt")
		with (
			_as(USER_A),
			mock.patch.object(personalise_api, "_validate_and_resolve_attachment", return_value=fdoc.name),
		):
			personalise_api.save_note(attachment=fdoc.file_url)
		row = frappe.db.get_value("File", fdoc.name, _FILE_FIELDS, as_dict=True)
		self.assertFalse(row.attached_to_doctype)
		self.assertFalse(row.attached_to_name)

	def test_file_attached_to_a_business_record_is_refused_and_unchanged(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "persattach todo"}).insert(
			ignore_permissions=True
		)
		self._todos.append(todo.name)
		fdoc = self._upload(USER_A, name="rec.txt", attached_to_doctype="ToDo", attached_to_name=todo.name)
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			personalise_api.save_note(attachment=fdoc.file_url)
		row = frappe.db.get_value("File", fdoc.name, _FILE_FIELDS, as_dict=True)
		self.assertEqual(row.attached_to_doctype, "ToDo")
		self.assertEqual(row.attached_to_name, todo.name)

	def test_file_in_another_users_chat_is_refused(self):
		conv = self._conversation(USER_B)
		fdoc = self._upload(USER_B, name="bchat.txt", attached_to_doctype=CONV, attached_to_name=conv)
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			personalise_api.save_note(attachment=fdoc.file_url)

	def test_another_users_bare_upload_is_refused(self):
		fdoc = self._upload(USER_B, name="bbare.txt")
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			personalise_api.save_note(attachment=fdoc.file_url)
		self.assertFalse(frappe.db.get_value("File", fdoc.name, "attached_to_name"))

	def test_shared_file_url_picks_the_qualifying_row(self):
		# One file_url, three rows: a business record's copy, another user's bare
		# copy and the caller's own bare upload. Only the last qualifies, and it is
		# the row that gets attached, whatever order the rows come back in.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "persattach todo 2"}).insert(
			ignore_permissions=True
		)
		self._todos.append(todo.name)
		first = self._upload(USER_B, name="dup.txt", content="same bytes")
		self._upload(
			USER_A,
			name="dup.txt",
			content="same bytes",
			attached_to_doctype="ToDo",
			attached_to_name=todo.name,
		)
		mine = self._upload(USER_A, name="dup.txt", content="same bytes")
		url = first.file_url
		rows = frappe.get_all("File", filters={"file_url": url}, pluck="name")
		if len(rows) < 3:
			self.skipTest("File dedupe collapsed the shared rows on this version")
		with _as(USER_A):
			self.assertEqual(personalise_api._validate_and_resolve_attachment(url), mine.name)

	def test_unknown_url_is_still_a_validation_error(self):
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			personalise_api._validate_and_resolve_attachment("/private/files/persattach-nope.txt")
