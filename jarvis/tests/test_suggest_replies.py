"""Suggested replies (jarvis.chat.suggest_replies): the user's likely next messages, offered
as chips under a finished reply. The parser cases are Frappe Flow's own
(flow/harness/tests/test_replies.py); the rest pins the gates, the job and the wiring."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import finalize, session_lifecycle, settlement
from jarvis.chat import suggest_replies as sr
from jarvis.chat import turn_state as ts


class TestParse(FrappeTestCase):
	def test_one_to_three_distinct_short_messages(self):
		fenced = '```json\n{"replies": [" Yes ", "No"], "multi_select": true}\n```'
		self.assertEqual(sr.parse_replies(fenced), {"replies": ["Yes", "No"], "multi_select": True})
		thought = '<think>Answer as {"replies": [...]} only.</think>\n{"replies": ["Yes", "No"]}'
		self.assertEqual(sr.parse_replies(thought)["replies"], ["Yes", "No"])
		self.assertEqual(
			sr.parse_replies('{"replies": ["Run the tests"]}'),
			{"replies": ["Run the tests"], "multi_select": False},
		)
		for text in (
			'{"replies": []}',
			'{"replies": ["a", "b", "c", "d"]}',
			'{"replies": ["Yes", "Yes "]}',
			'{"replies": ["Yes", " "]}',
			'{"replies": ["Yes", "' + "x" * 201 + '"]}',
			'{"replies": "Yes"}',
			"Yes or no",
			"{not json}",
			"",
			None,
		):
			self.assertIsNone(sr.parse_replies(text), text)

	def test_inner_whitespace_is_folded_and_multi_select_needs_a_real_true(self):
		self.assertEqual(
			sr.parse_replies('{"replies": ["Show  me\\nthe list"], "multi_select": "yes"}'),
			{"replies": ["Show me the list"], "multi_select": False},
		)


def _row(name, role, content, **kw):
	return frappe._dict(
		{"name": name, "role": role, "content": content, "error": None, "stopped": 0, "streaming": 0, **kw}
	)


class TestTranscript(FrappeTestCase):
	def test_newest_messages_in_order_labelled_and_bounded(self):
		rows = [
			_row("m1", "user", "How many  overdue invoices?"),
			_row("m2", "assistant", "There are **3**:\n- A\n- B"),
			_row("m3", "assistant", ""),
			_row("m4", "user", "x" * 5000),
		]
		with patch.object(sr, "_visible_rows", return_value=rows):
			out = sr.transcript("C1")
		self.assertTrue(
			out.startswith("User: How many overdue invoices?\n\nJarvis: There are **3**: - A - B")
		)
		self.assertTrue(out.endswith("User: " + "x" * sr.EXCERPT_USER_CHARS))
		self.assertNotIn("Jarvis: \n", out)

	def test_the_whole_excerpt_is_capped(self):
		rows = [_row(f"m{i}", "assistant", "y" * 4000) for i in range(6)]
		with patch.object(sr, "_visible_rows", return_value=rows):
			self.assertLessEqual(len(sr.transcript("C1")), sr.EXCERPT_TOTAL_CHARS)


class TestEnabled(FrappeTestCase):
	def test_on_unless_switched_off(self):
		for stored, expected in (([], True), ([(None,)], True), ([("1",)], True), ([("0",)], False)):
			with patch.object(sr.frappe.db, "sql", return_value=stored):
				self.assertIs(sr.enabled(), expected, stored)


class TestUserSwitch(FrappeTestCase):
	def test_on_unless_the_user_switched_it_off(self):
		for stored, expected in ((None, True), (1, True), ("1", True), (0, False), ("0", False)):
			with patch.object(sr.frappe.db, "get_value", return_value=stored):
				self.assertIs(sr.user_enabled("u@example.com"), expected, stored)
		self.assertIs(sr.user_enabled(None), True)
		with patch.object(sr.frappe.db, "get_value", side_effect=Exception("no column yet")):
			self.assertIs(sr.user_enabled("u@example.com"), True)

	def test_a_user_who_switched_off_gets_no_model_call(self):
		with (
			patch.object(sr, "enabled", return_value=True),
			patch.object(sr, "user_enabled", return_value=False),
		):
			self.assertEqual(sr.skip_reason("C1", "R1", "u@example.com"), "user_off")

	def test_the_settings_api_reads_and_writes_the_switch(self):
		from jarvis.chat import user_settings_api as usa

		doc = frappe._dict(user="u@example.com", suggest_replies=1, save=MagicMock())
		with (
			patch.object(usa, "require_jarvis_access"),
			patch.object(usa.usage, "get_or_create_user_settings", return_value=doc),
			patch.object(
				usa, "_settings_payload", side_effect=lambda d: {"suggest_replies": d.suggest_replies}
			),
		):
			self.assertEqual(usa.update_my_settings(suggest_replies=0)["data"], {"suggest_replies": 0})
			self.assertEqual(usa.update_my_settings(suggest_replies="1")["data"], {"suggest_replies": 1})


class TestSkipReason(FrappeTestCase):
	def _reason(self, rows, *, enabled=True, shared=False, pending=None, message="R1"):
		with (
			patch.object(sr, "enabled", return_value=enabled),
			patch("jarvis.chat.collaboration.is_collaborative", return_value=shared),
			patch.object(sr, "_visible_rows", return_value=rows),
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=pending or []),
		):
			return sr.skip_reason("C1", message, "u@example.com")

	def test_a_finished_latest_reply_qualifies(self):
		self.assertIsNone(self._reason([_row("R1", "assistant", "Done.")]))

	def test_every_gate(self):
		reply = _row("R1", "assistant", "Done.")
		self.assertEqual(self._reason([reply], enabled=False), "disabled")
		self.assertEqual(self._reason([reply], shared=True), "shared")
		self.assertEqual(self._reason([_row("U2", "user", "next")]), "not_latest")
		self.assertEqual(self._reason([]), "not_latest")
		self.assertEqual(self._reason([_row("R1", "assistant", "x", error="boom")]), "not_a_finished_reply")
		self.assertEqual(self._reason([_row("R1", "assistant", "x", stopped=1)]), "not_a_finished_reply")
		self.assertEqual(self._reason([_row("R1", "assistant", "  ")]), "empty")
		ask = _row("R1", "assistant", 'Which one?\n```jarvis-ask\n{"questions": []}\n```')
		self.assertEqual(self._reason([ask]), "asks_its_own_question")
		self.assertEqual(self._reason([reply], pending=[{"token": "t"}]), "pending_card")


class TestJob(FrappeTestCase):
	def _run(self, answer='{"replies": ["Export it", "Chart it"]}', *, claude=False, skips=(None, None)):
		settings = frappe._dict(agent_url="http://agent:18789")
		gen = MagicMock(return_value=answer)
		with (
			patch.object(sr, "skip_reason", side_effect=list(skips)),
			patch.object(sr.frappe, "get_single", return_value=settings),
			patch.object(sr.frappe, "get_doc", return_value=frappe._dict(name="C1")),
			patch("jarvis.chat.turn_handler._resolve_model_and_provider", return_value=("m", "p")),
			patch("jarvis.chat.turn_handler.one_shot_runs_on_claude_plan", return_value=claude),
			patch.object(sr, "transcript", return_value="User: hi\n\nJarvis: hello"),
			patch.object(sr, "_generate_via_gateway", gen),
			patch("jarvis.chat.txn.fresh_snapshot"),
			patch.object(sr.frappe.db, "set_value") as set_value,
			patch.object(sr.frappe.db, "commit"),
			patch.object(sr, "publish_to_user") as publish,
			patch.object(sr.frappe, "log_error") as log_error,
		):
			sr.job("C1", "R1", "u@example.com")
		return gen, set_value, publish, log_error

	def test_stores_on_the_reply_and_publishes(self):
		gen, set_value, publish, _ = self._run()
		gen.assert_called_once_with("ws://agent:18789", "User: hi\n\nJarvis: hello", model="m", provider="p")
		stored = {"replies": ["Export it", "Chart it"], "multi_select": False}
		set_value.assert_called_once_with(
			sr.MSG, "R1", "suggested_replies", json.dumps(stored), update_modified=False
		)
		publish.assert_called_once_with(
			"u@example.com",
			{"kind": "message:replies", "conversation_id": "C1", "message_id": "R1", **stored},
		)

	def test_no_model_call_on_the_claude_plan(self):
		gen, set_value, publish, _ = self._run(claude=True)
		gen.assert_not_called()
		set_value.assert_not_called()
		publish.assert_not_called()

	def test_a_chat_that_moved_on_meanwhile_gets_nothing(self):
		_, set_value, publish, _ = self._run(skips=(None, "not_latest"))
		set_value.assert_not_called()
		publish.assert_not_called()

	def test_a_bad_answer_gets_nothing(self):
		_, set_value, publish, _ = self._run(answer="Sure! Here are some ideas.")
		set_value.assert_not_called()
		publish.assert_not_called()

	def test_a_skipped_reply_never_calls_the_model(self):
		gen, set_value, _, _ = self._run(skips=("pending_card",))
		gen.assert_not_called()
		set_value.assert_not_called()

	def test_never_raises(self):
		with (
			patch.object(sr, "skip_reason", side_effect=RuntimeError("db down")),
			patch.object(sr.frappe, "log_error") as log_error,
		):
			sr.job("C1", "R1", "u@example.com")
		log_error.assert_called_once()


class TestEnqueue(FrappeTestCase):
	def test_queues_the_job_on_the_short_queue(self):
		with patch.object(sr, "enabled", return_value=True), patch.object(sr.frappe, "enqueue") as enqueue:
			sr.enqueue("C1", "R1", "u@example.com")
		enqueue.assert_called_once_with(
			"jarvis.chat.suggest_replies.job",
			queue="short",
			conversation="C1",
			message="R1",
			owner="u@example.com",
			enqueue_after_commit=True,
		)

	def test_nothing_when_off_or_without_a_reply(self):
		with patch.object(sr.frappe, "enqueue") as enqueue:
			with patch.object(sr, "enabled", return_value=False):
				sr.enqueue("C1", "R1", "u@example.com")
			with patch.object(sr, "enabled", return_value=True):
				sr.enqueue("C1", None, "u@example.com")
				sr.enqueue("C1", "R1", None)
		enqueue.assert_not_called()


class TestWiring(FrappeTestCase):
	def test_an_invisible_effect_owed_only_by_a_good_unshared_reply(self):
		self.assertIn("suggest_replies", ts.EFFECT_NAMES)
		self.assertIn("suggest_replies", ts.INVISIBLE_EFFECT_NAMES)
		self.assertNotIn("suggest_replies", ts.VISIBLE_EFFECT_NAMES)
		self.assertIn("suggest_replies", settlement.FINAL_EFFECTS)
		for owed in (
			settlement.TERMINAL_EFFECTS,
			settlement.SHARED_FINAL_EFFECTS,
			settlement.SHARED_TERMINAL_EFFECTS,
		):
			self.assertNotIn("suggest_replies", owed)
		self.assertIs(finalize._RUNNERS["suggest_replies"], finalize._effect_suggest_replies)

	def test_the_effect_enqueues_for_the_turns_reply_and_skips_an_errored_turn(self):
		ctx = MagicMock(
			errored=False, conversation="C1", owner="u@example.com", turn={"assistant_message": "R1"}
		)
		with patch.object(sr, "enqueue") as enqueue:
			finalize._effect_suggest_replies(ctx)
			enqueue.assert_called_once_with("C1", "R1", "u@example.com")
			enqueue.reset_mock()
			ctx.errored = True
			finalize._effect_suggest_replies(ctx)
			enqueue.assert_not_called()

	def test_the_orphan_sweep_knows_the_throwaway_sessions(self):
		self.assertIn("jarvis-replies-", session_lifecycle._THROWAWAY_LABEL_PREFIXES)
		self.assertIn("jarvis-suggest-", session_lifecycle._THROWAWAY_LABEL_PREFIXES)

	def test_a_shared_reader_never_gets_them(self):
		from jarvis.chat.share_projection import project_row

		row = {
			"name": "R1",
			"role": "assistant",
			"content": "You have 6 customers.",
			"seq": 2,
			"suggested_replies": {"replies": ["List them"], "multi_select": False},
		}
		self.assertNotIn("suggested_replies", project_row(row, "C1"))

	def test_the_reply_field_and_the_setting_exist(self):
		msg = frappe.get_file_json(
			frappe.get_app_path(
				"jarvis", "jarvis", "doctype", "jarvis_chat_message", "jarvis_chat_message.json"
			)
		)
		field = next(f for f in msg["fields"] if f["fieldname"] == "suggested_replies")
		self.assertEqual((field["fieldtype"], field.get("hidden"), field.get("read_only")), ("JSON", 1, 1))
		conf = frappe.get_file_json(
			frappe.get_app_path("jarvis", "jarvis", "doctype", "jarvis_settings", "jarvis_settings.json")
		)
		toggle = next(f for f in conf["fields"] if f["fieldname"] == sr.SETTING)
		self.assertEqual((toggle["fieldtype"], toggle["default"]), ("Check", "1"))
