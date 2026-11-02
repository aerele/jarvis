"""Real second-connection claims, cancellation and expiry under stale snapshots."""

import frappe

from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.tests._collaboration_committed import repeatable_read
from jarvis.tests._share_helpers import OWNER, as_user
from jarvis.tests.race_harness import as_job, open_read_view, other_connection, snapshot_isolation_on
from jarvis.tests.test_chat_collaboration_rq import RQCase, setUpModule


class TestCollaborationRQRaces(RQCase):
	def test_second_connection_cancel_before_worker_claim_prevents_model_send(self):
		with repeatable_read(), snapshot_isolation_on():
			open_read_view()
			self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "cancel_requested"))
			with other_connection() as other:
				other.sql(
					f"UPDATE `tab{ts.TURN}` SET cancel_requested=1, version=version+1 WHERE name=%s",
					(self.turn.name,),
				)
				other.commit()
			self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "cancel_requested"))
			self.drive()
			self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
			self.assertEqual(self.gateway.sends, [])
			self.assertEqual(self.gateway.calls, [])

	def test_second_connection_expiry_wins_against_a_stale_send_claim(self):
		from jarvis.chat.collaboration_rq import _claim, _claim_send

		with as_job():
			claim = _claim(self.bound, self.turn.name, self.turn.seed_message, self.target)
		frappe.db.set_value(
			ts.TURN, self.turn.name, "preparing_at", frappe.utils.add_to_date(None, seconds=-301)
		)
		frappe.db.commit()
		claim.prepared_at = frappe.db.get_value(ts.TURN, self.turn.name, "preparing_at")
		with repeatable_read(), snapshot_isolation_on():
			open_read_view()
			self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "dispatching")
			with other_connection() as other:
				other.sql(
					f"""UPDATE `tab{ts.TURN}` SET state='cancelled', reserved=0,
					done_at=%s, version=version+1 WHERE name=%s AND state='dispatching'
					AND version=%s AND collaboration_send_started_at IS NULL AND preparing_at < %s""",
					(
						frappe.utils.now_datetime(),
						self.turn.name,
						claim.version,
						frappe.utils.add_to_date(None, seconds=-300),
					),
				)
				self.assertEqual(other._cursor.rowcount, 1)
				other.commit()
			self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "dispatching")
			with as_job():
				self.assertFalse(_claim_send(self.bound, self.turn.name, self.target, claim, {}))
			self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
			self.assertEqual(self.gateway.sends, [])

	def test_stop_replays_after_another_connection_claimed_preparation(self):
		from jarvis.chat.collaboration_api import stop_run

		reply = insert_message(self.bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.commit()
		with repeatable_read(), snapshot_isolation_on():
			open_read_view()
			self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "assistant_message"))
			with other_connection() as other:
				other.sql(
					f"""UPDATE `tab{ts.TURN}` SET preparing_at=%s, assistant_message=%s,
					version=version+1 WHERE name=%s AND collaboration_send_started_at IS NULL""",
					(frappe.utils.now_datetime(), reply.name, self.turn.name),
				)
				self.assertEqual(other._cursor.rowcount, 1)
				other.commit()
			self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "assistant_message"))
			with as_user(OWNER):
				result = stop_run(self.conversation, self.turn.name)
			self.assertTrue(result["ok"])
			self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")
			self.assertFalse(frappe.db.get_value(ts.MSG, reply.name, "streaming"))
			self.assertEqual(self.gateway.sends, [])
