"""Audit copies of a private Chat question follow the question's own generic
policy (requester only), and a reviewer's recorded decision cannot be rewritten.

Every artifact here comes from a real producer: an ORM save (Version), a
Desk/REST or docmeta delete (Deleted Document), a stock-style attachment. File
Box and unrelated sources are the positive controls that must stay readable.
"""

import json

import frappe
from frappe import client
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import docmeta_api
from jarvis.tests import test_chat_approval_continuation as continuation_fixture
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = continuation_fixture.AR


class AuditReach:
	"""Every generic read surface of an audit artifact (plain mixin, not collected)."""

	def generic_reach(self, doctype, name):
		"""What the current user reaches through each generic read surface; a
		PermissionError (no DocPerm at all) counts as no reach."""
		from frappe.desk import reportview

		def reach(read, denied):
			try:
				return read()
			except frappe.PermissionError:
				frappe.clear_messages()
				return denied

		def report():
			form_dict = frappe.local.form_dict
			frappe.local.form_dict = frappe._dict(
				doctype=doctype, fields=json.dumps(["name"]), filters=json.dumps({"name": name})
			)
			try:
				return len((reportview.get() or {}).get("values") or [])
			finally:
				frappe.local.form_dict = form_dict

		return {
			"list": reach(
				lambda: frappe.get_list(doctype, filters={"name": name}, pluck="name") == [name], False
			),
			"count": reach(lambda: client.get_count(doctype, filters={"name": name}), 0),
			"report": reach(report, 0),
			"doc": bool(frappe.has_permission(doctype, "read", doc=name)),
			"rest": reach(lambda: client.get(doctype, name)["name"] == name, False),
		}

	def hidden(self):
		return {"list": False, "count": 0, "report": 0, "doc": False, "rest": False}

	def visible(self):
		return {"list": True, "count": 1, "report": 1, "doc": True, "rest": True}


class TestChatApprovalAuditPrivacy(
	AuditReach, continuation_fixture.ComposedChatReviewFixture, FrappeTestCase
):
	def versions(self, doctype, name):
		rows = frappe.get_all("Version", filters={"ref_doctype": doctype, "docname": name}, pluck="name")
		self.assertTrue(rows, f"a real Version of {doctype} must exist")
		return rows[0]

	def archive(self, doctype, name):
		rows = frappe.get_all(
			"Deleted Document", filters={"deleted_doctype": doctype, "deleted_name": name}, pluck="name"
		)
		self.assertTrue(rows, f"a real Deleted Document of {doctype} must exist")
		return rows[0]

	def comment(self, approval, content):
		name = docmeta_api.add_comment(AR, approval, content)["name"]
		self.comments.append(name)
		return name

	def test_question_and_review_comment_versions_are_hidden_from_untagged_manager(self):
		"""Sowmya #2a / G2 / N21."""
		self.tag()
		for name in (self.chat, self.filebox):
			with as_user(self.owner):
				doc = frappe.get_doc(AR, name)
				doc.question = self.secret + " question edited"
				doc.save()
			frappe.db.commit()
		with as_user(self.reviewer):
			chat_comment = self.comment(self.chat, self.secret + " reviewer note")
			docmeta_api.update_comment(chat_comment, self.secret + " reviewer note edited")
		with as_user(self.manager):
			filebox_comment = self.comment(self.filebox, "QA756 File Box note")
			docmeta_api.update_comment(filebox_comment, "QA756 File Box note edited")
		versions = {
			"chat question": self.versions(AR, self.chat),
			"chat comment": self.versions("Comment", chat_comment),
			"filebox question": self.versions(AR, self.filebox),
			"filebox comment": self.versions("Comment", filebox_comment),
		}
		seen = {}
		with as_user(self.owner):  # an ordinary Jarvis User requester has no Version DocPerm (D9)
			seen["ordinary requester chat question"] = self.generic_reach(
				"Version", versions["chat question"]
			)
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		for label, name in versions.items():
			with as_user(self.manager):
				seen["manager " + label] = self.generic_reach("Version", name)
		for label in ("chat question", "chat comment"):
			with as_user(self.owner):
				seen["requester " + label] = self.generic_reach("Version", versions[label])
		hidden, visible = self.hidden(), self.visible()
		self.assertEqual(
			seen,
			{
				"ordinary requester chat question": hidden,
				"manager chat question": hidden,
				"manager chat comment": hidden,
				"manager filebox question": visible,
				"manager filebox comment": visible,
				"requester chat question": visible,
				"requester chat comment": visible,
			},
		)

	def test_review_comment_version_orphaned_by_question_delete_is_not_left_readable(self):
		"""Clarification 1.4: comment edit, then requester-SM delete, then the stock
		delete_dynamic_links job (stubbed enqueue, so run as the worker would) bulk-deletes
		the Comment without its Version or an archive."""
		from frappe.model.delete_doc import delete_dynamic_links

		self.tag()
		with as_user(self.reviewer):
			note = self.comment(self.chat, self.secret + " reviewer note")
			docmeta_api.update_comment(note, self.secret + " reviewer note edited")
		version = self.versions("Comment", note)
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		with as_user(self.owner):
			client.delete(AR, self.chat)
			frappe.db.commit()
		delete_dynamic_links(AR, self.chat)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("Comment", note))
		self.assertFalse(
			frappe.db.exists("Deleted Document", {"deleted_doctype": "Comment", "deleted_name": note})
		)
		seen = {}
		for label, actor in (("manager", self.manager), ("requester", self.owner)):
			with as_user(actor):
				seen[label] = self.generic_reach("Version", version)
		# Unclassifiable: Administrator only.
		self.assertEqual(seen, {"manager": self.hidden(), "requester": self.hidden()})

	def test_deleted_question_comment_and_attachment_archives_are_hidden_from_untagged_manager(self):
		"""Sowmya #2b / G1 / N22: ordinary (non-root) producers only."""
		self.tag()
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		with as_user(self.owner):
			owner_comment = self.comment(self.chat, self.secret + " requester note")
			docmeta_api.toggle_like(AR, self.chat, 1)
			docmeta_api.toggle_like(AR, self.chat, 0)
		like = frappe.get_all(
			"Deleted Document",
			filters={
				"deleted_doctype": "Comment",
				"creation": [">=", self.started],
				"data": ["like", f'%"{self.chat}"%'],
			},
			pluck="name",
		)
		with as_user(self.reviewer):
			reviewer_comment = self.comment(self.chat, self.secret + " reviewer note")
			docmeta_api.delete_comment(reviewer_comment)
		with as_user(self.owner):
			docmeta_api.delete_comment(owner_comment)
		with as_user(self.manager):
			filebox_comment = self.comment(self.filebox, "QA756 File Box note")
			docmeta_api.delete_comment(filebox_comment)
		attachments = {}
		for name in (self.chat, self.filebox):
			with as_user(self.owner):
				from frappe.handler import check_write_permission

				check_write_permission(AR, name)  # stock upload_file's own gate
				file = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": "qa756-attachment-" + name + ".txt",
						"content": self.secret + " attachment",
						"is_private": 1,
						"attached_to_doctype": AR,
						"attached_to_name": name,
					}
				).insert()
				self.extra_files.append(file.name)
				frappe.db.commit()
				docmeta_api.delete_attachment(AR, name, file.name)
			attachments[name] = file.name
		gone = self.fx.question("Chat")  # its conversation is deleted before the question
		inline = self.fx.question("Chat")  # Chat ask inside a real File Box conversation
		frappe.db.set_value(continuation_fixture.CONV, self.conversation_of(inline), "file_box", 1)
		frappe.db.delete(continuation_fixture.CONV, {"name": self.conversation_of(gone)})
		frappe.db.commit()
		todo = self.owned_todo()
		with as_user(self.owner):
			todo_comment = frappe.get_doc(
				{
					"doctype": "Comment",
					"comment_type": "Comment",
					"reference_doctype": "ToDo",
					"reference_name": todo,
					"content": "QA756 unrelated note",
				}
			).insert(ignore_permissions=True)
			self.comments.append(todo_comment.name)
			frappe.delete_doc("Comment", todo_comment.name, ignore_permissions=True)
			self.assertTrue(frappe.has_permission(AR, "delete", doc=self.chat))  # preserved
			for name in (self.chat, self.filebox, gone, inline):
				client.delete(AR, name)
			frappe.db.commit()
		archives = {
			"chat question": self.archive(AR, self.chat),
			"chat reviewer comment": self.archive("Comment", reviewer_comment),
			"chat requester comment": self.archive("Comment", owner_comment),
			"chat like": like[0] if like else "",
			"chat attachment": self.archive("File", attachments[self.chat]),
			"chat question, conversation gone": self.archive(AR, gone),
			"filebox question": self.archive(AR, self.filebox),
			"filebox comment": self.archive("Comment", filebox_comment),
			"filebox attachment": self.archive("File", attachments[self.filebox]),
			"filebox-origin chat question": self.archive(AR, inline),
			"unrelated comment": self.archive("Comment", todo_comment.name),
		}
		self.assertTrue(archives["chat like"], "a real like-removal archive must exist")
		seen = {}
		for label, name in archives.items():
			with as_user(self.manager):
				seen["manager " + label] = self.generic_reach("Deleted Document", name)
		for label in ("chat question", "chat question, conversation gone", "chat reviewer comment"):
			with as_user(self.owner):
				seen["requester " + label] = self.generic_reach("Deleted Document", archives[label])
		hidden, visible = self.hidden(), self.visible()
		self.assertEqual(
			seen,
			{
				"manager chat question": hidden,
				"manager chat reviewer comment": hidden,
				"manager chat requester comment": hidden,
				"manager chat like": hidden,
				"manager chat attachment": hidden,
				"manager chat question, conversation gone": hidden,
				"manager filebox question": visible,
				"manager filebox comment": visible,
				"manager filebox attachment": visible,
				"manager filebox-origin chat question": visible,
				"manager unrelated comment": visible,
				"requester chat question": visible,
				"requester chat question, conversation gone": visible,
				"requester chat reviewer comment": visible,
			},
		)

	def decided(self, name, by):
		# The committed state decide_pending's raw UPDATE leaves (validate never runs).
		frappe.db.set_value(
			AR,
			name,
			{
				"status": "Approved",
				"decision": "Yes",
				"decided_by": by,
				"decided_at": frappe.utils.now_datetime(),
			},
		)
		frappe.db.commit()

	def decided_fields(self, name):
		return frappe.db.get_value(AR, name, ["status", "decision", "decided_by", "decided_at"])

	def test_requester_who_is_manager_cannot_rewrite_or_reopen_a_reviewers_decision(self):
		"""Sowmya re-review / G3 / C2 / N15."""
		from jarvis.tools.update_doc import update_doc

		self.tag()
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		self.decided(self.chat, self.reviewer)
		answered = self.fx.question("Chat")
		frappe.db.set_value(
			AR, answered, {"status": "Answered", "decision": "Answered in chat", "decided_by": self.owner}
		)
		frappe.db.commit()
		frozen = {name: self.decided_fields(name) for name in (self.chat, answered)}

		def ignore_permissions_save():
			doc = frappe.get_doc(AR, self.chat)
			doc.decision = "No"
			doc.flags.ignore_permissions = True
			doc.save()

		attempts = {
			"rest decision": (self.owner, lambda: client.set_value(AR, self.chat, "decision", "No")),
			"rest status": (self.owner, lambda: client.set_value(AR, self.chat, "status", "Rejected")),
			"rest reopen": (self.owner, lambda: client.set_value(AR, self.chat, "status", "Pending")),
			"rest decided_by self": (
				self.owner,
				lambda: client.set_value(AR, self.chat, "decided_by", self.owner),
			),
			"agent update_doc": (self.owner, lambda: update_doc(AR, self.chat, {"decision": "No"})),
			"ignore_permissions save": (self.owner, ignore_permissions_save),
			"Administrator save": ("Administrator", ignore_permissions_save),
			"rest reopen answered": (self.owner, lambda: client.set_value(AR, answered, "status", "Pending")),
		}
		outcomes = {}
		for label, (actor, attempt) in attempts.items():
			try:
				with as_user(actor):
					attempt()
				refused = None
			except Exception as exc:
				refused = type(exc).__name__
			outcomes[label] = (refused, {n: self.decided_fields(n) for n in frozen} == frozen)
			frappe.db.rollback()  # each attempt starts from the committed decision
		self.assertEqual(
			outcomes,
			{label: ("PermissionError", True) for label in attempts},
		)

	def test_allowed_post_decision_edits_and_filebox_policy_are_unchanged(self):
		"""C2 positive controls: reference/title edits and File Box rows keep today's policy."""
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		todo = self.owned_todo()
		self.decided(self.chat, self.reviewer)
		self.decided(self.filebox, self.manager)
		edits = {
			"title": "QA756 renamed",
			"question": self.secret + " question clarified",
			"context_md": "QA756 context",
			"options": '["Yes", "No", "Later"]',
			"ref_doctype": "ToDo",
			"ref_name": todo,
		}
		with as_user(self.owner):
			client.set_value(AR, self.chat, edits)
			client.set_value(AR, self.filebox, "decision", "QA756 File Box revised")
			frappe.db.commit()
		self.assertEqual(
			frappe.db.get_value(AR, self.chat, [*edits, "status", "decided_by"], as_dict=True),
			{**edits, "status": "Approved", "decided_by": self.reviewer},
		)
		self.assertEqual(frappe.db.get_value(AR, self.filebox, "decision"), "QA756 File Box revised")

	def test_attachment_metadata_versions_of_a_private_question_are_requester_only(self):
		"""F3 / V1: a normal File metadata save (stock upload gate, then a REST rename)
		writes a File Version carrying the new file name."""
		from frappe.handler import check_write_permission

		with as_user(self.owner):  # an ordinary Jarvis User requester
			check_write_permission(AR, self.chat)
			file = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "qa756-attachment.txt",
					"content": self.secret + " attachment",
					"is_private": 1,
					"attached_to_doctype": AR,
					"attached_to_name": self.chat,
				}
			).insert()
			self.extra_files.append(file.name)
			frappe.db.commit()
			private_name = "QA756 private salary revision.txt"
			client.set_value("File", file.name, "file_name", private_name)
			frappe.db.commit()
		version = self.versions("File", file.name)
		self.assertIn(private_name, frappe.db.get_value("Version", version, "data"))
		seen = {}
		with as_user(self.owner):
			seen["ordinary requester"] = self.generic_reach("Version", version)
		with as_user(self.manager):
			seen["untagged manager"] = self.generic_reach("Version", version)
			seen["untagged manager file itself"] = bool(frappe.has_permission("File", "read", doc=file.name))
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		with as_user(self.owner):
			seen["manager requester"] = self.generic_reach("Version", version)
		self.assertEqual(
			seen,
			{
				"ordinary requester": self.hidden(),  # no Version DocPerm, unchanged
				"untagged manager": self.hidden(),
				"untagged manager file itself": False,
				"manager requester": self.visible(),
			},
		)
