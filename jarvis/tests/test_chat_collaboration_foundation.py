"""C1 compatibility/security tests; run only on the disposable test site."""

from unittest.mock import patch

import frappe

from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user, payload
from jarvis.tests.test_chat_sharing import SharingCase


class TestCollaborationFoundation(SharingCase):
	def mark_conversation(self):
		frappe.db.set_value("Jarvis Conversation", self.conversation, "collaboration_active", 1)

	def session(self, key="agent:main:dashboard:legacy-test", **values):
		return frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": key, "user": OWNER, **values}
		).insert(ignore_permissions=True)

	def test_private_owner_history_characterization(self):
		from jarvis.chat.api import get_conversation

		with as_user(OWNER):
			original_title = frappe.db.get_value("Jarvis Conversation", self.conversation, "title")
			before = payload(get_conversation(self.conversation))
			# Golden contract read from 00cd15613; dynamic IDs/timestamps remain
			# asserted by the existing owner-response characterization suite.
			golden = frappe.parse_json(before)
			self.assertEqual(
				set(golden["conversation"]),
				{
					"name",
					"title",
					"status",
					"session_key",
					"model_override",
					"thinking_override",
					"origin_page",
					"auto_mode",
					"file_box",
					"last_active_at",
				},
			)
			self.assertEqual(
				set(golden["messages"][0]),
				{
					"name",
					"seq",
					"role",
					"content",
					"streaming",
					"error",
					"recovering",
					"stopped",
					"tool_name",
					"tool_args",
					"tool_result",
					"tool_status",
					"action_outcome",
					"tool_call_id",
					"tool_call_ids",
					"pending_card",
					"expires_at",
					"canvas",
					"steps",
					"reply_duration_ms",
					"model",
					"provider",
					"ref_doctype",
					"ref_name",
					"creation",
					"modified",
				},
			)
			self.assertEqual(golden["messages"][0]["content"], "Shared transcript sentinel")
			frappe.db.set_value("Jarvis Conversation", self.conversation, "title", "Renamed")
			after = get_conversation(self.conversation)
			after["conversation"]["title"] = original_title
			# New foundation metadata must not change the private response contract.
			self.assertEqual(before, payload(after))
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			get_conversation(self.conversation)

	def test_existing_grants_default_to_view_and_membership_client_preserves_chat(self):
		from jarvis.chat import collaboration as c
		from jarvis.chat import sharing

		self.share()
		row = frappe.get_doc(SHARE, {"conversation": self.conversation, "user": RECIPIENT})
		self.assertEqual((row.access_mode, row.access_revision), ("View", 1))
		frappe.db.set_value(SHARE, row.name, {"access_mode": "Chat", "access_revision": 2})
		with as_user(OWNER):
			before = sharing.get_shares(self.conversation)
			sharing.set_shares(self.conversation, [RECIPIENT])
			self.assertEqual(before, sharing.get_shares(self.conversation))
		self.assertEqual(frappe.db.get_value(SHARE, row.name, "access_mode"), "Chat")
		for mode in (None, "", "chat", "Write", 1):
			self.assertEqual(c.effective_mode(mode), "View")

	def test_rollout_stays_disabled_even_if_flag_is_manually_enabled(self):
		from jarvis.chat import collaboration as c

		self.assertFalse(c.writes_ready())
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 1, "chat_collaboration_reader_floor": 1}):
			self.assertFalse(c.writes_ready())
			with self.assertRaises(frappe.PermissionError):
				c.require_writes_ready()

	def test_legacy_session_unchanged_but_reserved_namespace_never_falls_back(self):
		from jarvis.chat import collaboration as c

		row = self.session()
		self.assertEqual(c.legacy_session_user(row.session_key), OWNER)
		key = "agent:main:dashboard:collab:opaque"
		# Simulate a partially migrated/rebuilt auth row, bypassing the ORM guard.
		frappe.db.set_value(row.doctype, row.name, "session_key", key)
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(key)

	def test_marked_session_without_binding_fails_closed(self):
		from jarvis.chat import collaboration as c

		row = self.session()
		frappe.db.set_value(row.doctype, row.name, "collaboration_binding", "missing-binding")
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(row.session_key)
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(row.session_key)

	def test_global_owner_session_is_retired_by_durable_thread_marker(self):
		from jarvis.chat import collaboration as c

		row = self.session()
		frappe.db.set_value("Jarvis Conversation", self.conversation, "session_key", row.session_key)
		self.mark_conversation()
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(row.session_key)

	def test_owner_raw_reads_and_legacy_history_are_refused_for_marked_threads(self):
		from jarvis.chat import chat_permissions
		from jarvis.chat.api import get_conversation

		self.mark_conversation()
		message = frappe.get_doc("Jarvis Chat Message", self.message)
		with as_user(OWNER):
			self.assertFalse(chat_permissions.has_message_permission(message, user=OWNER))
			self.assertEqual(frappe.get_list(message.doctype, filters={"name": message.name}), [])
			with self.assertRaises(frappe.PermissionError):
				get_conversation(self.conversation)

	def test_generic_writes_cannot_forge_or_clear_thread_marker(self):
		with as_user(OWNER):
			doc = frappe.get_doc("Jarvis Conversation", self.conversation)
			doc.collaboration_active = 1
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		self.mark_conversation()
		with as_user("Administrator"):
			doc = frappe.get_doc("Jarvis Conversation", self.conversation)
			doc.collaboration_active = 0
			with self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)

	def test_generic_share_mode_or_revision_mutation_is_denied(self):
		self.share()
		from jarvis.chat.sharing import share_write

		with as_user(OWNER), share_write():
			for field, value in (("access_mode", "Chat"), ("access_revision", 20)):
				doc = frappe.get_doc(SHARE, {"conversation": self.conversation, "user": RECIPIENT})
				doc.set(field, value)
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)

	def test_binding_insertion_requires_trusted_service_and_reader_floor(self):
		from jarvis.chat import collaboration as c

		values = dict(
			doctype=c.BINDING,
			conversation=self.conversation,
			actor=OWNER,
			logical_run_id="a" * 32,
			session_key="agent:main:dashboard:collab:test",
			conversation_epoch=1,
		)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(values).insert(ignore_permissions=True)
		doc = frappe.get_doc(values)
		doc.flags.jarvis_collaboration_write = True
		with self.assertRaises(frappe.PermissionError):
			doc.insert(ignore_permissions=True)

	def test_title_compaction_and_enqueue_do_not_use_global_owner_session(self):
		from jarvis.chat import compaction, title
		from jarvis.chat.api import _enqueue_turn

		self.mark_conversation()
		with patch("frappe.enqueue") as enqueue, patch.object(title, "_generate_via_gateway") as generate:
			title.enqueue_autotitle(self.conversation, OWNER)
			title.maybe_autotitle(
				self.conversation, OWNER, gateway_url="ws://invalid", model=None, provider=None
			)
			generate.assert_not_called()
			enqueue.assert_not_called()
			with self.assertRaises(frappe.PermissionError):
				compaction.start_compaction(self.conversation, OWNER, "")
			with self.assertRaises(frappe.PermissionError):
				_enqueue_turn(self.conversation, "do work", origin="continuation")

	def test_migration_twice_preserves_modes_and_revisions(self):
		from jarvis.patches.v2_28_chat_collaboration_foundation import execute

		self.share()
		row = frappe.get_doc(SHARE, {"conversation": self.conversation, "user": RECIPIENT})
		frappe.db.set_value(SHARE, row.name, {"access_mode": "Chat", "access_revision": 7})
		execute()
		execute()
		self.assertEqual(
			frappe.db.get_value(SHARE, row.name, ["access_mode", "access_revision"]), ("Chat", 7)
		)

	def test_rollback_floor_refuses_retained_bindings(self):
		from jarvis.chat import collaboration as c

		with patch.object(c, "has_binding_evidence", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				c.assert_rollback_floor(0)
			c.assert_rollback_floor(1)

	def binding(self, *, actor=OWNER):
		from jarvis.chat import collaboration as c

		frappe.db.set_value(
			c.CONVERSATION, self.conversation, {"collaboration_active": 1, "collaboration_epoch": 1}
		)
		share = None
		if actor != OWNER:
			self.share()
			share = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": actor}, "name")
			frappe.db.set_value(SHARE, share, {"access_mode": "Chat", "access_revision": 1})
		doc = frappe.get_doc(
			dict(
				doctype=c.BINDING,
				conversation=self.conversation,
				actor=actor,
				logical_run_id="a" * 32,
				session_key="agent:main:dashboard:collab:test",
				conversation_epoch=1,
				share=share,
				grant_revision=1 if share else 0,
			)
		)
		doc.flags.jarvis_collaboration_write = True
		with patch.dict(frappe.conf, {"chat_collaboration_reader_floor": 1}):
			doc.insert(ignore_permissions=True)
		auth = frappe.get_doc(
			dict(doctype=c.SESSION, session_key=doc.session_key, user=actor, collaboration_binding=doc.name)
		)
		auth.flags.jarvis_collaboration_write = True
		auth.insert(ignore_permissions=True)
		return doc, auth

	def test_binding_resolves_actor_without_authorizing_execution(self):
		from jarvis.chat import collaboration as c

		row, auth = self.binding()
		result = c.resolve_binding(row.session_key)
		self.assertEqual(
			(result.actor, result.conversation, result.logical_run_id),
			(OWNER, self.conversation, row.logical_run_id),
		)
		self.assertFalse(c.writes_ready())
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(auth.session_key)

	def test_binding_actor_key_and_identity_cannot_be_changed_even_by_admin(self):
		from jarvis.chat import collaboration as c

		row, auth = self.binding()
		for doctype, name, field, value in (
			(c.BINDING, row.name, "actor", RECIPIENT),
			(c.BINDING, row.name, "conversation_epoch", 2),
			(c.SESSION, auth.name, "user", RECIPIENT),
			(c.SESSION, auth.name, "session_key", "agent:main:dashboard:ordinary"),
			(c.SESSION, auth.name, "collaboration_binding", None),
		):
			doc = frappe.get_doc(doctype, name)
			doc.flags.jarvis_collaboration_write = True
			doc.set(field, value)
			with (
				patch.dict(frappe.conf, {"chat_collaboration_reader_floor": 1}),
				self.assertRaises(frappe.PermissionError),
			):
				doc.save(ignore_permissions=True)

	def test_missing_auth_or_corrupt_binding_cannot_be_reinterpreted_as_legacy(self):
		from jarvis.chat import collaboration as c

		row, auth = self.binding()
		frappe.db.delete(c.SESSION, {"name": auth.name})
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(row.session_key)
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(row.session_key)

	def test_downgrade_revoke_and_regrant_fence_old_binding(self):
		from jarvis.chat import collaboration as c

		# Create the grant before the C1 legacy-owner gate retires the thread.
		self.share()
		grant = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": RECIPIENT}, "name")
		frappe.db.set_value(SHARE, grant, {"access_mode": "Chat", "access_revision": 1})
		# binding() needs no public sharing call for this already granted recipient.
		with patch.object(self, "share"):
			row, _auth = self.binding(actor=RECIPIENT)
		self.assertEqual(c.resolve_binding(row.session_key).actor, RECIPIENT)
		for values in ({"access_mode": "View"}, {"access_mode": "Chat", "access_revision": 2}):
			frappe.db.set_value(SHARE, grant, values)
			with self.assertRaises(frappe.PermissionError):
				c.resolve_binding(row.session_key)
		frappe.db.delete(SHARE, {"name": grant})
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(row.session_key)

	def test_epoch_or_retired_state_blocks_resolver_and_rollback(self):
		from jarvis.chat import collaboration as c

		row, _ = self.binding()
		for values in (
			{"state": "Uncertain"},
			{"state": "Retired"},
			{"state": "Active", "conversation_epoch": 2},
		):
			frappe.db.set_value(c.BINDING, row.name, values)
			with self.assertRaises(frappe.PermissionError):
				c.resolve_binding(row.session_key)
		with self.assertRaises(frappe.PermissionError):
			c.assert_rollback_floor(0)

	def test_mixed_schema_refuses_shared_resolution(self):
		from jarvis.chat import collaboration as c

		with patch.object(c, "schema_ready", return_value=False), self.assertRaises(frappe.PermissionError):
			c.resolve_binding("agent:main:dashboard:collab:opaque")

	def test_migration_backfills_populated_grants_without_upgrading(self):
		from jarvis.patches.v2_28_chat_collaboration_foundation import execute

		self.share()
		grant = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": RECIPIENT}, "name")
		frappe.db.sql(
			"UPDATE `tabJarvis Conversation Share` SET access_mode=NULL, access_revision=0 WHERE name=%s",
			grant,
		)
		execute()
		execute()
		self.assertEqual(frappe.db.get_value(SHARE, grant, ["access_mode", "access_revision"]), ("View", 1))

	def test_orphan_sweep_preserves_binding_auth_and_ledger(self):
		from unittest.mock import MagicMock

		from jarvis.chat import collaboration as c
		from jarvis.chat import session_lifecycle

		row, auth = self.binding()
		gateway = MagicMock()
		self.assertFalse(session_lifecycle._delete_gateway_session(gateway, row.session_key, {"errors": 0}))
		self.assertFalse(
			session_lifecycle.reclaim_throwaway_session(gateway, row.session_key, logger_name="jarvis")
		)
		gateway.delete_session.assert_not_called()
		self.assertTrue(frappe.db.exists(c.BINDING, row.name))
		self.assertTrue(frappe.db.exists(c.SESSION, auth.name))

	def test_trusted_message_writer_still_cannot_stamp_mismatched_actor(self):
		row, _auth = self.binding()
		doc = frappe.get_doc(
			dict(
				doctype="Jarvis Chat Message",
				conversation=self.conversation,
				seq=2,
				role="user",
				content="not this actor",
				actor=RECIPIENT,
				collaboration_binding=row.name,
			)
		)
		doc.flags.jarvis_server_write = True
		doc.flags.jarvis_collaboration_write = True
		with self.assertRaises(frappe.PermissionError):
			doc.insert(ignore_permissions=True)

	def test_session_queries_hide_marked_auth_from_system_manager(self):
		from jarvis.tests._share_helpers import MANAGER

		row, auth = self.binding()
		with as_user(MANAGER):
			self.assertEqual(frappe.get_list(auth.doctype, filters={"name": auth.name}), [])
			self.assertFalse(frappe.has_permission(auth.doctype, "read", doc=auth))

	def test_plugin_refusal_occurs_before_any_tool_dispatch(self):
		from jarvis import api

		row, _auth = self.binding()
		with (
			patch.object(
				api,
				"_get_header",
				side_effect=lambda name: "test-token" if name == "X-Jarvis-Token" else None,
			),
			patch.object(api, "validate_plugin_request", return_value=row.session_key),
			patch.object(api, "_request_body_bytes", return_value=b"{}"),
			patch.object(api, "_dispatch_from_session") as dispatch,
		):
			result = api.call_tool("recall", {})
			self.assertFalse(result["ok"])
			self.assertEqual(frappe.local.response.http_status_code, 403)
			dispatch.assert_not_called()

	def test_pre_migration_session_and_share_controllers_accept_legacy_rows(self):
		from jarvis.jarvis.doctype.jarvis_chat_session.jarvis_chat_session import JarvisChatSession
		from jarvis.jarvis.doctype.jarvis_conversation_share.jarvis_conversation_share import (
			JarvisConversationShare,
		)

		# New code can arrive before model sync. Invoke the real validators with
		# document fields absent, as they are under pre-C1 metadata.
		row = JarvisChatSession({"doctype": "Jarvis Chat Session", "session_key": "legacy", "user": OWNER})
		row.__dict__.pop("collaboration_binding", None)
		row.set("__islocal", 1)
		row.validate()
		share = JarvisConversationShare(
			{"doctype": SHARE, "conversation": self.conversation, "user": RECIPIENT, "shared_by": OWNER}
		)
		share.__dict__.pop("access_mode", None)
		share.__dict__.pop("access_revision", None)
		share.set("__islocal", 1)
		from jarvis.chat.sharing import share_write

		with as_user(OWNER), share_write(), patch.object(share.meta, "has_field", return_value=False):
			share.validate()

	def test_owner_can_revoke_marked_thread_using_membership_api(self):
		from jarvis.chat import sharing

		self.share()
		with patch.object(self, "share"):
			row, _auth = self.binding(actor=RECIPIENT)
		with as_user(OWNER):
			self.assertEqual(len(sharing.get_shares(self.conversation)["users"]), 1)
			sharing.set_shares(self.conversation, [])
		self.assertFalse(frappe.db.exists(SHARE, row.share))
		self.assertTrue(frappe.db.exists("Jarvis Chat Binding", row.name))

	def test_admin_can_revoke_binding_backed_grant(self):
		from jarvis.chat import sharing
		from jarvis.tests._share_helpers import MANAGER

		self.share()
		with patch.object(self, "share"):
			row, _auth = self.binding(actor=RECIPIENT)
		with as_user(MANAGER):
			sharing.revoke_share(row.share)
		self.assertFalse(frappe.db.exists(SHARE, row.share))
		self.assertTrue(frappe.db.exists("Jarvis Chat Binding", row.name))

	def test_rollback_to_c1_requires_collaborative_turns_drained(self):
		from jarvis.chat import collaboration as c

		row, _auth = self.binding()
		turn = frappe.get_doc(
			dict(
				doctype="Jarvis Chat Turn",
				run_id="b" * 32,
				conversation=self.conversation,
				relay_target_id="collab-foundation-test",
				seed_message=self.message,
				state="ready",
				actor=OWNER,
				collaboration_binding=row.name,
			)
		)
		turn.flags.jarvis_collaboration_write = True
		turn.insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			c.assert_rollback_floor(1)
		frappe.db.set_value(turn.doctype, turn.name, "state", "done")
		c.assert_rollback_floor(1)

	def test_pending_action_authorization_refuses_marked_thread_before_claim(self):
		from jarvis.chat.pending_actions._execute import authorize

		self.mark_conversation()
		row = frappe._dict(
			conversation=self.conversation, owner_user=OWNER, exec_user=OWNER, tool="create_doc", kind="chat"
		)
		allowed, _adopt = authorize(row, OWNER, "chat", self.conversation)
		self.assertFalse(allowed)

	def test_owner_archive_removes_grants_but_retains_binding_evidence(self):
		from jarvis.chat.api import archive_conversation

		self.share()
		with patch.object(self, "share"):
			row, _auth = self.binding(actor=RECIPIENT)
		with as_user(OWNER), patch.object(frappe.db, "commit"):
			archive_conversation(self.conversation)
		self.assertEqual(frappe.db.get_value("Jarvis Conversation", self.conversation, "status"), "Archived")
		self.assertFalse(frappe.db.exists(SHARE, row.share))
		self.assertTrue(frappe.db.exists("Jarvis Chat Binding", row.name))

	def test_retired_binding_cannot_be_reactivated(self):
		row, _auth = self.binding()
		frappe.db.set_value(row.doctype, row.name, "state", "Retired")
		row.reload()
		row.state = "Active"
		row.flags.jarvis_collaboration_write = True
		with (
			patch.dict(frappe.conf, {"chat_collaboration_reader_floor": 1}),
			self.assertRaises(frappe.PermissionError),
		):
			row.save(ignore_permissions=True)

	def test_pending_action_executor_does_not_claim_or_spend_card_after_activation(self):
		from jarvis.chat import pending_actions as pa

		frappe.get_doc("Jarvis Conversation", self.conversation, for_update=True)
		# The real fixture writer commits at park completion. Keep fixture
		# creation inside this suite's savepoint; the executor is unpatched.
		with patch.object(frappe.db, "commit"):
			name = pa.park(
				kind="chat",
				owner_user=OWNER,
				exec_user=OWNER,
				tool="add_comment",
				args={
					"reference_doctype": "ToDo",
					"reference_name": "not-executed",
					"content": "private-action-sentinel",
				},
				conversation=self.conversation,
				summary="Pending comment",
				card={},
				preview={},
				locked=True,
			)
		self.mark_conversation()
		before = frappe.db.get_value("Jarvis Pending Action", name, ["status", "sealed_call"])
		with as_user(OWNER), patch("jarvis.api.dispatch_confirmed") as dispatch:
			for batch in (False, True):
				result = pa.execute(
					name,
					conversation=self.conversation,
					defer_continuation=batch,
					batch_id="test-batch" if batch else None,
				)
				self.assertFalse(result["ok"])
			dispatch.assert_not_called()
		self.assertEqual(
			frappe.db.get_value("Jarvis Pending Action", name, ["status", "sealed_call"]), before
		)

	def test_numeric_zero_cannot_forge_server_owned_actor_stamp(self):
		doc = frappe.get_doc(
			dict(
				doctype="Jarvis Chat Message",
				conversation=self.conversation,
				seq=2,
				role="user",
				content="malformed actor",
				actor=0,
			)
		)
		doc.flags.jarvis_server_write = True
		with self.assertRaises(frappe.PermissionError):
			doc.insert(ignore_permissions=True)

	def test_marked_operator_review_is_refused_before_decision_or_resumption(self):
		from jarvis.chat import approvals_api

		self.mark_conversation()
		doc = frappe.get_doc(
			dict(
				doctype=approvals_api.APPROVAL,
				name="collaboration-review-test",
				conversation=self.conversation,
				assessment_key="collaboration-review-test",
				status="Pending",
			)
		)
		with (
			as_user("Administrator"),
			patch.object(frappe, "get_doc", return_value=doc),
			patch("jarvis.chat.operator_review.check_review") as decide,
			patch.object(approvals_api, "_resume_conversation") as resume,
			self.assertRaises(frappe.PermissionError),
		):
			approvals_api.decide(doc.name, "Review acknowledged")
		decide.assert_not_called()
		resume.assert_not_called()
		self.assertEqual(doc.status, "Pending")
