"""Strict harness for snapshot-isolation race tests (see ``jarvis.chat.txn``).

A race test here reproduces the REAL engine error, not a Python stand-in: it arms
``innodb_snapshot_isolation`` on the test connection, opens a read view, commits the
competing write from a genuine second connection, and only then runs the unit under
test. MariaDB then raises ER_CHECKREAD 1020 exactly as it does in production.

Why arming works on CI: CI's ``mariadb:10.11`` (10.11.19) defaults the variable OFF,
but it exists there and ``SET SESSION innodb_snapshot_isolation=ON`` makes the same
interleaving fail with 1020 (probed in a throwaway 10.11.19 container, 2026-09-29).
MariaDB 11.6.2+ defaults it ON. A server without the variable cannot run these tests
at all, and ``snapshot_isolation_on`` FAILS there instead of skipping: a race test
that never armed the race would be a green check that proves nothing.

Why a test cannot pass without the fix: ``assert_fix_closes_race`` runs each scenario
twice. With ``fix_disabled()`` the defences in ``jarvis.chat.txn`` are switched off and
the scenario MUST raise 1020 (the race is armed and reaches the code under test);
with them on it must succeed. Every defence goes through ``jarvis.chat.txn`` for
exactly this reason.

Fixture rows are created and removed through the second connection (committed), never
left to the test transaction: the units under test commit on purpose, so a class
rollback cannot be what cleans up after them.
"""

import contextlib
from collections.abc import Callable, Iterator
from typing import TypeVar
from unittest.mock import patch

import frappe
from frappe.database import get_db

from jarvis.chat import txn

T = TypeVar("T")


@contextlib.contextmanager
def snapshot_isolation_on() -> Iterator[None]:
	"""Arm MariaDB's snapshot-isolation check for this test's connection, restore after.

	Fails (never skips) when the server has no such variable."""
	try:
		previous = frappe.db.sql("SELECT @@session.innodb_snapshot_isolation")[0][0]
	except Exception as e:
		raise AssertionError(
			"This MariaDB has no innodb_snapshot_isolation, so the snapshot race cannot be "
			"armed and this test would prove nothing. MariaDB 10.6.18+, 10.11.8+ or 11.x is required."
		) from e
	frappe.db.sql("SET SESSION innodb_snapshot_isolation=ON")
	armed = frappe.db.sql("SELECT @@session.innodb_snapshot_isolation")[0][0]
	if int(armed) != 1:
		raise AssertionError(f"innodb_snapshot_isolation did not turn on (reads {armed!r})")
	try:
		yield
	finally:
		frappe.db.sql(f"SET SESSION innodb_snapshot_isolation={int(previous)}")


@contextlib.contextmanager
def snapshot_isolation_off() -> Iterator[None]:
	"""Run as a MariaDB 10.x server does by default: the engine does not refuse a stale
	lock, so a ``doc.save()`` on a moved row reaches Frappe's own ``check_if_latest`` and
	fails there with ``TimestampMismatchError`` instead. Restores the previous value.
	Fails (never skips) when the server has no such variable, like the ON variant."""
	try:
		previous = frappe.db.sql("SELECT @@session.innodb_snapshot_isolation")[0][0]
	except Exception as e:
		raise AssertionError("This MariaDB has no innodb_snapshot_isolation to switch off.") from e
	frappe.db.sql("SET SESSION innodb_snapshot_isolation=OFF")
	try:
		yield
	finally:
		frappe.db.sql(f"SET SESSION innodb_snapshot_isolation={int(previous)}")


@contextlib.contextmanager
def other_connection() -> Iterator["frappe.database.database.Database"]:
	"""A second, independent connection to the site database, opened the way
	``frappe.connect`` opens the main one. Writes on it are real commits."""
	conf = frappe.conf
	db = get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)
	db.connect()
	try:
		yield db
	finally:
		with contextlib.suppress(Exception):
			db.rollback()
		db.close()


@contextlib.contextmanager
def as_job() -> Iterator[None]:
	"""Run as a background job would: ``txn.owns_transaction()`` is true exactly as in
	``execute_job``. Same shape as the #713 tests use."""
	had = hasattr(frappe.local, "job")
	previous = getattr(frappe.local, "job", None)
	frappe.local.job = frappe._dict(method="race-test", job_name="race-test", after_job=None)
	try:
		yield
	finally:
		if had and previous is not None:
			frappe.local.job = previous
		else:
			# A leftover None still reads as "in a job" to parts of frappe.
			with contextlib.suppress(AttributeError):
				del frappe.local.job


@contextlib.contextmanager
def fix_disabled() -> Iterator[None]:
	"""Switch every snapshot-race defence in ``jarvis.chat.txn`` off: no fresh snapshot,
	no replay, and no conflict is recognised as one. Never patches Frappe's own
	``commit``, which the code under test also needs for its ordinary work."""
	with (
		patch.object(txn, "fresh_snapshot", lambda **_kw: False),
		patch.object(txn, "replay_on_conflict", lambda fn, **_kw: fn()),
		patch.object(txn, "is_write_conflict", lambda _e: False),
	):
		yield


def open_read_view() -> None:
	"""Take the snapshot a long-running transaction would already hold: the first plain
	read of an InnoDB table opens the read view."""
	frappe.db.sql("SELECT name FROM `tabDocType` LIMIT 1")


def assert_fix_closes_race(
	testcase, scenario: Callable[[], T], check: Callable[[T], None] | None = None
) -> T:
	"""Run ``scenario`` twice and prove the defences are what make it correct.

	With the defences off the race must bite: the scenario raises the engine's 1020, or,
	where the unpatched code swallows the error, returns a result ``check`` rejects
	(``check`` raises ``AssertionError``). With the defences on the scenario must return
	and pass ``check``. Returns that result for further asserts.

	``scenario`` must build its own fixtures, arm the race and clean up in ``finally``,
	so each run starts from the same state."""
	with fix_disabled():
		try:
			broken = scenario()
		except frappe.QueryDeadlockError:
			pass
		else:
			if check is None:
				testcase.fail("race not armed: the scenario succeeded with the fix off")
			try:
				check(broken)
			except AssertionError:
				pass
			else:
				testcase.fail("race not armed: the fix-off outcome already passes the check")
	frappe.db.rollback()
	result = scenario()
	if check is not None:
		check(result)
	return result
