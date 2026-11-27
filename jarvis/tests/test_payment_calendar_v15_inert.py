"""The Payment Calendar reader and the batch permission check are inactive on Frappe 15.

The v15 branch carries the reader, the router, the shadow and the batch permission check, switched off: the
reader mode is `off`, both reader lists are empty and the batch switch is absent. Nothing serves a read from the
reader there until the v15 reader pins exist (S6), so a side above the caps still shows `too_large`. This module
pins that on a v15 site and records the small side effects that remain. It skips on any other Frappe major.

Everything is written inside the test's savepoint and rolled back: reader values go straight to `tabSingles`, a
Redis key a case writes is deleted by the case, and nothing is committed.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_v15_inert
"""

from time import monotonic
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from erpnext.accounts.utils import get_balance_on
from frappe.utils import add_days, flt

from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings
from jarvis.payment_calendar import (
	banks,
	batch_permission,
	builtin_reader,
	read_log,
	routing,
	shadow,
	shadow_state,
	snapshot,
	sources,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)
from jarvis.tests.payment_calendar_majors import interface_error, v15_only

SETTINGS = routing.SETTINGS_DOCTYPE
KEY = "k" * 44
SIDE = "Receivable"
MISSING_COMPANY = "Inert Test Company That No Site Has"
# A far-future date, so the counters of these cases never meet a real day's counters.
COUNTER_DATE = "2031-08-14"
SAVEPOINT = "calendar_v15_inert"
# The Error Log titles of the reader and of the batch permission check (the other Payment Calendar titles are old).
ERROR_LOG_TITLES = ("Payment Calendar reader%", "Payment Calendar batch permission%")
V15 = "a v15-only module"


def too_large(*args, **kwargs):
	raise CalendarSourceUnavailable("too_large")


def raw(field, value):
	"""Write one Singles row of Jarvis Settings directly (None deletes it); the savepoint takes it back."""
	frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
	if value is not None:
		frappe.db.sql(
			"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)", (SETTINGS, field, value)
		)


def stored(field):
	rows = frappe.db.sql("SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
	return rows[0][0] if rows else None


def error_rows() -> int:
	"""The Error Log rows with a reader or batch permission title; a case compares its own before and after."""
	query = "SELECT COUNT(*) FROM `tabError Log` WHERE method LIKE %s OR method LIKE %s"
	return frappe.db.sql(query, ERROR_LOG_TITLES)[0][0]


def reader_keys(company: str) -> list:
	"""The reader keys that exist for one company: its memo, in-flight and day keys, and the slots (exact names)."""
	names = [routing.slot_key(number) for number in range(routing.READER_SLOTS)]
	for side in ("Payable", "Receivable"):
		names += [routing.memo_key(company, side), routing.inflight_key(company, side)]
	digest = read_log.company_digest(company)
	names.append(frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest}:{COUNTER_DATE}"))
	return [name for name in names if frappe.cache.execute_command("EXISTS", name)]


def forget_batch_state():
	"""The per-request state of the batch permission check on `frappe.local` (stats, events, memos, trip)."""
	for name in (
		batch_permission.STATS,
		batch_permission.EVENTS,
		batch_permission.FREEZE,
		batch_permission.TRIPPED,
		batch_permission.PLAN_MEMO,
	):
		if hasattr(frappe.local, name):
			delattr(frappe.local, name)


class InertCase(TestCase):
	"""One savepoint per case, an explicit encryption key, and the three reader values absent."""

	# A Settings save decrypts the site's passwords with the site's own key, so that case keeps it.
	explicit_key = True

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self.rollback)
		if self.explicit_key:
			patcher = patch.dict(frappe.conf, {"encryption_key": KEY})
			patcher.start()
			self.addCleanup(patcher.stop)
		for field in routing.READER_FIELDS:
			raw(field, None)
		# The site date is fixed, so a run across midnight changes nothing (the counters use `COUNTER_DATE`).
		patcher = patch.object(routing, "_today", return_value=COUNTER_DATE)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.rows_before = None

	def rollback(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		forget_batch_state()
		banks._set_bank_route(None, None)
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.db.rollback()  # ends the transaction, so the raw writes leave no lock behind

	def watch_error_log(self):
		"""Count the reader and batch permission Error Log rows now; `assert_no_new_error_log_row` compares."""
		self.rows_before = error_rows()

	def assert_no_new_error_log_row(self):
		self.assertIsNotNone(self.rows_before, "the case did not call watch_error_log")
		self.assertEqual(error_rows(), self.rows_before)

	def assert_no_reader_key(self, company):
		self.assertEqual(reader_keys(company), [])

	def served(self, mode="on", company=MISSING_COMPANY, shadow_list=""):
		raw(routing.MODE_FIELD, mode)
		raw(routing.SERVED_FIELD, company)
		raw(routing.SHADOW_FIELD, shadow_list)

	def delete_day_counter(self, digest):
		frappe.cache.execute_command(
			"DEL", frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest}:{COUNTER_DATE}")
		)

	def restricted_user(self):
		"""A user who may read the invoices of one company but not the one of another; (user, names, denied)."""
		fx, _ = self.fixtures()
		other = CalendarFixtures()
		own = {
			fx.insert("Sales Invoice", f"Inert{n}", customer=fx.customer, company=fx.company)
			for n in range(5)
		}
		denied = other.insert("Sales Invoice", "InertDenied", customer=other.customer, company=other.company)
		user = fx.user()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Company",
				"for_value": fx.company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		self.addCleanup(frappe.clear_cache, user=user)
		return user, own | {denied}, denied

	def fixtures(self):
		"""A company with a fiscal year, a payable and a receivable invoice, and its scope."""
		fx = CalendarFixtures()
		fx.fiscal_year()
		frappe.cache().hdel("fiscal_years", fx.company)
		fx.invoice((100, 50), side="Payable", paid=30)
		fx.invoice((200,), side="Receivable", paid=20)
		sync_outstanding(fx.company)
		return fx, normalize_scope(fx.company, [fx.bank])

	def frappe_visible(self, doctype, names):
		"""The names the session user may read, by Frappe's own check, one document at a time."""
		return {name for name in names if frappe.has_permission(doctype, "read", doc=name)}

	def read_side(self, today_read, company, scope, outcomes):
		return routing.read_side(
			today_read,
			company,
			SIDE,
			scope["anchor"],
			scope["window_start"],
			scope["window_end"],
			started=monotonic(),
			outcomes=outcomes,
		)


@v15_only(V15)
class TestInactiveOnADefaultSite(InertCase):
	"""Reader mode `off`, both lists empty, the batch switch absent: the figures and the path are today's."""

	def test_a_default_site_never_calls_the_reader(self):
		fx, scope = self.fixtures()
		self.watch_error_log()
		spy, outcomes = MagicMock(), []
		with patch.object(builtin_reader, "read_obligations", spy):
			result = self.read_side(sources.read_obligations, fx.company, scope, outcomes)
		self.assertTrue(result["rows"])
		spy.assert_not_called()
		self.assertEqual((outcomes[0]["path"], outcomes[0]["outcome"]), ("today", "served"))
		read_log.reader_outcomes(outcomes, COUNTER_DATE)
		self.assert_no_reader_key(fx.company)
		self.assert_no_new_error_log_row()

	def test_a_default_site_keeps_the_too_large_refusal_and_writes_nothing(self):
		fx, scope = self.fixtures()
		self.watch_error_log()
		spy, outcomes = MagicMock(), []
		with (
			patch.object(builtin_reader, "read_obligations", spy),
			patch.object(sources, "MAX_VOUCHERS", 0),
			self.assertRaises(CalendarSourceUnavailable) as raised,
		):
			self.read_side(sources.read_obligations, fx.company, scope, outcomes)
		self.assertEqual(raised.exception.code, "too_large")
		spy.assert_not_called()
		self.assertEqual(outcomes[0]["step"], "settings")
		read_log.reader_outcomes(outcomes, COUNTER_DATE)
		self.assert_no_reader_key(fx.company)
		self.assert_no_new_error_log_row()

	def test_the_shadow_dispatch_enqueues_nothing_and_writes_no_row(self):
		with (
			patch.object(shadow.frappe, "enqueue") as enqueue,
			patch.object(read_log, "error_log_once") as error_log,
			patch.object(shadow_state, "start_night") as start_night,
		):
			shadow.dispatch()
		enqueue.assert_not_called()
		error_log.assert_not_called()
		start_night.assert_not_called()
		self.assertIsNone(shadow._runnable_units(routing.reader_settings(), COUNTER_DATE))

	def test_a_listed_but_missing_company_is_pinned_in_the_daily_row(self):
		"""Mode `off` with a saved list that names a company that no site has: the row says so, nothing runs."""
		raw(routing.MODE_FIELD, "off")
		raw(routing.SERVED_FIELD, MISSING_COMPANY)
		with (
			patch.object(shadow.frappe, "enqueue") as enqueue,
			patch.object(read_log, "error_log_once") as error_log,
		):
			shadow.dispatch()
		enqueue.assert_not_called()
		error_log.assert_called_once()
		title, message = error_log.call_args.args[:2]
		self.assertEqual(title, shadow.DAILY_TITLE)
		self.assertIn("listed_but_missing=1", message)
		self.assertIn(f"date={add_days(COUNTER_DATE, -1)}", message)

	def test_the_batch_switch_absent_is_the_per_document_loop_and_writes_nothing(self):
		"""The site must not set the switch (a site that does fails this case on purpose)."""
		user, names, denied = self.restricted_user()
		self.assertNotIn(batch_permission.SWITCH, frappe.conf)
		frappe.set_user(user)
		self.assertEqual(batch_permission._live_gate("Sales Invoice"), "switch_off")
		with (
			patch.object(batch_permission, "batch_plan") as plan,
			patch.object(read_log, "site_count") as site_count,
			patch.object(batch_permission, "queue_event") as queue_event,
		):
			result = batch_permission.batch_visible("Sales Invoice", names, "read")
		# An oracle that does not share the code under test: Frappe's own document check. It must filter.
		oracle = self.frappe_visible("Sales Invoice", names)
		self.assertTrue(oracle)
		self.assertIn(denied, names)
		self.assertNotIn(denied, oracle)
		self.assertEqual(result, oracle)
		plan.assert_not_called()
		site_count.assert_not_called()
		queue_event.assert_not_called()
		for name in (batch_permission.STATS, batch_permission.EVENTS, batch_permission.FREEZE):
			self.assertFalse(getattr(frappe.local, name, None), name)

	def test_the_batch_switch_on_stays_per_document_while_the_permission_pins_have_no_v15_entry(self):
		"""Switch = 1 by the request config only: the plan reason is `pin_drift` and Frappe's own check decides."""
		user, names, denied = self.restricted_user()
		self.assertGreater(len(names), batch_permission.SMALL_INPUT)
		frappe.set_user(user)
		with (
			patch.dict(frappe.local.conf, {batch_permission.SWITCH: 1}),
			# A key prefix of this case only: the site's own breaker keys are neither read nor deleted.
			patch.object(
				batch_permission, "BREAKER_KEY", f"jarvis:pcal:inert:{frappe.generate_hash(length=8)}:"
			),
			patch.object(frappe.installer, "update_site_config") as update_site_config,
			patch.object(read_log, "error_log_once") as error_log,
			patch.object(read_log, "site_count", return_value=1) as site_count,
		):
			forget_batch_state()
			result = batch_permission.batch_visible("Sales Invoice", names, "read")
			plan = batch_permission.batch_plan("Sales Invoice")
			oracle = self.frappe_visible("Sales Invoice", names)
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "pin_drift"))
		self.assertTrue(oracle)
		self.assertNotIn(denied, oracle)
		self.assertEqual(result, oracle)
		update_site_config.assert_not_called()
		site_count.assert_any_call("permission_batch:pin_drift", COUNTER_DATE)
		# The pin check of the plan builder queues one event for the drift, and nothing else is written.
		titles = {call.args[0] for call in error_log.call_args_list}
		self.assertEqual(titles, {f"{batch_permission.EVENT_TITLE} pcal_permission_pin_drift"})

	def test_batch_permission_status_on_a_default_site_only_reads(self):
		"""The operator's recipe (`batch_permission.status`): the switch is off, the plans are per document, nothing is written."""
		self.watch_error_log()
		with (
			# a key prefix of this case only: no breaker of the site is read
			patch.object(
				batch_permission, "BREAKER_KEY", f"jarvis:pcal:inert:{frappe.generate_hash(length=8)}:"
			),
			patch.object(read_log, "site_count") as site_count,
		):
			report = batch_permission.status()
		self.assertFalse(report["switch"])
		self.assertEqual(
			(report["breakers"], report["latch_write_failed"], report["counters"]), ([], False, {})
		)
		self.assertTrue(report["pin_drift"])  # no v15 entry: the pins have drifted by definition
		self.assertEqual(
			{doctype: (plan["mode"], plan["reason"]) for doctype, plan in report["plans"].items()},
			{doctype: ("per_doc", "pin_drift") for doctype in batch_permission.EXPECT_BATCH},
		)
		site_count.assert_not_called()
		self.assert_no_new_error_log_row()

	def test_the_bank_path_with_defaults_is_todays_loop_and_sets_no_path(self):
		fx, _ = self.fixtures()
		fx.gl(1000)
		fx.gl(-250)
		reads = MagicMock(wraps=routing._read)
		inputs = MagicMock(wraps=banks.bank_read_inputs)
		with patch.object(routing, "_read", reads), patch.object(banks, "bank_read_inputs", inputs):
			reply = banks.read_bank_balances(fx.company, [fx.bank], fx.today)
		direct = banks.today_amounts(fx.company, [fx.bank], fx.today, get_balance_on)
		self.assertEqual(flt(reply["accounts"][0]["balance"]), flt(direct[fx.bank], reply["precision"]))
		# The fixture posts 1000 and -250: a figure that does not come from the code under test.
		self.assertEqual(reply["total"], "750.00")
		self.assertIsNone(getattr(frappe.local, banks.BANK_PATH_KEY, None))
		self.assertIsNone(getattr(frappe.local, banks.BANK_FALLBACK_KEY, None))
		# The extra work of a bank read in mode `off` (D-6): one read of the mode and one read of the inputs
		# (which include the company's default finance book).
		reads.assert_called_once_with(routing.MODE_FIELD)
		inputs.assert_called_once()


@v15_only(V15)
class TestSettingsSave(InertCase):
	explicit_key = False

	def test_a_settings_save_works_and_a_reader_only_save_runs_no_llm_sync(self):
		frappe.db.delete("Jarvis LLM Pool Model", {"parent": SETTINGS, "parenttype": SETTINGS})
		frappe.db.set_single_value(SETTINGS, "preset", "")
		self.assertTrue(all(stored(field) is None for field in routing.READER_FIELDS))
		self.addCleanup(frappe.clear_document_cache, SETTINGS, SETTINGS)
		cls = jarvis_settings.JarvisSettings
		with (
			patch.object(cls, "_on_update_single_model_legacy") as legacy,
			patch.object(cls, "_on_update_unified_llm") as unified,
			patch("frappe.enqueue"),
		):
			settings = frappe.get_single(SETTINGS)
			settings.save()
			self.assertEqual(legacy.call_count + unified.call_count, 1)
			legacy.reset_mock()
			unified.reset_mock()
			settings = frappe.get_single(SETTINGS)
			settings.set(routing.MODE_FIELD, "shadow")
			settings.save()
			legacy.assert_not_called()
			unified.assert_not_called()
		self.assertEqual(routing.reader_settings().mode, "shadow")


@v15_only(V15)
class TestInactiveUntilS6(InertCase):
	"""Mode `on` with a served company, a key and the reader code present: still today's read on v15.

	S6 replaces each assertion here with the v15-serving one.
	"""

	def test_a_served_side_takes_the_v15_step_and_the_reader_is_never_called(self):
		fx, scope = self.fixtures()
		self.served("on", fx.company)
		reader, probe, outcomes = MagicMock(), MagicMock(), []
		with (
			patch.object(builtin_reader, "read_obligations", reader),
			patch.object(sources, "_ledger_rows_exceed", probe),
		):
			result = self.read_side(sources.read_obligations, fx.company, scope, outcomes)
			self.assertTrue(result["rows"])
			self.assertEqual((outcomes[0]["path"], outcomes[0]["outcome"]), ("today", "served"))
			with (
				patch.object(sources, "MAX_VOUCHERS", 0),
				self.assertRaises(CalendarSourceUnavailable) as raised,
			):
				self.read_side(sources.read_obligations, fx.company, scope, outcomes)
		self.assertEqual(raised.exception.code, "too_large")
		self.assertEqual(outcomes[1]["step"], "v15")
		self.assertEqual(outcomes[1]["path"], "today")
		reader.assert_not_called()
		probe.assert_not_called()

	def test_the_v15_step_is_one_day_counter_and_no_error_log_row(self):
		fx, scope = self.fixtures()
		self.watch_error_log()
		self.served("on", fx.company)
		digest = read_log.company_digest(fx.company)
		self.addCleanup(self.delete_day_counter, digest)
		outcomes = []
		with patch.object(sources, "MAX_VOUCHERS", 0), self.assertRaises(CalendarSourceUnavailable):
			self.read_side(sources.read_obligations, fx.company, scope, outcomes)
		read_log.reader_outcomes(outcomes, COUNTER_DATE)
		self.assertEqual(read_log.day_counts(digest, COUNTER_DATE), {"v15": 1})
		self.assert_no_new_error_log_row()
		# The next morning's daily row is the operator's signal: it carries `v15=1` for the served company.
		with (
			patch.object(routing, "_today", return_value=add_days(COUNTER_DATE, 1)),
			patch.object(shadow.frappe, "enqueue") as enqueue,
			patch.object(read_log, "error_log_once") as error_log,
		):
			shadow.dispatch()
		enqueue.assert_not_called()
		error_log.assert_called_once()
		self.assertEqual(error_log.call_args.args[0], shadow.DAILY_TITLE)
		self.assertIn("v15=1", error_log.call_args.args[1])

	def test_the_bank_path_of_a_served_company_is_today_with_the_v15_cause(self):
		fx, _ = self.fixtures()
		self.watch_error_log()
		fx.gl(1000)
		self.served("on", fx.company)
		with (
			patch.object(banks, "_logged_fallbacks", set()),
			patch.object(read_log, "speed_fallback") as speed_fallback,
			patch.object(read_log, "site_count") as site_count,
		):
			banks.read_bank_balances(fx.company, [fx.bank], fx.today)
			log = snapshot._bank_log(monotonic())
			snapshot._count_bank_fallback(log)
		self.assertEqual(getattr(frappe.local, banks.BANK_PATH_KEY), "today")
		self.assertEqual(getattr(frappe.local, banks.BANK_FALLBACK_KEY), "v15")
		self.assertEqual((log["path"], log["fallback"]), ("today", "v15"))
		speed_fallback.assert_called_once()
		self.assertEqual(speed_fallback.call_args.args[0], "bank_one_pass:v15")
		site_count.assert_called_once_with("bank_today:v15", COUNTER_DATE)
		self.assert_no_new_error_log_row()

	def test_the_reader_refuses_an_unsupported_major(self):
		with self.assertRaises(ReaderFallback) as raised:
			builtin_reader.require_supported_source()
		self.assertEqual(raised.exception.reason, "unsupported_major")

	def test_the_shadow_enqueues_nothing_in_mode_on_or_shadow(self):
		fx, _ = self.fixtures()  # a real company, so the lists are not the "listed but missing" case
		for mode in ("on", "shadow"):
			with self.subTest(mode=mode):
				self.served(mode, fx.company, fx.company)
				self.assertEqual(routing.listed_but_missing(routing.reader_settings()), ())
				self.assertIsNone(shadow._runnable_units(routing.reader_settings(), COUNTER_DATE))
				with (
					patch.object(shadow.frappe, "enqueue") as enqueue,
					patch.object(read_log, "error_log_once"),
					patch.object(shadow_state, "start_night") as start_night,
				):
					shadow.dispatch()
				enqueue.assert_not_called()
				start_night.assert_not_called()


@v15_only(V15)
class TestAClosedConnectionPoisonsTheViewOnV15(TestCase):
	"""D-4 and D-5: a rolled-back or closed-connection error in a Settings read breaks the snapshot view, in mode `off` too.

	A plain `TestCase`, no fixtures and no writes: `consistent_read()` rolls back, which would end a savepoint.
	"""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		patcher = patch.object(routing, "_today", return_value=COUNTER_DATE)
		patcher.start()
		self.addCleanup(patcher.stop)

	def read_settings_in_a_view(self, error):
		"""`reader_settings()` inside a view whose Settings read raises `error`: (what it saw, the exit refusal)."""
		seen, refusal = {}, None
		try:
			with patch.object(routing, "_read", side_effect=error), snapshot.consistent_read():
				seen["settings"] = routing.reader_settings()
				seen["broken"] = snapshot.view_is_broken()
		except CalendarSourceUnavailable as exc:
			refusal = exc
		return seen, refusal

	def test_a_rolled_back_or_closed_connection_error_breaks_the_view_and_the_exit_says_source_changed(self):
		errors = (frappe.db.OperationalError(1213, "x"), interface_error())
		for error in errors:
			with self.subTest(type(error).__name__):
				seen, refusal = self.read_settings_in_a_view(error)
				self.assertEqual(seen["settings"], routing._OFF)
				self.assertTrue(seen["broken"])
				self.assertEqual(refusal.code, "source_changed")

	def test_a_statement_error_does_not_break_the_view(self):
		seen, refusal = self.read_settings_in_a_view(frappe.db.OperationalError(1054, "x"))
		self.assertEqual(seen["settings"], routing._OFF)
		self.assertFalse(seen["broken"])
		self.assertIsNone(refusal)

	def test_a_broken_view_makes_the_router_refuse_with_the_view_broken_step(self):
		outcomes = []
		with self.assertRaises(CalendarSourceUnavailable) as exit_refusal, snapshot.consistent_read():
			with patch.object(routing, "_read", side_effect=interface_error()):
				routing.reader_settings()
			with self.assertRaises(CalendarSourceUnavailable) as refused:
				routing.read_side(
					too_large,
					MISSING_COMPANY,
					SIDE,
					COUNTER_DATE,
					COUNTER_DATE,
					COUNTER_DATE,
					started=monotonic(),
					outcomes=outcomes,
				)
		self.assertEqual(refused.exception.code, "too_large")
		self.assertEqual(outcomes[0]["step"], "view_broken")
		self.assertEqual(exit_refusal.exception.code, "source_changed")
