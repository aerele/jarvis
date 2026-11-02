"""Chat question review grants never confer private conversation access."""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api, docmeta_api
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = "Jarvis Approval Request"
GRANT = "Jarvis Approval Reviewer"
CONV = "Jarvis Conversation"


class TestChatReviewers(FrappeTestCase):
	def setUp(self):
		super().setUp()
		enqueue = patch("frappe.enqueue")  # User contact jobs are unrelated to review authority.
		enqueue.start()
		self.addCleanup(enqueue.stop)
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		self.users = []
		self.conversations = []
		self.approvals = []
		self.files = {}
		self.addCleanup(self.cleanup_fixtures)
		prefix = "qa756-" + uuid4().hex[:10]
		for label, roles in (
			("owner", ("Jarvis User",)),
			("reviewer", ("Jarvis User", "Jarvis Admin")),
			("manager", ("Jarvis User", "System Manager", "Purchase Master Manager")),
			("ordinary", ("Jarvis User",)),
		):
			user = prefix + "-" + label + "@example.com"
			self.users.append(user)
			ensure_user(user, roles)
			setattr(self, label, user)
		self.chat = self.question("Chat")
		self.filebox = self.question("File Box")

	def question(self, source, options='["Yes", "No"]'):
		with as_user(self.owner):
			conv = frappe.get_doc({"doctype": CONV, "title": "QA756 " + source}).insert(
				ignore_permissions=True
			)
			self.conversations.append(conv.name)
			doc = frappe.get_doc(
				{
					"doctype": AR,
					"title": "QA756 " + source,
					"source": source,
					"conversation": conv.name,
					"question": "Continue?",
					"options": options,
					"status": "Pending",
				}
			).insert(ignore_permissions=True)
			self.approvals.append(doc.name)
			if source == "File Box":
				frappe.db.set_value(CONV, conv.name, "file_box", 1)
			file = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "qa756-" + conv.name + ".txt",
					"content": "Private QA756 fixture " + conv.name,
					"is_private": 1,
					"attached_to_doctype": CONV,
					"attached_to_name": conv.name,
				}
			).insert(ignore_permissions=True)
			self.files[doc.name] = file.name
		frappe.db.commit()
		return doc.name

	def cleanup_fixtures(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for file in self.files.values():
			if frappe.db.exists("File", file):
				frappe.delete_doc("File", file, ignore_permissions=True, force=True)
		for approval in self.approvals:
			if frappe.db.table_exists(GRANT):
				frappe.db.delete(GRANT, {"approval": approval})
			frappe.db.delete("DocShare", {"share_doctype": AR, "share_name": approval})
			frappe.db.delete("Comment", {"reference_doctype": AR, "reference_name": approval})
			frappe.db.delete(AR, {"name": approval})
		for conv in self.conversations:
			frappe.db.delete("DocShare", {"share_doctype": CONV, "share_name": conv})
			frappe.db.delete("Jarvis Conversation Share", {"conversation": conv})
			frappe.db.delete("Jarvis Chat Message", {"conversation": conv})
			frappe.db.delete("Jarvis Chat Turn", {"conversation": conv})
			frappe.db.delete(CONV, {"name": conv})
		for user in self.users:
			frappe.db.delete("Notification Log", {"for_user": user})
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_untagged_manager_cannot_read_chat_question_but_filebox_is_unchanged(self):
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.get_approval(self.chat)
			self.assertEqual(approvals_api.get_approval(self.filebox)["name"], self.filebox)

	def test_untagged_manager_lists_and_facets_exclude_chat_question(self):
		with as_user(self.manager):
			page = approvals_api.list_approvals_page(search="QA756")
			self.assertNotIn(self.chat, {r["name"] for r in page["rows"]})
			self.assertIn(self.filebox, {r["name"] for r in page["rows"]})
			self.assertNotIn(self.chat, {r["name"] for r in approvals_api.list_approvals()})

	def test_generic_docshare_cannot_grant_chat_review_authority_or_conversation_access(self):
		from frappe.share import add

		with as_user(self.owner):
			with self.assertRaises(frappe.PermissionError):
				add(AR, self.chat, self.reviewer, read=1, flags={"ignore_share_permission": True})
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": self.chat}))
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		self.assertFalse(frappe.has_permission(CONV, "read", doc=conv, user=self.reviewer))

	def test_generic_share_ui_cannot_create_a_chat_review_tag(self):
		with as_user(self.owner):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_share(AR, self.chat, self.reviewer, "add")
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": self.chat}))

	def test_a_chat_question_cannot_switch_its_source_to_bypass_the_gate(self):
		with as_user(self.owner):
			doc = frappe.get_doc(AR, self.chat)
			doc.source = "File Box"
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_a_chat_question_cannot_move_to_a_different_conversation(self):
		with as_user(self.owner):
			doc = frappe.get_doc(AR, self.chat)
			doc.conversation = frappe.db.get_value(AR, self.filebox, "conversation")
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_tagged_eligible_reviewer_can_decide_but_cannot_manage_or_open_chat(self):
		with as_user(self.owner):
			approvals_api.tag_reviewer(self.chat, self.reviewer)
		with as_user(self.reviewer):
			row = approvals_api.get_approval(self.chat)
			self.assertEqual(row["can_act"], 1)
			self.assertEqual(row["can_manage"], 0)
			self.assertFalse(row.get("conversation"))
			self.assertFalse(row.get("origin_page"))
			with self.assertRaises(frappe.PermissionError):
				approvals_api.tag_reviewer(self.chat, self.manager)
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": self.chat}))

	def test_untagged_or_ineligible_reviewer_cannot_self_tag(self):
		for actor in (self.manager, self.reviewer, self.ordinary):
			with self.subTest(actor=actor), as_user(actor):
				with self.assertRaises(frappe.PermissionError):
					approvals_api.tag_reviewer(self.chat, actor)
		with as_user(self.owner):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.tag_reviewer(self.chat, self.ordinary)

	def tag(self, user=None):
		with as_user(self.owner):
			return approvals_api.tag_reviewer(self.chat, user or self.reviewer)

	def test_reviewer_decision_resumes_as_requester_without_reviewers_erp_permissions(self):
		self.tag(self.manager)
		seen = []

		def send(**kwargs):
			seen.append((frappe.session.user, bool(frappe.has_permission("Supplier", "create")), kwargs))
			return {"ok": True}

		self.assertFalse(frappe.has_permission("Supplier", "create", user=self.owner))
		self.assertTrue(frappe.has_permission("Supplier", "create", user=self.manager))
		with as_user(self.manager), patch("jarvis.chat.api.send_message", side_effect=send):
			result = approvals_api.decide(self.chat, "Yes")
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Approved")
		self.assertEqual(len(seen), 1)
		self.assertEqual(seen[0][:2], (self.owner, False))
		self.assertIn(frappe.db.get_value("User", self.manager, "full_name"), seen[0][2]["message"])
		self.assertEqual(frappe.db.get_value(AR, self.chat, "decided_by"), self.manager)

	def test_reviewer_without_options_can_answer_and_replay_does_not_resume_twice(self):
		frappe.db.set_value(AR, self.chat, "options", "[]")
		frappe.db.commit()
		self.tag()
		with as_user(self.reviewer), patch("jarvis.chat.api.send_message", return_value={"ok": True}) as send:
			result = approvals_api.decide(self.chat, "Use the listed supplier")
			self.assertTrue(result["ok"])
			with self.assertRaises(frappe.ValidationError):
				approvals_api.decide(self.chat, "Use the listed supplier")
			send.assert_called_once()

	def test_reviewer_cannot_dismiss_or_restore_requesters_question(self):
		self.tag(self.manager)
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.dismiss_approval(self.chat)
		with as_user(self.owner):
			approvals_api.dismiss_approval(self.chat)
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.restore_approval(self.chat)

	def test_new_grants_do_not_allow_generic_approval_chat_or_file_reads(self):
		self.tag(self.manager)
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		for doctype, name in ((AR, self.chat), (CONV, conv), ("File", self.files[self.chat])):
			with self.subTest(doctype=doctype):
				self.assertFalse(frappe.has_permission(doctype, "read", doc=name, user=self.manager))

	def test_reviewer_role_loss_or_disable_refuses_read_and_decision(self):
		for change in ("role", "disabled"):
			with self.subTest(change=change):
				self.tag()
				with as_user("Administrator"):
					user = frappe.get_doc("User", self.reviewer)
					if change == "role":
						user.set("roles", [r for r in user.roles if r.role != "Jarvis Admin"])
					else:
						user.enabled = 0
					user.save(ignore_permissions=True)
					frappe.db.commit()
				with as_user(self.reviewer):
					with self.assertRaises(frappe.PermissionError):
						approvals_api.get_approval(self.chat)
					with self.assertRaises(frappe.PermissionError):
						approvals_api.decide(self.chat, "Yes")
				with as_user("Administrator"):
					ensure_user(self.reviewer, ("Jarvis User", "Jarvis Admin"))

	def test_untag_revokes_list_badge_detail_comments_and_decision(self):
		with as_user(self.reviewer):
			before = approvals_api._ar_pending_count()
		self.tag()
		with as_user(self.reviewer):
			self.assertEqual(approvals_api._ar_pending_count(), before + 1)
			self.assertIn(self.chat, {r["name"] for r in approvals_api.list_approvals()})
			comment = docmeta_api.add_comment(AR, self.chat, "Please check the supplier")
		with as_user(self.owner):
			approvals_api.untag_reviewer(self.chat, self.reviewer)
		with as_user(self.reviewer):
			self.assertEqual(approvals_api._ar_pending_count(), before)
			with self.assertRaises(frappe.PermissionError):
				approvals_api.get_approval(self.chat)
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.update_comment(comment["name"], "After revocation")
			with self.assertRaises(frappe.PermissionError):
				approvals_api.decide(self.chat, "Yes")

	def test_untagged_manager_cannot_edit_a_comment_on_a_chat_question(self):
		with as_user(self.owner):
			comment = docmeta_api.add_comment(AR, self.chat, "Owner context")
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.update_comment(comment["name"], "Unrequested edit")

	def test_tagged_manager_cannot_delete_requesters_comment(self):
		self.tag(self.manager)
		with as_user(self.owner):
			comment = docmeta_api.add_comment(AR, self.chat, "Owner context")
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.delete_comment(comment["name"])

	def test_generic_grant_create_and_share_cannot_forge_reviewer_authority(self):
		from frappe import client
		from frappe.share import add

		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				client.insert(
					{
						"doctype": GRANT,
						"approval": self.chat,
						"user": self.manager,
						"tagged_by": self.owner,
						"tagged_at": frappe.utils.now_datetime(),
						"flags": {"ignore_permissions": True, "jarvis_server_write": True},
					}
				)
		self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.manager}))
		self.tag(self.manager)
		name = frappe.db.get_value(GRANT, {"approval": self.chat, "user": self.manager}, "name")
		with as_user(self.manager):
			with self.assertRaises(frappe.PermissionError):
				add(GRANT, name, self.ordinary, read=1, flags={"ignore_share_permission": True})

	def test_a_forged_filebox_question_cannot_mirror_access_to_someone_elses_private_chat(self):
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		with as_user(self.manager):
			proxy = frappe.get_doc(
				{
					"doctype": AR,
					"source": "File Box",
					"title": "QA756 proxy",
					"conversation": conv,
					"question": "Proxy?",
					"status": "Pending",
				}
			).insert()
			self.approvals.append(proxy.name)
			frappe.db.commit()
			docmeta_api.toggle_share(AR, proxy.name, self.manager, "add")
		self.assertFalse(frappe.has_permission(CONV, "read", doc=conv, user=self.manager))
		self.assertFalse(frappe.has_permission("File", "read", doc=self.files[self.chat], user=self.manager))

	def test_real_filebox_sharing_still_mirrors_attachment_access(self):
		with as_user(self.manager):
			docmeta_api.toggle_share(AR, self.filebox, self.reviewer, "add")
		conv = frappe.db.get_value(AR, self.filebox, "conversation")
		self.assertTrue(frappe.has_permission(CONV, "read", doc=conv, user=self.reviewer))
		self.assertTrue(
			frappe.has_permission("File", "read", doc=self.files[self.filebox], user=self.reviewer)
		)

	def test_private_question_cannot_switch_into_chat_review_from_another_source(self):
		with as_user(self.owner):
			doc = frappe.get_doc(AR, self.chat)
			frappe.db.set_value(AR, doc.name, "source", "File Box")
			frappe.db.commit()
			doc.reload()
			doc.source = "Chat"
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_inline_chat_style_question_in_real_filebox_keeps_its_existing_policy(self):
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		frappe.db.set_value(CONV, conv, "file_box", 1)
		frappe.db.commit()
		with as_user(self.manager):
			self.assertTrue(approvals_api.get_approval(self.chat)["can_act"])
			docmeta_api.toggle_share(AR, self.chat, self.reviewer, "add")
		self.assertTrue(frappe.has_permission("File", "read", doc=self.files[self.chat], user=self.reviewer))

	def legacy_share(self, doctype, name, user=None, **extra):
		# A pre-upgrade row: today's validate_share intentionally rejects new ones.
		doc = frappe.get_doc(
			{
				"doctype": "DocShare",
				"share_doctype": doctype,
				"share_name": name,
				"user": user or self.reviewer,
				"read": 1,
				"owner": self.owner,
				**extra,
			}
		)
		doc.name = "qa756-" + uuid4().hex[:16]
		doc.db_insert()
		return doc.name

	def test_migration_resets_only_legacy_chat_review_access_and_is_idempotent(self):
		from jarvis.patches.v2_29_chat_approval_reviewers import execute

		conv = frappe.db.get_value(AR, self.chat, "conversation")
		old = self.legacy_share(AR, self.chat)
		mirror = self.legacy_share(CONV, conv)
		unrelated = self.legacy_share(CONV, conv, self.ordinary)
		filebox = self.legacy_share(AR, self.filebox)
		fbconv = frappe.db.get_value(AR, self.filebox, "conversation")
		fbmirror = self.legacy_share(CONV, fbconv)
		first = execute()
		self.assertGreaterEqual(first["approval_shares_reset"], 1)
		for name in (old, mirror):
			self.assertFalse(frappe.db.exists("DocShare", name))
		for name in (unrelated, filebox, fbmirror):
			self.assertTrue(frappe.db.exists("DocShare", name))
		self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat}))
		self.assertEqual(execute(), {"approval_shares_reset": 0, "conversation_shares_reset": 0})

	def test_migration_preserves_real_shared_chat_grants_and_explicit_write_shares(self):
		from jarvis.chat.sharing import share_write
		from jarvis.patches.v2_29_chat_approval_reviewers import execute

		conv = frappe.db.get_value(AR, self.chat, "conversation")
		with as_user(self.owner), share_write():
			frappe.get_doc(
				{
					"doctype": "Jarvis Chat Message",
					"conversation": conv,
					"seq": 1,
					"role": "user",
					"content": "Fixture",
				}
			).insert(ignore_permissions=True)
			grant = frappe.get_doc(
				{
					"doctype": "Jarvis Conversation Share",
					"conversation": conv,
					"user": self.reviewer,
					"shared_by": self.owner,
					"access_mode": "View",
					"access_revision": 1,
				}
			).insert(ignore_permissions=True)
		before = frappe.get_doc(grant.doctype, grant.name).as_dict()
		self.legacy_share(AR, self.chat)
		write_share = self.legacy_share(CONV, conv, write=1)
		execute()
		self.assertEqual(frappe.get_doc(grant.doctype, grant.name).as_dict(), before)
		from jarvis.chat.sharing import _visible_share

		with as_user(self.reviewer):
			self.assertTrue(_visible_share(conv))
		self.assertTrue(frappe.db.exists("DocShare", write_share))

	def test_migration_preserves_inline_filebox_and_collaborative_approval_shares(self):
		from jarvis.patches.v2_29_chat_approval_reviewers import execute

		conv = frappe.db.get_value(AR, self.chat, "conversation")
		frappe.db.set_value(CONV, conv, "file_box", 1)
		inline = self.legacy_share(AR, self.chat)
		execute()
		self.assertTrue(frappe.db.exists("DocShare", inline))
		frappe.db.set_value(CONV, conv, {"file_box": 0, "collaboration_active": 1})
		execute()
		self.assertTrue(frappe.db.exists("DocShare", inline))

	def test_migration_resets_everyone_legacy_rows_without_changing_other_conversations(self):
		from jarvis.patches.v2_29_chat_approval_reviewers import execute

		conv = frappe.db.get_value(AR, self.chat, "conversation")
		old = self.legacy_share(AR, self.chat, everyone=1)
		mirror = self.legacy_share(CONV, conv, everyone=1)
		other = self.legacy_share(CONV, frappe.db.get_value(AR, self.filebox, "conversation"), everyone=1)
		execute()
		for name in (old, mirror):
			self.assertFalse(frappe.db.exists("DocShare", name))
		self.assertTrue(frappe.db.exists("DocShare", other))

	def test_public_share_rpc_cannot_inject_server_flags_to_reach_private_chat(self):
		from frappe.share import add

		conv = frappe.db.get_value(AR, self.chat, "conversation")
		with as_user(self.manager), self.assertRaises(frappe.PermissionError):
			frappe.call(
				add,
				doctype=CONV,
				name=conv,
				user=self.manager,
				read=1,
				flags={"ignore_share_permission": True},
			)
		self.assertFalse(frappe.has_permission(CONV, "read", doc=conv, user=self.manager))

	def test_tag_and_untag_notify_requester_other_tabs_and_reviewer_without_private_ids(self):
		from jarvis.chat import events

		with patch.object(events, "publish_to_user") as publish:
			self.tag()
			self.assertEqual({c.args[0] for c in publish.call_args_list}, {self.owner, self.reviewer})
			publish.reset_mock()
			with as_user(self.owner):
				approvals_api.untag_reviewer(self.chat, self.reviewer)
			self.assertEqual({c.args[0] for c in publish.call_args_list}, {self.owner, self.reviewer})
			for call in publish.call_args_list:
				self.assertEqual(call.args[1], {"kind": "approval:untagged", "name": self.chat})

	def test_role_revocation_removes_grants_and_does_not_reactivate_after_role_restore(self):
		self.tag()
		with as_user("Administrator"):
			user = frappe.get_doc("User", self.reviewer)
			user.set("roles", [r for r in user.roles if r.role != "Jarvis Admin"])
			user.save(ignore_permissions=True)
			frappe.db.commit()
			self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
			ensure_user(self.reviewer, ("Jarvis User", "Jarvis Admin"))
		with as_user(self.reviewer), self.assertRaises(frappe.PermissionError):
			approvals_api.get_approval(self.chat)

	def test_every_terminal_transition_notifies_current_reviewers(self):
		from jarvis.chat import chat_asks, events

		self.tag()
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		for action in ("dismiss", "restore", "decide", "answer"):
			with self.subTest(action=action), patch.object(events, "publish_to_user") as publish:
				with as_user(self.owner):
					if action == "dismiss":
						approvals_api.dismiss_approval(self.chat)
					elif action == "restore":
						approvals_api.restore_approval(self.chat)
					elif action == "decide":
						with patch("jarvis.chat.api.send_message", return_value={"ok": True}):
							approvals_api.decide(self.chat, "Yes")
					else:
						frappe.db.set_value(AR, self.chat, {"status": "Pending", "decision": ""})
						frappe.db.commit()
						publish.reset_mock()
						chat_asks.resolve_on_user_message(conv)
					frappe.db.commit()
				calls = [c for c in publish.call_args_list if c.args[0] == self.reviewer]
				self.assertEqual(len(calls), 1)
				self.assertEqual(calls[0].args[1]["name"], self.chat)
				self.assertNotIn("conversation_id", calls[0].args[1])

	def other_request(self, actor, callback):
		"""Actual independent Frappe connection, matching request teardown."""
		from concurrent.futures import ThreadPoolExecutor

		site, sites_path = frappe.local.site, frappe.local.sites_path

		def run():
			frappe.init(site=site, sites_path=sites_path)
			try:
				frappe.connect()
				frappe.flags.in_test = True
				frappe.set_user(actor)
				result = callback()
				frappe.db.commit()
				return result
			finally:
				frappe.db.rollback()
				frappe.destroy()

		executor = ThreadPoolExecutor(max_workers=1)
		self.addCleanup(executor.shutdown, wait=True)
		return executor.submit(run)

	def test_two_real_requests_can_resume_a_question_only_once(self):
		from threading import Barrier

		from jarvis.chat import approval_reviewers

		self.tag()
		self.tag(self.manager)
		barrier = Barrier(2, timeout=10)
		original = approval_reviewers._locked_user

		def lock(user):
			barrier.wait()
			original(user)

		def decide():
			try:
				result = approvals_api.decide(self.chat, "Yes")["ok"]
				frappe.db.commit()  # mirror a successful HTTP response
				return result
			except frappe.ValidationError:
				frappe.db.rollback()
				return False

		with (
			patch.object(approval_reviewers, "_locked_user", side_effect=lock),
			patch("jarvis.chat.api.send_message", return_value={"ok": True}) as send,
		):
			other = self.other_request(self.manager, decide)
			with as_user(self.reviewer):
				mine = decide()
			self.assertEqual(sorted([mine, other.result(timeout=20)]), [False, True])
			send.assert_called_once()

	def test_committed_untag_on_second_connection_wins_over_stale_read_view(self):
		from jarvis.tests._collaboration_committed import repeatable_read
		from jarvis.tests.race_harness import snapshot_isolation_on

		self.tag()
		with repeatable_read(), snapshot_isolation_on(), as_user(self.reviewer):
			self.assertTrue(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
			self.other_request(
				self.owner, lambda: approvals_api.untag_reviewer(self.chat, self.reviewer)
			).result(timeout=20)
			# Prove this connection still has the pre-revoke snapshot.
			self.assertTrue(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
			with patch("jarvis.chat.api.send_message") as send, self.assertRaises(frappe.PermissionError):
				approvals_api.decide(self.chat, "Yes")
			send.assert_not_called()
		self.assertEqual(frappe.db.get_value(AR, self.chat, "status"), "Pending")

	def test_real_user_save_role_loss_wins_over_stale_decision_snapshot(self):
		from jarvis.tests._collaboration_committed import repeatable_read
		from jarvis.tests.race_harness import snapshot_isolation_on

		self.tag()

		def revoke():
			user = frappe.get_doc("User", self.reviewer)
			user.set("roles", [r for r in user.roles if r.role != "Jarvis Admin"])
			user.save(ignore_permissions=True)

		with repeatable_read(), snapshot_isolation_on(), as_user(self.reviewer):
			self.assertTrue(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
			self.other_request("Administrator", revoke).result(timeout=20)
			self.assertTrue(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
			with patch("jarvis.chat.api.send_message") as send, self.assertRaises(frappe.PermissionError):
				approvals_api.decide(self.chat, "Yes")
			send.assert_not_called()

	def test_requester_answer_before_blocked_reviewer_decision_prevents_second_resume(self):
		from threading import Event

		from jarvis.chat import approval_reviewers, chat_asks

		self.tag()
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		entered = Event()
		original = approval_reviewers._locked_user
		frappe.db.sql("SELECT name FROM `tabJarvis Approval Request` WHERE name=%s FOR UPDATE", self.chat)

		def lock(user):
			original(user)
			entered.set()

		with (
			patch.object(approval_reviewers, "_locked_user", side_effect=lock),
			patch("jarvis.chat.api.send_message") as send,
		):
			other = self.other_request(self.reviewer, lambda: approvals_api.decide(self.chat, "Yes"))
			self.assertTrue(entered.wait(timeout=10))
			with as_user(self.owner):
				chat_asks.resolve_on_user_message(conv)
			with self.assertRaises(frappe.ValidationError):
				other.result(timeout=20)
			send.assert_not_called()
		self.assertEqual(frappe.db.get_value(AR, self.chat, "status"), "Answered")

	def test_deleting_reviewer_removes_server_grants(self):
		self.tag()
		with as_user("Administrator"):
			frappe.delete_doc("User", self.reviewer, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))

	def test_deleting_question_removes_its_grants_without_blocking_delete(self):
		self.tag()
		with as_user("Administrator"):
			frappe.delete_doc(AR, self.chat, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat}))

	def test_deleting_requester_removes_grants_they_issued(self):
		self.tag()
		with as_user("Administrator"):
			frappe.delete_doc("User", self.owner, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(GRANT, {"approval": self.chat}))

	def test_generic_list_count_get_and_status_write_do_not_bypass_review_grants(self):
		from frappe import client

		self.tag(self.manager)
		for actor in (self.manager, self.reviewer):
			with self.subTest(actor=actor), as_user(actor):
				self.assertEqual(frappe.get_list(AR, filters={"name": self.chat}), [])
				self.assertEqual(client.get_count(AR, filters={"name": self.chat}), 0)
				with self.assertRaises(frappe.PermissionError):
					client.get(AR, self.chat)
				with self.assertRaises(frappe.PermissionError):
					client.set_value(AR, self.chat, "status", "Approved")
		self.assertEqual(frappe.db.get_value(AR, self.chat, "status"), "Pending")

	def test_stock_docshare_insert_everyone_and_share_tool_cannot_create_review_access(self):
		from frappe import client
		from frappe.share import add

		from jarvis.tools.share_doc import share_doc

		for actor in (self.owner, self.manager):
			with self.subTest(actor=actor), as_user(actor):
				for operation in (
					lambda: client.insert(
						{
							"doctype": "DocShare",
							"share_doctype": AR,
							"share_name": self.chat,
							"everyone": 1,
							"read": 1,
						}
					),
					lambda: add(AR, self.chat, everyone=1),
					lambda: share_doc(doctype=AR, name=self.chat, user=self.reviewer),
				):
					with self.assertRaises(frappe.PermissionError):
						operation()
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": self.chat}))

	def test_reviewer_paged_list_total_facets_and_badge_agree_without_private_navigation(self):
		self.tag()
		with as_user(self.reviewer):
			page = approvals_api.list_approvals_page(search="QA756", filters={"status": "Pending"})
			self.assertEqual(page["total"], 1)
			self.assertEqual(len(page["rows"]), 1)
			row = page["rows"][0]
			self.assertEqual(row["name"], self.chat)
			self.assertEqual(row["requester"], self.owner)
			self.assertEqual(row["requester_name"], frappe.db.get_value("User", self.owner, "full_name"))
			self.assertEqual((row["review_scoped"], row["shared"]), (1, 1))
			self.assertFalse(row.get("conversation"))
			self.assertFalse(row.get("origin_page"))
			self.assertGreaterEqual(approvals_api._ar_pending_count(), 1)

	def test_conversationless_chat_question_uses_its_owner_as_requester(self):
		with as_user(self.owner):
			doc = frappe.get_doc(
				{
					"doctype": AR,
					"source": "Chat",
					"title": "QA756 orphan",
					"question": "Which account?",
					"status": "Pending",
				}
			).insert(ignore_permissions=True)
			self.approvals.append(doc.name)
			frappe.db.commit()
			approvals_api.tag_reviewer(doc.name, self.reviewer)
			page = approvals_api.list_approvals_page(search="QA756 orphan")
			self.assertEqual(page["rows"][0]["shared"], 0)
			self.assertTrue(approvals_api.get_approval(doc.name)["is_requester"])
		with as_user(self.reviewer), patch("jarvis.chat.api.send_message") as send:
			self.assertEqual(approvals_api.get_approval(doc.name)["requester"], self.owner)
			self.assertTrue(approvals_api.decide(doc.name, "Use the expense account")["ok"])
			send.assert_not_called()

	def test_removing_legacy_nonchat_share_also_removes_its_old_private_conversation_mirror(self):
		conv = frappe.db.get_value(AR, self.chat, "conversation")
		frappe.db.set_value(AR, self.chat, "source", "File Box")
		self.legacy_share(AR, self.chat)
		self.legacy_share(CONV, conv)
		frappe.db.commit()
		with as_user(self.manager):
			docmeta_api.toggle_share(AR, self.chat, self.reviewer, "remove")
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": CONV, "share_name": conv}))

	def test_generic_comment_list_does_not_leak_question_comments_to_untagged_manager(self):
		with as_user(self.owner):
			docmeta_api.add_comment(AR, self.chat, "QA756 private comment")
		with as_user(self.manager):
			rows = frappe.get_list(
				"Comment",
				filters={"reference_doctype": AR, "reference_name": self.chat},
				fields=["name", "content"],
			)
		self.assertEqual(rows, [])

	def test_generic_comment_scope_keeps_requester_and_filebox_reads(self):
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		with as_user(self.owner):
			own = docmeta_api.add_comment(AR, self.chat, "QA756 requester context")
			filebox = docmeta_api.add_comment(AR, self.filebox, "QA756 FileBox context")
			self.assertEqual(
				frappe.get_list("Comment", filters={"name": own["name"]}, pluck="name"), [own["name"]]
			)
		with as_user(self.manager):
			self.assertEqual(
				frappe.get_list("Comment", filters={"name": filebox["name"]}, pluck="name"), [filebox["name"]]
			)

	def test_no_generic_grant_oversight_even_for_untagged_system_manager(self):
		from frappe import client
		from frappe.desk.form.load import getdoc

		self.tag()
		name = frappe.db.get_value(GRANT, {"approval": self.chat, "user": self.reviewer}, "name")
		for actor in (self.owner, self.reviewer, self.manager, self.ordinary):
			with self.subTest(actor=actor), as_user(actor):
				for ptype in ("read", "write", "create", "delete", "share", "email", "print", "report"):
					self.assertFalse(frappe.has_permission(GRANT, ptype, doc=name), ptype)
				for operation, empty in (
					(lambda: client.get_list(GRANT, filters={"name": name}), []),
					(lambda: client.get_count(GRANT, filters={"name": name}), 0),
				):
					try:
						result = operation()
					except frappe.PermissionError:
						continue
					self.assertEqual(result, empty)
				with self.assertRaises(frappe.PermissionError):
					client.get(GRANT, name)
				with self.assertRaises(frappe.PermissionError):
					getdoc(GRANT, name)
		with as_user("Administrator"):
			self.assertEqual(client.get(GRANT, name)["name"], name)
			self.assertEqual(client.get_count(GRANT, filters={"name": name}), 1)

	def test_requester_can_revoke_read_and_comment_access_after_a_question_is_settled(self):
		self.tag()
		with as_user(self.reviewer), patch("jarvis.chat.api.send_message", return_value={"ok": True}):
			approvals_api.decide(self.chat, "Yes")
			self.assertEqual(approvals_api.get_approval(self.chat)["status"], "Approved")
			comment = docmeta_api.add_comment(AR, self.chat, "Post-decision context")
		with as_user(self.owner):
			approvals_api.untag_reviewer(self.chat, self.reviewer)
		with as_user(self.reviewer):
			with self.assertRaises(frappe.PermissionError):
				approvals_api.get_approval(self.chat)
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.update_comment(comment["name"], "After read access ended")

	def test_noop_untag_does_not_publish_to_an_unrelated_account(self):
		with as_user(self.owner), patch("jarvis.chat.events.publish_to_user") as publish:
			approvals_api.untag_reviewer(self.chat, self.ordinary)
			publish.assert_not_called()

	def comment_state(self, *names):
		return {
			name: frappe.db.get_value(
				"Comment", name, ["content", "reference_doctype", "reference_name"], as_dict=True
			)
			for name in names
		}

	def assert_generic_comment_writes_denied(self, private, moved):
		"""Every stock generic Comment write a System Manager has by role."""
		from frappe import client

		attempts = {
			"set_value content": lambda: client.set_value("Comment", private, "content", "QA756 overwritten"),
			"set_value move out": lambda: client.set_value(
				"Comment", private, {"reference_name": self.filebox}
			),
			"save with forged flags": lambda: frappe.call(
				client.save,
				doc={
					**frappe.get_doc("Comment", private).as_dict(),
					"content": "QA756 saved",
					"flags": {"ignore_permissions": True},
				},
			),
			"delete": lambda: client.delete("Comment", private),
			"insert": lambda: client.insert(
				{
					"doctype": "Comment",
					"comment_type": "Comment",
					"reference_doctype": AR,
					"reference_name": self.chat,
					"content": "QA756 forged",
				}
			),
			"set_value move in": lambda: client.set_value("Comment", moved, {"reference_name": self.chat}),
			"share to self": lambda: client.insert(
				{
					"doctype": "DocShare",
					"share_doctype": "Comment",
					"share_name": private,
					"user": frappe.session.user,
					"read": 1,
					"write": 1,
				}
			),
		}
		for label, attempt in attempts.items():
			try:
				with self.subTest(attempt=label), self.assertRaises(frappe.PermissionError):
					attempt()
			finally:
				frappe.db.rollback()  # each attempt starts from the committed fixture

	def test_untagged_manager_cannot_generically_rewrite_move_delete_or_forge_question_comments(self):
		from frappe import client

		with as_user(self.owner):
			private = docmeta_api.add_comment(AR, self.chat, "QA756 requester private context")["name"]
		with as_user(self.manager):
			moved = docmeta_api.add_comment(AR, self.filebox, "QA756 manager File Box note")["name"]
		before = self.comment_state(private, moved)
		with as_user(self.manager):
			self.assert_generic_comment_writes_denied(private, moved)
		self.assertEqual(self.comment_state(private, moved), before)
		self.assertEqual(
			frappe.get_all(
				"Comment", filters={"reference_doctype": AR, "reference_name": self.chat}, pluck="name"
			),
			[private],
		)
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "Comment", "share_name": private}))
		# File Box comments keep the stock System Manager policy; root is unaffected
		with as_user(self.manager):
			self.assertEqual(
				client.set_value("Comment", moved, "content", "QA756 edited")["reference_name"], self.filebox
			)
		with as_user("Administrator"):
			client.set_value("Comment", private, "content", "QA756 root edit")
		self.assertEqual(frappe.db.get_value("Comment", private, "content"), "QA756 root edit")

	def test_untagged_reviewer_keeps_no_generic_write_on_comments_learned_while_tagged(self):
		self.tag(self.manager)
		with as_user(self.owner):
			docmeta_api.add_comment(AR, self.chat, "QA756 requester note")
		with as_user(self.manager):
			own = docmeta_api.add_comment(AR, self.chat, "QA756 reviewer note")["name"]
			docmeta_api.update_comment(own, "QA756 reviewer edited")
			known = [c["name"] for c in docmeta_api.get_docmeta(AR, self.chat)["comments"]]
			moved = docmeta_api.add_comment(AR, self.filebox, "QA756 manager File Box note")["name"]
		self.assertEqual(len(known), 2)
		with as_user(self.owner):
			approvals_api.untag_reviewer(self.chat, self.manager)
		before = self.comment_state(*known, moved)
		with as_user(self.manager):
			for name in known:
				self.assert_generic_comment_writes_denied(name, moved)
		self.assertEqual(self.comment_state(*known, moved), before)

	def test_requester_keeps_generic_comment_access_and_reviewer_api_comments_still_work(self):
		from frappe import client

		self.tag()
		with as_user(self.reviewer):
			own = docmeta_api.add_comment(AR, self.chat, "QA756 reviewer note")["name"]
			docmeta_api.update_comment(own, "QA756 reviewer edited")
			docmeta_api.delete_comment(own)
		self.assertFalse(frappe.db.exists("Comment", own))
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		with as_user(self.owner):
			mine = docmeta_api.add_comment(AR, self.chat, "QA756 requester note")["name"]
			client.set_value("Comment", mine, "content", "QA756 requester edited")
			self.assertEqual(client.get("Comment", mine)["content"], "QA756 requester edited")
			client.delete("Comment", mine)
		self.assertFalse(frappe.db.exists("Comment", mine))
