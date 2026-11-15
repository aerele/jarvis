"""Payment Calendar chat hand-off: the send path, the server hint clause and its telemetry.

Plan: payment-calendar-chat-handoff (rev 4.1), A2 and A9. Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_handoff_clause
"""

import json
import statistics
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.meta import Meta
from frappe.tests.utils import FrappeTestCase

from jarvis import telemetry
from jarvis._session import impersonate
from jarvis.chat import admission, turn_handler, wiki
from jarvis.chat import api as chat_api
from jarvis.payment_calendar import visibility
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.test_chat_admission import MSG, TURN, _AdmissionTestCase

SETTINGS = "Jarvis Settings"
SWITCH = "payment_calendar_chat_handoff"
VISIBLE = "jarvis.payment_calendar.visibility.can_view_payment_calendar"
REPORTER = "jarvis.holiday_touch.report.report"
HINT = 'payment calendar page: available (jarvis-goto page "payment-calendar")'
CLAUSE = f"; {HINT}"
CAP = {"handoff_pages": ["payment-calendar"]}
# What send_message forwards when the capability is the only key set.
CAP_ONLY_CONTEXT = {
	"doctype": "",
	"name": "",
	"report_name": "",
	"filters": None,
	"ground_wiki": 0,
	"handoff_pages": ["payment-calendar"],
}
CHAT_USER = "pc-handoff-chat-user@example.com"


def setUpModule():
	install_synthetic_runtime_profile()


def forget_visibility(*users):
	"""Drop the clause's 60 s per-user visibility answer (Redis, not rolled back with the DB)."""
	for user in users:
		frappe.cache().delete_value(turn_handler._pc_visible_key(user))


def _settings():
	"""The Jarvis Settings document a turn loads (fresh read), as _assemble_prompt passes it."""
	return frappe.get_single(SETTINGS)


def _conv(origin_page=""):
	return frappe._dict(origin_page=origin_page)


def _bracket(prompt: str) -> str:
	start = prompt.index("[Context:")
	return prompt[start : prompt.index("]\n", start)]


class _SwitchCase(FrappeTestCase):
	"""Savepoint-scoped switch value; the clause reads it live from the database."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("pc_handoff_clause")
		self.set_switch(1)
		forget_visibility(CHAT_USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pc_handoff_clause")
		forget_visibility(CHAT_USER)
		super().tearDown()

	@staticmethod
	def set_switch(value):
		frappe.db.set_single_value(SETTINGS, SWITCH, value)


class TestClauseGates(_SwitchCase):
	def clause(self, **kw):
		args = {
			"chat_user": CHAT_USER,
			"conv": _conv(),
			"binding": None,
			"context": CAP,
			"seed_origin": "human",
			"settings": _settings(),
		}
		args.update(kw)
		return turn_handler._payment_calendar_clause(**args)

	@contextmanager
	def visible(self, answer=True):
		seen = []

		def fake():
			seen.append(frappe.session.user)
			return answer

		with patch(VISIBLE, side_effect=fake) as spy:
			spy.seen = seen
			yield spy

	def test_the_clause_string_is_pinned(self):
		self.assertEqual(HINT, turn_handler.PAYMENT_CALENDAR_HINT)
		with self.visible():
			self.assertEqual(self.clause(), CLAUSE)

	def test_switch_off_returns_before_visibility_is_evaluated(self):
		self.set_switch(0)
		with self.visible() as spy:
			self.assertEqual(self.clause(), "")
		spy.assert_not_called()

	def test_the_switch_is_read_live_on_every_call(self):
		with self.visible():
			self.assertEqual(self.clause(), CLAUSE)
			self.set_switch(0)
			self.assertEqual(self.clause(), "")
			self.set_switch(1)
			self.assertEqual(self.clause(), CLAUSE)

	def test_no_capability_means_no_hint(self):
		for context in (
			None,
			{},
			{"handoff_pages": []},
			{"handoff_pages": ["dashboards"]},
			"payment-calendar",
		):
			with self.subTest(context=context), self.visible() as spy:
				self.assertEqual(self.clause(context=context), "")
				spy.assert_not_called()

	def test_a_capability_that_is_not_a_list_is_no_capability(self):
		for pages in ("payment-calendar", {"payment-calendar": 1}, "xpayment-calendarx"):
			with self.subTest(pages=pages), self.visible() as spy:
				self.assertEqual(self.clause(context={"handoff_pages": pages}), "")
				spy.assert_not_called()

	def test_scope_exclusions_return_empty_without_evaluating_visibility(self):
		cases = {
			"shared binding": {"binding": object()},
			"builder origin page": {"conv": _conv("dashboards")},
			"triggers origin page": {"conv": _conv("triggers")},
			"page context": {"context": {**CAP, "page": "dashboards"}},
		}
		for origin in ("agent", "macro", "continuation", "file_box", "delegated", "board_answer", "system"):
			cases[f"origin {origin}"] = {"seed_origin": origin}
		for label, kw in cases.items():
			with self.subTest(label), self.visible() as spy:
				self.assertEqual(self.clause(**kw), "")
				spy.assert_not_called()

	def test_human_blank_and_missing_origins_are_in_scope(self):
		for origin in ("human", "", None):
			with self.subTest(origin=origin), self.visible():
				self.assertEqual(self.clause(seed_origin=origin), CLAUSE)

	def test_empty_origin_page_and_missing_conv_field_are_in_scope(self):
		with self.visible():
			self.assertEqual(self.clause(conv=_conv("")), CLAUSE)
			self.assertEqual(self.clause(conv=frappe._dict()), CLAUSE)

	def test_not_visible_means_no_hint(self):
		with self.visible(False) as spy:
			self.assertEqual(self.clause(), "")
		spy.assert_called_once()

	def test_visibility_is_evaluated_as_the_chat_user(self):
		with self.visible() as spy:
			self.clause()
		self.assertEqual(spy.seen, [CHAT_USER])
		self.assertEqual(frappe.session.user, "Administrator", "the session is restored")

	def test_frappe_local_state_is_the_snapshot_after_the_clause(self):
		cache, form_dict, role_permissions = {"k": 1}, frappe._dict(a=1), {"Doctype": {"role": 1}}
		frappe.local.cache, frappe.local.form_dict = cache, form_dict
		frappe.local.role_permissions = role_permissions
		with self.visible():
			self.assertEqual(self.clause(), CLAUSE)
		self.assertIs(frappe.local.cache, cache)
		self.assertIs(frappe.local.form_dict, form_dict)
		self.assertIs(frappe.local.role_permissions, role_permissions)

	def test_frappe_local_state_is_restored_when_visibility_raises(self):
		cache, form_dict, role_permissions = {"k": 1}, frappe._dict(a=1), {"Doctype": {"role": 1}}
		frappe.local.cache, frappe.local.form_dict = cache, form_dict
		frappe.local.role_permissions = role_permissions
		with patch(VISIBLE, side_effect=RuntimeError("boom")), patch(REPORTER):
			self.assertEqual(self.clause(), "")
		self.assertIs(frappe.local.cache, cache)
		self.assertIs(frappe.local.form_dict, form_dict)
		self.assertIs(frappe.local.role_permissions, role_permissions)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_the_visibility_answer_is_cached_per_user_after_the_gates(self):
		self.addCleanup(forget_visibility, CHAT_USER + ".b")
		with self.visible() as spy:
			self.assertEqual(self.clause(), CLAUSE)
			self.assertEqual(self.clause(), CLAUSE)
			self.assertEqual(len(spy.seen), 1, "second call is served from the 60 s cache")
			self.assertEqual(self.clause(chat_user=CHAT_USER + ".b"), CLAUSE)
			self.assertEqual(len(spy.seen), 2, "the key is the chat user")

	def test_the_cache_never_bypasses_the_switch_or_the_gates(self):
		with self.visible():
			self.assertEqual(self.clause(), CLAUSE)
			self.set_switch(0)
			self.assertEqual(self.clause(), "")
			self.set_switch(1)
			self.assertEqual(self.clause(context={}), "")
			self.assertEqual(self.clause(conv=_conv("dashboards")), "")

	def test_a_raising_check_is_not_cached(self):
		with patch(VISIBLE, side_effect=RuntimeError("boom")), patch(REPORTER):
			self.assertEqual(self.clause(), "")
		with self.visible() as spy:
			self.assertEqual(self.clause(), CLAUSE)
		self.assertEqual(len(spy.seen), 1)

	def test_a_false_answer_is_cached_fail_closed(self):
		# can_view_payment_calendar() swallows its own errors and returns False, so a False
		# (also an internal failure of the check) is cached for up to 60 s: the hint stays off.
		with self.visible(False) as spy:
			self.assertEqual(self.clause(), "")
			self.assertEqual(self.clause(), "")
		self.assertEqual(len(spy.seen), 1, "the False answer is served from the cache")
		with self.visible(True) as spy:
			self.assertEqual(self.clause(), "", "no leak: the cached False wins until it expires")
		spy.assert_not_called()
		forget_visibility(CHAT_USER)
		with self.visible(True):
			self.assertEqual(self.clause(), CLAUSE, "the hint returns once the entry is gone")

	def test_a_cache_outage_falls_through_to_the_live_check(self):
		with patch.object(frappe, "cache", side_effect=RuntimeError("redis down")), self.visible() as spy:
			self.assertEqual(self.clause(), CLAUSE)
		self.assertEqual(len(spy.seen), 1)

	def test_an_exception_returns_empty_and_logs_under_its_own_kind(self):
		with patch(VISIBLE, side_effect=RuntimeError("boom")), patch(REPORTER) as reporter:
			self.assertEqual(self.clause(), "")
		reporter.assert_called_once()
		self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_handoff:clause")
		self.assertTrue(
			reporter.call_args.kwargs["title"].startswith("payment_calendar_handoff:clause \u2014 "),
			"the kind token leads the Error Log title",
		)

	def test_a_failing_switch_read_is_swallowed_the_same_way(self):
		broken = SimpleNamespace(get=MagicMock(side_effect=RuntimeError("db")))
		with patch(REPORTER) as reporter, self.visible() as spy:
			self.assertEqual(self.clause(settings=broken), "")
		spy.assert_not_called()
		self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_handoff:clause")

	def test_a_missing_switch_field_before_migrate_is_off_without_error_or_log(self):
		has_field = Meta.has_field
		with (
			patch.object(
				Meta, "has_field", autospec=True, side_effect=lambda m, f: f != SWITCH and has_field(m, f)
			),
			patch(REPORTER) as reporter,
			patch.object(telemetry, "_emit") as emit,
			self.visible() as spy,
		):
			self.assertEqual(self.clause(), "")
		spy.assert_not_called()
		reporter.assert_not_called()
		emit.assert_not_called()

	def test_the_switch_is_read_from_the_passed_settings_not_a_second_query(self):
		self.set_switch(0)
		on = _settings()
		on.set(SWITCH, 1)  # the document this turn loaded says on, whatever a cache says
		with patch.object(frappe.db, "get_single_value") as get_single, self.visible():
			self.assertEqual(self.clause(settings=on), CLAUSE)
		get_single.assert_not_called()

	def test_a_new_turn_sees_a_flipped_switch_because_the_settings_read_is_fresh(self):
		with self.visible():
			self.assertEqual(self.clause(settings=_settings()), CLAUSE)
			self.set_switch(0)
			frappe.db.value_cache.clear()
			self.assertEqual(self.clause(settings=_settings()), "")

	def test_the_visibility_cache_leaves_no_local_copy_and_expires_in_redis_only(self):
		with self.visible() as spy:
			self.assertEqual(self.clause(), CLAUSE)
			made = frappe.cache().make_key(turn_handler._pc_visible_key(CHAT_USER))
			self.assertNotIn(made, frappe.local.cache, "set_value's never-expiring local copy is dropped")
			self.assertEqual(self.clause(), CLAUSE)
			self.assertNotIn(made, frappe.local.cache, "a hit does not create one either")
			self.assertEqual(len(spy.seen), 1)
			# the Redis key goes away (TTL or revocation): the next read in this process is live
			forget_visibility(CHAT_USER)
			self.assertEqual(self.clause(), CLAUSE)
			self.assertEqual(len(spy.seen), 2, "no process-local copy answers instead of Redis")

	def test_a_local_copy_planted_for_the_key_is_not_read(self):
		made = frappe.cache().make_key(turn_handler._pc_visible_key(CHAT_USER))
		frappe.local.cache[made] = 1
		self.addCleanup(frappe.local.cache.pop, made, None)
		with self.visible(False) as spy:
			self.assertEqual(self.clause(), "")
		self.assertEqual(len(spy.seen), 1, "the live check ran; the local entry was ignored")

	def test_the_error_log_is_rate_limited(self):
		from jarvis.holiday_touch import report

		frappe.cache().delete_value(f"{report.PREFIX}payment_calendar_handoff:clause")
		self.addCleanup(frappe.cache().delete_value, f"{report.PREFIX}payment_calendar_handoff:clause")
		with patch(VISIBLE, side_effect=RuntimeError("boom")), patch.object(frappe, "log_error") as log_error:
			self.clause()
			self.clause()
			self.clause()
		self.assertEqual(log_error.call_count, 1)


class TestHintTelemetry(_SwitchCase):
	def emitted(self, **kw):
		forget_visibility(CHAT_USER)
		with patch.object(telemetry, "_emit") as emit:
			result = TestClauseGates.clause(self, **kw)
		return result, [c.args[0] for c in emit.call_args_list]

	clause = TestClauseGates.clause

	def outcome(self, **kw):
		_, entries = self.emitted(**kw)
		hints = [e for e in entries if e["kind"] == "pc_handoff_hint"]
		self.assertEqual(len(hints), 1, entries)
		return hints[0]

	def test_each_outcome_is_recorded_with_the_hashed_chat_user(self):
		with patch(VISIBLE, return_value=True):
			on = self.outcome()
		with patch(VISIBLE, return_value=False):
			not_visible = self.outcome()
		no_capability = self.outcome(context={})
		with patch(VISIBLE, side_effect=RuntimeError("x")), patch(REPORTER):
			error = self.outcome()
		for entry, expected in (
			(on, "on"),
			(not_visible, "not_visible"),
			(no_capability, "no_capability"),
			(error, "error"),
		):
			self.assertEqual(entry["outcome"], expected)
			self.assertEqual(entry["user_hash"], telemetry._user_hash(CHAT_USER))
			self.assertNotIn(CHAT_USER, json.dumps(entry))
			self.assertNotEqual(entry["user_hash"], telemetry._user_hash(frappe.session.user))

	def test_every_hint_line_carries_the_run_id_and_no_text(self):
		with patch(VISIBLE, return_value=True):
			entry = self.outcome(run_id="run-join-1")
		self.assertEqual(entry["run_id"], "run-join-1")
		self.assertEqual(
			set(entry), {"kind", "ts", "site", "user_hash", "run_id", "outcome"}, "ids and the outcome only"
		)

	def test_switch_off_and_scope_exclusions_record_nothing(self):
		with patch(VISIBLE, return_value=True):
			self.set_switch(0)
			_, entries = self.emitted()
			self.assertEqual(entries, [])
			self.set_switch(1)
			_, entries = self.emitted(conv=_conv("dashboards"))
			self.assertEqual(entries, [])

	def test_the_wrapper_never_raises_and_ignores_unknown_outcomes(self):
		with patch.object(telemetry, "_emit", side_effect=RuntimeError("log down")):
			telemetry.record_pc_handoff_hint("on", CHAT_USER)
			telemetry.record_pc_handoff_goto('```jarvis-goto\n{"page":"payment-calendar","prompt":"p"}\n```')
		with patch.object(telemetry, "_emit") as emit:
			telemetry.record_pc_handoff_hint("surprise", CHAT_USER)
		emit.assert_not_called()

	def test_a_telemetry_failure_does_not_change_the_clause(self):
		with (
			patch(VISIBLE, return_value=True),
			patch.object(telemetry, "_emit", side_effect=RuntimeError("x")),
		):
			self.assertEqual(self.clause(), CLAUSE)


class TestGotoCount(_SwitchCase):
	def count(self, reply, conversation=None, run_id=None):
		with patch.object(telemetry, "_emit") as emit:
			telemetry.record_pc_handoff_goto(reply, conversation, run_id)
		return [c.args[0] for c in emit.call_args_list]

	@staticmethod
	def fence(page="payment-calendar", prompt="p", nl="\n"):
		return f"```jarvis-goto{nl}" + json.dumps({"page": page, "prompt": prompt}) + f"{nl}```"

	def test_a_calendar_goto_is_counted_without_any_reply_text(self):
		reply = (
			"Your Payment Calendar shows this.\n\n```jarvis-goto\n"
			'{"page": "payment-calendar", "prompt": "SECRET-PROMPT-TEXT"}\n```'
		)
		(entry,) = self.count(reply, "conv-1", "run-1")
		self.assertEqual(entry["kind"], "pc_handoff_goto")
		self.assertEqual(entry["count"], 1)
		self.assertEqual((entry["conversation"], entry["run_id"]), ("conv-1", "run-1"))
		self.assertEqual(set(entry), {"kind", "ts", "site", "conversation", "run_id", "count"})
		self.assertNotIn("SECRET-PROMPT-TEXT", json.dumps(entry))
		self.assertNotIn("Payment Calendar shows", json.dumps(entry))

	def test_other_replies_are_not_counted(self):
		for reply in (
			None,
			"",
			"plain answer about the payment-calendar page",
			self.fence("dashboards"),
			self.fence(prompt=""),
			"```jarvis-goto\nnot json\n```",
			'```jarvis-action\n{"page": "payment-calendar", "prompt": "p"}\n```',
		):
			with self.subTest(reply=reply):
				self.assertEqual(self.count(reply), [])

	def test_the_fence_needs_a_bare_newline_like_the_web_chat(self):
		# GOTO_RE in chatGoto.js is /```jarvis-goto[ \t]*\n.../ - a CRLF fence does not match there.
		self.assertEqual(len(self.count(self.fence())), 1)
		self.assertEqual(self.count(self.fence(nl="\r\n")), [], "CRLF is not a goto for the page")
		self.assertEqual(len(self.count(self.fence().replace("jarvis-goto\n", "jarvis-goto \t\n"))), 1)

	def test_page_and_prompt_are_coerced_like_javascript(self):
		def raw(body):
			return "```jarvis-goto\n" + body + "\n```"

		counted = (
			'{"page": " payment-calendar ", "prompt": "p"}',  # trim
			'{"page": ["payment-calendar"], "prompt": "p"}',  # String(["x"]) is "x"
			'{"page": "payment-calendar", "prompt": ["a", "b"]}',  # String([..]) is "a,b"
			'{"page": "payment-calendar", "prompt": 7}',  # String(7)
			'{"page": "payment-calendar", "prompt": true}',  # String(true)
			'{"page": "payment-calendar", "prompt": {}}',  # "[object Object]"
			"\ufeff" + '{"page": "payment-calendar", "prompt": "p"}',  # trim strips U+FEFF
		)
		not_counted = (
			'{"page": "payment-calendar", "prompt": 0}',  # 0 || "" is ""
			'{"page": "payment-calendar", "prompt": false}',
			'{"page": "payment-calendar", "prompt": null}',
			'{"page": "payment-calendar", "prompt": "   "}',
			'{"page": "payment-calendar", "prompt": []}',  # String([]) is ""
			'{"page": "payment-calendar"}',
			'{"page": ["payment-calendar", "x"], "prompt": "p"}',
			'{"page": {"a": 1}, "prompt": "p"}',
			"[1, 2]",
			"null",
			"0",
			'"payment-calendar"',
		)
		for body in counted:
			with self.subTest(counted=body):
				self.assertEqual(len(self.count(raw(body))), 1)
		for body in not_counted:
			with self.subTest(not_counted=body):
				self.assertEqual(self.count(raw(body)), [])

	def test_nan_and_infinity_are_rejected_like_json_parse(self):
		for constant in ("NaN", "Infinity", "-Infinity"):
			body = '{"page": "payment-calendar", "prompt": "p", "n": %s}' % constant
			with self.subTest(constant=constant):
				self.assertEqual(self.count("```jarvis-goto\n" + body + "\n```"), [])

	def test_only_the_first_block_counts(self):
		first_other = self.fence("dashboards") + "\n\n" + self.fence()
		self.assertEqual(self.count(first_other), [], "parseGoto reads the first block only")

	def test_the_finalize_hook_counts_a_finished_reply_only(self):
		from jarvis.chat import finalize

		reply = self.fence()

		def ctx(**kw):
			return finalize._Ctx(
				run_id="r-g",
				turn=kw.pop("turn", {"assistant_message": "m1"}),
				conversation="c-g",
				owner="u",
				errored=kw.pop("errored", False),
				payload={},
			)

		with (
			patch.object(frappe.db, "get_value", return_value={"content": reply, "error": ""}),
			patch.object(telemetry, "_emit") as emit,
		):
			finalize._count_pc_handoff_goto(ctx())
			self.assertEqual(emit.call_count, 1)
			line = emit.call_args.args[0]
			self.assertEqual((line["conversation"], line["run_id"]), ("c-g", "r-g"))
			finalize._count_pc_handoff_goto(ctx(errored=True))
			finalize._count_pc_handoff_goto(ctx(turn={}))
			self.assertEqual(emit.call_count, 1)
		with (
			patch.object(frappe.db, "get_value", return_value={"content": reply, "error": "boom"}),
			patch.object(telemetry, "_emit") as emit,
		):
			finalize._count_pc_handoff_goto(ctx())
		emit.assert_not_called()

	def test_the_finalize_hook_does_nothing_and_reads_nothing_while_the_switch_is_off(self):
		from jarvis.chat import finalize

		self.set_switch(0)
		ctx = finalize._Ctx(
			run_id="r-off",
			turn={"assistant_message": "m1"},
			conversation="c",
			owner="u",
			errored=False,
			payload={},
		)
		with (
			patch.object(frappe.db, "get_value") as get_value,
			patch.object(telemetry, "_emit") as emit,
		):
			finalize._count_pc_handoff_goto(ctx)
		get_value.assert_not_called()
		emit.assert_not_called()

	def test_the_finalize_hook_reuses_the_reply_an_earlier_effect_read(self):
		from jarvis.chat import finalize

		reply = self.fence()
		ctx = finalize._Ctx(
			run_id="r-re",
			turn={"assistant_message": "m1"},
			conversation="c",
			owner="u",
			errored=False,
			payload={},
		)
		with (
			patch.object(frappe.db, "get_value", return_value={"content": reply, "error": ""}) as get_value,
			patch("jarvis.chat.chat_asks.materialize_from_turn"),
			patch.object(telemetry, "_emit") as emit,
		):
			finalize._effect_chat_asks(ctx)
			finalize._count_pc_handoff_goto(ctx)
		self.assertEqual(get_value.call_count, 1, "one PK read serves both effects")
		self.assertEqual(emit.call_count, 1)

	def test_effect_telemetry_counts_one_goto_for_a_finished_reply_and_none_for_an_error(self):
		# Drives the real wiring: finalize._effect_telemetry must call the goto count.
		from jarvis.chat import finalize

		reply = self.fence()

		def run(errored):
			ctx = finalize._Ctx(
				run_id="r-wire",
				turn={"assistant_message": "m1"},
				conversation="c-wire",
				owner="u",
				errored=errored,
				payload={},
			)
			with (
				patch.object(frappe.db, "get_value", return_value={"content": reply, "error": ""}),
				patch.object(telemetry, "_emit") as emit,
				patch.object(
					telemetry, "record_pc_handoff_goto", wraps=telemetry.record_pc_handoff_goto
				) as goto,
			):
				finalize._effect_telemetry(ctx)
			return goto.call_count, [c.args[0]["kind"] for c in emit.call_args_list]

		calls, kinds = run(errored=False)
		self.assertEqual(calls, 1)
		self.assertEqual(kinds.count("pc_handoff_goto"), 1)
		calls, kinds = run(errored=True)
		self.assertEqual(calls, 0)
		self.assertNotIn("pc_handoff_goto", kinds)

	def test_the_legacy_goto_count_is_its_own_guarded_call(self):
		reply = self.fence()
		with patch.object(telemetry, "_emit") as emit:
			turn_handler._record_pc_handoff_goto({"content": reply, "error": ""}, "c-l", "r-l")
			turn_handler._record_pc_handoff_goto({"content": reply, "error": "boom"})
			turn_handler._record_pc_handoff_goto({})
		self.assertEqual(emit.call_count, 1)
		self.assertEqual(
			(emit.call_args.args[0]["conversation"], emit.call_args.args[0]["run_id"]), ("c-l", "r-l")
		)
		with patch.object(telemetry, "record_pc_handoff_goto", side_effect=RuntimeError("boom")):
			turn_handler._record_pc_handoff_goto({"content": reply, "error": ""})  # must not raise

	def test_the_legacy_goto_count_is_skipped_while_the_switch_is_off(self):
		self.set_switch(0)
		with patch.object(telemetry, "_emit") as emit:
			turn_handler._record_pc_handoff_goto({"content": self.fence(), "error": ""}, "c", "r")
		emit.assert_not_called()

	def test_a_raise_in_the_goto_count_cannot_skip_materialize_from_turn(self):
		# handle_chat_send is too heavy to drive here: pin the structure. materialize_from_turn
		# runs in its own try, and the goto count is a separate guarded call after it.
		import inspect

		source = inspect.getsource(turn_handler.handle_chat_send)
		materialize = source.index("chat_asks.materialize_from_turn(")
		try_at = source.rindex("try:", 0, materialize)
		except_at = source.index("except Exception:", materialize)
		self.assertNotIn("record_pc_handoff_goto", source[try_at:except_at], "not inside the try")
		goto = source.index("_record_pc_handoff_goto(_final, conversation_id, run_id)")
		self.assertGreater(goto, except_at, "the count runs after the materialize try/except")
		self.assertEqual(source.count("record_pc_handoff_goto("), 1)


class TestSendPath(_AdmissionTestCase):
	"""send_message allow-lists ``handoff_pages`` and forwards it on every transport."""

	def send(self, context, machine=True):
		# Earlier sends of the same test must not leave the shard full (the next would queue).
		frappe.db.delete(TURN, {"state": ["in", ("queued", "dispatching")]})
		frappe.db.commit()
		conv = self._mk_conv()
		calls = []
		with (
			patch.object(chat_api, "_dispatch_turn", side_effect=lambda *a, **k: calls.append(a[0])),
			patch.object(admission, "turn_machine_enabled", return_value=machine),
		):
			res = chat_api.send_message(
				conversation=conv, message="per day view", context=json.dumps(context)
			)
		self.assertTrue(res["ok"], res)
		stored = frappe.db.get_value(TURN, res["run_id"], "dispatch_payload")
		return calls[0], json.loads(stored) if stored else {}, res

	def test_the_capability_reaches_enqueue_kwargs_and_the_dispatch_payload(self):
		kwargs, payload, _ = self.send(CAP)
		self.assertEqual(kwargs["context"], CAP_ONLY_CONTEXT)
		self.assertEqual(payload["context"], CAP_ONLY_CONTEXT)

	def test_the_capability_reaches_the_legacy_dispatch_too(self):
		kwargs, _, _ = self.send(CAP, machine=False)
		self.assertEqual(kwargs["context"], CAP_ONLY_CONTEXT)

	def test_only_the_literal_survives(self):
		kwargs, _, _ = self.send({"handoff_pages": ["x", "payment-calendar", {"a": 1}, "dashboards"]})
		self.assertEqual(kwargs["context"]["handoff_pages"], ["payment-calendar"])

	def test_other_values_are_dropped(self):
		for pages in (
			["dashboards"],
			["payment-calendar-x"],
			"payment-calendar",
			{"payment-calendar": 1},
			[["payment-calendar"]],
			[],
			None,
			1,
		):
			with self.subTest(pages=pages):
				kwargs, payload, _ = self.send({"handoff_pages": pages})
				self.assertNotIn("context", kwargs)
				self.assertNotIn("context", payload)

	def test_other_context_behaviour_is_unchanged_and_carries_the_capability(self):
		kwargs, _, _ = self.send({"page": "dashboards", "theme": "midnight", **CAP})
		self.assertEqual(kwargs["context"]["page"], "dashboards")
		self.assertEqual(kwargs["context"]["handoff_pages"], ["payment-calendar"])
		kwargs, _, _ = self.send({"doctype": "Sales Invoice", "name": "SINV-1"})
		self.assertNotIn("handoff_pages", kwargs["context"])
		self.assertEqual(kwargs["context"]["doctype"], "Sales Invoice")
		kwargs, _, _ = self.send({"page": "other"})
		self.assertNotIn("context", kwargs)

	def test_a_capability_only_context_changes_no_other_prompt_output(self):
		plain = {}
		for fn in (
			lambda ctx: turn_handler._prepend_doc_context("hi", ctx, "conv"),
			lambda ctx: wiki.wiki_clause("conv", ctx),
		):
			self.assertEqual(fn(CAP_ONLY_CONTEXT), fn(plain))
		self.assertEqual(turn_handler._prepend_doc_context("hi", CAP_ONLY_CONTEXT, "conv"), "hi")

	def test_a_retry_keeps_the_capability(self):
		conv = self._mk_conv()
		u = self._mk_msg(conv, 1, role="user", content="per day view")
		run_id = "pc-retry-" + conv[-6:]
		self._insert_turn(
			conv, run_id, u, "errored", dispatch_payload=json.dumps({"context": CAP_ONLY_CONTEXT})
		)
		a = self._mk_msg(conv, 2, role="assistant", content="", error="Agent couldn't generate a response.")
		frappe.db.set_value(TURN, run_id, "assistant_message", a)
		frappe.db.commit()
		calls = []
		with patch.object(chat_api, "_dispatch_turn", side_effect=lambda *a, **k: calls.append(a[0])):
			res = chat_api.retry_message(a)
		self.assertTrue(res["ok"], res)
		self.assertEqual(calls[0]["context"], CAP_ONLY_CONTEXT)
		self.assertEqual(
			json.loads(frappe.db.get_value(TURN, res["run_id"], "dispatch_payload"))["context"],
			CAP_ONLY_CONTEXT,
		)

	def test_a_queued_promotion_keeps_the_capability(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv, 1)
		row = {
			"conversation": conv,
			"seed_message": seed,
			"run_id": "pc-promote",
			"turn_class": "interactive",
			"dispatch_payload": json.dumps({"context": CAP_ONLY_CONTEXT}),
		}
		with patch.object(chat_api, "_dispatch_turn") as dispatch:
			admission._dispatch_promoted(row)
		self.assertEqual(dispatch.call_args.args[0]["context"], CAP_ONLY_CONTEXT)


class TestPromptAssembly(_SwitchCase):
	"""The clause inside the real prompt, for the chat user, on every transport."""

	def make_turn(self, owner, origin="human", origin_page=""):
		conv = frappe.get_doc(
			{"doctype": turn_handler.CONV, "title": "pc handoff", "auto_apply": 0, "origin_page": origin_page}
		).insert(ignore_permissions=True)
		msg = frappe.get_doc(
			{
				"doctype": turn_handler.MSG,
				"conversation": conv.name,
				"seq": 1,
				"role": "user",
				"content": "Build a per day view of what I owe suppliers",
				"origin": origin,
			}
		)
		msg.flags.jarvis_server_write = True
		msg.insert(ignore_permissions=True)
		frappe.db.set_value(turn_handler.MSG, msg.name, "owner", owner, update_modified=False)
		return conv, msg

	def prompt(self, owner, context=None, **turn):
		conv, msg = self.make_turn(owner, **turn)
		return turn_handler.assemble_prompt(
			conv,
			message_id=msg.name,
			conversation_id=conv.name,
			context=CAP if context is None else context,
			attachments=[],
			user=owner,
		).user_message

	def test_the_clause_sits_in_the_main_bracket(self):
		with patch(VISIBLE, return_value=True):
			prompt = self.prompt(CHAT_USER)
		self.assertIn(CLAUSE, _bracket(prompt))
		self.assertEqual(prompt.count(HINT), 1)

	def test_prompt_bytes_differ_only_by_the_clause_line(self):
		fixed = frappe.utils.now_datetime()
		with patch(VISIBLE, return_value=True), patch("frappe.utils.now_datetime", return_value=fixed):
			conv, msg = self.make_turn(CHAT_USER)

			def build():
				return turn_handler.assemble_prompt(
					conv,
					message_id=msg.name,
					conversation_id=conv.name,
					context=CAP,
					attachments=[],
					user=CHAT_USER,
				).user_message

			on = build()
			self.set_switch(0)
			off = build()
		self.assertIn(CLAUSE, on)
		self.assertNotIn(HINT, off)
		self.assertEqual(on.replace(CLAUSE, "", 1), off)

	def test_a_capability_only_context_adds_nothing_with_the_switch_off(self):
		fixed = frappe.utils.now_datetime()
		self.set_switch(0)
		with patch("frappe.utils.now_datetime", return_value=fixed):
			conv, msg = self.make_turn(CHAT_USER)
			outputs = [
				turn_handler.assemble_prompt(
					conv,
					message_id=msg.name,
					conversation_id=conv.name,
					context=ctx,
					attachments=[],
					user=CHAT_USER,
				).user_message
				for ctx in ({}, CAP_ONLY_CONTEXT)
			]
		self.assertEqual(outputs[0], outputs[1])

	def hint_lines(self, turn=None, **assemble):
		"""Run the REAL assemble_prompt; return (prompt, the pc_handoff_hint telemetry lines)."""
		fixed = frappe.utils.now_datetime()
		with (
			patch(VISIBLE, return_value=True),
			patch("frappe.utils.now_datetime", return_value=fixed),
			patch.object(telemetry, "_emit") as emit,
		):
			conv, msg = turn or self.make_turn(CHAT_USER)
			prompt = turn_handler.assemble_prompt(
				conv,
				message_id=msg.name,
				conversation_id=conv.name,
				context=CAP,
				attachments=[],
				user=CHAT_USER,
				**assemble,
			).user_message
		lines = [c.args[0] for c in emit.call_args_list if c.args[0]["kind"] == "pc_handoff_hint"]
		return prompt, lines

	def test_a_non_binding_turn_forwards_its_run_id_to_the_hint_telemetry(self):
		_, lines = self.hint_lines(run_id="run-nb-1")
		self.assertEqual([(ln["outcome"], ln["run_id"]) for ln in lines], [("on", "run-nb-1")])

	def test_forwarding_the_run_id_leaves_a_non_binding_prompt_identical(self):
		turn = self.make_turn(CHAT_USER)
		with_id, _ = self.hint_lines(turn, run_id="run-nb-2")
		without, lines = self.hint_lines(turn)
		self.assertEqual(with_id, without)
		self.assertEqual([ln["run_id"] for ln in lines], [None])

	def test_the_seed_origin_comes_from_the_message_row(self):
		with patch(VISIBLE, return_value=True):
			self.assertIn(HINT, self.prompt(CHAT_USER, origin="human"))
			self.assertIn(HINT, self.prompt(CHAT_USER, origin=""))
			for origin in ("macro", "agent", "continuation", "file_box", "system", "delegated"):
				with self.subTest(origin=origin):
					self.assertNotIn(HINT, self.prompt(CHAT_USER, origin=origin))
			self.assertNotIn(HINT, self.prompt(CHAT_USER, origin_page="dashboards"))
			self.assertNotIn(HINT, self.prompt(CHAT_USER, context={**CAP, "page": "dashboards"}))
			self.assertNotIn(HINT, self.prompt(CHAT_USER, context={}))


class TestChatUserIdentity(FrappeTestCase):
	"""Real visibility: the answer is U's, whatever identity assembles the prompt."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("pc_handoff_identity")
		visibility.reset_contract_cache()
		self.fx = CalendarFixtures()
		frappe.db.set_single_value(SETTINGS, SWITCH, 1)
		self.seer = self.user("Jarvis User", "Accounts User")
		self.blind = self.user("Jarvis User", "HR Manager")
		self.other = self.user("Jarvis User", "HR Manager")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pc_handoff_identity")
		frappe.clear_document_cache("Report", "Accounts Payable")
		visibility.reset_contract_cache()
		self.forget_all()

	def forget_all(self):
		# The 60 s answer lives in Redis, not the rolled-back DB: every identity must hit the real check.
		forget_visibility(self.seer, self.blind, self.other)

	def user(self, *roles):
		user = self.fx.user(role=roles[0])
		frappe.get_doc("User", user).add_roles(*roles[1:])
		frappe.clear_cache(user=user)
		return user

	def clause(self, chat_user):
		return turn_handler._payment_calendar_clause(chat_user, _conv(), None, CAP, "human", _settings())

	def test_real_visibility_baseline(self):
		with impersonate(self.seer):
			self.assertTrue(visibility.can_view_payment_calendar())
		with impersonate(self.blind):
			self.assertFalse(visibility.can_view_payment_calendar())

	def test_an_administrator_session_answers_for_the_chat_user(self):
		frappe.set_user("Administrator")
		self.forget_all()
		self.assertEqual(self.clause(self.seer), CLAUSE)
		self.assertEqual(self.clause(self.blind), "")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_an_rq_job_enqueued_by_another_user_answers_for_the_chat_user(self):
		for session_user in (self.other, self.blind, self.seer):
			frappe.set_user(session_user)
			self.forget_all()
			with self.subTest(session_user=session_user):
				self.assertEqual(self.clause(self.seer), CLAUSE)
				self.assertEqual(self.clause(self.blind), "")
				self.assertEqual(frappe.session.user, session_user)

	def test_the_pump_path_answers_for_the_chat_user(self):
		# prepare.py assembles under impersonate(chat_user) with the pump worker's own session.
		for chat_user, expected in ((self.seer, CLAUSE), (self.blind, "")):
			frappe.set_user("Administrator")
			self.forget_all()
			with self.subTest(chat_user=chat_user), impersonate(chat_user):
				self.assertEqual(self.clause(chat_user), expected)
			self.assertEqual(frappe.session.user, "Administrator")

	def test_the_full_prompt_has_the_same_answer_on_every_transport(self):
		prompts = {}
		for label, session_user in (("admin", "Administrator"), ("other", self.other)):
			frappe.set_user(session_user)
			self.forget_all()
			conv = frappe.get_doc({"doctype": turn_handler.CONV, "title": "pc id", "auto_apply": 0}).insert(
				ignore_permissions=True
			)
			msg = frappe.get_doc(
				{
					"doctype": turn_handler.MSG,
					"conversation": conv.name,
					"seq": 1,
					"role": "user",
					"content": "per day view",
					"origin": "human",
				}
			)
			msg.flags.jarvis_server_write = True
			msg.insert(ignore_permissions=True)
			frappe.db.set_value(turn_handler.MSG, msg.name, "owner", self.seer, update_modified=False)
			prompts[label] = turn_handler.assemble_prompt(
				conv,
				message_id=msg.name,
				conversation_id=conv.name,
				context=CAP,
				attachments=[],
				user=self.seer,
			).user_message
		for label, prompt in prompts.items():
			with self.subTest(label=label):
				self.assertIn(CLAUSE, _bracket(prompt))

	def measure(self, call, runs=300):
		samples = []
		for _ in range(runs):
			start = time.perf_counter()
			call()
			samples.append((time.perf_counter() - start) * 1000)
		samples.sort()
		return statistics.median(samples), samples[int(len(samples) * 0.95) - 1], samples[-1]

	def test_the_clause_cost_p95(self):
		frappe.set_user(self.other)
		self.clause(self.seer)  # warm: contract probe, report doc
		# Uncached (a real impersonated visibility check every time): the number that
		# decided the cache (plan 4.2: cache only if p95 > 10 ms).
		raw = self.measure(lambda: turn_handler._visible_as_uncached(self.seer))
		print("\nPC_HANDOFF_UNCACHED_MS p50=%.2f p95=%.2f max=%.2f" % raw)
		cached = self.measure(lambda: self.clause(self.seer))
		print("PC_HANDOFF_CLAUSE_MS (cached) p50=%.2f p95=%.2f max=%.2f" % cached)
		# The median is the stable signal on a busy CI box; p95 / max are printed for the record.
		self.assertLess(cached[0], 5)
