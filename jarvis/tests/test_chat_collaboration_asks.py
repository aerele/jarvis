"""Only the initiator answers a shared ask or resumes its logical binding."""

import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user

APPROVAL = "Jarvis Approval Request"
ASK = 'Choose one.\n```jarvis-ask\n{"questions":[{"q":"Which warehouse?","options":["A","B"]}]}\n```'


class TestCollaborationAsks(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		self.reply = insert_message(self.bound, {"role": "assistant", "content": ASK, "streaming": 0})
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{
				"state": "finalizing",
				"assistant_message": self.reply.name,
			},
		)
		frappe.db.commit()
		for item in (
			patch("frappe.publish_realtime"),
			patch("jarvis.chat.pump.ensure_pump"),
			patch("jarvis.chat.pump.lpush_wake"),
		):
			item.start()
			self.addCleanup(item.stop)

	def materialize(self, run=None):
		from jarvis.chat.collaboration_asks import materialize

		return materialize(run or self.turn.name)

	def answer(self, name, text="A"):
		from jarvis.chat.collaboration_api import answer_question

		return answer_question(self.conversation, name, text)

	def test_materialized_question_is_owned_and_bound_to_the_initiator(self):
		name = self.materialize()
		row = frappe.get_doc(APPROVAL, name)
		self.assertEqual((row.owner, row.actor), (RECIPIENT, RECIPIENT))
		self.assertEqual(
			(row.collaboration_binding, row.collaboration_run_id), (self.bound.name, self.turn.name)
		)
		self.assertEqual(row.collaboration_grant_revision, self.bound.grant_revision)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.source, "Chat")

	def test_materialization_is_idempotent_for_a_child_run(self):
		first = self.materialize()
		self.assertEqual(self.materialize(), first)
		self.assertEqual(frappe.db.count(APPROVAL, {"conversation": self.conversation}), 1)

	def test_revoked_actor_gets_no_fresh_usable_question(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		self.assertIsNone(self.materialize())
		self.assertEqual(frappe.db.count(APPROVAL, {"conversation": self.conversation}), 0)

	def test_owner_administrator_and_outsider_cannot_spend_another_actors_answer(self):
		name = self.materialize()
		for actor in (OWNER, "Administrator", OUTSIDER):
			with self.subTest(actor=actor), as_user(actor), self.assertRaises(frappe.PermissionError):
				self.answer(name)
			frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")
		self.assertEqual(frappe.db.count("Jarvis Chat Turn", {"conversation": self.conversation}), 1)

	def test_answer_and_visible_seed_commit_with_one_same_actor_continuation(self):
		name = self.materialize()
		with as_user(RECIPIENT):
			first = self.answer(name)
			second = self.answer(name)
		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertTrue(second["duplicate"])
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Answered")
		turns = frappe.get_all(
			"Jarvis Chat Turn",
			{"conversation": self.conversation},
			["name", "actor", "collaboration_binding", "seed_message"],
		)
		self.assertEqual(len(turns), 2)
		child = next(row for row in turns if row.name != self.turn.name)
		self.assertEqual((child.actor, child.collaboration_binding), (RECIPIENT, self.bound.name))
		seed = frappe.get_doc("Jarvis Chat Message", child.seed_message)
		self.assertFalse(seed.hidden)
		self.assertIn("A", seed.content)
		self.assertEqual((seed.actor, seed.owner), (RECIPIENT, RECIPIENT))

	def test_downgrade_and_regrant_never_resurrect_old_question_capability(self):
		name = self.materialize()
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "View", "access_revision": 3})
		frappe.db.commit()
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			self.answer(name)
		frappe.db.rollback()
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "Chat", "access_revision": 4})
		frappe.db.commit()
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			self.answer(name)

	def test_another_actor_message_does_not_resolve_this_actors_ask(self):
		from jarvis.chat.collaboration_asks import resolve_on_user_message

		name = self.materialize()
		resolve_on_user_message(self.conversation, OWNER)
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Pending")
		resolve_on_user_message(self.conversation, RECIPIENT)
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "status"), "Answered")

	def test_generic_orm_cannot_forge_actor_binding_even_as_administrator(self):
		row = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "Forged",
				"question": "Forged?",
				"source": "Chat",
				"conversation": self.conversation,
				"actor": RECIPIENT,
				"collaboration_binding": self.bound.name,
				"collaboration_run_id": self.turn.name,
			}
		)
		with self.assertRaises(frappe.PermissionError):
			row.insert(ignore_permissions=True)

	def test_different_answer_winning_after_precheck_cannot_be_reported_as_accepted(self):
		from jarvis.chat import collaboration_continuations

		name = self.materialize()
		original = collaboration_continuations.enqueue

		def rival_then_enqueue(*args, **kwargs):
			with patch.object(collaboration_continuations, "enqueue", original):
				self.assertTrue(self.answer(name, "B")["ok"])
			return original(*args, **kwargs)

		with (
			as_user(RECIPIENT),
			patch.object(collaboration_continuations, "enqueue", rival_then_enqueue),
			self.assertRaises(frappe.ValidationError),
		):
			self.answer(name, "A")
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "decision"), "B")
		self.assertEqual(frappe.db.count("Jarvis Chat Turn", {"conversation": self.conversation}), 2)

	def test_normal_chat_resolving_question_after_precheck_returns_friendly_refusal(self):
		from jarvis.chat import collaboration_continuations
		from jarvis.chat.collaboration_asks import resolve_on_user_message

		name = self.materialize()
		original = collaboration_continuations.enqueue

		def resolve_then_enqueue(*args, **kwargs):
			resolve_on_user_message(self.conversation, RECIPIENT)
			frappe.db.commit()
			return original(*args, **kwargs)

		with (
			as_user(RECIPIENT),
			patch.object(collaboration_continuations, "enqueue", resolve_then_enqueue),
			self.assertRaises(frappe.ValidationError),
		):
			self.answer(name, "A")
		self.assertEqual(frappe.db.get_value(APPROVAL, name, "decision"), "(answered in chat)")
		self.assertEqual(frappe.db.count("Jarvis Chat Turn", {"conversation": self.conversation}), 1)
