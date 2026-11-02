"""Current authorization must outrank an older request snapshot."""

import uuid

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_bindings import require_live
from jarvis.tests._collaboration_committed import CommittedCollaborationCase, repeatable_read
from jarvis.tests.race_harness import open_read_view, other_connection, snapshot_isolation_off


class TestCollaborationLiveRaces(CommittedCollaborationCase):
	def test_uncertain_binding_is_not_revived_by_an_old_snapshot(self):
		binding = self.binding(uuid.uuid4().hex)
		frappe.db.commit()
		with repeatable_read(), snapshot_isolation_off():
			open_read_view()
			self.assertEqual(frappe.db.get_value(c.BINDING, binding.name, "state"), "Active")
			with other_connection() as other:
				other.sql("UPDATE `tabJarvis Chat Binding` SET state='Uncertain' WHERE name=%s", binding.name)
				other.commit()
			self.assertEqual(frappe.db.get_value(c.BINDING, binding.name, "state"), "Active")
			with self.assertRaises(frappe.PermissionError):
				require_live(binding)

	def test_callback_claim_outlives_a_terminal_turn_for_rollback_drain(self):
		binding, turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			"Jarvis Chat Turn",
			turn.name,
			{
				"state": "done",
				"reserved": 0,
				"collaboration_dispatches": '{"tool-call":{"tool":"get_doc","state":"Running"}}',
			},
		)
		frappe.db.commit()
		self.assertTrue(c.has_undrained_work(conversation=self.conversation))
		frappe.db.set_value(
			"Jarvis Chat Turn",
			turn.name,
			"collaboration_dispatches",
			'{"tool-call":{"tool":"get_doc","state":"Done","message":"receipt"}}',
		)
		frappe.db.commit()
		self.assertFalse(c.has_undrained_work(conversation=self.conversation))
