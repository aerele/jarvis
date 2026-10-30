"""An explicit replacement survives File Box question/approval continuations."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import frappe
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jarvis.chat import approvals_api, filebox
from jarvis.tests._pending_action_helpers import as_user
from jarvis.tests.test_filebox_rerun import OTHER, USER, _Base

CONV = "Jarvis Conversation"


def readable_pdf(text):
	writer = PdfWriter()
	page = writer.add_blank_page(width=612, height=792)
	font = DictionaryObject(
		{
			NameObject("/Type"): NameObject("/Font"),
			NameObject("/Subtype"): NameObject("/Type1"),
			NameObject("/BaseFont"): NameObject("/Helvetica"),
		}
	)
	page[NameObject("/Resources")] = DictionaryObject(
		{NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
	)
	stream = DecodedStreamObject()
	stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
	page[NameObject("/Contents")] = writer._add_object(stream)
	result = BytesIO()
	writer.write(result)
	return result.getvalue()


class TestFileBoxSource(_Base):
	def _msg(self, *args, **kwargs):
		with as_user(USER):
			return super()._msg(*args, **kwargs)

	def manager(self):
		from jarvis.tests.test_filebox_rerun import _ensure_user

		manager = "source-reviewer@example.com"
		_ensure_user(manager)
		frappe.get_doc("User", manager).add_roles("System Manager")
		return manager

	def fixture(self):
		conv = self._conv()
		with as_user(USER):
			original = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"source-{conv}.docx",
					"content": f"corrupt DOCX {conv}",
					"is_private": 1,
					"attached_to_doctype": CONV,
					"attached_to_name": conv,
				}
			).insert(ignore_permissions=True)
			pdf = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"replacement-{conv}.pdf",
					"content": readable_pdf(f"Readable replacement {conv}"),
					"is_private": 1,
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(CONV, conv, "filebox_source_file", original.name, update_modified=False)
		item = {"name": "replacement", "type": "pdf", "file_url": pdf.file_url, "title": pdf.file_name}
		msg = self._msg(conv, 1, "user", "Use this readable replacement", canvas=json.dumps([item]))
		frappe.db.commit()
		self.addCleanup(
			lambda: frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
			if frappe.db.exists("File", pdf.name)
			else None
		)
		return conv, original, pdf, msg, item

	def select(self, conv, msg, item, expected=""):
		from jarvis.chat.filebox_source import set_source

		with as_user(USER):
			return set_source(
				conversation=conv,
				message=msg,
				item=item["name"],
				file_url=item["file_url"],
				expected=expected,
			)

	def approval(self, conv):
		with as_user(USER):
			doc = frappe.get_doc(
				{
					"doctype": "Jarvis Approval Request",
					"title": "Which supplier?",
					"question": "Which supplier?",
					"conversation": conv,
					"source": "File Box",
					"status": "Pending",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def decided_attachment(self, conv):
		name = self.approval(conv)
		with (
			as_user(USER),
			patch.object(approvals_api, "_resume_conversation", return_value={"ok": True}) as resume,
		):
			out = approvals_api.decide(name, "Supplier A")
		self.assertTrue(out["resumed"])
		return json.loads(resume.call_args.kwargs["attachments"])

	def test_00_question_resume_uses_explicit_replacement_instead_of_original(self):
		# Pin the actual faulty boundary before selecting, then test two resumes.
		conv, original, pdf, msg, item = self.fixture()
		self.assertEqual(self.decided_attachment(conv)[0]["file_url"], original.file_url)
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))
		self.select(conv, msg, item)
		for _ in range(2):
			atts = self.decided_attachment(conv)
			self.assertEqual([a["file_url"] for a in atts], [pdf.file_url])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_source_file"), original.name)
		self.assertEqual(frappe.db.get_value("File", original.name, "attached_to_name"), conv)

	def test_an_attachment_without_selection_does_not_replace_source(self):
		conv, original, pdf, msg, item = self.fixture()
		with as_user(USER):
			self.assertEqual(filebox._source_file(conv).file_url, original.file_url)

	def test_source_selection_does_not_dispatch_or_decide_pending_questions(self):
		conv, original, pdf, msg, item = self.fixture()
		approval = self.approval(conv)
		with patch.object(approvals_api, "_resume_conversation") as resume:
			state = self.select(conv, msg, item)
		resume.assert_not_called()
		self.assertEqual(frappe.db.get_value("Jarvis Approval Request", approval, "status"), "Pending")
		self.assertEqual(state["source"]["name"], pdf.name)
		self.assertEqual(frappe.db.get_value("File", pdf.name, "attached_to_name"), conv)

	def test_foreign_owner_is_refused_before_any_source_change(self):
		from jarvis.chat.filebox_source import set_source

		conv, original, pdf, msg, item = self.fixture()
		with as_user(OTHER), self.assertRaises(frappe.PermissionError):
			set_source(conversation=conv, message=msg, item=item["name"], file_url=pdf.file_url, expected="")
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_reference_to_another_conversations_file_is_not_reparented(self):
		conv, original, pdf, msg, item = self.fixture()
		other = self._conv()
		frappe.db.set_value("File", pdf.name, {"attached_to_doctype": CONV, "attached_to_name": other})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.select(conv, msg, item)
		self.assertEqual(frappe.db.get_value("File", pdf.name, "attached_to_name"), other)

	def test_same_source_retry_is_idempotent_and_a_stale_switch_is_refused(self):
		conv, original, pdf, msg, item = self.fixture()
		first = self.select(conv, msg, item)
		self.assertEqual(self.select(conv, msg, item)["source"], first["source"])
		from jarvis.chat.filebox_source import set_source

		original_item = {
			"name": "original",
			"file_url": original.file_url,
			"type": "file",
			"title": original.file_name,
		}
		original_msg = self._msg(conv, 2, "user", "Original", canvas=json.dumps([original_item]))
		frappe.db.commit()
		with as_user(USER):
			stale = set_source(
				conversation=conv,
				message=original_msg,
				item="original",
				file_url=original.file_url,
				expected="",
			)
		self.assertFalse(stale["ok"])
		self.assertEqual(stale["reason"], "source_changed")
		self.assertEqual(stale["source"]["name"], pdf.name)

	def test_missing_selected_file_never_falls_back_to_docx(self):
		from jarvis.chat.filebox_source import SourceLost

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with as_user(USER), self.assertRaises(SourceLost):
			filebox._source_file(conv)

	def test_typed_answer_prompt_contains_pdf_once_without_original_docx(self):
		from jarvis.chat import turn_handler
		from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile

		install_synthetic_runtime_profile()
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		answer = self._msg(conv, 2, "user", "Supplier A")
		frappe.db.commit()
		with as_user(USER), patch.object(turn_handler, "_vision_enabled", return_value=False):
			prepared = turn_handler.assemble_prompt(
				frappe.get_doc(CONV, conv),
				message_id=answer,
				conversation_id=conv,
				context={},
				attachments=None,
				user=USER,
			)
		self.assertIn(f"Readable replacement {conv}", prepared.user_message)
		self.assertNotIn(original.file_url, prepared.user_message)
		self.assertEqual(prepared.user_message.count(f"Readable replacement {conv}"), 1)

	def test_worker_input_no_selection_and_ordinary_chat_keep_object_identity(self):
		from jarvis.chat.filebox_source import prepare_attachments

		conv, original, pdf, msg, item = self.fixture()
		attachments = [{"file_url": pdf.file_url, "file_name": pdf.file_name}]
		with as_user(USER):
			self.assertIs(prepare_attachments(frappe.get_doc(CONV, conv), attachments), attachments)
		self.assertIs(prepare_attachments(frappe._dict(file_box=0), attachments), attachments)

	def test_worker_input_dedupes_selected_pdf_removes_docx_and_keeps_references(self):
		from jarvis.chat.filebox_source import prepare_attachments

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		reference = {"file_url": "/private/files/extra.txt", "file_name": "extra.txt"}
		with as_user(USER):
			out = prepare_attachments(
				frappe.get_doc(CONV, conv),
				[{"file_url": original.file_url}, {"file_url": pdf.file_url}, reference, reference],
			)
		self.assertEqual([a["file_url"] for a in out], [pdf.file_url, reference["file_url"]])

	def test_revoked_selected_file_is_refused_before_attachment_preparation(self):
		from jarvis.chat.filebox_source import SourceLost, prepare_attachments

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		real_permission = frappe.has_permission

		def permission(doctype, *args, **kwargs):
			return (
				False
				if doctype == "File" and kwargs.get("doc") == pdf.name
				else real_permission(doctype, *args, **kwargs)
			)

		with (
			as_user(USER),
			patch("frappe.has_permission", side_effect=permission),
			self.assertRaises(SourceLost),
		):
			prepare_attachments(frappe.get_doc(CONV, conv), None)

	def test_unreferenced_file_and_canvas_name_are_refused(self):
		conv, original, pdf, msg, item = self.fixture()
		for change in ({"name": "forged"}, {"file_url": original.file_url}):
			with self.subTest(change=change), self.assertRaises(frappe.PermissionError):
				self.select(conv, msg, {**item, **change})
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_no_file_permission_does_not_link_the_attachment(self):
		conv, original, pdf, msg, item = self.fixture()
		real_permission = frappe.has_permission

		def permission(doctype, *args, **kwargs):
			return (
				False
				if doctype == "File" and kwargs.get("doc") == pdf.name
				else real_permission(doctype, *args, **kwargs)
			)

		with (
			patch("frappe.has_permission", side_effect=permission),
			self.assertRaises(frappe.PermissionError),
		):
			self.select(conv, msg, item)
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_original_selection_clears_override_and_keeps_both_files(self):
		from jarvis.chat.filebox_source import set_source

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		original_msg = self._msg(
			conv,
			2,
			"user",
			"Original",
			canvas=json.dumps([{"name": "original", "file_url": original.file_url}]),
		)
		frappe.db.commit()
		with as_user(USER):
			state = set_source(
				conversation=conv,
				message=original_msg,
				item="original",
				file_url=original.file_url,
				expected=pdf.name,
			)
		self.assertEqual(state["version"], "")
		self.assertEqual(state["source"]["name"], original.name)
		self.assertEqual(frappe.db.get_value("File", pdf.name, "attached_to_name"), conv)

	def test_queued_and_running_turn_states_refuse_source_changes(self):
		from jarvis.chat import admission

		conv, original, pdf, msg, item = self.fixture()
		turn = frappe.generate_hash(length=12)
		frappe.db.sql(
			"INSERT INTO `tabJarvis Chat Turn` (name, conversation, state) VALUES (%s,%s,%s)",
			(turn, conv, "queued"),
		)
		for state in admission._CONV_BLOCKING_STATES:
			frappe.db.set_value("Jarvis Chat Turn", turn, "state", state)
			frappe.db.commit()
			with self.subTest(state=state):
				out = self.select(conv, msg, item)
				self.assertFalse(out["ok"])
				self.assertEqual(out["reason"], "busy")
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_legacy_stream_and_executing_action_refuse_source_changes(self):
		conv, original, pdf, msg, item = self.fixture()
		stream = self._msg(conv, 2, "assistant", "working", streaming=1)
		frappe.db.commit()
		self.assertEqual(self.select(conv, msg, item)["reason"], "busy")
		frappe.db.set_value("Jarvis Chat Message", stream, "streaming", 0)
		pa = frappe.generate_hash(length=12)
		frappe.db.sql(
			"INSERT INTO `tabJarvis Pending Action` (name, conversation, kind, status, owner_user) VALUES (%s,%s,%s,%s,%s)",
			(pa, conv, "file_box_held", "Executing", USER),
		)
		frappe.db.commit()
		self.assertEqual(self.select(conv, msg, item)["reason"], "busy")
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_claimed_resume_and_fresh_rerun_refuse_source_changes(self):
		from frappe.utils import now_datetime

		from jarvis.chat.pending_actions._store import WAITER

		conv, original, pdf, msg, item = self.fixture()
		waiter = frappe.generate_hash(length=12)
		frappe.db.sql(
			f"INSERT INTO `tab{WAITER}` (name, conversation, resume_state) VALUES (%s,%s,%s)",
			(waiter, conv, "claimed"),
		)
		frappe.db.commit()
		self.assertEqual(self.select(conv, msg, item)["reason"], "busy")
		frappe.db.delete(WAITER, {"name": waiter})
		frappe.db.set_value(CONV, conv, "filebox_rerun_at", now_datetime())
		frappe.db.commit()
		self.assertEqual(self.select(conv, msg, item)["reason"], "busy")

	def test_link_and_pointer_roll_back_together(self):
		conv, original, pdf, msg, item = self.fixture()
		real_set_value = frappe.db.set_value

		def fail_pointer(doctype, name, field, *args, **kwargs):
			if doctype == CONV and field == "filebox_active_source_file":
				raise RuntimeError("write failure")
			return real_set_value(doctype, name, field, *args, **kwargs)

		with patch.object(frappe.db, "set_value", side_effect=fail_pointer), self.assertRaises(RuntimeError):
			self.select(conv, msg, item)
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_active_source_file"))

	def test_pre_migration_selection_refuses_without_mutation(self):
		from jarvis.chat import filebox_source

		conv, original, pdf, msg, item = self.fixture()
		with (
			patch.object(filebox_source, "ready", return_value=False),
			self.assertRaises(frappe.ValidationError),
		):
			self.select(conv, msg, item)
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))

	def test_server_field_cannot_be_forged_by_an_orm_save(self):
		conv, original, pdf, msg, item = self.fixture()
		with as_user(USER), self.assertRaises(frappe.PermissionError):
			doc = frappe.get_doc(CONV, conv)
			doc.filebox_active_source_file = pdf.name
			doc.save(ignore_permissions=True)

	def test_routing_resume_uses_selected_pdf(self):
		from jarvis.chat import filebox_skills

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		with (
			as_user(USER),
			patch.object(approvals_api, "_resume_conversation", return_value={"ok": True}) as resume,
		):
			self.assertTrue(filebox_skills._resume(conv, "Continue"))
		self.assertEqual(
			[a["file_url"] for a in json.loads(resume.call_args.kwargs["attachments"])], [pdf.file_url]
		)

	def test_held_resume_uses_selected_pdf_and_claims_once(self):
		from jarvis.chat import held_writes
		from jarvis.chat.api import SEND_CLAIM_FLAG
		from jarvis.chat.pending_actions._store import PA, WAITER

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		parent, waiter = frappe.generate_hash(length=12), frappe.generate_hash(length=12)
		frappe.db.sql(
			f"INSERT INTO `tab{PA}` (name, conversation, kind, status, owner_user) VALUES (%s,%s,%s,%s,%s)",
			(parent, conv, "file_box_held", "Executed", USER),
		)
		frappe.db.sql(
			f"INSERT INTO `tab{WAITER}` (name, parent, conversation, resume_state) VALUES (%s,%s,%s,%s)",
			(waiter, parent, conv, "due"),
		)
		frappe.db.commit()
		row, wait = frappe._dict(name=parent, owner_user=USER), frappe._dict(name=waiter, conversation=conv)

		def accepted(*args, **kwargs):
			frappe.flags[SEND_CLAIM_FLAG]()
			frappe.db.commit()
			return {"ok": True}

		with (
			as_user(USER),
			patch.object(approvals_api, "_resume_conversation", side_effect=accepted) as resume,
		):
			self.assertTrue(held_writes._resume_one(row, wait, "Continue"))
			self.assertFalse(held_writes._resume_one(row, wait, "Continue"))
		resume.assert_called_once()
		self.assertEqual(
			[a["file_url"] for a in json.loads(resume.call_args.kwargs["attachments"])], [pdf.file_url]
		)

	def test_new_drafts_use_pdf_but_an_existing_drafts_original_is_preserved(self):
		conv, original, pdf, msg, item = self.fixture()

		def draft():
			with as_user(USER):
				doc = frappe.get_doc(
					{"doctype": "ToDo", "description": "File source test", "allocated_to": USER}
				).insert(ignore_permissions=True)
			self.addCleanup(
				lambda: frappe.delete_doc("ToDo", doc.name, force=True, ignore_permissions=True)
				if frappe.db.exists("ToDo", doc.name)
				else None
			)
			return doc.name

		old, new = draft(), draft()
		with as_user(USER):
			first = filebox.attach_source_to_draft(conv, "ToDo", old)
		self.select(conv, msg, item)
		with as_user(USER):
			second = filebox.attach_source_to_draft(conv, "ToDo", new)
		self.assertEqual(frappe.db.get_value("File", first["attached_file"], "file_url"), original.file_url)
		self.assertEqual(frappe.db.get_value("File", second["attached_file"], "file_url"), pdf.file_url)
		self.assertEqual(frappe.db.count("File", {"attached_to_doctype": "ToDo", "attached_to_name": old}), 1)

	def test_deleting_selected_conversation_copy_keeps_draft_bytes(self):
		from jarvis.compat import file_bytes

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		other = self._conv()
		with as_user(USER):
			copy = frappe.get_doc("File", pdf.name).create_attachment_copy(
				CONV, other, ignore_permissions=True
			)
		self.assertEqual(copy.file_url, pdf.file_url)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		self.assertIn(b"%PDF", file_bytes(frappe.get_doc("File", copy.name)))

	def test_filebox_list_preview_tracks_pdf_without_renaming_original_row(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		with as_user(USER):
			page = filebox.list_inbound_page()
		row = next(r for r in page["rows"] if r["name"] == conv)
		self.assertEqual(row["file_url"], pdf.file_url)
		self.assertEqual(row["file_name"], pdf.file_name)
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_source_file"), original.name)

	def test_lost_selected_file_cannot_resume_an_approved_question_with_docx(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		name = self.approval(conv)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with as_user(USER), patch.object(approvals_api, "_resume_conversation") as resume:
			out = approvals_api.decide(name, "Supplier A")
		self.assertEqual(out["status"], "Approved")
		self.assertFalse(out["resumed"])
		resume.assert_not_called()

	def test_duplicate_blob_url_uses_the_selected_file_permission(self):
		from jarvis.chat import turn_handler
		from jarvis.chat.filebox_source import prepare_attachments

		conv, original, pdf, msg, item = self.fixture()
		other = self._conv(owner=OTHER)
		copy = frappe.get_doc("File", pdf.name).create_attachment_copy(CONV, other, ignore_permissions=True)
		first = frappe.get_doc("File", {"file_url": pdf.file_url}).name
		own = copy.name if first == pdf.name else pdf.name
		frappe.db.set_value(
			"File",
			first,
			{"owner": OTHER, "attached_to_doctype": CONV, "attached_to_name": other},
			update_modified=False,
		)
		frappe.db.set_value(
			"File",
			own,
			{"owner": USER, "attached_to_doctype": None, "attached_to_name": None},
			update_modified=False,
		)
		frappe.db.commit()
		self.assertEqual(frappe.get_doc("File", {"file_url": pdf.file_url}).name, first)
		self.select(conv, msg, item)
		with as_user(USER):
			self.assertFalse(frappe.has_permission("File", "read", doc=first))
			atts = prepare_attachments(frappe.get_doc(CONV, conv), None)
			text, _parts = turn_handler._prepare_attachments("Supplier A", atts, vision_ok=False)
		self.assertIn(f"Readable replacement {conv}", text)

	def test_invalid_attachment_file_identity_does_not_read_bytes(self):
		from jarvis.chat import turn_handler

		conv, original, pdf, msg, item = self.fixture()
		for file_id in ({"name": pdf.name}, [pdf.name], "", original.name):
			with self.subTest(file_id=file_id), as_user(USER), patch("jarvis.compat.file_bytes") as read:
				text, parts = turn_handler._prepare_attachments(
					"answer",
					[{"file_url": pdf.file_url, "file_name": pdf.file_name, "file_id": file_id}],
					vision_ok=False,
				)
				self.assertIn("Could not read", text)
				self.assertEqual(parts, [])
				read.assert_not_called()

	def test_selected_source_copy_requires_write_access_to_target(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		with as_user(OTHER):
			target = frappe.get_doc(
				{"doctype": "ToDo", "description": "Source permission test", "allocated_to": OTHER}
			).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("ToDo", target.name, force=True, ignore_permissions=True))
		with as_user(USER):
			out = filebox.attach_source_to_draft(conv, "ToDo", target.name)
		self.assertIn("attach_error", out)
		self.assertEqual(
			frappe.db.count("File", {"attached_to_doctype": "ToDo", "attached_to_name": target.name}), 0
		)

	def make_sheet(self, conv):
		from jarvis.chat import held_sheets
		from jarvis.chat.pending_actions._store import seal_sheet

		approval = self.approval(conv)
		with as_user(USER):
			name = held_sheets._open(conv, USER, [], questions=[approval])
			seal_sheet(name)
		frappe.db.commit()
		return name

	def test_sheet_display_and_labels_survive_a_lost_selected_source(self):
		from jarvis.chat import filebox_skills, held_sheets

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		sheet = self.make_sheet(conv)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with as_user(USER):
			out = approvals_api.get_pending_action(sheet)
			self.assertEqual(out["status"], "Pending")
			self.assertEqual(len(out["questions"]), 1)
			self.assertTrue(filebox_skills._file_name(conv))
			self.assertTrue(held_sheets._summary(conv))

	def test_system_manager_can_read_sheet_with_an_owners_selected_source(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		sheet = self.make_sheet(conv)
		with as_user(self.manager()):
			self.assertFalse(frappe.has_permission("File", "read", doc=pdf.name))
			out = approvals_api.get_pending_action(sheet)
		self.assertEqual(out["status"], "Pending")
		self.assertEqual(out["file_name"], pdf.file_name)

	def test_system_manager_decision_resolves_source_as_the_conversation_owner(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		approval = self.approval(conv)
		with (
			as_user(self.manager()),
			patch.object(approvals_api, "_resume_conversation", return_value={"ok": True}) as resume,
		):
			self.assertFalse(frappe.has_permission("File", "read", doc=pdf.name))
			out = approvals_api.decide(approval, "Supplier A")
		self.assertTrue(out["resumed"])
		self.assertEqual(json.loads(resume.call_args.kwargs["attachments"])[0]["file_url"], pdf.file_url)

	def test_reselect_after_a_newer_draft_copy_keeps_the_conversation_file(self):
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		with as_user(USER):
			target = frappe.get_doc(
				{"doctype": "ToDo", "description": "Source retry test", "allocated_to": USER}
			).insert(ignore_permissions=True)
			frappe.get_doc("File", pdf.name).create_attachment_copy(
				"ToDo", target.name, ignore_permissions=True
			)
		frappe.db.commit()
		self.addCleanup(lambda: frappe.delete_doc("ToDo", target.name, force=True, ignore_permissions=True))
		out = self.select(conv, msg, item)
		self.assertEqual(out["source"]["name"], pdf.name)

	def test_legacy_lost_source_finishes_placeholder_with_reselect_error(self):
		from jarvis.chat import turn_handler
		from jarvis.chat.filebox_source import SourceLost
		from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile

		install_synthetic_runtime_profile()
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with (
			as_user(USER),
			patch("jarvis.chat.worker.publish_to_user") as publish,
			patch("jarvis.chat.agent_session_pool.AgentSession.connect") as connect,
		):
			try:
				turn_handler.handle_chat_send(
					{"conversation_id": conv, "message_id": msg, "run_id": "source-lost-legacy"}
				)
			except SourceLost:
				pass  # The old path leaks this; the assertions must still fail.
		connect.assert_not_called()
		row = frappe.db.get_value(
			"Jarvis Chat Message",
			{"conversation": conv, "role": "assistant"},
			["streaming", "error"],
			as_dict=True,
		)
		self.assertFalse(row.streaming)
		self.assertIn("Select a source file again", row.error)
		errors = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "run:error"]
		self.assertEqual(errors[-1]["code"], "source_unavailable")

	def test_pump_lost_source_releases_turn_with_reselect_error(self):
		from jarvis.chat import prepare, turn_state
		from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile

		install_synthetic_runtime_profile()
		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		run = frappe.generate_hash(length=12)
		frappe.db.sql(
			"INSERT INTO `tabJarvis Chat Turn` (name, conversation, seed_message, state, version, reserved) VALUES (%s,%s,%s,%s,%s,%s)",
			(run, conv, msg, "queued", 0, 1),
		)
		frappe.db.commit()
		with (
			as_user(USER),
			patch.object(turn_state, "publish_to_user") as publish,
			patch("jarvis.chat.agent_session_pool.checkout") as checkout,
		):
			out = prepare.run_prepare(run)
		checkout.assert_not_called()
		self.assertEqual(out["reason"], "source_unavailable")
		turn = frappe.db.get_value("Jarvis Chat Turn", run, ["state", "reserved", "error"], as_dict=True)
		self.assertEqual(turn.state, "errored")
		self.assertEqual(turn.reserved, 0)
		self.assertIn("Select a source file again", turn.error)
		errors = [c.args[1] for c in publish.call_args_list if c.args[1].get("kind") == "run:error"]
		self.assertEqual(errors[-1]["code"], "source_unavailable")

	def test_two_chats_cannot_reparent_the_same_upload_concurrently(self):
		import threading

		from jarvis.chat.filebox_source import set_source

		conv, original, pdf, msg, item = self.fixture()
		second = self._conv()
		second_msg = self._msg(second, 1, "user", "Reference", canvas=json.dumps([item]))
		frappe.db.commit()
		site, sites_path = frappe.local.site, frappe.local.sites_path
		results, started = [], [threading.Event(), threading.Event()]
		database_type = type(frappe.local.db)
		real_sql = database_type.sql

		def observe_file_lock(database, query, *args, **kwargs):
			name = threading.current_thread().name
			if (
				isinstance(query, str)
				and query.startswith("SELECT name, file_url,")
				and query.endswith("FOR UPDATE")
				and name.startswith("source-select-")
			):
				started[int(name.rsplit("-", 1)[1])].set()
			return real_sql(database, query, *args, **kwargs)

		def select_in_thread(index, conversation, message):
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user(USER)
			try:
				out = set_source(
					conversation=conversation,
					message=message,
					item=item["name"],
					file_url=pdf.file_url,
					expected="",
				)
				results.append((conversation, "selected" if out["ok"] else out["reason"]))
			except frappe.PermissionError:
				results.append((conversation, "refused"))
			except Exception as error:
				results.append((conversation, type(error).__name__))
			finally:
				frappe.db.rollback()
				frappe.destroy()

		# Hold the actual File row so both requests reach the critical race.
		frappe.db.sql("SELECT name FROM `tabFile` WHERE name=%s FOR UPDATE", (pdf.name,))
		threads = [
			threading.Thread(target=select_in_thread, args=args, name=f"source-select-{args[0]}", daemon=True)
			for args in [(0, conv, msg), (1, second, second_msg)]
		]
		with patch.object(database_type, "sql", observe_file_lock):
			try:
				for thread in threads:
					thread.start()
				for event in started:
					self.assertTrue(event.wait(5), "both requests reached the File lock")
			finally:
				frappe.db.commit()
			for thread in threads:
				thread.join(10)
				self.assertFalse(thread.is_alive())
		self.assertEqual(sorted(status for _, status in results), ["refused", "selected"])
		winner = next(conversation for conversation, status in results if status == "selected")
		self.assertEqual(frappe.db.get_value("File", pdf.name, "attached_to_name"), winner)
		self.assertEqual(frappe.db.get_value(CONV, winner, "filebox_active_source_file"), pdf.name)

	def test_attachment_availability_read_keeps_source_and_link_unchanged(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		with as_user(USER):
			out = get_source_state(conv, message=msg, item=item["name"], file_url=pdf.file_url)
		self.assertTrue(out["candidate_available"])
		self.assertFalse(frappe.db.get_value("File", pdf.name, "attached_to_name"))
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_active_source_file"))

	def test_lost_selected_attachment_has_no_available_candidate(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with as_user(USER):
			out = get_source_state(conv, message=msg, item=item["name"], file_url=pdf.file_url)
		self.assertEqual(out["status"], "lost")
		self.assertFalse(out["candidate_available"])
		self.assertFalse(out["preview_available"])
		self.assertIsNone(out["source"])

	def test_a_lost_source_does_not_block_a_valid_replacement_candidate(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		self.select(conv, msg, item)
		frappe.delete_doc("File", pdf.name, force=True, ignore_permissions=True)
		with as_user(USER):
			other = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"second-{conv}.pdf",
					"content": readable_pdf("Second replacement"),
					"is_private": 1,
				}
			).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.delete_doc("File", other.name, force=True, ignore_permissions=True)
			if frappe.db.exists("File", other.name)
			else None
		)
		entry = {"name": "second", "file_url": other.file_url, "type": "pdf", "title": other.file_name}
		other_msg = self._msg(conv, 3, "user", "A new copy", canvas=json.dumps([entry]))
		frappe.db.commit()
		with as_user(USER):
			out = get_source_state(conv, message=other_msg, item="second", file_url=other.file_url)
		self.assertEqual(out["status"], "lost")
		self.assertTrue(out["candidate_available"])
		self.assertTrue(out["preview_available"])
		self.assertEqual(self.select(conv, other_msg, entry, expected=pdf.name)["source"]["name"], other.name)

	def test_readable_reference_keeps_preview_without_becoming_selectable(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		other = self._conv()
		frappe.db.set_value("File", pdf.name, {"attached_to_doctype": CONV, "attached_to_name": other})
		frappe.db.commit()
		with as_user(USER):
			state = get_source_state(conv, message=msg, item=item["name"], file_url=pdf.file_url)
		self.assertFalse(state["candidate_available"])
		self.assertTrue(state["preview_available"])
		self.assertEqual(frappe.db.get_value("File", pdf.name, "attached_to_name"), other)

	def test_availability_does_not_reveal_an_unreadable_files_existence(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		other = self._conv(owner=OTHER)
		frappe.db.set_value(
			"File", pdf.name, {"owner": OTHER, "attached_to_doctype": CONV, "attached_to_name": other}
		)
		frappe.db.commit()
		with as_user(USER):
			self.assertFalse(frappe.has_permission("File", "read", doc=pdf.name))
			state = get_source_state(conv, message=msg, item=item["name"], file_url=pdf.file_url)
		self.assertFalse(state["candidate_available"])
		self.assertFalse(state["preview_available"])

	def test_preview_access_checks_readable_rows_after_a_foreign_unattached_duplicate(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		other = self._conv()
		frappe.db.set_value("File", pdf.name, {"attached_to_doctype": CONV, "attached_to_name": other})
		foreign = frappe.get_doc("File", pdf.name).create_attachment_copy(
			CONV, other, ignore_permissions=True
		)
		frappe.db.set_value(
			"File", foreign.name, {"owner": OTHER, "attached_to_doctype": None, "attached_to_name": None}
		)
		self.addCleanup(
			lambda: frappe.delete_doc("File", foreign.name, force=True, ignore_permissions=True)
			if frappe.db.exists("File", foreign.name)
			else None
		)
		frappe.db.commit()
		with as_user(USER):
			self.assertFalse(frappe.has_permission("File", "read", doc=foreign.name))
			state = get_source_state(conv, message=msg, item=item["name"], file_url=pdf.file_url)
		self.assertFalse(state["candidate_available"])
		self.assertTrue(state["preview_available"])

	def test_an_unreferenced_url_has_no_preview_availability_result(self):
		from jarvis.chat.filebox_source import get_source_state

		conv, original, pdf, msg, item = self.fixture()
		with as_user(USER):
			state = get_source_state(conv, message=msg, item="wrong", file_url=pdf.file_url)
		self.assertFalse(state["candidate_available"])
		self.assertNotIn("preview_available", state)
