"""Tests for jarvis.chat.feedback_outbox: feedback is stored before it is
acknowledged, delivered after the request, retried with backoff, delivered once,
and bounded (audit F17, admin-v2#715).

Every admin call is mocked, and the after-commit enqueue is patched out by the
shared fixture: the fixtures commit, and a real enqueue would put a delivery job
on this site's queue. ``deliver`` and ``retry_due`` are called directly instead.
"""

import json
from datetime import timedelta
from unittest.mock import patch

import frappe

from jarvis.chat import feedback_outbox as outbox
from jarvis.chat.feature_usage import FEATURES
from jarvis.chat.feedback import (
	_pulse_period,
	submit_feedback,
	submit_pulse_feedback,
	submit_session_feedback,
)
from jarvis.exceptions import (
	AdminContractError,
	AdminRateLimitedError,
	AdminUnreachableError,
)
from jarvis.tests.test_chat_feedback import TEST_USER, _add_msg, _FeedbackTestCase

OUTBOX = "Jarvis Feedback Outbox"
_PUSH = "jarvis.admin_client.push_chat_feedback"
#: The real function, captured before the fixture patches it out.
_REAL_ENQUEUE = outbox._enqueue_delivery
#: Free text longer than every per-field cap (1000), so the callers clip it.
_TAMIL = "நன்றி தமிழ் " * 200
_HINDI = "धन्यवाद हिन्दी " * 200
_EMOJI = "😀🙏" * 600
#: Characters JSON has to escape even with ensure_ascii=False: a quote, a
#: backslash and a control character, worth up to six encoded characters each.
_ESCAPED = '"\\\x01' * 400


def _now():
	return frappe.utils.now_datetime()


class _OutboxTestCase(_FeedbackTestCase):
	def setUp(self):
		super().setUp()
		configured = patch("jarvis.chat.feedback_outbox._admin_configured", return_value=True)
		configured.start()
		self.addCleanup(configured.stop)
		# The fixtures commit, so a real Error Log row would outlive the test.
		log_error = patch("frappe.log_error")
		self.log_error = log_error.start()
		self.addCleanup(log_error.stop)

	def _row(self, name=None):
		return frappe.db.get_value(
			OUTBOX,
			name or self._name(),
			[
				"name",
				"status",
				"attempts",
				"revision",
				"next_retry_at",
				"delivered_at",
				"last_error",
				"payload",
			],
			as_dict=True,
		)

	def _name(self, msg=None):
		return outbox.outbox_name("Reply", msg or self.msg, TEST_USER)

	def _make_due(self, name=None):
		frappe.db.set_value(
			OUTBOX,
			name or self._name(),
			"next_retry_at",
			_now() - timedelta(seconds=1),
			update_modified=False,
		)

	def _assert_near(self, value, expected, slack_s=60):
		self.assertIsNotNone(value)
		self.assertLess(abs((value - expected).total_seconds()), slack_s, f"{value} is not near {expected}")


class TestAccept(_OutboxTestCase):
	def test_an_admin_outage_does_not_lose_an_acknowledged_rating(self):
		with patch(_PUSH, side_effect=AdminUnreachableError("admin is unreachable")):
			res = submit_feedback(message_id=self.msg, rating="down", note="wrong total")
		self.assertTrue(res["ok"])
		stored = frappe.db.table_exists(OUTBOX) and frappe.db.exists(OUTBOX, {"ref": self.msg})
		self.assertTrue(stored, "the rating was acknowledged with ok:true but nothing was stored")
		row = self._row()
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.attempts, 0)
		self.assertEqual(json.loads(row.payload)["note"], "wrong total")
		self.assertFalse(res["delivered"], "a stored rating is not reported as delivered")

	def test_a_new_answer_replaces_the_row_and_restarts_it(self):
		submit_feedback(message_id=self.msg, rating="up")
		frappe.db.set_value(
			OUTBOX,
			self._name(),
			{"status": "Failed", "attempts": 7, "last_error": "x", "next_retry_at": None},
		)
		submit_feedback(message_id=self.msg, rating="down", note="changed my mind")
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		row = self._row()
		self.assertEqual((row.status, row.attempts, row.revision, row.last_error), ("Pending", 0, 2, ""))
		self.assertEqual(json.loads(row.payload)["note"], "changed my mind")
		self._assert_near(row.next_retry_at, _now() + timedelta(seconds=outbox.FIRST_JOB_GRACE_SECONDS))

	def test_the_payload_is_exactly_the_admin_item(self):
		# Privacy bound: nothing beyond what admin's ingest takes, never the reply text.
		submit_feedback(message_id=self.msg, rating="down", note="n" * 5000)
		item = self._item()
		self.assertEqual(
			set(item),
			{
				"rating",
				"message_ref",
				"conversation_ref",
				"session_id",
				"model",
				"user_ref",
				"reply_duration_ms",
				"note",
			},
		)
		self.assertEqual(len(item["note"]), 1000)
		self.assertNotIn("answer", json.dumps(item))  # the fixture reply's content

	def test_the_name_is_the_identity(self):
		self.assertEqual(outbox.outbox_name("Reply", "m1", "a@x"), outbox.outbox_name("Reply", "m1", "a@x"))
		self.assertNotEqual(
			outbox.outbox_name("Reply", "m1", "a@x"), outbox.outbox_name("Session", "m1", "a@x")
		)
		self.assertNotEqual(
			outbox.outbox_name("Pulse", "M:2026-09", "a@x"), outbox.outbox_name("Pulse", "M:2026-09", "b@x")
		)

	def test_rejects_an_unknown_kind_and_an_oversized_payload(self):
		with self.assertRaises(frappe.ValidationError):
			outbox.accept("Thumbs", self.msg, {})
		with self.assertRaises(frappe.ValidationError):
			outbox.accept("Reply", self.msg, {"note": "x" * (outbox.MAX_PAYLOAD_CHARS + 1)})


class TestEnqueue(_OutboxTestCase):
	def test_delivery_is_queued_after_commit_and_deduplicated_per_row(self):
		with patch("frappe.enqueue") as enqueue:
			_REAL_ENQUEUE("Reply-abc")
		enqueue.assert_called_once_with(
			"jarvis.chat.feedback_outbox.deliver",
			queue="short",
			job_id="jarvis-feedback-outbox::Reply-abc",
			deduplicate=True,
			enqueue_after_commit=True,
			name="Reply-abc",
		)

	def test_a_queue_failure_never_loses_the_stored_answer(self):
		with patch("frappe.enqueue", side_effect=ConnectionError("redis down")):
			_REAL_ENQUEUE("Reply-abc")  # must not raise: the sweep sends the row instead


class TestDelivery(_OutboxTestCase):
	def test_failure_then_retry_then_delivered_once(self):
		submit_feedback(message_id=self.msg, rating="down", note="slow")
		name = self._name()

		with patch(_PUSH, side_effect=AdminUnreachableError("admin is unreachable")) as push:
			outbox.deliver(name)
		push.assert_called_once()
		row = self._row()
		self.assertEqual((row.status, row.attempts), ("Pending", 1))
		self.assertIn("unreachable", row.last_error)
		self._assert_near(row.next_retry_at, _now() + timedelta(minutes=outbox.BACKOFF_MINUTES[0]))

		# Backoff not elapsed: neither a stray job nor the sweep sends it.
		with patch(_PUSH) as push:
			outbox.deliver(name)
			outbox.retry_due()
		push.assert_not_called()

		self._make_due()
		with patch(_PUSH) as push:
			outbox.retry_due()
		push.assert_called_once()
		sent = push.call_args.args[0]
		self.assertEqual((sent["rating"], sent["note"], sent["message_ref"]), ("down", "slow", self.msg))
		row = self._row()
		self.assertEqual((row.status, row.attempts, row.last_error), ("Delivered", 2, ""))
		self.assertIsNone(row.next_retry_at)
		self._assert_near(row.delivered_at, _now())

		# A duplicate job or a later sweep never sends a delivered row again.
		with patch(_PUSH) as push:
			outbox.deliver(name)
			outbox.retry_due()
		push.assert_not_called()

	def test_a_refused_payload_fails_without_retry(self):
		submit_feedback(message_id=self.msg, rating="up")
		refused = AdminContractError(
			"rating must be 'up' or 'down'", code="InvalidChatFeedback", http_status=400
		)
		with patch(_PUSH, side_effect=refused):
			outbox.deliver(self._name())
		row = self._row()
		self.assertEqual((row.status, row.attempts), ("Failed", 1))
		self.assertIsNone(row.next_retry_at)
		with patch(_PUSH) as push:
			outbox.retry_due()
		push.assert_not_called()

	def test_a_tenant_without_a_running_container_is_retried(self):
		submit_feedback(message_id=self.msg, rating="up")
		busy = AdminContractError("no running tenant", code="NoRunningTenant", http_status=409)
		with patch(_PUSH, side_effect=busy):
			outbox.deliver(self._name())
		row = self._row()
		self.assertEqual((row.status, row.attempts), ("Pending", 1))
		self.assertIsNotNone(row.next_retry_at)

	def test_a_rate_limit_defers_without_burning_an_attempt(self):
		submit_feedback(message_id=self.msg, rating="up")
		with patch(_PUSH, side_effect=AdminRateLimitedError("rate_limited", retry_after_seconds=900)):
			outbox.deliver(self._name())
		row = self._row()
		self.assertEqual((row.status, row.attempts), ("Pending", 0))
		self._assert_near(row.next_retry_at, _now() + timedelta(seconds=900))
		# A second job for the row (a re-run, a stray enqueue) honours the deferral too.
		with patch(_PUSH) as push:
			outbox.deliver(self._name())
		push.assert_not_called()

	def test_gives_up_after_max_attempts(self):
		submit_feedback(message_id=self.msg, rating="down", note="private words")
		frappe.db.set_value(OUTBOX, self._name(), "attempts", outbox.MAX_ATTEMPTS - 1)
		self._make_due()
		with patch(_PUSH, side_effect=AdminUnreachableError("admin is unreachable")):
			outbox.retry_due()
		row = self._row()
		self.assertEqual((row.status, row.attempts), ("Failed", outbox.MAX_ATTEMPTS))
		self.assertIsNone(row.next_retry_at)
		# Logged once when it gives up, naming the row but never the answer.
		self.log_error.assert_called_once()
		logged = self.log_error.call_args.kwargs
		self.assertEqual(logged["title"], "Feedback outbox: delivery gave up")
		self.assertIn(self._name(), logged["message"])
		self.assertNotIn("private words", logged["message"])

	def test_a_newer_answer_saved_during_the_push_is_not_marked_delivered(self):
		submit_feedback(message_id=self.msg, rating="up")

		def rerate_meanwhile(_item):
			submit_feedback(message_id=self.msg, rating="down", note="actually wrong")

		with patch(_PUSH, side_effect=rerate_meanwhile):
			outbox.deliver(self._name())
		row = self._row()
		self.assertEqual((row.status, row.revision), ("Pending", 2), "the newer answer still has to go")

		with patch(_PUSH) as push:
			outbox.deliver(self._name())
		self.assertEqual(push.call_args.args[0]["note"], "actually wrong")
		self.assertEqual(self._row().status, "Delivered")

	def test_a_stale_push_that_finishes_last_sends_the_current_revision_again(self):
		# Revision 1's push lands at admin AFTER revision 2 was delivered, so admin
		# may now hold the older answer: the row must go out once more.
		submit_feedback(message_id=self.msg, rating="up")
		stale = frappe._dict(self._row(), kind="Reply")
		submit_feedback(message_id=self.msg, rating="down")
		with patch(_PUSH):
			outbox.deliver(self._name())
		self.assertEqual(self._row().status, "Delivered")
		outbox._record(stale, outbox._Outcome("delivered"))
		row = self._row()
		self.assertEqual((row.status, row.revision), ("Pending", 2))
		self._assert_near(row.next_retry_at, _now())

	def test_a_stale_push_leaves_a_newer_pending_revision_on_its_backoff(self):
		submit_feedback(message_id=self.msg, rating="up")
		stale = frappe._dict(self._row(), kind="Reply")
		submit_feedback(message_id=self.msg, rating="down")
		later = _now() + timedelta(hours=1)
		frappe.db.set_value(OUTBOX, self._name(), {"attempts": 3, "next_retry_at": later})
		outbox._record(stale, outbox._Outcome("delivered"))
		row = self._row()
		self.assertEqual((row.status, row.attempts, row.revision), ("Pending", 3, 2))
		self._assert_near(row.next_retry_at, later, slack_s=2)

	def test_a_stale_push_leaves_a_newer_refused_revision_failed(self):
		submit_feedback(message_id=self.msg, rating="up")
		stale = frappe._dict(self._row(), kind="Reply")
		submit_feedback(message_id=self.msg, rating="down")
		refused = AdminContractError("bad", code="InvalidChatFeedback", http_status=400)
		with patch(_PUSH, side_effect=refused):
			outbox.deliver(self._name())
		self.assertEqual(self._row().status, "Failed")
		self.log_error.reset_mock()
		outbox._record(stale, outbox._Outcome("delivered"))
		row = self._row()
		self.assertEqual(row.status, "Failed")
		self.assertIsNone(row.next_retry_at)
		self.log_error.assert_not_called()

	def test_a_bench_that_is_not_onboarded_sends_nothing(self):
		submit_feedback(message_id=self.msg, rating="up")
		self._make_due()
		with patch("jarvis.chat.feedback_outbox._admin_configured", return_value=False):
			with patch(_PUSH) as push:
				outbox.deliver(self._name())
				outbox.retry_due()
		push.assert_not_called()
		self.assertEqual((self._row().status, self._row().attempts), ("Pending", 0))


class TestKinds(_OutboxTestCase):
	def test_each_kind_goes_to_its_own_admin_endpoint(self):
		session = outbox.accept("Session", self.conv, {"kind": "Session", "session_ref": self.conv})
		pulse = outbox.accept("Pulse", "M:2026-09", {"kind": "Pulse", "period_key": "M:2026-09"})
		with (
			patch("jarvis.admin_client.push_session_feedback") as push_session,
			patch("jarvis.admin_client.push_pulse_feedback") as push_pulse,
			patch(_PUSH) as push_reply,
		):
			outbox.deliver(session)
			outbox.deliver(pulse)
		push_session.assert_called_once_with({"kind": "Session", "session_ref": self.conv})
		push_pulse.assert_called_once_with({"kind": "Pulse", "period_key": "M:2026-09"})
		push_reply.assert_not_called()
		self.assertEqual({self._row(session).status, self._row(pulse).status}, {"Delivered"})


class TestTextAtTheFieldCaps(_OutboxTestCase):
	"""An answer the per-field caps accept is always stored and delivered as
	written, whatever script or escaping it needs."""

	def _deliver(self, name, push_path):
		with patch(push_path) as push:
			outbox.deliver(name)
		push.assert_called_once()
		self.assertEqual(self._row(name).status, "Delivered")
		return push.call_args.args[0]

	def _stored(self, name):
		raw = self._row(name).payload
		return raw, json.loads(raw)

	def test_a_tamil_thumbs_note_at_the_cap(self):
		submit_feedback(message_id=self.msg, rating="down", note=_TAMIL)
		raw, item = self._stored(self._name())
		self.assertEqual(item["note"], _TAMIL.strip()[:1000])
		self.assertEqual(len(item["note"]), 1000)
		self.assertIn("தமிழ்", raw, "stored as written, not as escapes")
		self.assertEqual(self._deliver(self._name(), _PUSH), item)

	def test_an_astral_emoji_thumbs_note_at_the_cap(self):
		submit_feedback(message_id=self.msg, rating="down", note=_EMOJI)
		_raw, item = self._stored(self._name())
		self.assertEqual(item["note"], _EMOJI[:1000])
		self.assertEqual(self._deliver(self._name(), _PUSH), item)

	def test_a_hindi_session_note_at_the_cap(self):
		res = submit_session_feedback(self.conv, "Frustrating", note=_HINDI)
		self.assertEqual(res, {"ok": True, "recorded": True, "delivered": False})
		name = outbox.outbox_name("Session", self.conv, TEST_USER)
		_raw, item = self._stored(name)
		self.assertEqual(item["note"], _HINDI.strip()[:1000])
		self.assertEqual(self._deliver(name, "jarvis.admin_client.push_session_feedback"), item)

	def test_a_pulse_answer_with_every_field_at_its_cap(self):
		for use_case, note in ((_TAMIL, _HINDI), (_EMOJI, _TAMIL), (_ESCAPED, _ESCAPED)):
			with self.subTest(use_case=use_case[:4], note=note[:4]):
				res = submit_pulse_feedback(
					stars=1,
					features_offered=list(FEATURES),
					features_selected=list(FEATURES),
					use_case_text=use_case,
					note=note,
				)
				self.assertEqual(res, {"ok": True, "delivered": False})
				name = outbox.outbox_name("Pulse", _pulse_period()[0], TEST_USER)
				_raw, item = self._stored(name)
				self.assertEqual(item["use_case_text"], use_case.strip()[:1000])
				self.assertEqual(item["note"], note.strip()[:1000])
				self.assertEqual(item["features_offered"], list(FEATURES))
				self.assertEqual(self._deliver(name, "jarvis.admin_client.push_pulse_feedback"), item)

	def test_the_cap_sits_far_above_the_largest_answer_the_field_caps_allow(self):
		# Every free-text field at 1000 characters of the worst escaping case,
		# every ref at admin's 140-char bound, all feature keys twice.
		ref = "r" * 140
		largest = {
			"kind": "Pulse",
			"period_key": "M:2026-09",
			"stars": 5,
			"features_offered": list(FEATURES),
			"features_selected": list(FEATURES),
			"use_case_text": _ESCAPED[:1000],
			"user_ref": ref,
			"note": _ESCAPED[:1000],
		}
		reply = {
			"rating": "down",
			"message_ref": ref,
			"conversation_ref": ref,
			"session_id": ref,
			"model": ref,
			"user_ref": ref,
			"reply_duration_ms": 10**12,
			"note": _ESCAPED[:1000],
		}
		for payload in (largest, reply):
			self.assertLess(outbox._content_chars(payload) * 2, outbox.MAX_PAYLOAD_CHARS)
		# The JSON form of the same answer is far bigger, which is why it is not
		# what the cap counts.
		self.assertGreater(len(json.dumps(largest, ensure_ascii=False)), 4000)


class TestSweep(_OutboxTestCase):
	def _three_due_rows(self):
		names = []
		for seq in (2, 4, 6):
			msg = self.msg if seq == 2 else _add_msg(self.conv, seq=seq)
			submit_feedback(message_id=msg, rating="up")
			names.append(self._name(msg))
			self._make_due(names[-1])
		return names

	def test_stops_at_the_first_transient_failure(self):
		names = self._three_due_rows()
		with patch(_PUSH, side_effect=AdminUnreachableError("admin is unreachable")) as push:
			outbox.retry_due()
		push.assert_called_once()
		attempts = sorted(self._row(n).attempts for n in names)
		self.assertEqual(attempts, [0, 0, 1], "only the first row was tried; the rest keep their place")

	def test_is_bounded_per_run(self):
		names = self._three_due_rows()
		with patch.object(outbox, "SWEEP_BATCH", 2), patch(_PUSH) as push:
			outbox.retry_due()
		self.assertEqual(push.call_count, 2)
		statuses = sorted(self._row(n).status for n in names)
		self.assertEqual(statuses, ["Delivered", "Delivered", "Pending"])

	def test_a_refused_row_does_not_stop_the_rest(self):
		names = self._three_due_rows()
		refused = AdminContractError("bad", code="InvalidChatFeedback", http_status=400)
		with patch(_PUSH, side_effect=[refused, None, None]) as push:
			outbox.retry_due()
		self.assertEqual(push.call_count, 3)
		statuses = sorted(self._row(n).status for n in names)
		self.assertEqual(statuses, ["Delivered", "Delivered", "Failed"])


class TestPrune(_OutboxTestCase):
	def _stored(self, ref, **values):
		name = outbox.accept("Reply", ref, {"rating": "up", "message_ref": ref})
		if values:
			frappe.db.set_value(OUTBOX, name, values, update_modified=False)
		return name

	def test_prune_keeps_the_table_bounded(self):
		old_delivered = self._stored(
			"prune-a",
			status="Delivered",
			delivered_at=_now() - timedelta(days=outbox.DELIVERED_RETENTION_DAYS + 1),
		)
		fresh_delivered = self._stored("prune-b", status="Delivered", delivered_at=_now())
		old_failed = self._stored(
			"prune-c",
			status="Failed",
			modified=_now() - timedelta(days=outbox.UNDELIVERED_RETENTION_DAYS + 1),
		)
		fresh_pending = self._stored("prune-d")
		# Inside the retention window but the oldest row left, so the ceiling takes it.
		oldest_pending = self._stored(
			"prune-e", modified=_now() - timedelta(days=outbox.UNDELIVERED_RETENTION_DAYS - 1)
		)

		# A ceiling one below what survives the two retention rules.
		survivors = frappe.db.count(OUTBOX) - 2
		with patch.object(outbox, "MAX_ROWS", survivors - 1):
			outbox.prune()
		names = (old_delivered, fresh_delivered, old_failed, fresh_pending, oldest_pending)
		self.assertEqual(
			{n: bool(frappe.db.exists(OUTBOX, n)) for n in names},
			{
				old_delivered: False,
				fresh_delivered: True,
				old_failed: False,
				fresh_pending: True,
				oldest_pending: False,
			},
		)
