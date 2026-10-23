"""Actor ledger and trusted settlement on the disposable test site."""

from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user


class TestCollaborationBindings(CollaborationCase):
	def test_role_profile_selection_does_not_reuse_a_removed_cached_role(self):
		original = frappe.get_cached_doc
		settings = frappe._dict(frappe.get_single("Jarvis Settings").as_dict(), enable_role_profiles=1)
		choice = frappe._dict(agent_id="role-qa", tier="full", set_keys=[], skills=[], n_tools=40)
		frappe.get_doc("User", RECIPIENT).add_roles("Accounts User")
		self.assertIn("Accounts User", frappe.get_roles(RECIPIENT))
		frappe.db.delete("Has Role", {"parent": RECIPIENT, "role": "Accounts User"})

		def cached(doctype, *args, **kwargs):
			return settings if doctype == "Jarvis Settings" else original(doctype, *args, **kwargs)

		def profile(user):
			self.assertNotIn("Accounts User", frappe.get_roles(user))
			return choice

		with (
			patch("frappe.get_cached_doc", side_effect=cached),
			patch("jarvis.chat.api._pushed_profile_slugs", return_value={"role-qa"}),
			patch("jarvis.chat.role_profiles.resolve_profile", side_effect=profile),
		):
			binding = self.binding()
		self.assertIn("agent:role-qa:", binding.session_key)

	def test_first_activation_refuses_uninventoried_or_live_legacy_queue_work(self):
		for result in ((1, ["legacy-job"], True), (0, [], False)):
			with (
				self.subTest(result=result),
				patch("jarvis.chat.admission._legacy_turn_jobs", return_value=result),
				self.assertRaises(frappe.ValidationError),
			):
				self.binding()
		self.assertFalse(c.is_collaborative(self.conversation))

	def test_new_binding_and_auth_have_same_recipient_and_opaque_session(self):
		binding = self.binding()
		auth = frappe.db.get_value(
			c.SESSION, {"session_key": binding.session_key}, ["user", "collaboration_binding"], as_dict=True
		)
		self.assertEqual(
			(binding.actor, auth.user, auth.collaboration_binding), (RECIPIENT, RECIPIENT, binding.name)
		)
		self.assertIn(c.NAMESPACE, binding.session_key)
		self.assertNotIn(RECIPIENT, binding.session_key)
		self.assertNotIn(OWNER, binding.session_key)
		self.assertEqual(c.resolve_binding(binding.session_key), binding)

	def test_first_binding_marks_thread_and_never_overwrites_owner_session(self):
		frappe.db.set_value(c.CONVERSATION, self.conversation, "session_key", "legacy-owner-key")
		binding = self.binding()
		row = frappe.db.get_value(
			c.CONVERSATION,
			self.conversation,
			["session_key", "collaboration_active", "collaboration_epoch"],
			as_dict=True,
		)
		self.assertEqual(
			(row.session_key, row.collaboration_active, row.collaboration_epoch), ("legacy-owner-key", 1, 1)
		)
		self.assertEqual(binding.conversation_epoch, 1)
		with self.assertRaises(frappe.PermissionError):
			c.legacy_session_user(row.session_key)

	def test_each_top_level_request_gets_distinct_binding_even_for_same_actor(self):
		first, second = self.binding(), self.binding("b" * 32)
		self.assertNotEqual(first.name, second.name)
		self.assertNotEqual(first.session_key, second.session_key)
		self.assertEqual(first.conversation_epoch, second.conversation_epoch)

	def test_owner_also_uses_an_isolated_binding_in_a_marked_thread(self):
		self.binding()
		owner = self.binding("b" * 32, actor=OWNER)
		self.assertEqual(owner.actor, OWNER)
		self.assertFalse(owner.share)
		self.assertEqual(owner.grant_revision, 0)

	def test_activation_refuses_unfinished_legacy_generation(self):
		frappe.db.set_value("Jarvis Chat Message", self.message, {"streaming": 1, "role": "assistant"})
		with self.assertRaises(frappe.ValidationError):
			self.binding()
		self.assertFalse(c.is_collaborative(self.conversation))
		self.assertFalse(frappe.db.exists(c.BINDING, {"conversation": self.conversation}))

	def test_invalid_or_duplicate_run_does_not_rebind_existing_identity(self):
		for run in ("", "../../bad", "a" * 31, "a" * 33):
			with self.subTest(run=run), self.assertRaises(frappe.ValidationError):
				self.binding(run)
		first = self.binding()
		with self.assertRaises(frappe.DuplicateEntryError):
			self.binding(actor=OWNER)
		self.assertEqual(c.resolve_binding(first.session_key).actor, RECIPIENT)

	def test_auth_insert_failure_leaves_no_partial_binding_or_marker_after_rollback(self):
		from frappe.model.document import Document

		original = Document.insert

		def fail_auth(doc, *args, **kwargs):
			if doc.doctype == c.SESSION:
				raise RuntimeError("planted auth failure")
			return original(doc, *args, **kwargs)

		frappe.db.savepoint("binding_atomic")
		with patch.object(Document, "insert", fail_auth), self.assertRaises(RuntimeError):
			self.binding()
		frappe.db.rollback(save_point="binding_atomic")
		self.assertFalse(c.is_collaborative(self.conversation))
		self.assertFalse(frappe.db.exists(c.BINDING, {"conversation": self.conversation}))

	def test_identity_survives_downgrade_but_execution_resolver_does_not(self):
		binding = self.binding()
		frappe.db.set_value(SHARE, binding.share, {"access_mode": "View", "access_revision": 3})
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(binding.session_key)
		self.assertEqual(c.resolve_identity(binding.session_key), binding)

	def test_missing_or_mismatched_auth_cannot_use_identity_only_resolver(self):
		binding = self.binding()
		frappe.db.set_value(c.SESSION, {"session_key": binding.session_key}, "user", OWNER)
		with self.assertRaises(frappe.PermissionError):
			c.resolve_identity(binding.session_key)

	def test_settlement_cannot_insert_new_user_work_after_revocation(self):
		from jarvis.chat.collaboration_bindings import insert_message

		binding = self.binding()
		frappe.db.set_value(SHARE, binding.share, {"access_mode": "View", "access_revision": 3})
		with self.assertRaises(frappe.PermissionError):
			insert_message(binding, {"role": "user", "content": "new work"}, settled_run_id="a" * 32)

	def test_settlement_requires_an_already_dispatched_turn(self):
		from jarvis.chat.collaboration_bindings import insert_message

		binding = self.binding()
		frappe.db.set_value(SHARE, binding.share, {"access_mode": "View", "access_revision": 3})
		with self.assertRaises(frappe.PermissionError):
			insert_message(binding, {"role": "tool", "content": "actual receipt"}, settled_run_id="a" * 32)

	def test_live_binding_message_is_stamped_as_initiator_not_ambient_user(self):
		from jarvis.chat.collaboration_bindings import insert_message

		binding = self.binding()
		with as_user(OWNER):
			message = insert_message(binding, {"role": "assistant", "content": "recipient reply"})
		self.assertEqual(
			(message.actor, message.owner, message.collaboration_binding),
			(RECIPIENT, RECIPIENT, binding.name),
		)
		self.assertFalse(frappe.get_doc(message.doctype, message.name).has_permission("read", user=OWNER))

	def test_actual_dispatched_receipt_can_settle_after_revoke(self):
		from jarvis.chat.collaboration_bindings import insert_message, stamp

		binding = self.binding()
		seed = insert_message(binding, {"role": "user", "content": "original request"})
		turn = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": binding.logical_run_id,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": "dispatching",
				"seed_message": seed.name,
				"dispatching_at": frappe.utils.now_datetime(),
				"collaboration_send_started_at": frappe.utils.now_datetime(),
			}
		)
		stamp(turn, binding)
		turn.insert(ignore_permissions=True)
		frappe.db.delete(SHARE, {"name": binding.share})
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(binding.session_key)
		receipt = insert_message(
			binding,
			{"role": "tool", "content": "actual partial result"},
			settled_run_id=binding.logical_run_id,
		)
		self.assertEqual(
			(receipt.actor, receipt.collaboration_binding, receipt.collaboration_run_id),
			(RECIPIENT, binding.name, binding.logical_run_id),
		)
		with self.assertRaises(frappe.PermissionError):
			insert_message(
				binding, {"role": "tool", "content": "not the dispatched run"}, settled_run_id="b" * 32
			)
