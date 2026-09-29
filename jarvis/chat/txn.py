"""Snapshot-isolation write conflicts: one policy for every shared-row write.

MariaDB 11.6.2+ ships ``innodb_snapshot_isolation=ON`` (MDEV-35124). Under
REPEATABLE-READ a transaction's first plain read fixes its snapshot, and a later
LOCKING read or WRITE (``FOR UPDATE``, ``UPDATE``, ``DELETE``, ``set_value``,
``save``, an ``INSERT`` meeting a unique key) on a row another connection committed
after that snapshot fails with ER_CHECKREAD 1020 "Record has changed since last
read". The server then aborts the WHOLE transaction: every earlier write in it is
gone and every savepoint with it (``ROLLBACK TO SAVEPOINT`` fails with 1305).
Frappe raises it as ``frappe.QueryDeadlockError`` (a plain ``Exception``), so
``execute_job``'s deadlock retry, which catches ``InternalError``, never replays it.

In chat this showed up as a spreadsheet export stalling ~80 s: the pump locked the
reply row on a snapshot older than the web request's file-card write 27 ms before,
lost, and parked the turn until the next hop. #713 was the same race on Jarvis
Settings; ``jarvis_settings._write_settings_fields`` fixed it there and its rules are
the ones generalised here:

* A retry on the same snapshot fails forever. The rollback before a replay is what
  makes the next attempt read the present.
* Only replay where nothing else is lost. The server has already discarded the
  whole transaction, so replaying one unit after the caller's own writes vanished
  would publish our write without theirs. ``replay_on_conflict`` checks this at run
  time (``replay_is_safe``) and otherwise re-raises to the transaction's owner.
* Only end a transaction you own (``owns_transaction``): a background job or a
  migrate. A web request's transaction belongs to the request.

Every defence against this race goes through this module (call sites use
``txn.fresh_snapshot()`` / ``txn.replay_on_conflict(...)``, never a local copy), so
the race tests can switch the whole defence off and prove each one is load-bearing
(``jarvis.tests.race_harness.fix_disabled``).
"""

from collections.abc import Callable
from typing import TypeVar

import frappe

T = TypeVar("T")

_WRITE_CONFLICT_ERRORS: tuple[type[BaseException], ...] = (frappe.QueryDeadlockError,)


def is_write_conflict(e: BaseException) -> bool:
	"""Did MariaDB refuse this statement because another connection moved a row after
	our transaction's snapshot opened?

	Frappe folds ER_CHECKREAD 1020 into ``QueryDeadlockError`` alongside a true
	ER_LOCK_DEADLOCK 1213 (``frappe/database/mariadb/database.py``: ``is_deadlocked``
	tests for both), and both want the same treatment: the statement did not land, the
	transaction is gone, and a replay from a fresh snapshot is the whole recovery.
	Matching the class rather than the errno keeps an older server that reports the
	same lost race as a plain 1213 on this branch too."""
	return isinstance(e, _WRITE_CONFLICT_ERRORS)


def owns_transaction() -> bool:
	"""May this code END the current transaction (commit or roll back)?

	True only where this process started one and nothing upstream depends on it: a
	background job (``frappe.local.job`` is set exclusively by ``execute_job``) or a
	migrate patch, where committing between units is normal.

	False in a web request, whose transaction belongs to the request: frappe commits
	it on success and rolls it back on any exception, and code that ends it halfway
	takes that decision away. False from ``bench execute`` and the CLI too, which is
	imprecise (they do own their transaction) but harmless: the only thing withheld
	there is a commit that would have shrunk the race window."""
	return bool(getattr(frappe.local, "job", None) or frappe.flags.in_migrate)


def fresh_snapshot(*, owned: bool = False) -> bool:
	"""End our own transaction so the NEXT statement reads the present.

	Call it after the slow part of a job (an HTTP call, a gateway poll, a sleep) and
	right before a write to a row other processes also write: it shrinks the exposure
	from "everything since the job started" to "this one write". A no-op where the
	transaction is not ours (``owns_transaction``). Returns whether it committed.

	``owned=True`` is for code that owns its transaction in every context, not only in
	a job: the pump's tool applier, whose commit-first #1523 made unconditional (its
	tests call it outside a job). Going through here rather than ``frappe.db.commit()``
	keeps it switchable by the race tests."""
	if not (owned or owns_transaction()):
		return False
	# A job owns its transaction and the runtime only commits it at the job's end;
	# ending it here is what drops a snapshot that would otherwise fail the write
	# below with 1020. Callers only reach this with their earlier work complete.
	frappe.db.commit()
	return True


def replay_is_safe() -> bool:
	"""Would a server-side abort of the current transaction destroy nothing but reads?

	True when the transaction holds no uncommitted write and no queued commit or
	rollback callback (a realtime publish, an ``enqueue_after_commit`` job, a file
	cleanup). Then rolling back and running a unit again is equivalent to running it
	the first time. The attribute names are Frappe internals; ``test_race_harness``
	pins them so a Frappe upgrade that renames one fails loudly instead of silently
	turning every replay off."""
	db = frappe.db
	return db.transaction_writes == 0 and not any(
		cb._functions for cb in (db.before_commit, db.after_commit, db.before_rollback, db.after_rollback)
	)


def replay_on_conflict(
	fn: Callable[[], T],
	*,
	label: str,
	attempts: int = 2,
	fresh: bool = False,
	before_replay: Callable[[], None] | None = None,
	also: tuple[type[BaseException], ...] = (),
) -> T:
	"""Run ``fn``, replaying it from a fresh transaction if it loses a snapshot race.

	``fn`` is one self-contained unit of DB work: it may read, lock and write, and it
	must be safe to run again after a rollback (the server undid its first attempt).
	Side effects outside the database (publish, enqueue, Redis) belong after the
	commit that follows the unit, not inside it.

	``fresh=True`` first calls ``fresh_snapshot()`` (a no-op outside a job), so in a job
	the unit starts on a snapshot taken now and the first attempt usually wins.

	Replay happens only when ``replay_is_safe()`` held BEFORE the first attempt;
	otherwise the conflict is re-raised to the transaction's owner, which is the only
	code that knows what else the transaction held. ``before_replay`` runs after the
	rollback and before the next attempt (for example to re-read a version the unit
	fences on). The last attempt's conflict is re-raised.

	``also`` names further exceptions that mean the same lost race for THIS unit. The
	one in use is ``frappe.TimestampMismatchError``: on a server without snapshot
	isolation (MariaDB 10.x, where the check is off) a ``doc.save()`` whose row moved
	after the load does not fail in the engine; Frappe's own ``check_if_latest`` refuses
	it instead. Only a unit that reloads its document inside ``fn`` may pass it, so the
	replay saves on the present row. It is opt-in per call rather than part of
	``is_write_conflict``, which other code (the #713 settings workers) also relies on."""
	if fresh:
		fresh_snapshot()
	safe = replay_is_safe()
	attempt = 1
	while True:
		try:
			return fn()
		except Exception as e:
			lost_race = is_write_conflict(e) or (bool(also) and isinstance(e, also))
			if not lost_race or not safe or attempt >= attempts:
				raise
			# MariaDB has already aborted the transaction; this resets Frappe's side
			# (value cache, callbacks) and starts the fresh one the replay reads from.
			frappe.db.rollback()
			frappe.logger("jarvis.txn").warning(
				"write conflict in %s (attempt %d of %d), replaying: %s", label, attempt, attempts, e
			)
			if before_replay is not None:
				before_replay()
			attempt += 1
