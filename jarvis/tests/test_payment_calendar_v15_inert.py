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
	compatibility,
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
from jarvis.tests.payment_calendar_majors import v15_only

SETTINGS = routing.SETTINGS_DOCTYPE
KEY = "k" * 44
SIDE = "Receivable"
MISSING_COMPANY = "Inert Test Company That No Site Has"
# A far-future date, so the counters of these cases never meet a real day's counters.
COUNTER_DATE = "2031-08-14"
SAVEPOINT = "calendar_v15_inert"
# The Error Log titles of the reader and of the batch permission check (the other Payment Calendar titles are old).
ERROR_LOG_TITLES = ("Payment Calendar reader%", "Payment Calendar batch permission%")
V15 = "no v15 reader pins until S6"


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
	"""The Error Log rows with a reader or batch permission title; the case compares its own before and after."""
	return sum(frappe.db.count("Error Log", {"method": ["like", title]}) for title in ERROR_LOG_TITLES)


def reader_keys(company: str) -> set:
	"""The memo, in-flight and day keys of one company (by its digest). A fixture company is new to the site."""
	digest = read_log.company_digest(company)
	pattern = frappe.cache.make_key(f"jarvis:pcal:reader:*:{digest}:*")
	return set(frappe.cache.execute_command("KEYS", pattern))


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
		self.rows_before = error_rows()

	def rollback(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		forget_batch_state()
		banks._set_bank_route(None, None)
		frappe.db.rollback(save_point=SAVEPOINT)

	def assert_no_new_error_log_row(self):
		self.assertEqual(error_rows(), self.rows_before)

	def assert_no_reader_key(self, company):
		self.assertEqual(reader_keys(company), set())

	def served(self, mode="on", company=MISSING_COMPANY, shadow_list=""):
		raw(routing.MODE_FIELD, mode)
		raw(routing.SERVED_FIELD, company)
		raw(routing.SHADOW_FIELD, shadow_list)

	def delete_day_counter(self, digest):
		frappe.cache.execute_command(
			"DEL", frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest}:{COUNTER_DATE}")
		)

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
		fx, _ = self.fixtures()
		names = {
			fx.insert("Sales Invoice", f"Inert{n}", customer=fx.customer, company=fx.company)
			for n in range(6)
		}
		self.assertNotIn(batch_permission.SWITCH, frappe.conf)
		self.assertEqual(batch_permission._live_gate("Sales Invoice"), "switch_off")
		with (
			patch.object(batch_permission, "batch_plan") as plan,
			patch.object(read_log, "site_count") as site_count,
			patch.object(batch_permission, "queue_event") as queue_event,
		):
			result = batch_permission.batch_visible("Sales Invoice", names, "read")
		# An oracle that does not share the code under test: Frappe's own document check.
		self.assertEqual(result, self.frappe_visible("Sales Invoice", names))
		self.assertEqual(result, names)
		plan.assert_not_called()
		site_count.assert_not_called()
		queue_event.assert_not_called()
		for name in (batch_permission.STATS, batch_permission.EVENTS, batch_permission.FREEZE):
			self.assertFalse(getattr(frappe.local, name, None), name)

	def test_the_batch_switch_on_stays_per_document_while_the_permission_pins_have_no_v15_entry(self):
		"""Switch = 1 by the request config only: the plan reason is `pin_drift` and Frappe's own check decides."""
		fx, _ = self.fixtures()
		names = {
			fx.insert("Sales Invoice", f"Inert{n}", customer=fx.customer, company=fx.company)
			for n in range(6)
		}
		user = fx.user()
		self.assertGreater(len(names), batch_permission.SMALL_INPUT)
		frappe.set_user(user)
		for key in ("Sales Invoice", batch_permission.LATCH_FAILED):
			frappe.cache.execute_command("DEL", frappe.cache.make_key(batch_permission.BREAKER_KEY + key))
		with (
			patch.dict(frappe.local.conf, {batch_permission.SWITCH: 1}),
			patch.object(frappe.installer, "update_site_config", side_effect=AssertionError("site config")),
			patch.object(read_log, "error_log_once") as error_log,
			patch.object(read_log, "site_count", return_value=1) as site_count,
		):
			forget_batch_state()
			result = batch_permission.batch_visible("Sales Invoice", names, "read")
			plan = batch_permission.batch_plan("Sales Invoice")
			oracle = self.frappe_visible("Sales Invoice", names)
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "pin_drift"))
		self.assertTrue(result)
		self.assertEqual(result, oracle)
		site_count.assert_any_call("permission_batch:pin_drift", COUNTER_DATE)
		# The pin check of the plan builder queues one event for the drift, and nothing else is written.
		titles = {call.args[0] for call in error_log.call_args_list}
		self.assertEqual(titles, {f"{batch_permission.EVENT_TITLE} pcal_permission_pin_drift"})

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
		self.assertEqual(flt(reply["total"]), flt(reply["accounts"][0]["balance"]))
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
		self.served("on", fx.company)
		digest = read_log.company_digest(fx.company)
		self.addCleanup(self.delete_day_counter, digest)
		outcomes = []
		with patch.object(sources, "MAX_VOUCHERS", 0), self.assertRaises(CalendarSourceUnavailable):
			self.read_side(sources.read_obligations, fx.company, scope, outcomes)
		read_log.reader_outcomes(outcomes, COUNTER_DATE)
		self.assertEqual(read_log.day_counts(digest, COUNTER_DATE), {"v15": 1})
		self.assert_no_new_error_log_row()

	def test_the_bank_path_of_a_served_company_is_today_with_the_v15_cause(self):
		fx, _ = self.fixtures()
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
		self.assertEqual(compatibility.source_profile().major, "15")

	def test_the_shadow_enqueues_nothing_in_mode_on_or_shadow(self):
		for mode in ("on", "shadow"):
			with self.subTest(mode=mode):
				self.served(mode, MISSING_COMPANY, MISSING_COMPANY)
				self.assertIsNone(shadow._runnable_units(routing.reader_settings(), COUNTER_DATE))
				with (
					patch.object(shadow.frappe, "enqueue") as enqueue,
					patch.object(read_log, "error_log_once"),
					patch.object(shadow_state, "start_night") as start_night,
				):
					shadow.dispatch()
				enqueue.assert_not_called()
				start_night.assert_not_called()
