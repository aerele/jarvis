"""R2-3 bench-stamped single correction, across REAL parks and settlements.

No model argument anywhere: the bench stamps the fixable failure with the hidden
continuation message it sends, a card parked in the turn that message starts carries
``corrects`` (sealed), and that card's own failure is sent as not fixable."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pending_actions as pa
from jarvis.chat import turn_message_binding
from jarvis.tests._pending_action_helpers import MSG, OWNER, PendingActionTestMixin, as_user, fake_dispatch

FIXABLE_TEXT = "correct only the fields named in the failure"
NOT_FIXABLE_TEXT = "Retrying will not fix it"
BAD_USER = "nobody-j2a-stamp@example.com"


class TestCorrectionStamp(PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = self.make_conv()
		self.todo = self.make_todo()
		self.addCleanup(frappe.cache().delete_value, turn_message_binding._key(self.conv))

	# -- helpers ---------------------------------------------------------------

	def park_update(self, changes: dict) -> str:
		args = {"doctype": "ToDo", "name": self.todo, "changes": changes}
		return self.park(self.conv, tool="update_doc", args=args, summary="Update a ToDo")

	def confirm(self, name: str, **kw) -> dict:
		with as_user(OWNER):
			return pa.execute(name, conversation=self.conv, **kw)

	def last_continuation(self) -> frappe._dict:
		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "origin": "continuation", "hidden": 1},
			fields=["name", "content"],
			order_by="seq desc",
			limit=1,
		)
		self.assertTrue(rows, "no continuation was sent")
		return rows[0]

	def start_turn(self, message: str) -> None:
		"""The worker binds the running turn's message at turn start."""
		turn_message_binding.bind_turn_message(self.conv, message)

	def fail_fixably(self) -> tuple[str, frappe._dict]:
		first = self.park_update({"allocated_to": BAD_USER})
		self.assertEqual(self.row(first).corrects or "", "")
		out = self.confirm(first)
		self.assertEqual((out["ok"], out["error"]["kind"]), (False, "fixable"), out)
		cont = self.last_continuation()
		self.assertIn(FIXABLE_TEXT, cont.content)
		self.assertEqual(
			self.row(first).correction_message, cont.name, "stamped in the row's own transaction"
		)
		return first, cont

	# -- tests -----------------------------------------------------------------

	def test_one_correction_then_explain_and_stop(self):
		first, cont = self.fail_fixably()
		self.start_turn(cont.name)
		second = self.park_update({"allocated_to": BAD_USER})  # the "correction", still wrong
		self.assertEqual(self.row(second).corrects, first)
		out = self.confirm(second)
		self.assertEqual((out["ok"], out["error"]["kind"]), (False, "fixable"), "the envelope keeps its kind")
		again = self.last_continuation()
		self.assertNotEqual(again.name, cont.name)
		self.assertIn(NOT_FIXABLE_TEXT, again.content)
		self.assertNotIn(FIXABLE_TEXT, again.content)
		self.assertFalse(self.row(second).correction_message, "a spent correction is never stamped")

	def test_update_then_retry_counts_as_one_correction(self):
		first, cont = self.fail_fixably()
		self.start_turn(cont.name)
		fix = self.park_update({"description": "pa-test fixed value"})
		self.assertEqual(self.row(fix).corrects, first)
		self.assertTrue(self.confirm(fix)["ok"])
		ok_cont = self.last_continuation()
		self.assertIn("[System] Applied:", ok_cont.content)
		self.assertEqual(
			self.row(fix).correction_message, ok_cont.name, "the ok continuation carries the stamp"
		)
		self.start_turn(ok_cont.name)
		retry = self.park_update({"allocated_to": BAD_USER})
		self.assertEqual(self.row(retry).corrects, first, "the action after the fix is the same correction")
		self.assertFalse(self.confirm(retry)["ok"])
		self.assertIn(NOT_FIXABLE_TEXT, self.last_continuation().content)

	def test_a_new_human_turn_starts_clean(self):
		self.fail_fixably()
		self.start_turn("human-message-j2a")
		fresh = self.park_update({"allocated_to": BAD_USER})
		self.assertEqual(self.row(fresh).corrects or "", "")

	def test_clearing_corrects_in_the_table_reads_as_tampered(self):
		first, cont = self.fail_fixably()
		self.start_turn(cont.name)
		second = self.park_update({"allocated_to": BAD_USER})
		self.set_col(second, corrects="")
		out = self.confirm(second)
		self.assertEqual((out["ok"], out["reason_code"]), (False, "tampered"), out)

	def test_a_late_settle_is_not_offered_a_correction(self):
		first = self.park_update({"allocated_to": BAD_USER})
		out = self.confirm(first, defer_continuation=True)
		self.assertFalse(out["ok"])
		self.assertEqual(self.row(first).settled, 0)
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action` SET modified=%(m)s WHERE name=%(n)s",
			{"m": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10), "n": first},
		)
		frappe.db.commit()
		pa.reconcile()
		self.assertEqual(self.row(first).settled, 1)
		cont = self.last_continuation()
		self.assertIn(NOT_FIXABLE_TEXT, cont.content)
		self.assertFalse(self.row(first).correction_message)

	def test_a_busy_failure_says_try_again(self):
		def _deadlock(tool, args):
			raise frappe.QueryDeadlockError("Deadlock found")

		name = self.park_update({"description": "pa-test busy"})
		with fake_dispatch(_deadlock):
			out = self.confirm(name)
		self.assertEqual(
			(out["ok"], out["error"]["code"], out["outcome"]), (False, "RetryLaterError", "failed")
		)
		content = self.last_continuation().content
		self.assertIn("The system was busy (a lock or timeout); nothing was changed.", content)
		self.assertFalse(self.row(name).correction_message)

	def test_a_real_deadlock_is_retry_later_not_partial(self):
		# The real shape: InnoDB 1213 rolls the whole transaction back in the database
		# (savepoint included) before the driver raises.
		def _real_deadlock(tool, args):
			frappe.db._conn.rollback()
			raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")

		name = self.park_update({"description": "pa-test real deadlock"})
		with fake_dispatch(_real_deadlock):
			out = self.confirm(name)
		self.assertEqual((out["ok"], out["error"]["code"]), (False, "RetryLaterError"), out)
		self.assertEqual((out["outcome"], out["reason_code"]), ("failed", "failed"))
		content = self.last_continuation().content
		self.assertIn("The system was busy (a lock or timeout); nothing was changed.", content)
		self.assertNotIn("could NOT be verified", content)

	def test_a_commit_before_a_real_deadlock_is_still_partial(self):
		def _commit_then_real_deadlock(tool, args):
			frappe.db.commit()
			frappe.db._conn.rollback()
			raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")

		name = self.park_update({"description": "pa-test commit then deadlock"})
		with fake_dispatch(_commit_then_real_deadlock):
			out = self.confirm(name)
		self.assertEqual((out["ok"], out["outcome"]), (False, "partial"))
		self.assertIn("could NOT be verified", self.last_continuation().content)

	def test_an_unmigrated_site_still_parks_and_settles(self):
		# Code deployed, migrate not run yet: the stamp columns do not exist.
		from unittest.mock import patch

		real = frappe.db.has_column

		def _missing(doctype, column):
			return False if column == "correction_message" else real(doctype, column)

		with patch.object(frappe.db, "has_column", side_effect=_missing):
			first = self.park_update({"allocated_to": BAD_USER})
			self.assertEqual(self.row(first).corrects or "", "")
			out = self.confirm(first)
		self.assertFalse(out["ok"])
		self.assertIn(FIXABLE_TEXT, self.last_continuation().content)
		self.assertFalse(self.row(first).correction_message, "no stamp written")
		self.assertEqual(self.row(first).settled, 1)

	def test_a_busy_failure_after_a_commit_is_partial(self):
		def _commit_then_deadlock(tool, args):
			frappe.db.commit()
			raise frappe.QueryDeadlockError("Deadlock found")

		name = self.park_update({"description": "pa-test partial"})
		with fake_dispatch(_commit_then_deadlock):
			out = self.confirm(name)
		self.assertEqual((out["ok"], out["outcome"]), (False, "partial"))
		content = self.last_continuation().content
		self.assertIn("could NOT be verified", content)
		self.assertNotIn("try again in a moment", content)
