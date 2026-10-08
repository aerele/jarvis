"""Which terminals the pump sends again once: the plain empty reply of the agent runtime,
only when nothing ran and no one acted (``jarvis.chat.empty_reply_recovery``)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import empty_reply_recovery as recovery
from jarvis.tests._empty_reply_fixtures import MARKER_ONLY, ResendOn
from jarvis.tests.harness.transcripts import EMPTY_REPLY, EMPTY_REPLY_TOOLS
from jarvis.tests.race_harness import as_job, other_connection
from jarvis.tests.test_pump import MSG, TEST_USER, TURN, _PumpTestCase

RUN = "Jarvis Macro Run"
CONV = "Jarvis Conversation"
# Every ``origin`` of a user message, and whether it is a person's send.
ORIGINS = {
	"human": True,
	"": False,
	"delegated": False,
	"board_answer": False,
	"file_box": False,
	"agent": False,
	"macro": False,
	"continuation": False,
	"system": False,
}


def _error(error=EMPTY_REPLY, *, state="error", text=""):
	return {"state": state, "error": error, "text": text}


def _no_read(*_a, **_k):
	raise AssertionError("read")


class TestVariant(FrappeTestCase):
	def test_each_variant(self):
		for text, code, want in (
			(EMPTY_REPLY, "empty-reply", "plain"),
			("Agent couldn’t generate a response. Please try again.", "empty-reply", "plain"),
			("agent couldn't generate a response. please try again", "empty-reply", "plain"),
			("Agent couldn't generate a response.", "empty-reply", "bare"),
			("Agent couldn't generate a response: 500 from upstream", "empty-reply", "other"),
			("All models failed: Agent couldn't generate a response. Please try again.", "gateway", "other"),
			(
				"Agent couldn't generate a response. Please try again. Then call support.",
				"empty-reply",
				"other",
			),
			(EMPTY_REPLY_TOOLS, "empty-reply-tools", "tools"),
			(EMPTY_REPLY, "empty-reply-tools", "tools"),
			("", "empty-reply", "other"),
			(None, "empty-reply", "other"),
		):
			with self.subTest(text=text, code=code):
				self.assertEqual(recovery.variant(text, code), want)

	def test_only_the_first_300_characters_are_read_and_fast(self):
		started = time.monotonic()
		self.assertEqual(recovery.variant(EMPTY_REPLY + " " * 30000), "plain")
		self.assertEqual(recovery.variant("Agent couldn't generate a response." + " " * 30000 + "x"), "bare")
		self.assertLess(time.monotonic() - started, 0.5)


class TestKillSwitch(FrappeTestCase):
	def test_off_unless_an_explicit_on_value(self):
		conf = frappe.local.conf
		prev = conf.pop(recovery.SWITCH, None)
		try:
			self.assertEqual((recovery.switch_on(), recovery.switch_state()), (False, "off"), "absent = off")
			for value in (None, "", 0, "0", False, "false", "no", "off", "OFF", " 0 "):
				with self.subTest(value=value), patch.dict(conf, {recovery.SWITCH: value}):
					self.assertEqual((recovery.switch_on(), recovery.switch_state()), (False, "off"))
			for value in (1, "1", True, "true", "TRUE", "on", "yes", " yes "):
				with self.subTest(value=value), patch.dict(conf, {recovery.SWITCH: value}):
					self.assertEqual((recovery.switch_on(), recovery.switch_state()), (True, "on"))
			for value in ("2", "enabled", "maybe", 2):
				with self.subTest(value=value), patch.dict(conf, {recovery.SWITCH: value}):
					self.assertFalse(recovery.switch_on(), "any other value = off")
		finally:
			if prev is not None:
				conf[recovery.SWITCH] = prev


class TestStoredDispatch(FrappeTestCase):
	def test_only_a_json_object_is_read(self):
		for raw, stored in (
			(None, None),
			("", None),
			("not json", None),
			("[1]", None),
			('{"redispatch": 1}', {"redispatch": 1}),
		):
			with self.subTest(raw=raw):
				self.assertEqual(recovery.stored_dispatch(raw), stored)


class TestResentTurn(_PumpTestCase):
	def test_only_a_json_object_with_the_reason_is_resent(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		rid = "er_rt_" + frappe.generate_hash(length=8)
		for i, (raw, is_resent) in enumerate(
			(
				(None, False),
				("", False),
				("not json", False),
				("[1]", False),
				('{"redispatch": 1}', False),
				('{"redispatch": 1, "redispatch_reason": "other_reason"}', False),
				('{"redispatch_reason": {"reason": "empty_reply"}}', False),
				(json.dumps({"redispatch": 1, "redispatch_reason": recovery.REASON}), True),
			)
		):
			self._mk_turn(conv, f"{rid}_{i}", seed, "done", dispatch_payload=raw)
			with self.subTest(raw=raw):
				self.assertIs(recovery.resent_turn(f"{rid}_{i}"), is_resent)
				self.assertIs(recovery.resent_turn(f"{rid}_{i}", conv), is_resent)
		self.assertIs(recovery.resent_turn(f"{rid}_7", conv + "x"), False, "another chat")
		self.assertIs(recovery.resent_turn(rid + "_missing"), False)

	def test_only_the_marker_key_is_read(self):
		conv = self._mk_conv()
		rid = "er_rk_" + frappe.generate_hash(length=8)
		stored = {"message": "x" * 1000, "redispatch": 1, "redispatch_reason": recovery.REASON}
		self._mk_turn(conv, rid, self._mk_msg(conv), "done", dispatch_payload=json.dumps(stored))
		with (
			patch.object(frappe.db, "get_value", side_effect=_no_read),
			patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql,
		):
			self.assertIs(recovery.resent_turn(rid, conv), True)
		(query,) = [frappe.db.mogrify(*c.args[:2]) for c in sql.call_args_list]
		self.assertIn("SELECT JSON_VALUE(`dispatch_payload`,'$.redispatch_reason') FROM", query)


class TestDecisionLine(FrappeTestCase):
	FMT = "empty_reply decision=%s reason=%s run_id=%s conversation=%s target=%s"

	def test_one_line_per_decision(self):
		logger = MagicMock()
		with patch("jarvis.chat.latency.get_logger", return_value=logger):
			recovery.log_decision("resent", "plain", run_id="r1", conversation="c1", target="t1")
			recovery.log_decision("skip", "tool:get_list", run_id="r2", conversation="c1", target="t1")
		self.assertEqual(
			[c.args for c in logger.info.call_args_list],
			[
				(self.FMT, "resent", "plain", "r1", "c1", "t1"),
				(self.FMT, "skip", "tool:get_list", "r2", "c1", "t1"),
			],
		)

	def test_an_error_is_a_warning_with_its_traceback(self):
		logger = MagicMock()
		with patch("jarvis.chat.latency.get_logger", return_value=logger):
			try:
				raise RuntimeError("db hiccup")
			except RuntimeError:
				recovery.log_decision("skip", "error", run_id="r1", conversation="c1", target="t1")
		logger.warning.assert_called_once_with(self.FMT, "skip", "error", "r1", "c1", "t1", exc_info=True)

	def test_never_raises(self):
		with patch("jarvis.chat.latency.get_logger", side_effect=RuntimeError("log down")):
			recovery.log_decision("resent", "plain", run_id="r1", conversation="c1", target="t1")


class _DecisionCase(ResendOn, _PumpTestCase):
	"""Real rows: a streaming turn of a person's message in a fresh chat."""

	def setUp(self):
		super().setUp()
		self.conv = self._mk_conv()
		self.seed = self._mk_msg(self.conv, content="Reply with one word", server=True, origin="human")
		self.amsg = self._mk_msg(self.conv, role="assistant", content="", streaming=1)
		self.rid = "er_" + frappe.generate_hash(length=8)
		self._mk_turn(
			self.conv, self.rid, self.seed, "streaming", version=4, reserved=1, assistant_message=self.amsg
		)

	def tearDown(self):
		frappe.db.delete(RUN, {"conversation": self.conv})
		frappe.db.delete("Error Log", {"method": recovery.BREAKER_ALERT, "reference_name": self._target})
		frappe.db.commit()
		super().tearDown()

	def _decide(self, payload=None, *, kind="relay:error", sent_again=False, saw_step=lambda: False, **turn):
		"""``resend_decision`` for this test's turn, or for ``conv`` / ``rid`` / ``amsg``."""
		return recovery.resend_decision(
			kind=kind,
			payload=_error() if payload is None else payload,
			run_id=turn.get("rid", self.rid),
			conversation=turn.get("conv", self.conv),
			owner=TEST_USER,
			assistant_message=turn.get("amsg", self.amsg),
			relay_target_id=self._target,
			sent_again=sent_again,
			saw_step=saw_step,
		)

	def _resent_turn(
		self, conv, rid, seed, state="errored", *, error=EMPTY_REPLY, resent=recovery.REASON, **extra
	):
		"""A turn the pump sent again (``resent``: its reason, or ``MARKER_ONLY``), ended ``state``."""
		stored = {"session_key": f"sess-{rid}", "message": "hi", "redispatch": 1}
		if resent != MARKER_ONLY:
			stored["redispatch_reason"] = resent
		extra.setdefault("finalizing_at", frappe.utils.now())
		self._mk_turn(conv, rid, seed, state, dispatch_payload=json.dumps(stored), error=error, **extra)

	def _tool(self, name, call_id=None, *, conv=None):
		return self._mk_msg(conv or self.conv, "tool", "", server=True, tool_name=name, tool_call_id=call_id)


class TestResendDecision(_DecisionCase):
	"""``resend_decision`` against real rows: each guard on its own."""

	def test_an_eligible_turn(self):
		self.assertEqual(self._decide(), (True, "plain"))

	def test_no_decision_and_no_read_for_other_terminals(self):
		with (
			patch.object(frappe.db, "sql", side_effect=_no_read),
			patch.object(frappe.db, "get_value", side_effect=_no_read),
			patch.object(frappe, "cache", side_effect=_no_read),
		):
			for payload, kind in (
				({"state": "final", "text": "PONG"}, "relay:final"),
				(_error(), "relay:final"),
				(_error(state="aborted"), "relay:error"),
				(_error(text="partial answer"), "relay:error"),
				(_error("429 rate_limit_error"), "relay:error"),
				("not a dict", "relay:error"),
			):
				with self.subTest(payload=payload):
					self.assertIsNone(self._decide(payload, kind=kind, saw_step=_no_read))

	def test_with_the_switch_off_there_is_no_decision_and_no_read(self):
		with (
			patch.dict(frappe.local.conf, {recovery.SWITCH: 0}),
			patch.object(frappe.db, "sql", side_effect=_no_read),
			patch.object(frappe.db, "get_value", side_effect=_no_read),
		):
			self.assertIsNone(self._decide(saw_step=_no_read))
			self.assertIsNone(self._decide(_error(EMPTY_REPLY_TOOLS), saw_step=_no_read))

	def test_the_variant_used_and_steps_are_decided_before_any_read(self):
		with (
			patch.object(frappe.db, "sql", side_effect=_no_read),
			patch.object(frappe.db, "get_value", side_effect=_no_read),
		):
			for text in (
				EMPTY_REPLY_TOOLS,
				"Agent couldn't generate a response.",
				"Agent couldn't generate a response. Tool call failed.",
				EMPTY_REPLY + " " * recovery.MAX_CHARS,
			):
				with self.subTest(text=text):
					self.assertEqual(self._decide(_error(text), saw_step=_no_read), (False, "variant"))
			self.assertEqual(self._decide(sent_again=True, saw_step=_no_read), (False, "used"))
			self.assertEqual(self._decide(saw_step=lambda: True), (False, "steps"))

	def test_the_whole_text_must_fit_in_the_read_limit(self):
		base = "Agent couldn't generate a response. Please try again."
		at_limit = base + " " * (recovery.MAX_CHARS - len(base))
		self.assertEqual(self._decide(_error(at_limit)), (True, "plain"))
		self.assertEqual(self._decide(_error(at_limit + " ")), (False, "variant"))

	def test_a_background_turn_is_never_sent_again(self):
		frappe.db.set_value(TURN, self.rid, "turn_class", "background")
		self.assertEqual(self._decide(), (False, "class"))

	def test_only_a_message_a_person_typed_is_sent_again(self):
		origins = frappe.get_meta(MSG).get_field("origin").options.split("\n")
		self.assertEqual(sorted(origins), sorted(ORIGINS), "a new origin needs a row in ORIGINS")
		for origin in origins:
			with self.subTest(origin=origin):
				frappe.db.set_value(MSG, self.seed, "origin", origin)
				self.assertEqual(self._decide(), (True, "plain") if ORIGINS[origin] else (False, "origin"))

	def test_a_file_box_chat_is_never_sent_again(self):
		frappe.db.set_value(CONV, self.conv, "file_box", 1)
		self.assertEqual(self._decide(), (False, "file_box"))

	def test_an_armed_conversation_or_a_live_macro_run_skips(self):
		frappe.db.set_value(CONV, self.conv, "skip_confirmation", 1)
		self.assertEqual(self._decide(), (False, "armed"))
		frappe.db.set_value(CONV, self.conv, "skip_confirmation", 0)
		run = frappe.get_doc({"doctype": RUN, "conversation": self.conv, "status": "queued"})
		run.insert(ignore_permissions=True)
		for status in ("queued", "running", "waiting_capacity"):
			with self.subTest(status=status):
				frappe.db.set_value(RUN, run.name, "status", status)
				self.assertEqual(self._decide(), (False, "armed"))
		frappe.db.set_value(RUN, run.name, "status", "completed")
		self.assertEqual(self._decide(), (True, "plain"), "an ended run does not count")

	def test_only_the_memory_hook_recall_may_come_after_the_placeholder(self):
		self._tool("recall")
		self.assertEqual(self._decide(), (True, "plain"), "the hook's recall has no call id")
		for name, call_id in (
			("recall", "call-1"),
			("get_list", None),
			("remember", None),
			("create_doc", "c2"),
		):
			with self.subTest(tool=name, call_id=call_id):
				row = self._tool(name, call_id)
				self.assertEqual(self._decide(), (False, f"tool:{name}"))
				frappe.db.delete(MSG, {"name": row})

	def test_a_tool_name_from_the_runtime_is_one_clean_token(self):
		self._tool("get_list\nempty_reply decision=resent reason=plain", "c9")
		self.assertEqual(self._decide(), (False, "tool:get_list_empty_reply_decision_resent_reason_plain"))
		frappe.db.delete(MSG, {"conversation": self.conv, "role": "tool"})
		self._tool("x" * 120, "c10")
		self.assertEqual(self._decide(), (False, "tool:" + "x" * 64))

	def test_a_receipt_committed_after_the_slice_began_is_seen(self):
		"""The pump's transaction holds a snapshot from the start of its slice; a tool's
		web request commits its receipt after that. The decision reads the present."""
		seq = self._next_seq(self.conv)  # this read opens the slice's snapshot
		with as_job():
			with other_connection() as other:
				now = frappe.utils.now()
				other.sql(
					"""INSERT INTO `tabJarvis Chat Message`
					(name, creation, modified, owner, modified_by, docstatus, idx,
					 conversation, seq, role, content, tool_name, tool_call_id)
					VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, 'tool', '', 'create_doc', 'call-late')""",
					(frappe.generate_hash(length=10), now, now, TEST_USER, TEST_USER, self.conv, seq),
				)
				other.commit()
			self.assertEqual(self._decide(), (False, "tool:create_doc"))

	def test_a_tool_row_of_an_earlier_turn_counts_only_without_a_placeholder(self):
		conv = self._mk_conv()
		self._tool("create_doc", "old", conv=conv)
		seed = self._mk_msg(conv, server=True, origin="human")
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(conv, "er_earlier", seed, "streaming", version=4, assistant_message=amsg)
		self.assertEqual(self._decide(conv=conv, rid="er_earlier", amsg=amsg), (True, "plain"))
		self.assertEqual(self._decide(conv=conv, rid="er_earlier", amsg=None), (False, "tool:create_doc"))

	def test_a_parked_card_skips(self):
		with patch("jarvis.chat.pending_confirm.has_live_card", return_value=True) as card:
			self.assertEqual(self._decide(), (False, "card"))
		card.assert_called_once_with(TEST_USER, self.conv)

	def test_another_unfinished_attempt_at_the_same_message_skips(self):
		self._mk_turn(self.conv, self.rid + "b", self.seed, "queued")
		self.assertEqual(self._decide(), (False, "retried"))

	def test_a_queued_next_message_does_not_block(self):
		nxt = self._mk_msg(self.conv, content="and another thing")
		self._mk_turn(self.conv, self.rid + "n", nxt, "queued")
		self.assertEqual(self._decide(), (True, "plain"))

	def test_once_per_user_message(self):
		self._mk_turn(self.conv, self.rid + "a", self.seed, "errored", error=EMPTY_REPLY)
		self.assertEqual(self._decide(), (False, "retried"), "an earlier attempt already got the empty reply")

	def test_an_earlier_attempt_with_another_error_does_not_count(self):
		self._mk_turn(self.conv, self.rid + "a", self.seed, "errored", error="upstream closed the stream")
		self.assertEqual(self._decide(), (True, "plain"))

	def _chat_after(self, name, *earlier):
		"""A chat whose earlier turns are ``(state, error, minutes_ago, resent)``, oldest
		first, and a streaming turn of a person's message after them."""
		conv = self._mk_conv()
		for i, (state, error, ago, resent) in enumerate(earlier):
			msg = self._mk_msg(conv, content=f"earlier {i}")
			at = frappe.utils.add_to_date(None, minutes=-ago)
			if resent:
				self._resent_turn(conv, f"er_{name}_{i}", msg, state, error=error)
			else:
				self._mk_turn(conv, f"er_{name}_{i}", msg, state, error=error)
			frappe.db.set_value(TURN, f"er_{name}_{i}", "creation", at, update_modified=False)
		seed = self._mk_msg(conv, content="now", server=True, origin="human")
		amsg = self._mk_msg(conv, role="assistant", content="", streaming=1)
		self._mk_turn(conv, f"er_{name}_now", seed, "streaming", version=4, assistant_message=amsg)
		return self._decide(conv=conv, rid=f"er_{name}_now", amsg=amsg)

	def test_a_chat_whose_last_turn_got_an_empty_reply_is_not_sent_again(self):
		day = recovery.CHAT_WINDOW_S // 60
		for name, error, ago in (
			("plain", EMPTY_REPLY, 5),
			("tools", EMPTY_REPLY_TOOLS, 5),
			("edge", EMPTY_REPLY, day - 5),
		):
			with self.subTest(previous=name):
				self.assertEqual(
					self._chat_after(f"cf_{name}", ("errored", error, ago, False)),
					(False, "chat"),
					"sent again or not",
				)

	def test_an_unfinished_turn_does_not_hide_the_last_finished_one(self):
		self.assertEqual(
			self._chat_after("cf_queued", ("errored", EMPTY_REPLY, 10, False), ("queued", None, 5, False)),
			(False, "chat"),
		)

	def test_only_the_last_recent_turn_of_the_chat_counts(self):
		day = recovery.CHAT_WINDOW_S // 60
		for name, earlier in (
			("ok_after", (("errored", EMPTY_REPLY, 30, True), ("done", None, 10, False))),
			("other", (("errored", "upstream closed the stream", 5, False),)),
			("old", (("errored", EMPTY_REPLY, day + 5, True),)),
		):
			with self.subTest(case=name):
				self.assertEqual(self._chat_after(f"co_{name}", *earlier), (True, "plain"))


class TestBreaker(_DecisionCase):
	"""The breaker reads the settled Turn rows of the relay target."""

	OPEN = "empty_reply breaker=open target=%s count=%s"

	def _failed(self, n, **kw):
		"""``n`` turns sent again on this relay target, in another chat, settled with ``kw``."""
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="earlier")
		for i in range(n):
			self._resent_turn(conv, f"erb_{frappe.generate_hash(length=8)}_{i}", seed, **kw)

	def test_three_second_empty_replies_open_it(self):
		self._failed(2)
		self.assertEqual(self._decide(), (True, "plain"))
		self._failed(1)
		with patch.object(frappe, "log_error"):
			self.assertEqual(self._decide(), (False, "breaker"))
		self.assertEqual(self.lines_of(self.OPEN), [(self._target, 3)], "a WARNING for each refusal")

	def test_the_payloads_are_read_only_from_three_empty_replies(self):
		since = frappe.utils.add_to_date(None, seconds=-recovery.BREAKER_WINDOW_S)
		self._failed(2)
		self._failed(3, error="429 rate_limit_error")
		for failed, want, reads in ((0, 2, False), (1, 3, True)):
			with self.subTest(empty_replies=2 + failed):
				self._failed(failed)
				with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
					self.assertEqual(recovery._breaker_count(self._target, since), want)
				queries = [c.args[0] for c in sql.call_args_list]
				self.assertEqual(any("dispatch_payload" in q for q in queries), reads)

	def test_a_second_empty_reply_with_the_tools_form_counts(self):
		self._failed(3, error=EMPTY_REPLY_TOOLS)
		with patch.object(frappe, "log_error"):
			self.assertEqual(self._decide(), (False, "breaker"))

	def test_other_failures_do_not_count(self):
		self._failed(3, error="429 rate_limit_error")
		self._failed(3, resent="other_reason")
		self._failed(3, resent=MARKER_ONLY)
		self._failed(3, state="cancelled")
		self.assertEqual(self._decide(), (True, "plain"))

	def test_a_missing_or_broken_payload_is_not_counted(self):
		since = frappe.utils.add_to_date(None, seconds=-recovery.BREAKER_WINDOW_S)
		self._failed(2)
		conv = self._mk_conv()
		seed = self._mk_msg(conv, content="earlier")
		for i, raw in enumerate((None, "not json")):
			rid = f"erb_{frappe.generate_hash(length=8)}_{i}"
			self._mk_turn(
				conv,
				rid,
				seed,
				"errored",
				error=EMPTY_REPLY,
				dispatch_payload=raw,
				finalizing_at=frappe.utils.now(),
			)
		self.assertEqual(recovery._breaker_count(self._target, since), 2)
		self.assertEqual(self._decide(), (True, "plain"))
		self.assertEqual(self.lines_of("empty_reply breaker=unreadable target=%s"), [])

	def test_the_window_is_ten_minutes(self):
		for ago, want in (
			(recovery.BREAKER_WINDOW_S - 1, (False, "breaker")),
			(recovery.BREAKER_WINDOW_S + 1, (True, "plain")),
		):
			with self.subTest(seconds_ago=ago):
				frappe.db.delete(TURN, {"relay_target_id": self._target, "name": ["like", "erb_%"]})
				self._failed(3, finalizing_at=frappe.utils.add_to_date(None, seconds=-ago))
				with patch.object(frappe, "log_error"):
					self.assertEqual(self._decide(), want)

	def test_it_comes_after_the_cheap_checks(self):
		self._failed(3)
		frappe.db.set_value(TURN, self.rid, "turn_class", "background")
		self.assertEqual(self._decide(), (False, "class"), "the true reason, not breaker")

	def test_a_read_error_counts_as_open(self):
		with patch.object(recovery, "_breaker_count", side_effect=RuntimeError("db hiccup")):
			self.assertEqual(self._decide(), (False, "breaker"), "fail closed: no re-send")
		self.assertEqual(self.lines_of("empty_reply breaker=unreadable target=%s"), [(self._target,)])

	def test_at_most_one_error_log_per_target_per_window(self):
		self._failed(3)
		for _ in range(3):
			self.assertEqual(self._decide(), (False, "breaker"))
		rows = frappe.get_all(
			"Error Log", filters={"method": recovery.BREAKER_ALERT, "reference_name": self._target}
		)
		self.assertEqual(len(rows), 1)

	def _alert(self, target, seconds_ago=0):
		doc = frappe.log_error(
			title=recovery.BREAKER_ALERT, reference_doctype="Jarvis Relay Pump", reference_name=target
		)
		at = frappe.utils.add_to_date(None, seconds=-seconds_ago)
		frappe.db.set_value("Error Log", doc.name, "creation", at, update_modified=False)
		return doc.name

	def test_an_older_alert_or_one_of_another_target_does_not_count(self):
		self._failed(3)
		self._alert(self._target, recovery.BREAKER_WINDOW_S + 60)
		other = self._alert(self._target + "x")
		try:
			self.assertEqual(self._decide(), (False, "breaker"))
			rows = frappe.get_all(
				"Error Log", filters={"method": recovery.BREAKER_ALERT, "reference_name": self._target}
			)
			self.assertEqual(len(rows), 2, "a new alert")
		finally:
			frappe.db.delete("Error Log", {"name": other})
			frappe.db.commit()

	def test_a_failing_alert_still_refuses(self):
		self._failed(3)
		with patch.object(frappe, "log_error", side_effect=RuntimeError("db hiccup")):
			self.assertEqual(self._decide(), (False, "breaker"))
		self.assertEqual(self.lines_of("empty_reply breaker_alert_failed target=%s"), [(self._target,)])


class TestOutcomeLine(FrappeTestCase):
	FMT = "empty_reply outcome=%s code=%s run_id=%s conversation=%s target=%s"

	def test_each_settled_state(self):
		logger = MagicMock()
		with patch("jarvis.chat.latency.get_logger", return_value=logger):
			for state, code in (("finalizing", ""), ("errored", "empty-reply"), ("cancelled", "x")):
				recovery.note_outcome(
					run_id="r1", conversation="c1", state=state, relay_target_id="t1", code=code
				)
		self.assertEqual(
			[c.args for c in logger.info.call_args_list],
			[
				(self.FMT, "ok", "", "r1", "c1", "t1"),
				(self.FMT, "failed", "empty-reply", "r1", "c1", "t1"),
				(self.FMT, "stopped", "", "r1", "c1", "t1"),
			],
		)

	def test_never_raises(self):
		with patch("jarvis.chat.latency.get_logger", side_effect=RuntimeError("log down")):
			recovery.note_outcome(run_id="r1", conversation="c1", state="errored", relay_target_id="t1")
