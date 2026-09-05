"""Context meter + Compact (spec 2026-09-05-chat-context-meter-compact-design)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import api as chat_api
from jarvis.chat import compaction
from jarvis.chat.events import parse_event

CHAT_SESSION = "Jarvis Chat Session"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN_USAGE = "Jarvis Turn Usage"


def _mk_conversation(session_key: str | None) -> str:
	doc = frappe.get_doc({"doctype": CONV, "title": "compact test", "session_key": session_key or ""})
	doc.insert(ignore_permissions=True)
	if session_key and not frappe.db.exists(CHAT_SESSION, {"session_key": session_key}):
		frappe.get_doc({"doctype": CHAT_SESSION, "session_key": session_key, "user": "Administrator"}).insert(
			ignore_permissions=True
		)
	return doc.name


def _cleanup() -> None:
	"""These fixtures are inserted as Administrator (see ``_mk_conversation``) and
	the compaction job commits, so a test-transaction rollback cannot undo them -
	delete by name/session_key explicitly so a fresh CI DB stays clean."""
	convs = frappe.get_all(CONV, filters={"title": "compact test"}, pluck="name")
	if convs:
		# retry_message fixtures insert real Message rows under these conversations
		# (force=True on the conversation delete below only ignores the link, it
		# does not cascade), so drop them first to avoid leaving orphan rows.
		frappe.db.delete(MSG, {"conversation": ["in", convs]})
	for name in convs:
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(
		CHAT_SESSION, filters={"session_key": ["like", "agent:main:c-%"]}, pluck="name"
	):
		frappe.delete_doc(CHAT_SESSION, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(TURN_USAGE, filters={"session_key": ["like", "agent:main:c-%"]}, pluck="name"):
		frappe.delete_doc(TURN_USAGE, name, ignore_permissions=True, force=True)


class TestCompactionFields(FrappeTestCase):
	def test_session_has_budget_fields(self):
		meta = frappe.get_meta(CHAT_SESSION)
		for f, t in (
			("budget_route", "Data"),
			("reserve_tokens", "Int"),
			("compaction_count", "Int"),
			("last_compacted_at", "Datetime"),
		):
			self.assertEqual(meta.get_field(f).fieldtype, t, f)

	def test_conversation_has_compacting_since(self):
		self.assertEqual(frappe.get_meta(CONV).get_field("compacting_since").fieldtype, "Datetime")


class TestCompactionHelpers(FrappeTestCase):
	def tearDown(self):
		_cleanup()
		frappe.db.commit()

	def test_sanitize_hint(self):
		self.assertEqual(
			compaction.sanitize_hint("  keep the invoice\x00 inputs  "), "keep the invoice inputs"
		)
		self.assertEqual(len(compaction.sanitize_hint("x" * 900)), compaction.HINT_MAX_CHARS)
		self.assertEqual(compaction.sanitize_hint(None), "")
		with self.assertRaises(frappe.ValidationError):
			compaction.sanitize_hint("/reset")

	def test_classify_notice(self):
		self.assertEqual(
			compaction.classify_notice("⚙️ Compacted (58k before) • Context 58k/200k"), "compacted"
		)
		self.assertEqual(compaction.classify_notice("⚙️ Compaction skipped: nothing compactable"), "skipped")
		self.assertEqual(compaction.classify_notice("⚙️ Compaction failed: rate limited"), "failed")
		self.assertEqual(compaction.classify_notice(""), "failed")

	def test_is_compacting_window(self):
		conv = _mk_conversation("agent:main:c-lock")
		self.assertFalse(compaction.is_compacting(conv))
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		self.assertTrue(compaction.is_compacting(conv))
		self.assertTrue(compaction.context_payload(conv)["compacting"])
		frappe.db.set_value(
			CONV,
			conv,
			"compacting_since",
			frappe.utils.add_to_date(
				frappe.utils.now_datetime(), seconds=-(compaction.COMPACT_LOCK_SECONDS + 5)
			),
			update_modified=False,
		)
		self.assertFalse(compaction.is_compacting(conv))
		self.assertFalse(compaction.context_payload(conv)["compacting"])

	def test_context_payload_fresh_and_unmeasured(self):
		conv = _mk_conversation("agent:main:c-pay")
		p = compaction.context_payload(conv)
		self.assertFalse(p["fresh"])
		self.assertEqual(p["warn_pct"], 80)
		frappe.db.sql(
			"""UPDATE `tabJarvis Chat Session` SET last_total_tokens=50000, context_capacity=200000,
			context_pct=25, last_usage_at=NOW(), reserve_tokens=20000, budget_route='fits', compaction_count=1
			WHERE session_key=%s""",
			("agent:main:c-pay",),
		)
		frappe.get_doc(
			{
				"doctype": TURN_USAGE,
				"session_key": "agent:main:c-pay",
				"user": "Administrator",
				"profile_agent_id": "",
				"profile_tier": "full",
				"model": "gpt-5.5",
				"tokens_in": 1200,
				"tokens_out": 300,
				"cache_read": 0,
				"cache_write": 0,
				"cache_reported": 0,
				"tool_calls": 0,
				"day": frappe.utils.today(),
				"run_id": "",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		p = compaction.context_payload(conv)
		self.assertTrue(p["fresh"])
		self.assertEqual((p["used"], p["capacity"], p["pct"]), (50000, 200000, 25.0))
		self.assertEqual(p["auto_compact_pct"], 90.0)
		self.assertEqual(p["route"], "fits")
		self.assertEqual(p["compaction_count"], 1)
		self.assertEqual((p["last_in"], p["last_out"], p["model"]), (1200, 300, "gpt-5.5"))

	def test_context_payload_no_session(self):
		conv = _mk_conversation(None)
		p = compaction.context_payload(conv)
		self.assertEqual((p["used"], p["capacity"], p["fresh"]), (0, 0, False))
		self.assertEqual((p["last_in"], p["last_out"], p["model"]), (0, 0, ""))


class TestCompactionLock(FrappeTestCase):
	"""R1: start_compaction's lock is a compare-and-set, not a plain read-then-write,
	so two near-simultaneous callers can't both win it."""

	def tearDown(self):
		_cleanup()
		frappe.db.commit()

	def test_fresh_lock_refuses_and_does_not_enqueue(self):
		conv = _mk_conversation("agent:main:c-lock1")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			out = compaction.start_compaction(conv, "Administrator", "")
		self.assertEqual(out, {"ok": False, "reason": "already_compacting"})
		enq.assert_not_called()

	def test_expired_lock_is_taken_and_enqueues(self):
		conv = _mk_conversation("agent:main:c-lock2")
		frappe.db.set_value(
			CONV,
			conv,
			"compacting_since",
			frappe.utils.add_to_date(
				frappe.utils.now_datetime(), seconds=-(compaction.COMPACT_LOCK_SECONDS + 5)
			),
			update_modified=False,
		)
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			out = compaction.start_compaction(conv, "Administrator", "")
		self.assertEqual(out, {"ok": True, "queued": True})
		enq.assert_called_once()
		self.assertTrue(compaction.is_compacting(conv))

	def test_back_to_back_calls_only_the_first_wins(self):
		conv = _mk_conversation("agent:main:c-lock3")
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			first = compaction.start_compaction(conv, "Administrator", "")
			second = compaction.start_compaction(conv, "Administrator", "")
		self.assertEqual(first, {"ok": True, "queued": True})
		self.assertEqual(second, {"ok": False, "reason": "already_compacting"})
		enq.assert_called_once()


class TestRunCompact(FrappeTestCase):
	def tearDown(self):
		_cleanup()
		frappe.db.commit()

	def _run(self, conv, sess):
		with (
			patch("jarvis.chat.agent_session_pool.checkout") as co,
			patch("jarvis.chat.compaction.publish_to_user") as pub,
		):
			co.return_value.__enter__.return_value = sess
			compaction.run_compact(conv, "Administrator", "keep invoices")
		return pub

	def test_happy_path_writes_result_and_publishes(self):
		key = "agent:main:c-run1"
		conv = _mk_conversation(key)
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		sess = MagicMock()
		sess.list_sessions.return_value = [
			{
				"key": key,
				"totalTokens": 58000,
				"totalTokensFresh": True,
				"contextTokens": 200000,
				"compactionCheckpointCount": 1,
				"contextBudgetStatus": {"route": "fits", "reserveTokens": 20000},
			}
		]
		sess.compact_session.return_value = {
			"state": "final",
			"text": "⚙️ Compacted (58k before)",
			"run_id": "r",
		}
		pub = self._run(conv, sess)
		sess.compact_session.assert_called_once_with(
			key, "keep invoices", timeout_s=compaction.COMPACT_RPC_TIMEOUT_S
		)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("context:compacted", kinds)
		got = frappe.db.get_value(
			CHAT_SESSION, {"session_key": key}, ["compaction_count", "last_compacted_at"], as_dict=True
		)
		self.assertEqual(got.compaction_count, 1)
		self.assertIsNotNone(got.last_compacted_at)
		self.assertIsNone(frappe.db.get_value(CONV, conv, "compacting_since"))

	def test_declined_publishes_failure_and_clears_lock(self):
		key = "agent:main:c-run2"
		conv = _mk_conversation(key)
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		sess = MagicMock()
		sess.list_sessions.return_value = [{"key": key, "totalTokens": 1000, "totalTokensFresh": True}]
		sess.compact_session.return_value = {
			"state": "final",
			"text": "⚙️ Compaction skipped: nothing",
			"run_id": "r",
		}
		pub = self._run(conv, sess)
		payload = [c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "context:compact_failed"][0]
		self.assertEqual(payload["reason"], "runtime_declined")
		self.assertIsNone(frappe.db.get_value(CONV, conv, "compacting_since"))

	def test_failed_notice_publishes_runtime_failed(self):
		key = "agent:main:c-run2b"
		conv = _mk_conversation(key)
		sess = MagicMock()
		sess.list_sessions.return_value = [{"key": key, "totalTokens": 1000, "totalTokensFresh": True}]
		sess.compact_session.return_value = {
			"state": "final",
			"text": "⚙️ Compaction failed: rate limited",
			"run_id": "r",
		}
		pub = self._run(conv, sess)
		payload = [c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "context:compact_failed"][0]
		self.assertEqual(payload["reason"], "runtime_failed")

	def test_timeout_reason(self):
		from jarvis.chat.agent_client import AgentUnreachableError

		key = "agent:main:c-run3"
		conv = _mk_conversation(key)
		sess = MagicMock()
		sess.list_sessions.return_value = []
		sess.compact_session.side_effect = AgentUnreachableError("t", code="compact-timeout")
		pub = self._run(conv, sess)
		payload = [c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "context:compact_failed"][0]
		self.assertEqual(payload["reason"], "timeout")

	def test_gateway_unreachable_reason(self):
		from jarvis.chat.agent_client import AgentUnreachableError

		key = "agent:main:c-run4"
		conv = _mk_conversation(key)
		with (
			patch("jarvis.chat.agent_session_pool.checkout", side_effect=AgentUnreachableError("down")),
			patch("jarvis.chat.compaction.publish_to_user") as pub,
		):
			compaction.run_compact(conv, "Administrator", "")
		payload = [c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "context:compact_failed"][0]
		self.assertEqual(payload["reason"], "gateway_unreachable")


class TestCompactEndpoints(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		_cleanup()
		frappe.db.commit()

	def test_get_conversation_context_shape(self):
		conv = _mk_conversation("agent:main:c-ep1")
		out = chat_api.get_conversation_context(conv)
		for k in (
			"used",
			"capacity",
			"pct",
			"warn_pct",
			"auto_compact_pct",
			"route",
			"compaction_count",
			"last_compacted_at",
			"compacting",
			"fresh",
		):
			self.assertIn(k, out)

	def test_compact_refuses_without_session(self):
		conv = _mk_conversation(None)
		self.assertEqual(chat_api.compact_conversation(conv), {"ok": False, "reason": "nothing_to_compact"})

	def test_compact_refuses_when_nothing_new_since_last_compaction(self):
		key = "agent:main:c-ep11"
		conv = _mk_conversation(key)
		now = frappe.utils.now_datetime()
		frappe.db.set_value(
			CHAT_SESSION,
			{"session_key": key},
			{
				"last_compacted_at": now,
				"last_usage_at": frappe.utils.add_to_date(now, seconds=-60),
			},
		)
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			out = chat_api.compact_conversation(conv, "keep invoices")
		self.assertEqual(out, {"ok": False, "reason": "nothing_to_compact"})
		enq.assert_not_called()

	def test_compact_proceeds_after_a_turn_since_last_compaction(self):
		key = "agent:main:c-ep12"
		conv = _mk_conversation(key)
		now = frappe.utils.now_datetime()
		frappe.db.set_value(
			CHAT_SESSION,
			{"session_key": key},
			{
				"last_compacted_at": now,
				"last_usage_at": frappe.utils.add_to_date(now, seconds=1),
			},
		)
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			out = chat_api.compact_conversation(conv, "keep invoices")
		self.assertEqual(out, {"ok": True, "queued": True})
		enq.assert_called_once()

	def test_compact_refuses_when_already_compacting(self):
		conv = _mk_conversation("agent:main:c-ep2")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		self.assertEqual(chat_api.compact_conversation(conv), {"ok": False, "reason": "already_compacting"})

	def test_compact_refuses_when_busy(self):
		conv = _mk_conversation("agent:main:c-ep3")
		with patch("jarvis.chat.api._conversation_busy", return_value=True):
			self.assertEqual(
				chat_api.compact_conversation(conv), {"ok": False, "reason": "conversation_busy"}
			)

	def test_compact_bad_hint(self):
		conv = _mk_conversation("agent:main:c-ep4")
		self.assertEqual(chat_api.compact_conversation(conv, "/new"), {"ok": False, "reason": "bad_hint"})

	def test_compact_enqueues(self):
		conv = _mk_conversation("agent:main:c-ep5")
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			out = chat_api.compact_conversation(conv, "keep the invoice inputs")
		self.assertEqual(out, {"ok": True, "queued": True})
		self.assertEqual(enq.call_args.kwargs["hint"], "keep the invoice inputs")
		self.assertEqual(enq.call_args.kwargs["method"], "jarvis.chat.compaction.run_compact")
		self.assertTrue(compaction.is_compacting(conv))

	def test_compact_queue_name_from_site_config(self):
		conv = _mk_conversation("agent:main:c-ep-queue")
		with patch("jarvis.chat.compaction.frappe.enqueue") as enq:
			chat_api.compact_conversation(conv, "keep totals")
		self.assertEqual(enq.call_args.kwargs["queue"], "long")
		conv2 = _mk_conversation("agent:main:c-ep-queue2")
		with (
			patch("jarvis.chat.compaction.frappe.enqueue") as enq,
			patch.dict(frappe.conf, {"jarvis_compact_queue": "compact"}),
		):
			chat_api.compact_conversation(conv2, "keep totals")
		self.assertEqual(enq.call_args.kwargs["queue"], "compact")

	def test_conversation_busy_while_compacting(self):
		conv = _mk_conversation("agent:main:c-ep6")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		self.assertTrue(chat_api._conversation_busy(conv))

	def test_get_usage_carries_context(self):
		conv = _mk_conversation("agent:main:c-ep7")
		out = chat_api.get_usage(conv)
		self.assertIn("context", out)
		self.assertIn("capacity", out["context"])

	def test_send_message_refuses_while_compacting_machine_enabled(self):
		conv = _mk_conversation("agent:main:c-ep8")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		with (
			patch.object(chat_api.admission, "turn_machine_enabled", return_value=True),
			patch("jarvis.chat.api.validate_can_send", return_value=(True, None)),
		):
			out = chat_api.send_message(conv, "hello")
		self.assertEqual(out, {"ok": False, "reason": "Compacting this chat, try again in a moment"})
		self.assertFalse(frappe.db.exists(MSG, {"conversation": conv}))

	def test_send_message_refuses_while_compacting_legacy(self):
		conv = _mk_conversation("agent:main:c-ep9")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		with (
			patch.object(chat_api.admission, "turn_machine_enabled", return_value=False),
			patch("jarvis.chat.api.validate_can_send", return_value=(True, None)),
		):
			out = chat_api.send_message(conv, "hello")
		self.assertEqual(out, {"ok": False, "reason": "Compacting this chat, try again in a moment"})
		self.assertFalse(frappe.db.exists(MSG, {"conversation": conv}))

	def test_retry_message_refuses_while_compacting(self):
		conv = _mk_conversation("agent:main:c-ep10")
		frappe.db.set_value(
			CONV, conv, "compacting_since", frappe.utils.now_datetime(), update_modified=False
		)
		user_doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": 1, "role": "user", "content": "hi"}
		)
		user_doc.insert(ignore_permissions=True)
		asst_doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 2,
				"role": "assistant",
				"content": "",
				"error": "rate limit",
			}
		)
		asst_doc.insert(ignore_permissions=True)
		with (
			patch.object(chat_api.admission, "turn_machine_enabled", return_value=True),
			patch("jarvis.chat.api.validate_can_send", return_value=(True, None)),
		):
			out = chat_api.retry_message(asst_doc.name)
		self.assertEqual(out, {"ok": False, "reason": "Compacting this chat, try again in a moment"})


class TestCompactionEvent(FrappeTestCase):
	def test_parse_event_maps_compaction(self):
		self.assertEqual(
			parse_event({"stream": "compaction", "data": {"phase": "start"}}),
			{"kind": "compaction", "phase": "start", "completed": False},
		)
		self.assertEqual(
			parse_event({"stream": "compaction", "data": {"phase": "end", "completed": True}}),
			{"kind": "compaction", "phase": "end", "completed": True},
		)
		self.assertEqual(parse_event({"stream": "compaction", "data": {"phase": "before"}})["phase"], "start")
		self.assertEqual(parse_event({"stream": "compaction", "data": {"phase": "after"}})["phase"], "end")
