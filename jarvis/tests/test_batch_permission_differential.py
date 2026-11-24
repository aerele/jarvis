"""The differential runs the batch with the switch at 0 and a breaker key, selects the
special documents by SQL, and changes nothing.

Names these tests rely on in `batch_permission`:
- `differential(users=None, *, names=None, expect_batch=..., time_box_s=...)` returns `{"users": {user: {doctype:
  {"mode", "reason", "batch_evaluated", "compared", "supplied", "selected", "differences", "branches"}}},
  "gate_failed": [{"user", "doctype", "reason"}], "not_compared": [user], "not_done": [user], "names_cut":
  [{"user", "doctype", "selector"}], "plan_events": [{"operation", "message"}], "differences": int, "elapsed_s": float, "gate_passed": bool}`.
- `names`: `{user: {doctype: iterable of names}}`, the names the calendar passes to `visible_names` (captured by
  the outside pilot script). The compared set is these names plus the SQL-selected documents.
- `_selected_names(user, doctype) -> (set of names, list of the selectors that hit the limit)`; selectors:
  `owner`, `shared`, `child_row`, `restricted_master`; `DIFFERENTIAL_NAME_LIMIT` is the limit for each.
- `_differential_users() -> list[str]` (real SQL).
- `_sample_oracle` and `_trip` as in `test_batch_permission_limits`.

The real site config is never written (a recorder stands in for `frappe.installer.update_site_config`; the file
bytes are compared before and after). Breaker keys use a private prefix.
"""

import contextlib
import functools
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import frappe
import frappe.installer

from jarvis.payment_calendar import batch_permission, compatibility, read_log
from jarvis.tests.batch_permission_fixtures import SI, BatchCase, clear_plan_memo
from jarvis.tests.test_batch_permission_limits import PRIVATE_BREAKER, FakeClock, Recorder, redis

SWITCH = batch_permission.SWITCH
DB_CLASS = type(frappe.db._get_current_object())  # `frappe.db` is a proxy: patch the real class
CUSTOMER = "Customer"
EXPECT = (SI, CUSTOMER)
WRITES = ("insert", "update", "delete", "replace", "alter", "create", "drop", "truncate")


class DifferentialCase(BatchCase):
	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		self.recorder = Recorder()
		self.statements: list[str] = []
		self.queries: list[str] = []
		real_sql = frappe.db.sql

		def sql(query, *args, **kwargs):
			self.statements.append(str(query).lstrip().split(None, 1)[0].lower())
			self.queries.append(str(query))
			return real_sql(query, *args, **kwargs)

		self.spies = {}
		for target, name in (
			(batch_permission, "_live_gate"),
			(batch_permission, "_sample_oracle"),
			(batch_permission, "_trip"),
			(read_log, "site_count"),
			(read_log, "error_log_once"),
		):
			# `create=True`: before the sample and the trip exist, the red must come from `differential`
			self.spies[name] = stack.enter_context(patch.object(target, name, create=True))
		self.spies["_live_gate"].side_effect = AssertionError("the differential never calls the live gate")
		self.evaluate = stack.enter_context(
			patch.object(batch_permission, "_evaluate_batch", wraps=batch_permission._evaluate_batch)
		)
		# Patched on the class, never on the instance: `"rollback" in vars(frappe.db)` is how the code tells that
		# a snapshot view is open, and it must stay as in production (false) in these tests.
		self.commit = stack.enter_context(patch.object(DB_CLASS, "commit", autospec=True))
		# The differential ends with a rollback; here that would also drop the uncommitted fixtures.
		self.rollback = stack.enter_context(patch.object(DB_CLASS, "rollback", autospec=True))
		self.assertNotIn("rollback", vars(frappe.db))
		for patcher in (
			patch.object(batch_permission, "BREAKER_KEY", PRIVATE_BREAKER),
			patch.object(frappe.installer, "update_site_config", self.recorder),
			patch.dict(frappe.local.conf, {SWITCH: 0}),
			patch.object(frappe.db, "sql", sql),
		):
			stack.enter_context(patcher)
		self.clean_keys()
		self.addCleanup(self.clean_keys)
		clear_plan_memo()
		self.addCleanup(clear_plan_memo)
		self.addCleanup(frappe.set_user, "Administrator")

	def clean_keys(self):
		for key in redis("KEYS", frappe.cache.make_key(PRIVATE_BREAKER + "*")):
			redis("DEL", key)

	def breaker_keys(self):
		return sorted(redis("KEYS", frappe.cache.make_key(PRIVATE_BREAKER + "*")))

	def permission_counters(self):
		return sorted(redis("KEYS", frappe.cache.make_key("jarvis:pcal:site:day:*:permission_*")))

	def config_bytes(self):
		return (Path(frappe.local.site_path) / "site_config.json").read_bytes()

	def error_log_count(self):
		return frappe.db.count("Error Log")

	def supplied(self, user=None):
		"""The calendar's names, as the pilot script would pass them: every fixture document."""
		return {user or self.f.u1: {SI: set(self.f.names(SI)), CUSTOMER: set(self.f.names(CUSTOMER))}}

	def run_differential(self, users=None, names=True, **options):
		users = [self.f.u1] if users is None else users
		if names is True:
			names = self.supplied(users[0] if users else None)
		options.setdefault("show_users", True)  # the tests read results by user id
		return batch_permission.differential(users, names=names, expect_batch=EXPECT, **options)

	def wrong(self, change):
		"""`_evaluate_batch` with `change(result, user)` applied to its answer: a forced fault."""
		real = batch_permission._evaluate_batch

		def evaluate(*args, **kwargs):
			result = real(*args, **kwargs)
			# only names of the compared set can be wrong: the fault must not add a name nobody compared
			return replace(result, visible=change(result.visible) & set(args[1]))

		return patch.object(batch_permission, "_evaluate_batch", evaluate)


class TestDifferentialRunsTheBatch(DifferentialCase):
	def setUp(self):
		super().setUp()
		for doctype in (SI, CUSTOMER):
			redis("SET", frappe.cache.make_key(PRIVATE_BREAKER + doctype), "now|mismatch")

	def test_every_expected_doctype_runs_in_batch_mode_with_the_switch_at_zero_and_a_breaker_key(self):
		self.assertFalse(frappe.conf.get(SWITCH))
		result = self.run_differential()
		per_doctype = result["users"][self.f.u1]
		for doctype in EXPECT:
			with self.subTest(doctype=doctype):
				row = per_doctype[doctype]
				self.assertEqual((row["mode"], row["differences"]), ("batch", 0))
				self.assertGreater(row["batch_evaluated"], 0)
				self.assertGreaterEqual(row["compared"], len(self.f.names(doctype)))
				self.assertTrue(row["branches"])
		self.assertEqual(result["gate_failed"], [])
		self.assertEqual({call.args[0] for call in self.evaluate.call_args_list}, set(EXPECT))

	def test_it_never_calls_the_live_gate_the_sample_or_the_trip(self):
		self.run_differential()
		self.spies["_live_gate"].assert_not_called()
		self.spies["_sample_oracle"].assert_not_called()
		self.spies["_trip"].assert_not_called()

	def test_it_changes_no_breaker_key_no_site_config_no_counter_and_no_error_log_row(self):
		keys, config, counters, rows = (
			self.breaker_keys(),
			self.config_bytes(),
			self.permission_counters(),
			self.error_log_count(),
		)
		self.run_differential()
		self.assertEqual(self.breaker_keys(), keys)
		self.assertEqual(self.config_bytes(), config)
		self.assertEqual(self.recorder.calls, [])
		self.assertEqual(self.permission_counters(), counters)
		self.spies["site_count"].assert_not_called()
		self.spies["error_log_once"].assert_not_called()
		self.assertEqual(self.error_log_count(), rows)

	def test_it_is_read_only_uses_parameters_only_and_rolls_back_at_the_end(self):
		self.run_differential()
		self.commit.assert_not_called()
		self.rollback.assert_called()
		self.assertFalse([word for word in self.statements if word in WRITES])
		for query in self.queries:
			self.assertNotIn(self.f.u1, query)
			self.assertNotIn(self.f.si_plain, query)

	def test_the_output_carries_the_time_and_the_gate(self):
		result = self.run_differential()
		self.assertIsInstance(result["elapsed_s"], float)
		self.assertEqual(result["names_cut"], [])
		self.assertEqual(result["not_done"], [])
		self.assertTrue(result["gate_passed"])

	def test_it_restores_the_session_user(self):
		self.run_differential()
		self.assertEqual(frappe.session.user, "Administrator")


class TestDifferentialGate(DifferentialCase):
	def test_a_doctype_that_a_guard_sends_to_the_loop_fails_the_gate_with_the_reason(self):
		real = batch_permission._mro_reason

		def guarded(doctype, layout):
			return "controller_override" if doctype == CUSTOMER else real(doctype, layout)

		with patch.object(batch_permission, "_mro_reason", guarded):
			result = self.run_differential()
		self.assertEqual(
			[(row["user"], row["doctype"], row["reason"]) for row in result["gate_failed"]],
			[(self.f.u1, CUSTOMER, "controller_override")],
		)
		row = result["users"][self.f.u1][CUSTOMER]
		self.assertEqual((row["mode"], row["reason"]), ("per_doc", "controller_override"))
		self.assertEqual(result["users"][self.f.u1][SI]["mode"], "batch")

	def test_a_user_with_a_non_company_user_permission_is_listed_as_not_compared(self):
		result = self.run_differential([self.f.u7])
		self.assertIn(self.f.u7, result["not_compared"])
		self.assertEqual(
			[row["reason"] for row in result["gate_failed"]], ["not_exercised"], "nothing was compared"
		)
		self.assertFalse(result["gate_passed"])

	def test_a_doctype_with_names_and_no_batch_evaluated_fails_the_gate(self):
		real = batch_permission._evaluate_batch

		def nothing_evaluated(*args, **kwargs):
			from dataclasses import replace

			return replace(real(*args, **kwargs), batch_evaluated=0)

		with patch.object(batch_permission, "_evaluate_batch", nothing_evaluated):
			result = self.run_differential()
		self.assertEqual({row["doctype"] for row in result["gate_failed"]}, set(EXPECT))


class TestSelectors(DifferentialCase):
	"""Each special document is found by SQL, and an ordinary one is not."""

	def selected(self, user, doctype=SI):
		names, cut = batch_permission._selected_names(user, doctype)
		return names, cut

	def test_a_document_that_the_user_owns(self):
		f = self.f
		names, _ = self.selected(f.u4)
		self.assertIn(f.si_owner_b, names)
		self.assertNotIn(f.si_plain, names)

	def test_a_share_for_the_user_or_for_everyone_but_not_a_share_without_read(self):
		f = self.f
		names, _ = self.selected(f.u1)
		self.assertTrue({f.si_shared, f.si_everyone} <= names)
		self.assertNotIn(f.si_share_no_read, names)

	def test_a_child_row_outside_the_allowed_values_or_empty(self):
		f = self.f
		names, _ = self.selected(f.u1)
		self.assertTrue({f.si_twe_b, f.si_twe_empty} <= names)
		self.assertNotIn(f.si_twe_a, names)
		self.assertNotIn(f.si_plain, names)

	def test_a_link_to_a_master_restricted_to_companies(self):
		f = self.f
		names, _ = self.selected(f.u1)
		self.assertTrue({f.si_restr_b, f.si_restr_a} <= names)
		self.assertNotIn(f.si_plain, names)

	def test_a_party_that_is_restricted_to_companies_itself(self):
		f = self.f
		names, _ = self.selected(f.u1, CUSTOMER)
		self.assertTrue({f.cust_restricted_b, f.cust_restricted_a} <= names)
		self.assertNotIn(f.cust_plain, names)

	def test_a_limit_is_reported_never_silent(self):
		with patch.object(batch_permission, "DIFFERENTIAL_NAME_LIMIT", 1):
			names, cut = self.selected(self.f.u1)
		self.assertIn("shared", cut)
		self.assertLessEqual(len(names), 4 * 1)

	def test_the_output_names_the_cut_selector_for_the_user_and_doctype(self):
		with patch.object(batch_permission, "DIFFERENTIAL_NAME_LIMIT", 1):
			result = self.run_differential(names=None)
		self.assertIn({"user": self.f.u1, "doctype": SI, "selector": "shared"}, result["names_cut"])
		self.assertFalse(result["gate_passed"], "a cut name set is not a complete differential")

	def test_the_default_users_are_every_user_with_a_user_permission_and_five_without(self):
		users = batch_permission._differential_users()
		with_permissions = set(frappe.get_all("User Permission", pluck="user"))
		self.assertTrue({self.f.u1, self.f.u4} <= set(users))
		self.assertLessEqual(len([user for user in users if user not in with_permissions]), 5)
		self.assertEqual(len(users), len(set(users)))


class TestGateCatchesWhatTheSelectorsFind(DifferentialCase):
	def test_a_fault_in_the_child_row_branch_is_caught_without_any_supplied_name(self):
		f = self.f
		with self.wrong(lambda visible: visible | {f.si_twe_b}):  # the child-row deny is lost
			result = self.run_differential(names=None)
		self.assertGreaterEqual(result["users"][f.u1][SI]["differences"], 1)
		self.assertFalse(result["gate_passed"])

	def test_a_fault_in_the_restricted_master_branch_is_caught_too(self):
		f = self.f
		with self.wrong(lambda visible: visible ^ {f.si_restr_b}):
			result = self.run_differential(names=None)
		self.assertGreaterEqual(result["differences"], 1)

	def test_without_a_fault_the_selected_documents_give_no_difference(self):
		result = self.run_differential(names=None)
		self.assertEqual(result["differences"], 0)
		self.assertTrue(result["gate_passed"])

	def test_the_supplied_names_are_compared_too(self):
		f = self.f
		with self.wrong(lambda visible: visible - {f.si_plain}):
			unsupplied = self.run_differential(names=None)
			supplied = self.run_differential(names={f.u1: {SI: [f.si_plain]}})
		self.assertEqual(unsupplied["differences"], 0, "an ordinary document is selected by no selector")
		self.assertGreaterEqual(supplied["users"][f.u1][SI]["differences"], 1)
		self.assertGreaterEqual(supplied["users"][f.u1][SI]["supplied"], 1)


class TestTimeBox(DifferentialCase):
	"""The time box ends the run between users and between doctypes; the cut users are `not_done`."""

	def run_on_a_clock(self, users, box):
		clock = FakeClock(100.0)
		real = batch_permission._evaluate_batch

		def evaluate(*args, **kwargs):
			clock.now += 10
			return real(*args, **kwargs)

		with (
			patch.object(batch_permission, "monotonic", clock),
			patch.object(batch_permission, "_evaluate_batch", evaluate),
		):
			return self.run_differential(users, time_box_s=box)

	def test_a_user_that_the_box_did_not_reach_is_not_done(self):
		result = self.run_on_a_clock([self.f.u1, self.f.u3], 15)
		self.assertEqual(result["not_done"], [self.f.u3])
		self.assertNotIn(self.f.u3, result["users"])
		self.assertFalse(result["gate_passed"])
		self.assertEqual(result["elapsed_s"], 20.0)

	def test_the_box_is_checked_between_the_doctypes_of_one_user(self):
		result = self.run_on_a_clock([self.f.u1], 5)
		self.assertEqual(result["not_done"], [self.f.u1])
		self.assertEqual(list(result["users"][self.f.u1]), [SI])
		self.assertFalse(result["gate_passed"])

	def test_inside_the_box_every_user_is_done(self):
		result = self.run_on_a_clock([self.f.u1, self.f.u3], 10_000)
		self.assertEqual(result["not_done"], [])
		self.assertTrue(result["gate_passed"])


class TestNothingIsWritten(DifferentialCase):
	"""A plan that hits a drift queues events; the differential reports them and writes nothing."""

	def keys(self):
		return sorted(
			key
			for pattern in (
				"jarvis:pcal:errlog:*",
				"jarvis:pcal:site:day:*",
				"jarvis:pcal:permission_breaker:*",
			)
			for key in redis("KEYS", frappe.cache.make_key(pattern))
		)

	def wrapper(self):
		"""A real identity failure: a wrapper installed on a pinned callable (the shape another app gives it)."""
		original = frappe.is_table

		@functools.wraps(original)
		def wrapped(*args, **kwargs):
			return original(*args, **kwargs)

		return patch.object(frappe, "is_table", wrapped)

	def drift(self):
		return patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"])

	def run_with(self, fixture):
		before = (
			self.error_log_count(),
			self.keys(),
			list(getattr(frappe.local, "pcal_permission_events", None) or []),
		)
		with fixture():
			result = self.run_differential()
		return before, result

	def assert_nothing_written(self, before, result):
		rows, keys, events = before
		self.spies["error_log_once"].assert_not_called()
		self.assertEqual(self.error_log_count(), rows)
		self.assertEqual(self.keys(), keys)
		self.assertEqual(list(getattr(frappe.local, "pcal_permission_events", None) or []), events)
		self.assertFalse(getattr(frappe.local, batch_permission.QUIET, False))
		self.assertNotIn("rollback", vars(frappe.db))

	def test_an_identity_failure_is_reported_and_nothing_is_written(self):
		before, result = self.run_with(self.wrapper)
		self.assert_nothing_written(before, result)
		self.assertIn("pcal_permission_identity", [event["operation"] for event in result["plan_events"]])
		row = result["users"][self.f.u1][SI]
		self.assertEqual((row["mode"], row["reason"]), ("per_doc", "entry_patched"))
		self.assertFalse(result["gate_passed"])

	def test_a_pin_drift_is_reported_and_nothing_is_written(self):
		before, result = self.run_with(self.drift)
		self.assert_nothing_written(before, result)
		self.assertIn("pcal_permission_pin_drift", [event["operation"] for event in result["plan_events"]])
		self.assertEqual(result["users"][self.f.u1][SI]["reason"], "pin_drift")

	def test_without_a_drift_there_are_no_plan_events(self):
		before, result = self.run_with(contextlib.nullcontext)
		self.assert_nothing_written(before, result)
		self.assertEqual(result["plan_events"], [])
		self.assertEqual(result["users"][self.f.u1][SI]["mode"], "batch")

	def test_an_event_written_before_the_run_stays_and_the_run_adds_none(self):
		batch_permission.queue_event("pcal_permission_pin_drift", "earlier")
		self.spies["error_log_once"].reset_mock()
		before, result = self.run_with(self.drift)
		self.assertEqual(before[2][0]["message"], "earlier")
		self.assert_nothing_written(before, result)


class TestHardening(DifferentialCase):
	"""A pass must mean that something was compared, and a bad call must fail loudly."""

	def test_users_must_be_a_list(self):
		with self.assertRaises(TypeError):
			batch_permission.differential(self.f.u1, expect_batch=EXPECT)

	def test_administrator_and_guest_are_not_compared(self):
		result = self.run_differential(["Administrator", "Guest"])
		self.assertEqual(result["not_compared"], ["Administrator", "Guest"])
		self.assertFalse(result["gate_passed"])
		self.assertEqual([row["reason"] for row in result["gate_failed"]], ["not_exercised"])

	def test_a_missing_doctype_is_a_gate_row_not_an_abort(self):
		result = batch_permission.differential(
			[self.f.u1], names=self.supplied(), expect_batch=(SI, "Jpc Missing Doctype"), show_users=True
		)
		rows = [row for row in result["gate_failed"] if row["doctype"] == "Jpc Missing Doctype"]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["reason"], "plan_error")  # the plan fails closed itself
		self.assertIn(SI, result["users"][self.f.u1])
		self.assertFalse(result["gate_passed"])

	def test_no_users_is_not_a_pass(self):
		result = self.run_differential([], names=None)
		self.assertFalse(result["gate_passed"])
		self.assertEqual([row["reason"] for row in result["gate_failed"]], ["not_exercised"])

	def test_a_doctype_that_no_user_had_names_for_is_not_exercised(self):
		with patch.object(batch_permission, "_selected_names", return_value=(set(), [])):
			result = self.run_differential(names=None)
		self.assertEqual({row["doctype"] for row in result["gate_failed"]}, set(EXPECT))
		self.assertTrue(all(row["reason"] == "not_exercised" for row in result["gate_failed"]))
		self.assertFalse(result["gate_passed"])

	def test_an_error_in_the_rollback_does_not_hide_the_first_error(self):
		self.rollback.side_effect = RuntimeError("rollback failed")
		with (
			patch.object(batch_permission, "_differential_user", side_effect=ValueError("first")),
			self.assertRaises(ValueError),
		):
			self.run_differential()

	def test_an_error_in_the_rollback_alone_does_not_fail_the_run(self):
		self.rollback.side_effect = RuntimeError("rollback failed")
		self.assertTrue(self.run_differential()["gate_passed"])

	def test_users_are_shown_by_a_keyed_digest_unless_asked(self):
		with patch.dict(frappe.local.conf, {"encryption_key": "k" * 44}):
			hidden = batch_permission.differential([self.f.u1], names=self.supplied(), expect_batch=EXPECT)
			shown = self.run_differential()
		self.assertNotIn(self.f.u1, repr(hidden))
		self.assertEqual(len(hidden["users"]), 1)
		self.assertIn(self.f.u1, shown["users"])

	def test_without_a_key_the_users_are_numbered(self):
		with patch.dict(frappe.local.conf, {"encryption_key": ""}):
			result = batch_permission.differential([self.f.u1], names=self.supplied(), expect_batch=EXPECT)
		self.assertEqual(list(result["users"]), ["user-0"])
