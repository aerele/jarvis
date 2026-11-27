"""Payment Calendar S4b B7: the hooks of the cache (plan S4 D-4 purge, D-3 permission, section 8 uninstall).

What `hooks.py` wires and what the thin hook functions of `jarvis.payment_calendar.lifecycle` do:

* every dotted path the cache adds to `hooks.py` resolves, the cron entry is `*/5 * * * *`, and the
  `pcal_cache_purge` hook is declared;
* Company `on_trash` and `after_rename` (the old name), and Transaction Deletion Record `on_submit` and
  `on_change` (to Queued, Running, Completed) call `cache_store.purge_company` once, with the real Frappe
  composer, and only when the cache is active (`effective_mode`, never the raw mode);
* a non-database error never fails the user's action: it is logged by class (no name, no text). A job deadline
  and EVERY database error propagate (plan C2-2: the Company delete or TDR step fails as one unit and rolls
  back); `before_uninstall` alone logs a statement error that keeps the transaction, so a missing table cannot
  block an uninstall;
* the purge job is queued after the commit only; a rollback queues nothing and changes nothing;
* with the real `hooks.py`, no user but Administrator can list, read, share or write the three cache tables
  (the System Manager row Frappe adds and a Custom DocPerm both stay closed);
* `before_uninstall` deletes this site's `jarvis:pcal:cache:*` keys and not the bench-wide slot keys.

Nothing here commits except the one purge test that needs a durable head (it removes its own rows).

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_hooks
"""

import json
from unittest.mock import patch

import frappe
import frappe.handler
from frappe.tests.utils import FrappeTestCase
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import cache_state, cache_store
from jarvis.payment_calendar.lifecycle import PURGE_STATUSES

HEAD = "Jarvis Payment Calendar Cache Head"
GENERATION = "Jarvis Payment Calendar Cache Generation"
ROW = "Jarvis Payment Calendar Cache Row"
CACHE_DOCTYPES = (HEAD, GENERATION, ROW)
TDR = "Transaction Deletion Record"
LIFECYCLE = "jarvis.payment_calendar.lifecycle"
COMPANY = "PCAL-B7-Secret Company"
SAVEPOINT = "pcal_cache_hooks"
MANAGER = "pc-cache-hooks-manager@example.com"
ACCOUNTS = "pc-cache-hooks-accounts@example.com"

OFF = cache_state.CacheSettings("off", (), False)
SHADOW = cache_state.CacheSettings("shadow", (COMPANY,), False)
# The raw mode `on` is accepted and behaves as `shadow` (errata E1): a hook that tested `mode == "shadow"` would miss it.
ON = cache_state.CacheSettings("on", (COMPANY,), False)


def _handlers(doctype: str, event: str) -> list[str]:
	"""The paths Jarvis registers for a doc event in the real hooks.py."""
	found = frappe.get_hooks("doc_events").get(doctype, {}).get(event, [])
	found = [found] if isinstance(found, str) else found
	return [path for path in found if path.startswith(f"{LIFECYCLE}.")]


def _rows(doctype: str, *, api: bool = False) -> list:
	"""The names a user can list. Frappe raises PermissionError when the doctype itself is closed: no rows."""
	try:
		if api:
			found = frappe.call(
				frappe.client.get_list, doctype=doctype, fields=json.dumps(["name"]), limit_page_length=0
			)
			return [str(row["name"]) for row in found]
		return [str(name) for name in frappe.get_list(doctype, pluck="name", limit=0)]
	except frappe.PermissionError:
		return []


def _settings(value):
	return patch.object(cache_state, "cache_settings", side_effect=lambda **_: value)


def _ensure_user(email: str, role: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "pccachehooks",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(role)


class HookCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for name in ("head", "generation", "row"):
			frappe.reload_doc("jarvis", "doctype", f"jarvis_payment_calendar_cache_{name}")
		for doctype in CACHE_DOCTYPES:
			frappe.clear_cache(doctype=doctype)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=SAVEPOINT)
		for doctype in CACHE_DOCTYPES:
			frappe.clear_cache(doctype=doctype)
		frappe.local.role_permissions = {}


class TestHooksPy(HookCase):
	def test_every_registered_path_resolves(self):
		paths = [
			*_handlers("Company", "on_trash"),
			*_handlers("Company", "after_rename"),
			*_handlers(TDR, "on_submit"),
			*_handlers(TDR, "on_change"),
		]
		self.assertEqual(len(paths), 4)
		paths.append("jarvis.payment_calendar.cache.tick")
		paths += [p for p in frappe.get_hooks("before_uninstall", app_name="jarvis")]
		for hook in ("has_permission", "permission_query_conditions"):
			for doctype in CACHE_DOCTYPES:
				found = frappe.get_hooks(hook).get(doctype)
				paths += [found] if isinstance(found, str) else list(found or [])
		self.assertEqual(len(paths), 4 + 1 + 1 + 6)
		for path in paths:
			self.assertTrue(callable(frappe.get_attr(path)), path)

	def test_the_doc_events_are_registered_once_each(self):
		for doctype, event in (
			("Company", "on_trash"),
			("Company", "after_rename"),
			(TDR, "on_submit"),
			(TDR, "on_change"),
		):
			self.assertEqual(len(_handlers(doctype, event)), 1, (doctype, event))

	def test_the_doc_events_keep_the_handlers_that_were_already_there(self):
		# Company on_update (holiday rotation) must survive: the cache only adds to Company.
		on_update = frappe.get_hooks("doc_events").get("Company", {}).get("on_update", [])
		on_update = [on_update] if isinstance(on_update, str) else on_update
		self.assertIn("jarvis.holiday_touch.cache.rotate_after_commit", on_update)

	def test_the_tick_runs_every_five_minutes_and_nowhere_else(self):
		cron = frappe.get_hooks("scheduler_events").get("cron", {})
		tick = "jarvis.payment_calendar.cache.tick"
		self.assertIn(tick, cron["*/5 * * * *"])
		self.assertEqual([expr for expr, jobs in cron.items() if tick in jobs], ["*/5 * * * *"])
		for other in ("all", "hourly", "daily", "weekly", "monthly", "hourly_long", "daily_long"):
			self.assertNotIn(tick, frappe.get_hooks("scheduler_events").get(other, []), other)

	def test_the_purge_hook_is_declared_empty_for_other_modules_to_fill(self):
		own = frappe._load_app_hooks("jarvis")
		self.assertEqual(own.get("pcal_cache_purge"), [])

	def test_the_deny_functions_are_registered_for_all_three_tables(self):
		for hook, path in (
			("has_permission", "jarvis.permissions.deny_cache_table_permission"),
			("permission_query_conditions", "jarvis.permissions.deny_cache_table_query_conditions"),
		):
			for doctype in CACHE_DOCTYPES:
				found = frappe.get_hooks(hook).get(doctype)
				self.assertEqual([found] if isinstance(found, str) else found, [path], (hook, doctype))

	def test_the_uninstall_hook_is_registered(self):
		self.assertEqual(
			frappe.get_hooks("before_uninstall", app_name="jarvis"), [f"{LIFECYCLE}.before_uninstall"]
		)


class _Fire:
	"""Run a doc event through the real Frappe composer, with the controller method stubbed out."""

	@staticmethod
	def company(event: str, *args, name: str = COMPANY):
		from erpnext.setup.doctype.company.company import Company

		doc = frappe.get_doc({"doctype": "Company", "name": name, "company_name": name})
		with patch.object(Company, event, create=True, return_value=None):
			doc.run_method(event, *args)

	@staticmethod
	def deletion_record(event: str, status: str, company: str = COMPANY):
		from erpnext.setup.doctype.transaction_deletion_record.transaction_deletion_record import (
			TransactionDeletionRecord,
		)

		doc = frappe.get_doc({"doctype": TDR, "name": "TDR-B7-1", "company": company, "status": status})
		# ERPNext's own `on_submit` queues the deletion tasks and sets the status: only the hooks run here.
		with patch.object(TransactionDeletionRecord, event, create=True, return_value=None):
			doc.run_method(event)


class TestDocEvents(HookCase):
	def setUp(self):
		super().setUp()
		patcher = patch.object(cache_store, "purge_company", return_value=True)
		self.purge = patcher.start()
		self.addCleanup(patcher.stop)

	def test_company_trash_purges_that_company_once(self):
		with _settings(SHADOW):
			_Fire.company("on_trash")
		self.purge.assert_called_once_with(COMPANY)

	def test_company_rename_purges_the_old_name_only(self):
		with _settings(SHADOW):
			_Fire.company("after_rename", "Old Name Co", COMPANY, False, name=COMPANY)
		self.purge.assert_called_once_with("Old Name Co")

	def test_company_merge_rename_purges_the_old_name_too(self):
		with _settings(SHADOW):
			_Fire.company("after_rename", "Old Name Co", COMPANY, True, name=COMPANY)
		self.purge.assert_called_once_with("Old Name Co")

	def test_deletion_record_submit_purges_its_company_once(self):
		with _settings(SHADOW):
			_Fire.deletion_record("on_submit", "")
		self.purge.assert_called_once_with(COMPANY)

	def test_deletion_record_change_purges_for_queued_running_and_completed(self):
		self.assertEqual(PURGE_STATUSES, ("Queued", "Running", "Completed"))
		for status in PURGE_STATUSES:
			self.purge.reset_mock()
			with _settings(SHADOW):
				_Fire.deletion_record("on_change", status)
			self.purge.assert_called_once_with(COMPANY)

	def test_deletion_record_change_to_any_other_status_does_nothing(self):
		for status in ("", None, "Failed", "Cancelled"):
			with _settings(SHADOW):
				_Fire.deletion_record("on_change", status)
		self.purge.assert_not_called()

	def test_raw_mode_on_counts_as_active(self):
		with _settings(ON):
			_Fire.company("on_trash")
			_Fire.deletion_record("on_change", "Completed")
			_Fire.company("after_rename", COMPANY, "New Co", False)
		self.assertEqual(self.purge.call_count, 3)

	def test_with_the_cache_off_no_hook_does_any_work(self):
		with _settings(OFF):
			_Fire.company("on_trash")
			_Fire.company("after_rename", COMPANY, "New Co", False)
			_Fire.deletion_record("on_submit", "")
			for status in PURGE_STATUSES:
				_Fire.deletion_record("on_change", status)
		self.purge.assert_not_called()

	def test_the_off_check_reads_only_the_mode(self):
		with _settings(OFF) as settings:
			_Fire.company("on_trash")
		settings.assert_called_once_with(lists_when_off=False)

	def test_a_missing_company_name_does_nothing(self):
		with _settings(SHADOW):
			_Fire.deletion_record("on_change", "Queued", company="")
		self.purge.assert_not_called()


class TestHookErrors(HookCase):
	def setUp(self):
		super().setUp()
		patcher = patch.object(cache_store, "purge_company")
		self.purge = patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, error):
		self.purge.side_effect = error
		with _settings(SHADOW):
			_Fire.company("on_trash")

	def test_an_ordinary_error_is_logged_by_class_and_the_action_goes_on(self):
		with patch.object(frappe, "logger") as logger:
			self._run(ValueError(f"secret text of {COMPANY}"))
		warning = logger.return_value.warning
		warning.assert_called_once()
		self.assertIn("ValueError", str(warning.call_args))
		self.assertNotIn(COMPANY, str(warning.call_args))
		self.assertNotIn("secret", str(warning.call_args))

	def test_every_database_error_propagates_even_one_that_keeps_the_transaction(self):
		# Plan C2-2: a failed purge fails the Company delete or TDR step as one unit (a missing table included).
		for error in (
			frappe.db.ProgrammingError(1146, "no such table"),
			frappe.db.ProgrammingError(1054, "unknown column"),
			frappe.db.OperationalError(1205, "lock wait timeout"),
			frappe.db.DataError(1264, "out of range"),
			frappe.db.InternalError(1030, "storage engine"),
		):
			with self.subTest(type(error).__name__, args=error.args), self.assertRaises(type(error)):
				self._run(error)

	def test_a_non_database_error_is_logged_not_raised_whatever_its_name(self):
		for error in (KeyError("x"), RuntimeError("y"), frappe.ValidationError("z")):
			with self.subTest(type(error).__name__), patch.object(frappe, "logger") as logger:
				self._run(error)
			logger.return_value.warning.assert_called_once()

	def test_a_rollback_class_database_error_propagates(self):
		for error in (
			frappe.QueryDeadlockError(),
			frappe.QueryTimeoutError(),
			frappe.db.OperationalError(1213, "deadlock"),
			frappe.db.OperationalError(2013, "lost connection"),
		):
			with self.subTest(type(error).__name__, args=error.args), self.assertRaises(type(error)):
				self._run(error)

	def test_a_job_deadline_propagates(self):
		for error in (JobTimeoutException(), TimeoutError()):
			with self.subTest(type(error).__name__), self.assertRaises(type(error)):
				self._run(error)

	def test_the_deletion_record_and_rename_hooks_follow_the_same_rule(self):
		self.purge.side_effect = ValueError("x")
		with _settings(SHADOW), patch.object(frappe, "logger") as logger:
			_Fire.deletion_record("on_change", "Queued")
			_Fire.company("after_rename", COMPANY, "New Co", False)
		self.assertEqual(logger.return_value.warning.call_count, 2)
		self.purge.side_effect = frappe.QueryDeadlockError()
		with _settings(SHADOW):
			with self.assertRaises(frappe.QueryDeadlockError):
				_Fire.deletion_record("on_change", "Queued")
			with self.assertRaises(frappe.QueryDeadlockError):
				_Fire.company("after_rename", COMPANY, "New Co", False)


class TestPurgeAfterCommit(FrappeTestCase):
	"""A real head, a real `purge_company`, the real composer: the job is queued after the commit only."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for name in ("head", "generation", "row"):
			frappe.reload_doc("jarvis", "doctype", f"jarvis_payment_calendar_cache_{name}")

	def setUp(self):
		frappe.set_user("Administrator")
		self._remove()
		self.addCleanup(self._remove)
		frappe.db.sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side) values (%s, now(6), now(6), %s, 'Receivable')",
			(cache_store.head_name(COMPANY, "Receivable"), COMPANY),
		)
		frappe.db.sql(
			f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state) "
			"values (%s, now(6), now(6), %s, 'Receivable', 'current')",
			(frappe.db.get_next_sequence_val(GENERATION), COMPANY),
		)
		frappe.db.commit()

	def _remove(self):
		frappe.db.rollback()
		for doctype in (GENERATION, HEAD):
			frappe.db.sql(f"delete from `tab{doctype}` where company=%s", (COMPANY,))
		frappe.db.commit()

	def _state(self):
		frappe.db.rollback()
		epoch = frappe.db.sql(f"select purge_epoch from `tab{HEAD}` where company=%s", (COMPANY,))[0][0]
		states = {
			r[0] for r in frappe.db.sql(f"select state from `tab{GENERATION}` where company=%s", (COMPANY,))
		}
		return epoch, states

	def test_the_bump_is_in_the_callers_transaction_and_the_job_waits_for_the_commit(self):
		with _settings(SHADOW), patch.object(frappe, "enqueue") as enqueue:
			_Fire.deletion_record("on_change", "Queued")
			self.assertEqual(frappe.db.sql(f"select purge_epoch from `tab{HEAD}`")[0][0], 1)
			enqueue.assert_not_called()
			frappe.db.commit()
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual((args[0], kwargs["queue"]), (cache_store.PURGE_METHOD, "long"))
		self.assertNotIn(COMPANY, json.dumps([args, {k: str(v) for k, v in kwargs.items()}]))
		self.assertEqual(self._state(), (1, {"purged"}))

	def test_a_rolled_back_event_queues_no_job_and_changes_nothing(self):
		with _settings(SHADOW), patch.object(frappe, "enqueue") as enqueue:
			_Fire.deletion_record("on_change", "Queued")
			frappe.db.rollback()
			frappe.db.after_commit.run()
		enqueue.assert_not_called()
		self.assertEqual(self._state(), (0, {"current"}))

	def test_a_statement_error_in_the_purge_fails_the_step_and_everything_rolls_back(self):
		# The bump ran in the caller's transaction, then a statement fails: the TDR step raises, the caller
		# rolls back, the epoch and the generation are untouched and no purge job is queued.
		real = cache_store.bump_epoch

		def bump_then_fail(company):
			real(company)
			raise frappe.db.ProgrammingError(1146, "no such table")

		with (
			_settings(SHADOW),
			patch.object(cache_store, "bump_epoch", side_effect=bump_then_fail),
			patch.object(frappe, "enqueue") as enqueue,
		):
			with self.assertRaises(frappe.db.ProgrammingError):
				_Fire.deletion_record("on_change", "Queued")
			frappe.db.rollback()
			frappe.db.after_commit.run()
		enqueue.assert_not_called()
		self.assertEqual(self._state(), (0, {"current"}))

	def test_with_the_cache_off_nothing_is_bumped_and_no_job_is_queued(self):
		with _settings(OFF), patch.object(frappe, "enqueue") as enqueue:
			_Fire.deletion_record("on_change", "Queued")
			_Fire.company("on_trash")
			frappe.db.commit()
		enqueue.assert_not_called()
		self.assertEqual(self._state(), (0, {"current"}))

	def test_a_company_with_no_cache_rows_queues_no_job(self):
		with _settings(SHADOW), patch.object(frappe, "enqueue") as enqueue:
			_Fire.company("on_trash", name="PCAL-B7-Other Co")
			frappe.db.commit()
		enqueue.assert_not_called()


class _Seeded:
	"""One row in each cache table, written inside the savepoint of the test."""

	def seed(self) -> dict[str, str]:
		head = "pcal-b7-head"
		frappe.db.sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side) values (%s, now(6), now(6), %s, 'Receivable')",
			(head, COMPANY),
		)
		generation = frappe.db.get_next_sequence_val(GENERATION)
		frappe.db.sql(
			f"insert into `tab{GENERATION}` (name, creation, modified, company, side, state) "
			"values (%s, now(6), now(6), %s, 'Receivable', 'current')",
			(generation, COMPANY),
		)
		row = f"{generation:012d}{0:08d}"
		frappe.db.sql(
			f"insert into `tab{ROW}` (name, creation, modified, generation, row_id) values (%s, now(6), now(6), %s, 'id0')",
			(row, generation),
		)
		return {HEAD: head, GENERATION: str(generation), ROW: row}


class TestTablesStayClosed(HookCase, _Seeded):
	"""The real hooks.py: only Administrator reads the tables (plan section 8)."""

	def setUp(self):
		super().setUp()
		_ensure_user(MANAGER, "System Manager")
		_ensure_user(ACCOUNTS, "Accounts User")
		for doctype in CACHE_DOCTYPES:
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": "Accounts User",
					"permlevel": 0,
					"read": 1,
					"write": 1,
					"create": 1,
					"delete": 1,
					"share": 1,
				}
			).insert(ignore_permissions=True)
			frappe.clear_cache(doctype=doctype)
		frappe.local.role_permissions = {}
		self.names = self.seed()

	def _as(self, user):
		frappe.set_user(user)
		frappe.local.role_permissions = {}

	def test_administrator_lists_and_reads_every_table(self):
		for doctype, name in self.names.items():
			self.assertIn(name, _rows(doctype), doctype)
			self.assertIn(name, _rows(doctype, api=True), doctype)
			frappe.get_doc(doctype, name).check_permission("read")

	def test_no_other_user_lists_a_table(self):
		for user in (MANAGER, ACCOUNTS):
			self._as(user)
			for doctype in CACHE_DOCTYPES:
				self.assertEqual(_rows(doctype), [], (user, doctype))

	def test_the_api_get_list_returns_nothing_to_other_users(self):
		method = frappe.client.get_list
		frappe.handler.is_whitelisted(method)
		for user in (MANAGER, ACCOUNTS):
			self._as(user)
			for doctype in CACHE_DOCTYPES:
				self.assertEqual(_rows(doctype, api=True), [], (user, doctype))

	def test_no_other_user_reads_a_document(self):
		for user in (MANAGER, ACCOUNTS):
			self._as(user)
			for doctype, name in self.names.items():
				with self.assertRaises(frappe.PermissionError, msg=(user, doctype)):
					frappe.get_doc(doctype, name).check_permission("read")
				self.assertFalse(frappe.has_permission(doctype, "read", doc=name), (user, doctype))
				with self.assertRaises(frappe.PermissionError, msg=(user, doctype)):
					frappe.call(frappe.client.get, doctype=doctype, name=name)

	def test_no_other_user_writes_creates_deletes_or_shares(self):
		for user in (MANAGER, ACCOUNTS):
			self._as(user)
			for doctype, name in self.names.items():
				for ptype in ("write", "create", "delete", "share", "export", "report"):
					self.assertFalse(frappe.has_permission(doctype, ptype, doc=name), (user, doctype, ptype))
				with self.assertRaises(frappe.PermissionError, msg=(user, doctype)):
					frappe.share.add(doctype, name, "Administrator", read=1)

	def test_the_user_reads_an_ordinary_doctype_the_same_way(self):
		# Control: the denial above comes from the cache hooks, not from a broken test user.
		self._as(ACCOUNTS)
		self.assertTrue(frappe.has_permission("Account", "read"))


class TestBeforeUninstall(HookCase, _Seeded):
	"""`before_uninstall` cleans Redis and tells the purge hooks; the tables are dropped by Frappe itself."""

	NIGHT = "jarvis:pcal:cache:night:b7digest:Receivable:2026-11-24"
	SLOT = "jarvis:pcal:cache:slot:99"
	READER = "jarvis:pcal:reader:b7-unrelated"

	def setUp(self):
		super().setUp()
		from jarvis.payment_calendar import lifecycle

		self.lifecycle = lifecycle
		self.seed()
		cache = frappe.cache
		cache.set_value(self.NIGHT, "1", expires_in_sec=120)
		cache.set_value(self.READER, "1", expires_in_sec=120)
		cache.set(cache.make_key(self.SLOT, shared=True), "x", ex=120)
		self.addCleanup(cache.delete_value, self.NIGHT)
		self.addCleanup(cache.delete_value, self.READER)
		self.addCleanup(cache.delete, cache.make_key(self.SLOT, shared=True))

	def test_it_deletes_this_sites_cache_keys_and_nothing_else(self):
		with _settings(SHADOW), patch.object(cache_store, "notify_purged"):
			self.lifecycle.before_uninstall()
		cache = frappe.cache
		self.assertIsNone(cache.get_value(self.NIGHT))
		self.assertEqual(cache.get_value(self.READER), "1")
		self.assertTrue(cache.exists(self.SLOT, shared=True))

	def test_it_runs_with_the_cache_off_too(self):
		with _settings(OFF), patch.object(cache_store, "notify_purged") as notify:
			self.lifecycle.before_uninstall()
		self.assertIsNone(frappe.cache.get_value(self.NIGHT))
		notify.assert_called_once()

	def test_it_tells_the_purge_hooks_each_company_once_by_digest(self):
		frappe.db.sql(
			f"insert into `tab{HEAD}` (name, creation, modified, company, side) values (%s, now(6), now(6), %s, 'Payable')",
			("pcal-b7-head-2", COMPANY),
		)
		with _settings(SHADOW), patch.object(cache_store, "notify_purged") as notify:
			self.lifecycle.before_uninstall()
		notify.assert_called_once()
		(digest,) = notify.call_args.args
		self.assertNotIn(COMPANY, digest)

	def test_an_error_does_not_stop_the_uninstall(self):
		with _settings(SHADOW), patch.object(frappe, "logger") as logger:
			with patch.object(cache_store, "notify_purged", side_effect=ValueError(COMPANY)):
				self.lifecycle.before_uninstall()
		self.assertIsNone(frappe.cache.get_value(self.NIGHT))
		self.assertNotIn(COMPANY, str(logger.return_value.warning.call_args))
		self.assertIn("ValueError", str(logger.return_value.warning.call_args))

	def test_a_statement_error_that_keeps_the_transaction_is_logged_not_raised(self):
		# A table that is already gone (a half-removed app) must not block the uninstall.
		with _settings(SHADOW), patch.object(frappe, "logger") as logger:
			with patch.object(
				cache_store, "notify_purged", side_effect=frappe.db.ProgrammingError(1146, "x")
			):
				self.lifecycle.before_uninstall()
		self.assertIsNone(frappe.cache.get_value(self.NIGHT))
		logger.return_value.warning.assert_called_once()

	def test_rollback_class_errors_and_deadlines_propagate(self):
		for error in (frappe.QueryDeadlockError(), frappe.db.OperationalError(2013, "gone"), TimeoutError()):
			with (
				self.subTest(type(error).__name__),
				_settings(SHADOW),
				patch.object(cache_store, "notify_purged", side_effect=error),
				self.assertRaises(type(error)),
			):
				self.lifecycle.before_uninstall()
