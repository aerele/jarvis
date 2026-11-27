"""Payment Calendar S4b: `guard`, the one error policy of the cache's housekeeping.

The errors here are the real ones: MariaDB 1054 (an unknown column), 1969 (the statement time limit) and a real
lock wait (1205) from a second connection. Nothing in `guard` is patched.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_guard
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import guard
from jarvis.tests.race_harness import other_connection
from jarvis.tests.test_payment_calendar_cache_refresh import real_statement_timeout

LOCK_TABLE = "tabJarvis Payment Calendar Cache Head"


def statement_error() -> Exception:
	"""A real MariaDB 1054: the statement fails and the transaction stays."""
	try:
		frappe.db.sql("select no_such_column_pcal from `tabUser` limit 1")
	except Exception as exc:
		return exc
	raise AssertionError("the server accepted the statement")


def lock_wait_error() -> Exception:
	"""A real lock wait: a second connection holds the row, this one waits one second and Frappe raises."""
	name = "pcal-guard-lock"
	frappe.db.sql(f"delete from `{LOCK_TABLE}` where name=%s", (name,))
	frappe.db.sql(
		f"insert into `{LOCK_TABLE}` (name, creation, modified, company, side) values (%s, now(6), now(6), 'x', 'Bank')",
		(name,),
	)
	frappe.db.commit()
	try:
		with other_connection() as other:
			other.sql(f"select name from `{LOCK_TABLE}` where name=%s for update", (name,))
			frappe.db.sql("set session innodb_lock_wait_timeout=1")
			try:
				frappe.db.sql(f"update `{LOCK_TABLE}` set side='Payable' where name=%s", (name,))
			except Exception as exc:
				return exc
			finally:
				frappe.db.sql("set session innodb_lock_wait_timeout=50")
		raise AssertionError("the server did not time the lock wait out")
	finally:
		frappe.db.rollback()
		frappe.db.sql(f"delete from `{LOCK_TABLE}` where name=%s", (name,))
		frappe.db.commit()


class TestRealErrors(FrappeTestCase):
	"""The three real errors have the shapes that the policy relies on."""

	def test_the_real_errors_are_what_the_policy_assumes(self):
		self.assertEqual(statement_error().args[0], 1054)
		self.assertEqual(real_statement_timeout().args[0], 1969)
		self.assertIsInstance(lock_wait_error(), frappe.QueryTimeoutError)


class TestPropagates(FrappeTestCase):
	def test_a_job_deadline_and_a_socket_timeout_propagate(self):
		for exc in (JobTimeoutException(), TimeoutError()):
			self.assertTrue(guard.propagates(exc), type(exc).__name__)

	def test_a_real_lock_wait_propagates_as_a_rollback_class_error(self):
		self.assertTrue(guard.propagates(lock_wait_error()))

	def test_a_lost_connection_and_a_deadlock_propagate(self):
		for exc in (frappe.QueryDeadlockError(), frappe.db.OperationalError(2013, "x")):
			self.assertTrue(guard.propagates(exc), repr(exc))

	def test_a_real_statement_error_and_a_real_statement_timeout_do_not_by_default(self):
		self.assertFalse(guard.propagates(statement_error()))
		self.assertFalse(guard.propagates(real_statement_timeout()))
		self.assertFalse(guard.propagates(ValueError("x")))

	def test_every_database_error_propagates_when_asked_and_a_plain_error_still_does_not(self):
		for exc in (statement_error(), real_statement_timeout()):
			self.assertTrue(guard.propagates(exc, all_database_errors=True))
		self.assertFalse(guard.propagates(ValueError("x"), all_database_errors=True))
		self.assertFalse(guard.propagates(KeyError("x"), all_database_errors=True))


class TestGuarded(FrappeTestCase):
	def run_guarded(self, error, **kwargs):
		logger = MagicMock()

		def step():
			raise error

		with patch.object(frappe, "logger", return_value=logger):
			guard.guarded(step, "cache_probe", **kwargs)
		return logger

	def test_a_step_that_succeeds_runs_once(self):
		calls = []
		guard.guarded(lambda: calls.append(1), "cache_probe")
		self.assertEqual(calls, [1])

	def test_an_error_that_stays_is_logged_by_class_with_the_label_and_no_text(self):
		for error in (ValueError("Acme Ltd"), statement_error(), real_statement_timeout()):
			logger = self.run_guarded(error)
			text = str(logger.method_calls)
			self.assertIn("cache_probe", text)
			self.assertIn(type(error).__name__, text)
			self.assertNotIn("Acme", text)
			self.assertNotIn("no_such_column", text)

	def test_a_job_deadline_and_a_rollback_class_error_are_raised_not_logged(self):
		for error in (JobTimeoutException(), TimeoutError(), lock_wait_error(), frappe.QueryDeadlockError()):
			with self.subTest(type(error).__name__), self.assertRaises(type(error)):
				self.run_guarded(error)

	def test_a_swallowed_error_is_handed_to_the_callback_and_a_propagating_one_is_not(self):
		seen = []
		self.run_guarded(ValueError("x"), on_swallow=seen.append)
		self.assertEqual([type(error) for error in seen], [ValueError])
		with self.assertRaises(TimeoutError):
			self.run_guarded(TimeoutError(), on_swallow=seen.append)
		self.assertEqual(len(seen), 1)

	def test_the_flag_makes_a_plain_database_error_propagate_too(self):
		error = statement_error()
		with self.assertRaises(type(error)):
			self.run_guarded(error, all_database_errors=True)
		logger = self.run_guarded(ValueError("x"), all_database_errors=True)
		logger.warning.assert_called_once()
