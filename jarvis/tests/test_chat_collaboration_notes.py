"""Deferred decisions are scoped to one actor's logical chain and drained by ACK."""

import uuid

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_committed import CommittedCollaborationCase


class TestCollaborationNotes(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.binding_a, self.turn_a = self.running_turn(self.binding(uuid.uuid4().hex))
		self.binding_b, self.turn_b = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.commit()

	def test_note_is_scrubbed_and_only_read_by_its_own_logical_binding(self):
		from jarvis.chat.collaboration_notes import append, read

		append(self.binding_a, "Do not retry this action. api_key=NOTE_SECRET_SENTINEL", note_id="first")
		frappe.db.commit()
		self.assertEqual(read(self.binding_b), [])
		text = read(self.binding_a)[0]["text"]
		self.assertIn("Do not retry this action", text)
		self.assertNotIn("NOTE_SECRET_SENTINEL", text)

	def test_ack_only_clears_delivered_ids_and_keeps_a_newly_appended_note(self):
		from jarvis.chat.collaboration_notes import append, clear_after_ack, read

		append(self.binding_a, "First veto", note_id="first")
		delivered = [note["id"] for note in read(self.binding_a)]
		append(self.binding_a, "New veto", note_id="new")
		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn_a.name, {"state": "streaming", "gateway_run_id": "acknowledged-run"}
		)
		frappe.db.commit()
		clear_after_ack(self.binding_a, self.turn_a.name, delivered)
		self.assertEqual([note["id"] for note in read(self.binding_a)], ["new"])

	def test_unsent_or_wrong_turn_cannot_consume_notes(self):
		from jarvis.chat.collaboration_notes import append, clear_after_ack, read

		append(self.binding_a, "Do not retry", note_id="first")
		frappe.db.commit()
		for run_id in (self.turn_a.name, self.turn_b.name):
			with self.subTest(run_id=run_id), self.assertRaises(frappe.PermissionError):
				clear_after_ack(self.binding_a, run_id, ["first"])
		self.assertEqual([note["id"] for note in read(self.binding_a)], ["first"])

	def test_duplicate_note_is_idempotent_and_queue_is_bounded_without_dropping_decisions(self):
		from jarvis.chat.collaboration_notes import append, read

		append(self.binding_a, "First veto", note_id="first")
		append(self.binding_a, "First veto", note_id="first")
		self.assertEqual(len(read(self.binding_a)), 1)
		for index in range(24):
			append(self.binding_a, "Another veto", note_id=f"note-{index}")
		with self.assertRaises(frappe.ValidationError):
			append(self.binding_a, "A decision that must not silently replace another", note_id="overflow")
		self.assertEqual(len(read(self.binding_a)), 25)

	def test_undelivered_active_binding_note_is_inventoried_before_rollback(self):
		from jarvis.chat.collaboration_notes import append

		append(self.binding_a, "A human decision not yet delivered", note_id="first")
		frappe.db.commit()
		self.assertTrue(c.has_undrained_work(conversation=self.conversation))
		for run_id in (self.turn_a.name, self.turn_b.name):
			frappe.db.set_value("Jarvis Chat Turn", run_id, {"state": "done", "reserved": 0})
		frappe.db.commit()
		# No continuation can consume these notes after every actual consumer
		# drained. Retain the note as audit instead of blocking rollback forever.
		self.assertFalse(c.has_undrained_work(conversation=self.conversation))
		self.assertTrue(frappe.db.get_value(c.BINDING, self.binding_a.name, "pending_agent_notes"))
