"""Tests for jarvis.chat.turn_recovery (scheduler-driven long-turn recovery).

Isolation note: the test conversation uses a UNIQUE session_key and the fake
gateway returns transcript content only for that key, so any unrelated
recovering rows on the test site are left in the waiting state untouched. No
destructive global cleanup is needed.
"""

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import turn_recovery
from jarvis.chat.turn_recovery import MSG as MSG_DT

SK = "sk_rec_unique_test"


class TestTurnRecovery(FrappeTestCase):
	def setUp(self):
		self.conv = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "rec",
				"session_key": SK,
			}
		).insert(ignore_permissions=True)
		self.msg = self._add_msg(seq=1, started_min=-10)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(MSG_DT, {"conversation": self.conv.name})
		frappe.db.delete(turn_recovery.CONV, {"name": self.conv.name})
		frappe.db.commit()

	def _add_msg(self, seq, started_min, content="partial..."):
		return frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv.name,
				"seq": seq,
				"role": "assistant",
				"content": content,
				"streaming": 1,
				"recovering": 1,
				"recovery_started_at": frappe.utils.add_to_date(
					frappe.utils.now_datetime(), minutes=started_min
				),
			}
		).insert(ignore_permissions=True)

	def _fake_sess(self, *, active=None, messages_by_key=None):
		active = active or set()
		messages_by_key = messages_by_key or {}
		sess = MagicMock()

		def _request(method, params, *, timeout_s):
			if method == "sessions.list":
				return {"payload": {"sessions": [{"key": k, "hasActiveRun": True} for k in active]}}
			return {"payload": {}}

		sess._request = _request
		sess.get_session_messages = lambda key, limit=50: messages_by_key.get(key, [])
		sess.is_run_active = lambda key, timeout_s=None: key in active
		return sess

	def _run(self, sess):
		@contextlib.contextmanager
		def fake_conn(_gateway_url):
			yield sess

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", fake_conn),
			patch("jarvis.chat.turn_recovery.publish_to_user") as pub,
		):
			out = turn_recovery.recover_pending_turns()
		return out, pub

	def _run_now(self, sess, conv_id=None):
		@contextlib.contextmanager
		def fake_conn(_gateway_url):
			yield sess

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", fake_conn),
			patch("jarvis.chat.turn_recovery.publish_to_user") as pub,
		):
			out = turn_recovery.recover_now(conv_id or self.conv.name)
		return out, pub

	def _row(self, name=None):
		return frappe.db.get_value(
			MSG_DT, name or self.msg.name, ["content", "streaming", "recovering", "error"], as_dict=True
		)

	# --- finalize from the RAW transcript (no truncation), overwrite ---------
	def test_finalizes_when_run_done_with_text(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "user", "content": "q", "__openclaw": {"seq": 1}},
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.content, "the full answer")  # overwrite, not append
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)
		self.assertFalse(row.error)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("assistant:delta", kinds)
		self.assertIn("run:end", kinds)

	def test_uses_raw_session_messages_not_truncating_history(self):
		# Content must come from get_session_messages (raw), never get_history (#1).
		sess = self._fake_sess(
			messages_by_key={SK: [{"role": "assistant", "content": "x", "__openclaw": {"seq": 1}}]}
		)
		self._run(sess)
		sess.get_history.assert_not_called()

	def test_leaves_active_run_untouched(self):
		sess = self._fake_sess(
			active={SK},
			messages_by_key={SK: [{"role": "assistant", "content": "x", "__openclaw": {"seq": 1}}]},
		)
		self._run(sess)
		row = self._row()
		self.assertEqual(row.recovering, 1)
		self.assertEqual(row.streaming, 1)

	def test_waits_when_no_output_yet_does_not_error(self):
		# Not active, no transcript content -> wait (NOT errored before ceiling).
		sess = self._fake_sess(messages_by_key={SK: []})
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)
		self.assertFalse(row.error)
		self.assertNotIn("run:error", [c.args[1]["kind"] for c in pub.call_args_list])

	# --- #3/#5: unconditional ceiling backstop, even when gateway is down ----
	def test_ceiling_errors_even_when_gateway_unreachable(self):
		old = self._add_msg(seq=2, started_min=-120)  # 2h, past the 60min ceiling
		frappe.db.commit()

		def boom(_url):
			raise RuntimeError("gateway down")

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", boom),
			patch("jarvis.chat.turn_recovery.publish_to_user"),
		):
			turn_recovery.recover_pending_turns()
		row = self._row(old.name)
		self.assertEqual(row.streaming, 0)  # errored despite gateway down
		self.assertEqual(row.recovering, 0)
		self.assertTrue(row.error)

	# --- #2: no cross-row content bleed -------------------------------------
	def test_no_cross_row_bleed_only_latest_finalized(self):
		older = self.msg  # seq 1
		newer = self._add_msg(seq=2, started_min=-10, content="partial2")
		frappe.db.commit()
		sess = self._fake_sess(
			messages_by_key={
				SK: [{"role": "assistant", "content": "latest answer", "__openclaw": {"seq": 9}}]
			}
		)
		self._run(sess)
		self.assertEqual(self._row(newer.name).content, "latest answer")
		older_row = self._row(older.name)
		self.assertNotEqual(older_row.content, "latest answer")  # no bleed
		self.assertEqual(older_row.streaming, 1)  # older rides the ceiling backstop

	# --- #8: conditional finalize is idempotent -----------------------------
	def test_conditional_finalize_no_op_on_already_cleared_row(self):
		frappe.db.set_value(MSG_DT, self.msg.name, {"streaming": 0, "recovering": 0})
		frappe.db.commit()
		pub = MagicMock()
		with patch("jarvis.chat.turn_recovery.publish_to_user", pub):
			turn_recovery._finalize(
				{"name": self.msg.name, "conversation": self.conv.name, "owner": "x"}, "ignored"
			)
		pub.assert_not_called()
		self.assertNotEqual(self._row().content, "ignored")

	# --- #10: type-guarded extraction ---------------------------------------
	def test_extract_latest_assistant_text_handles_block_list(self):
		text = turn_recovery._latest_assistant_text(
			[
				{
					"role": "assistant",
					"__openclaw": {"seq": 5},
					"content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
				}
			]
		)
		self.assertEqual(text, "hello\nworld")

	def test_extract_ignores_non_string_text(self):
		text = turn_recovery._latest_assistant_text(
			[
				{
					"role": "assistant",
					"__openclaw": {"seq": 5},
					"content": [{"type": "text", "text": {"unexpected": "dict"}}],
					"text": 123,
				}
			]
		)
		self.assertEqual(text, "")

	# --- recover_now: in-worker immediate recovery --------------------------
	def test_recover_now_finalizes_when_run_done_with_text(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		out, pub = self._run_now(sess)
		self.assertEqual(out, "finalized")
		row = self._row()
		self.assertEqual(row.content, "the full answer")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)
		self.assertFalse(row.error)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("assistant:delta", kinds)
		self.assertIn("run:end", kinds)
		run_ids = {c.args[1].get("run_id") for c in pub.call_args_list}
		self.assertEqual(run_ids, {"recovered"})

	def test_recover_now_leaves_active_run_untouched(self):
		sess = self._fake_sess(
			active={SK},
			messages_by_key={SK: [{"role": "assistant", "content": "x", "__openclaw": {"seq": 1}}]},
		)
		out, pub = self._run_now(sess)
		self.assertEqual(out, "active")
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)
		pub.assert_not_called()

	def test_recover_now_waits_when_transcript_empty(self):
		sess = self._fake_sess(messages_by_key={SK: []})
		out, pub = self._run_now(sess)
		self.assertEqual(out, "waiting")
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)
		pub.assert_not_called()

	def test_recover_now_skipped_when_no_recovering_row(self):
		other = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "rec-other",
				"session_key": "sk_rec_other_test",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			sess = self._fake_sess()
			out, pub = self._run_now(sess, conv_id=other.name)
			self.assertEqual(out, "skipped")
			pub.assert_not_called()
		finally:
			frappe.db.delete(turn_recovery.CONV, {"name": other.name})
			frappe.db.commit()

	def test_recover_now_skipped_when_self_hosted(self):
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=True),
			patch("jarvis.chat.turn_recovery.publish_to_user") as pub,
		):
			out = turn_recovery.recover_now(self.conv.name)
		self.assertEqual(out, "skipped")
		pub.assert_not_called()
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)

	def test_recover_now_skipped_on_connect_failure(self):
		def boom(_url):
			raise RuntimeError("gateway down")

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", boom),
			patch("jarvis.chat.turn_recovery.publish_to_user") as pub,
		):
			out = turn_recovery.recover_now(self.conv.name)
		self.assertEqual(out, "skipped")
		pub.assert_not_called()
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)

	# --- macro chaining hook: finalize/error must advance a running macro ---
	def test_finalize_advances_macro_with_errored_false(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		with patch("jarvis.chat.macros.advance_after_turn") as advance:
			self._run(sess)
		advance.assert_called_once_with(self.conv.name, errored=False)

	def test_error_advances_macro_with_errored_true(self):
		old = self._add_msg(seq=2, started_min=-120)  # past the ceiling -> _error
		frappe.db.commit()

		def boom(_url):
			raise RuntimeError("gateway down")

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", boom),
			patch("jarvis.chat.turn_recovery.publish_to_user"),
			patch("jarvis.chat.macros.advance_after_turn") as advance,
		):
			turn_recovery.recover_pending_turns()
		advance.assert_called_once_with(old.conversation, errored=True)

	def test_losing_conditional_clear_does_not_advance_macro(self):
		# Row already cleared by another cycle: _conditional_clear returns
		# False, so _finalize must return before ever touching the macro.
		frappe.db.set_value(MSG_DT, self.msg.name, {"streaming": 0, "recovering": 0})
		frappe.db.commit()
		with (
			patch("jarvis.chat.turn_recovery.publish_to_user"),
			patch("jarvis.chat.macros.advance_after_turn") as advance,
		):
			turn_recovery._finalize(
				{"name": self.msg.name, "conversation": self.conv.name, "owner": "x"}, "ignored"
			)
		advance.assert_not_called()

	def test_macro_advance_exception_is_swallowed_row_still_finalized(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		with (
			patch(
				"jarvis.chat.macros.advance_after_turn",
				side_effect=RuntimeError("macro engine bug"),
			),
			patch("frappe.log_error") as log_err,
		):
			_, pub = self._run(sess)
		# The macro exception was logged, not raised.
		log_err.assert_called()
		# The row still finalized and the publishes still happened.
		row = self._row()
		self.assertEqual(row.content, "the full answer")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("assistant:delta", kinds)
		self.assertIn("run:end", kinds)

	# --- transcript-seq watermark: never stamp a previous turn's answer ----
	def test_watermark_ignores_older_assistant_message(self):
		# Transcript's newest assistant message (seq 5) is OLDER than the
		# watermark (7) captured before this turn's send - a run that died
		# server-side with zero output must not have the previous reply
		# wrongly stamped onto this row.
		frappe.db.set_value(MSG_DT, self.msg.name, "openclaw_seq_watermark", 7)
		frappe.db.commit()
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the OLD reply", "__openclaw": {"seq": 5}},
				]
			}
		)
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)
		self.assertFalse(row.error)
		self.assertNotIn("run:end", [c.args[1]["kind"] for c in pub.call_args_list])

	def test_watermark_finalizes_from_strictly_newer_message(self):
		frappe.db.set_value(MSG_DT, self.msg.name, "openclaw_seq_watermark", 7)
		frappe.db.commit()
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the NEW reply", "__openclaw": {"seq": 9}},
				]
			}
		)
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.content, "the NEW reply")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)
		self.assertIn("run:end", [c.args[1]["kind"] for c in pub.call_args_list])

	def test_watermark_zero_behaves_as_before(self):
		# Default watermark (0, i.e. never captured) must not change any
		# pre-existing recovery behavior.
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.content, "the full answer")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)

	def test_latest_assistant_text_min_seq_filters_out_older_message(self):
		text = turn_recovery._latest_assistant_text(
			[
				{"role": "assistant", "__openclaw": {"seq": 5}, "content": "old"},
			],
			min_seq=7,
		)
		self.assertEqual(text, "")

	def test_latest_assistant_text_min_seq_keeps_strictly_newer_message(self):
		text = turn_recovery._latest_assistant_text(
			[
				{"role": "assistant", "__openclaw": {"seq": 5}, "content": "old"},
				{"role": "assistant", "__openclaw": {"seq": 9}, "content": "new"},
			],
			min_seq=7,
		)
		self.assertEqual(text, "new")

	# --- upper bound: never steal a LATER turn's answer (live catch,
	# --- 2026-07-03: a parked row recovered with the next question's reply)

	def test_latest_assistant_text_max_seq_excludes_later_turns_message(self):
		msgs = [
			{"role": "assistant", "__openclaw": {"seq": 2}, "content": "GOLF"},
			{"role": "assistant", "__openclaw": {"seq": 4}, "content": "HOTEL"},
		]
		self.assertEqual(turn_recovery._latest_assistant_text(msgs, min_seq=0, max_seq=2), "GOLF")
		self.assertEqual(turn_recovery._latest_assistant_text(msgs, min_seq=0), "HOTEL")

	def _add_later_assistant_row(self, watermark: int):
		"""A completed later turn in the same conversation whose watermark
		(captured before ITS send) caps the parked row's window."""
		seq0 = frappe.db.get_value(MSG_DT, self.msg.name, "seq")
		doc = frappe.get_doc(
			{
				"doctype": MSG_DT,
				"conversation": self.conv.name,
				"seq": (seq0 or 0) + 2,
				"role": "assistant",
				"content": "HOTEL",
				"streaming": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(MSG_DT, doc.name, "openclaw_seq_watermark", watermark)
		frappe.db.commit()
		return doc

	def test_recovery_bounded_by_next_turns_watermark(self):
		"""Parked row's own reply IS in the transcript window: recovery
		must take it, not the later turn's newer reply."""
		self._add_later_assistant_row(watermark=2)
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "GOLF", "__openclaw": {"seq": 2}},
					{"role": "assistant", "content": "HOTEL", "__openclaw": {"seq": 4}},
				]
			}
		)
		self._run(sess)
		row = self._row()
		self.assertEqual(row.content, "GOLF")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)

	def test_recovery_waits_when_window_empty_instead_of_stealing(self):
		"""Parked row's reply is genuinely lost (window empty); a later
		turn's reply exists past the bound. Recovery must WAIT (ceiling
		errors it later), never stamp the later answer onto this row."""
		self._add_later_assistant_row(watermark=2)
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "HOTEL", "__openclaw": {"seq": 4}},
				]
			}
		)
		_, pub = self._run(sess)
		row = self._row()
		self.assertEqual(row.streaming, 1)
		self.assertEqual(row.recovering, 1)
		self.assertNotEqual(row.content, "HOTEL")
		self.assertNotIn("run:end", [c.args[1]["kind"] for c in pub.call_args_list])

	def test_next_turn_watermark_none_without_later_usable_row(self):
		self.assertIsNone(
			turn_recovery._next_turn_watermark(
				self.conv.name,
				frappe.db.get_value(MSG_DT, self.msg.name, "seq"),
			)
		)
		self._add_later_assistant_row(watermark=0)  # unusable: never captured
		self.assertIsNone(
			turn_recovery._next_turn_watermark(
				self.conv.name,
				frappe.db.get_value(MSG_DT, self.msg.name, "seq"),
			)
		)

	def test_recover_now_idempotent_after_finalize_no_double_publish(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		out1, pub1 = self._run_now(sess)
		self.assertEqual(out1, "finalized")
		pub1.assert_called()

		sess2 = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		out2, pub2 = self._run_now(sess2)
		self.assertEqual(out2, "skipped")
		pub2.assert_not_called()


class TestRecoveryRichOutputsAndWasRecovered(FrappeTestCase):
	"""Recovery-completeness batch: _finalize routes the recovered turn's
	canvas/generated-image outputs through the same persist_rich_outputs
	the worker's clean exit calls, and both _finalize and _error stamp
	was_recovered=1 (the never-error machinery's fingerprint).

	Deliberately does NOT subclass TestTurnRecovery (that would re-run its
	whole suite under this class name too) - the small amount of shared
	setup is duplicated instead."""

	def setUp(self):
		self.conv = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "rec2",
				"session_key": SK,
			}
		).insert(ignore_permissions=True)
		self.msg = self._add_msg(seq=1, started_min=-10)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(MSG_DT, {"conversation": self.conv.name})
		frappe.db.delete(turn_recovery.CONV, {"name": self.conv.name})
		frappe.db.commit()

	def _add_msg(self, seq, started_min, content="partial..."):
		return frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv.name,
				"seq": seq,
				"role": "assistant",
				"content": content,
				"streaming": 1,
				"recovering": 1,
				"recovery_started_at": frappe.utils.add_to_date(
					frappe.utils.now_datetime(), minutes=started_min
				),
			}
		).insert(ignore_permissions=True)

	def _fake_sess(self, *, active=None, messages_by_key=None):
		active = active or set()
		messages_by_key = messages_by_key or {}
		sess = MagicMock()

		def _request(method, params, *, timeout_s):
			if method == "sessions.list":
				return {"payload": {"sessions": [{"key": k, "hasActiveRun": True} for k in active]}}
			return {"payload": {}}

		sess._request = _request
		sess.get_session_messages = lambda key, limit=50: messages_by_key.get(key, [])
		sess.is_run_active = lambda key, timeout_s=None: key in active
		return sess

	def _run(self, sess):
		@contextlib.contextmanager
		def fake_conn(_gateway_url):
			yield sess

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", fake_conn),
			patch("jarvis.chat.turn_recovery.publish_to_user") as pub,
		):
			out = turn_recovery.recover_pending_turns()
		return out, pub

	def _row(self, name=None):
		return frappe.db.get_value(
			MSG_DT, name or self.msg.name, ["content", "streaming", "recovering", "error"], as_dict=True
		)

	def test_finalize_calls_persist_rich_outputs_best_effort(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		with patch("jarvis.chat.turn_handler.persist_rich_outputs") as rich:
			self._run(sess)
		rich.assert_called_once()
		args = rich.call_args.args
		self.assertEqual(args[0], self.msg.name)
		self.assertEqual(args[1], self.conv.name)
		self.assertEqual(args[3], "recovered")

	def test_finalize_survives_persist_rich_outputs_raising(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		with (
			patch(
				"jarvis.chat.turn_handler.persist_rich_outputs",
				side_effect=RuntimeError("canvas fetch exploded"),
			),
			patch("frappe.log_error") as log_err,
		):
			_, pub = self._run(sess)
		log_err.assert_called()
		row = self._row()
		self.assertEqual(row.content, "the full answer")
		self.assertEqual(row.streaming, 0)
		self.assertEqual(row.recovering, 0)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("assistant:delta", kinds)
		self.assertIn("run:end", kinds)

	def test_finalize_stamps_was_recovered(self):
		sess = self._fake_sess(
			messages_by_key={
				SK: [
					{"role": "assistant", "content": "the full answer", "__openclaw": {"seq": 2}},
				]
			}
		)
		with patch("jarvis.chat.turn_handler.persist_rich_outputs"):
			self._run(sess)
		self.assertEqual(
			frappe.db.get_value(MSG_DT, self.msg.name, "was_recovered"),
			1,
		)

	def test_error_stamps_was_recovered(self):
		old = self._add_msg(seq=2, started_min=-120)  # past the ceiling -> _error
		frappe.db.commit()

		def boom(_url):
			raise RuntimeError("gateway down")

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.chat.turn_recovery.frappe.get_single", return_value=settings),
			patch("jarvis.chat.turn_recovery._recovery_connection", boom),
			patch("jarvis.chat.turn_recovery.publish_to_user"),
		):
			turn_recovery.recover_pending_turns()
		self.assertEqual(
			frappe.db.get_value(MSG_DT, old.name, "was_recovered"),
			1,
		)


class TestRecoveryRateWatch(FrappeTestCase):
	"""Hourly spike alarm: high 24h recovered-turn rate -> one Error Log,
	deduped for _RATE_WATCH_DEDUPE_HOURS so a sustained problem alarms
	roughly once a day rather than every hour."""

	TITLE = "chat: high recovered-turn rate"

	def setUp(self):
		# Neutralize residue: the watch is site-global over the last 24h, so
		# stray recovered rows or a stray alarm left by an aborted earlier
		# run would make these deterministic threshold tests flaky.
		frappe.db.sql("UPDATE `tabJarvis Chat Message` SET was_recovered = 0 WHERE was_recovered = 1")
		# DENOMINATOR control (the recurring dilution): the alarm's rate is
		# recovered / ALL assistant rows in the last 24h (site-global by design).
		# Committed assistant rows from sibling tests / prior runs that fall in the
		# window inflate the denominator and push the rate under the threshold, so
		# the alarm never fires (0 != 1) even though this test's own fixtures are
		# well over it. Neutralizing only was_recovered fixes the NUMERATOR but not
		# the denominator — so push every pre-existing in-window assistant row OUT of
		# the 24h window; only this test's own rows (inserted after setUp) then count.
		since_24h = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-24)
		out_of_window = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-2)
		frappe.db.sql(
			"UPDATE `tabJarvis Chat Message` SET creation = %(old)s "
			"WHERE role = 'assistant' AND creation >= %(since)s",
			{"old": out_of_window, "since": since_24h},
		)
		frappe.db.delete("Error Log", {"method": self.TITLE})
		self.conv = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "rate-watch",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(MSG_DT, {"conversation": self.conv.name})
		frappe.db.delete(turn_recovery.CONV, {"name": self.conv.name})
		frappe.db.delete("Error Log", {"method": self.TITLE})
		frappe.db.commit()

	def _add_assistant(self, *, recovered):
		frappe.get_doc(
			{
				"doctype": MSG_DT,
				"conversation": self.conv.name,
				"seq": 1,
				"role": "assistant",
				"content": "x",
				"streaming": 0,
				"was_recovered": 1 if recovered else 0,
			}
		).insert(ignore_permissions=True)

	def test_below_threshold_no_error_log(self):
		# 1 recovered out of 2: rate 50% but recovered count (1) below the
		# minimum (5), so no alarm.
		self._add_assistant(recovered=True)
		self._add_assistant(recovered=False)
		frappe.db.commit()
		with patch("jarvis.selfhost.is_self_hosted", return_value=False):
			turn_recovery.recovery_rate_watch()
		self.assertEqual(frappe.db.count("Error Log", {"method": self.TITLE}), 0)

	def test_above_threshold_logs_exactly_one(self):
		for _ in range(6):
			self._add_assistant(recovered=True)
		for _ in range(2):
			self._add_assistant(recovered=False)
		frappe.db.commit()
		with patch("jarvis.selfhost.is_self_hosted", return_value=False):
			turn_recovery.recovery_rate_watch()
		self.assertEqual(frappe.db.count("Error Log", {"method": self.TITLE}), 1)

	def test_second_run_within_20h_still_exactly_one(self):
		for _ in range(6):
			self._add_assistant(recovered=True)
		for _ in range(2):
			self._add_assistant(recovered=False)
		frappe.db.commit()
		with patch("jarvis.selfhost.is_self_hosted", return_value=False):
			turn_recovery.recovery_rate_watch()
			turn_recovery.recovery_rate_watch()
		self.assertEqual(frappe.db.count("Error Log", {"method": self.TITLE}), 1)

	def test_self_hosted_returns_early_no_query(self):
		for _ in range(6):
			self._add_assistant(recovered=True)
		frappe.db.commit()
		with patch("jarvis.selfhost.is_self_hosted", return_value=True):
			turn_recovery.recovery_rate_watch()
		self.assertEqual(frappe.db.count("Error Log", {"method": self.TITLE}), 0)
