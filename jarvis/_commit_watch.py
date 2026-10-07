"""Did a write's transaction stay its own from start to finish? (R2-3 ``partial``)

A failed write is rolled back, so the person can be told nothing was saved. That is
only true when nothing committed in between: a commit inside the write (``run_import``,
DDL, a controller that commits) keeps what came before it, and a full rollback inside it
resets the callback queues, so a later commit would go unseen. ``CommitWatch`` arms a
``before_commit`` sentinel for the length of one write and says afterwards whether the
transaction was interfered with (the sentinel ran, or vanished).

One exception: the write path's own full rollback after a deadlock
(``api._dispatch_and_wrap``) records what was armed just before it in
``frappe.local.jarvis_rolled_back_after_failure``. A sentinel still armed then saw no
commit, so that failure is a clean retry-later, not partial. A commit before the
deadlock ran the sentinel, so it stays partial.

``arm(savepoint=True)`` also opens a savepoint, which ANY commit releases, including
one the sentinel cannot see (a raw ``COMMIT`` statement, or a commit made while
``frappe.db.commit`` is neutralised by the write-risk fence). ``disarm(probe=True)``
then counts a lost savepoint as interfered. Not after a deadlock: InnoDB's own rollback
drops the savepoint too, and that failure changed nothing. A caller that sees the
deadlock itself passes ``probe=False``; one whose write path rolled back after it
(``jarvis_rolled_back_after_failure``) is skipped here. A lock wait timeout rolls back
only its statement, so the savepoint survives and is probed.

Only the outermost watch holds a savepoint: releasing a savepoint also releases every
later one, so a nested watch's savepoint could vanish under it and read as a commit.
A nested ``arm(savepoint=True)`` falls back to the sentinel alone; the outer watch
still sees a raw commit."""

from __future__ import annotations

import frappe


class CommitWatch:
	"""``arm()`` before the write, ``disarm()`` straight after it (before any commit or
	rollback of the caller's own). ``disarm()`` returns True when the write's
	transaction was not its own."""

	def __init__(self):
		self._ran: list[bool] = []
		self._savepoint = ""

	def arm(self, *, savepoint: bool = False) -> CommitWatch:
		frappe.local.jarvis_rolled_back_after_failure = ()
		frappe.db.before_commit.add(self._jarvis_commit_sentinel)
		if savepoint and not getattr(frappe.local, "jarvis_commit_watch_savepoint", ""):
			self._savepoint = f"jarvis_watch_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(self._savepoint)
			frappe.local.jarvis_commit_watch_savepoint = self._savepoint
		return self

	def disarm(self, *, probe: bool = False) -> bool:
		callbacks = frappe.db.before_commit
		gone = self._jarvis_commit_sentinel not in callbacks._functions
		reset_by_rollback = self._jarvis_commit_sentinel in (
			getattr(frappe.local, "jarvis_rolled_back_after_failure", ()) or ()
		)
		frappe.local.jarvis_rolled_back_after_failure = ()
		if not gone:
			callbacks._functions.remove(self._jarvis_commit_sentinel)  # disarm before any later commit
		interfered = bool(self._ran) or (gone and not reset_by_rollback)
		# MariaDB only: on Postgres any failed statement aborts the transaction, so the
		# probe would read every database error as a lost savepoint.
		probe = probe and not reset_by_rollback and frappe.db.db_type == "mariadb"
		if not interfered and probe and self._savepoint:
			interfered = self._savepoint_lost()
		if self._savepoint and getattr(frappe.local, "jarvis_commit_watch_savepoint", "") == self._savepoint:
			frappe.local.jarvis_commit_watch_savepoint = ""
		return interfered

	def _savepoint_lost(self) -> bool:
		try:
			frappe.db.release_savepoint(self._savepoint)
		except Exception:
			return True  # MariaDB 1305: the savepoint went with a commit or rollback
		return False

	def _jarvis_commit_sentinel(self) -> None:
		self._ran.append(True)
