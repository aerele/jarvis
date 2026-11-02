"""Shared confirmations bind the actual human and retain at-most-once execution."""

import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT, as_user


class TestCollaborationActions(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		for target in ("jarvis.chat.pump.ensure_pump", "jarvis.chat.pump.lpush_wake"):
			mock = patch(target)
			mock.start()
			self.addCleanup(mock.stop)
		mode = patch(
			"jarvis.chat.pump.transport_predicates_from_row",
			return_value={"mode": "pump", "pump_active": True, "configured": True},
		)
		mode.start()
		self.addCleanup(mode.stop)
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING, self.bound.name, {"runtime_session_id": "physical", "runtime_policy_hash": "verified"}
		)
		frappe.db.commit()
		self.title = "Collaboration action " + uuid.uuid4().hex

	def park(self, *, owner=RECIPIENT, executor=RECIPIENT):
		from jarvis.chat.collaboration_callbacks import callback_scope, record_gateway_tool
		from jarvis.chat.pending_actions import park

		record_gateway_tool(self.turn.name, "qa-create", "jarvis__create_doc")
		with callback_scope(self.bound.session_key, "create_doc", "qa-create", wait_s=0):
			return park(
				kind="chat",
				owner_user=owner,
				exec_user=executor,
				tool="create_doc",
				args={
					"doctype": "Note",
					"values": {"title": self.title, "content": "shared authorised note"},
				},
				conversation=self.conversation,
				run_id=self.turn.name,
				summary="Create a Note",
			)

	def confirm(self, name, user=RECIPIENT):
		from jarvis.chat.collaboration_actions import confirm_action

		with as_user(user):
			return confirm_action(self.conversation, name)

	def discard(self, name, user=RECIPIENT):
		from jarvis.chat.collaboration_actions import discard_action

		with as_user(user):
			return discard_action(self.conversation, name)

	def test_actor_discards_without_execution_or_another_actors_deferred_note(self):
		from jarvis.chat.collaboration_notes import read

		name = self.park()
		other = self.binding("b" * 32)
		frappe.db.commit()
		result = self.discard(name)
		self.assertTrue(result["ok"], result)
		self.assertFalse(frappe.db.exists("Note", {"title": self.title}))
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", name, "status"), "Discarded")
		self.assertEqual(len(read(self.bound)), 1)
		self.assertEqual(read(other), [])
		self.discard(name)
		self.assertEqual(len(read(self.bound)), 1)
		self.assertEqual(frappe.db.count("Jarvis Chat Turn", {"conversation": self.conversation}), 1)

	def test_owner_and_downgraded_actor_cannot_discard_using_another_execution_context(self):
		name = self.park()
		with self.assertRaises(frappe.PermissionError):
			self.discard(name, OWNER)
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "View", "access_revision": 3})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.discard(name)
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", name, "status"), "Pending")

	def test_pending_human_action_keeps_rollback_drain_closed_after_model_finishes(self):
		self.park()
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, {"state": "done", "reserved": 0})
		frappe.db.commit()
		self.assertTrue(c.has_undrained_work(conversation=self.conversation))

	def test_park_stamps_and_seals_actor_binding_revision_and_epoch(self):
		from jarvis.chat.pending_actions import _seal

		name = self.park()
		row = frappe.get_doc("Jarvis Pending Action", name)
		self.assertEqual(row.actor, RECIPIENT)
		self.assertEqual(row.collaboration_binding, self.bound.name)
		self.assertEqual(row.collaboration_grant_revision, self.bound.grant_revision)
		self.assertEqual(row.collaboration_epoch, self.bound.conversation_epoch)
		row.actor = OWNER
		with self.assertRaises(_seal.SealError):
			_seal.unseal_call(row)

	def test_park_cannot_borrow_owner_or_a_different_executor(self):
		for owner, executor in ((OWNER, RECIPIENT), (RECIPIENT, OWNER), (OWNER, OWNER)):
			with self.subTest(owner=owner), self.assertRaises(frappe.PermissionError):
				self.park(owner=owner, executor=executor)
		self.assertFalse(frappe.db.exists("Jarvis Pending Action", {"conversation": self.conversation}))

	def test_conversation_owner_cannot_spend_recipients_action(self):
		name = self.park()
		with self.assertRaises(frappe.PermissionError):
			self.confirm(name, OWNER)
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", name, "status"), "Pending")
		self.assertFalse(frappe.db.exists("Note", {"title": self.title}))

	def test_recipient_confirms_real_note_as_self_once(self):
		name = self.park()
		result = self.confirm(name)
		created = frappe.db.get_value("Note", {"title": self.title}, ["name", "owner"], as_dict=True)
		if created:
			self.records.append(("Note", created.name))
		self.assertTrue(result["ok"], result)
		self.assertIsNotNone(created)
		self.assertEqual(created.owner, RECIPIENT)
		self.confirm(name)
		self.assertEqual(frappe.db.count("Note", {"title": self.title}), 1)
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", name, "settled"), 1)
		children = frappe.get_all(
			"Jarvis Chat Turn",
			{"conversation": self.conversation, "name": ["!=", self.turn.name]},
			["actor", "collaboration_binding", "seed_message"],
		)
		self.assertEqual(len(children), 1)
		self.assertEqual((children[0].actor, children[0].collaboration_binding), (RECIPIENT, self.bound.name))
		self.assertEqual(frappe.db.get_value("Jarvis Chat Message", children[0].seed_message, "hidden"), 1)

	def test_downgrade_blocks_confirmation_without_executing(self):
		name = self.park()
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "View", "access_revision": 3})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.confirm(name)
		self.assertFalse(frappe.db.exists("Note", {"title": self.title}))

	def test_missing_executor_is_not_replaced_with_owner(self):
		name = self.park()
		frappe.db.set_value("Jarvis Pending Action", name, "exec_user", "")
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.confirm(name)
		self.assertFalse(frappe.db.exists("Note", {"title": self.title}))
