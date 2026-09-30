"""The race harness and ``jarvis.chat.txn`` against the real engine.

These are the anchor for every snapshot-race test in the app: if MariaDB does not
raise ER_CHECKREAD 1020 for a stale-snapshot lock here, no race test elsewhere can be
trusted, so this module FAILS rather than skips (see ``race_harness``).

Scratch rows live in ``tabDefaultValue`` under a parent no real record uses, created
and removed through a second connection.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import txn
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	fix_disabled,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)

PARENT = "__jarvis_race_harness__"


def _conflict() -> frappe.QueryDeadlockError:
	return frappe.QueryDeadlockError("simulated write conflict")


class _Base(FrappeTestCase):
	def setUp(self):
		frappe.db.rollback()

	def tearDown(self):
		frappe.db.rollback()
		with other_connection() as other:
			other.sql("DELETE FROM `tabDefaultValue` WHERE parent=%s", PARENT)
			other.commit()

	def _row(self, value: str = "a") -> str:
		name = frappe.generate_hash(length=12)
		with other_connection() as other:
			other.sql(
				"""INSERT INTO `tabDefaultValue` (name, parent, defkey, defvalue, creation, modified)
				VALUES (%s, %s, %s, %s, NOW(), NOW())""",
				(name, PARENT, name, value),
			)
			other.commit()
		return name

	def _compete(self, name: str, value: str = "b") -> None:
		with other_connection() as other:
			other.sql("UPDATE `tabDefaultValue` SET defvalue=%s WHERE name=%s", (value, name))
			other.commit()

	def _value(self, name: str) -> str:
		with other_connection() as other:
			return other.sql("SELECT defvalue FROM `tabDefaultValue` WHERE name=%s", name)[0][0]

	def _lock(self, name: str):
		return frappe.db.sql("SELECT defvalue FROM `tabDefaultValue` WHERE name=%s FOR UPDATE", name)

	def _append_unit(self, name: str, calls: list | None = None):
		def unit():
			if calls is not None:
				calls.append(1)
			self._lock(name)
			frappe.db.sql(
				"UPDATE `tabDefaultValue` SET defvalue=CONCAT(defvalue, '+unit') WHERE name=%s",
				name,
			)

		return unit


class TestTheRaceIsArmed(_Base):
	def test_a_lock_on_a_stale_snapshot_raises_the_engine_1020(self):
		name = self._row()
		with snapshot_isolation_on():
			open_read_view()
			self._compete(name)
			with self.assertRaises(frappe.QueryDeadlockError) as cm:
				self._lock(name)
		self.assertEqual(cm.exception.__cause__.args[0], 1020)

	def test_a_fresh_transaction_reads_the_present_and_wins(self):
		name = self._row()
		with snapshot_isolation_on():
			open_read_view()
			self._compete(name)
			frappe.db.commit()
			self.assertEqual(self._lock(name)[0][0], "b")


class TestReplayOnConflict(_Base):
	def test_a_safe_unit_is_replayed_and_both_writes_land(self):
		name = self._row()
		calls: list = []
		with snapshot_isolation_on():
			open_read_view()
			self._compete(name)
			txn.replay_on_conflict(self._append_unit(name, calls), label="harness")
			frappe.db.commit()
		self.assertEqual(self._value(name), "b+unit")
		self.assertEqual(len(calls), 2, "the first attempt must lose and the replay win")

	def test_it_reraises_when_the_transaction_already_wrote(self):
		name, mine = self._row(), self._row()
		with snapshot_isolation_on():
			open_read_view()
			frappe.db.sql("UPDATE `tabDefaultValue` SET defvalue='mine' WHERE name=%s", mine)
			self._compete(name)
			with self.assertRaises(frappe.QueryDeadlockError):
				txn.replay_on_conflict(self._append_unit(name), label="harness")
		frappe.db.rollback()
		# The server aborted the whole transaction: the earlier write is gone too, which
		# is exactly why the unit alone must not be replayed here.
		self.assertEqual(self._value(mine), "a")
		self.assertEqual(self._value(name), "b")

	def test_it_reraises_when_a_commit_callback_is_queued(self):
		name = self._row()
		with snapshot_isolation_on():
			open_read_view()
			frappe.db.after_commit.add(lambda: None)
			self._compete(name)
			with self.assertRaises(frappe.QueryDeadlockError):
				txn.replay_on_conflict(self._append_unit(name), label="harness")

	def test_a_non_conflict_error_is_never_replayed(self):
		calls: list = []

		def unit():
			calls.append(1)
			raise ValueError("not a race")

		with self.assertRaises(ValueError):
			txn.replay_on_conflict(unit, label="harness", attempts=3)
		self.assertEqual(len(calls), 1)

	def test_replays_are_bounded_and_the_last_conflict_is_raised(self):
		calls: list = []

		def unit():
			calls.append(1)
			raise _conflict()

		with self.assertRaises(frappe.QueryDeadlockError):
			txn.replay_on_conflict(unit, label="harness", attempts=3)
		self.assertEqual(len(calls), 3)

	def test_also_replays_the_named_extra_exception_and_only_when_named(self):
		# MariaDB 10.x: a doc.save() on a moved row fails in Frappe's check_if_latest.
		def flaky(calls):
			def unit():
				calls.append(1)
				if len(calls) == 1:
					raise frappe.TimestampMismatchError("Document has been modified after you have opened it")
				return "saved"

			return unit

		calls: list = []
		result = txn.replay_on_conflict(flaky(calls), label="harness", also=(frappe.TimestampMismatchError,))
		self.assertEqual((result, len(calls)), ("saved", 2))
		calls = []
		with self.assertRaises(frappe.TimestampMismatchError):
			txn.replay_on_conflict(flaky(calls), label="harness")
		self.assertEqual(len(calls), 1)

	def test_before_replay_runs_after_the_rollback_and_before_the_next_attempt(self):
		events: list = []

		def unit():
			events.append("attempt")
			if events.count("attempt") == 1:
				raise _conflict()
			return "done"

		result = txn.replay_on_conflict(unit, label="harness", before_replay=lambda: events.append("resync"))
		self.assertEqual(result, "done")
		self.assertEqual(events, ["attempt", "resync", "attempt"])


class TestFreshSnapshot(_Base):
	def test_in_a_job_it_drops_the_stale_snapshot(self):
		name = self._row()
		with snapshot_isolation_on(), as_job():
			open_read_view()
			self._compete(name)
			self.assertTrue(txn.fresh_snapshot())
			self.assertEqual(self._lock(name)[0][0], "b")

	def test_outside_a_job_it_leaves_the_transaction_alone(self):
		name = self._row()
		with snapshot_isolation_on():
			open_read_view()
			self._compete(name)
			self.assertFalse(txn.fresh_snapshot())
			with self.assertRaises(frappe.QueryDeadlockError):
				self._lock(name)

	def test_owned_commits_outside_a_job_too(self):
		# The pump's tool applier owns its transaction in every context (#1523 made its
		# commit-first unconditional); ``owned=True`` keeps that behaviour through txn.
		name = self._row()
		with snapshot_isolation_on():
			open_read_view()
			self._compete(name)
			self.assertTrue(txn.fresh_snapshot(owned=True))
			self.assertEqual(self._lock(name)[0][0], "b")

	def test_a_fresh_unit_in_a_job_wins_on_the_first_attempt(self):
		name = self._row()
		calls: list = []
		with snapshot_isolation_on(), as_job():
			open_read_view()
			self._compete(name)
			txn.replay_on_conflict(self._append_unit(name, calls), label="harness", fresh=True)
			frappe.db.commit()
		self.assertEqual(len(calls), 1)
		self.assertEqual(self._value(name), "b+unit")


class TestTheHarnessIsStrict(_Base):
	def test_fix_disabled_switches_every_defence_off(self):
		calls: list = []

		def unit():
			calls.append(1)
			raise _conflict()

		with fix_disabled(), as_job():
			self.assertFalse(txn.fresh_snapshot())
			self.assertFalse(txn.is_write_conflict(_conflict()))
			with self.assertRaises(frappe.QueryDeadlockError):
				txn.replay_on_conflict(unit, label="harness", attempts=3)
		self.assertEqual(len(calls), 1)

	def test_a_scenario_that_never_races_fails_the_pair_check(self):
		with self.assertRaises(AssertionError):
			assert_fix_closes_race(self, lambda: None)

	def test_the_pair_check_passes_a_real_race_the_fix_closes(self):
		def scenario():
			name = self._row()
			with snapshot_isolation_on():
				open_read_view()
				self._compete(name)
				txn.replay_on_conflict(self._append_unit(name), label="harness")
				frappe.db.commit()
			return self._value(name)

		self.assertEqual(assert_fix_closes_race(self, scenario), "b+unit")

	def test_the_frappe_internals_replay_safety_reads_still_exist(self):
		# jarvis.chat.txn.replay_is_safe reads these private names; a Frappe upgrade that
		# renames one must fail here, not silently turn every replay off.
		self.assertIsInstance(frappe.db.transaction_writes, int)
		for name in ("before_commit", "after_commit", "before_rollback", "after_rollback"):
			self.assertTrue(hasattr(getattr(frappe.db, name), "_functions"), name)
		frappe.db.rollback()
		self.assertTrue(txn.replay_is_safe())
		frappe.db.sql("UPDATE `tabDefaultValue` SET defvalue=defvalue WHERE parent=%s", PARENT)
		self.assertFalse(txn.replay_is_safe())
