"""Tests for jarvis.realtime.handlers (Path B chat subscriber).

The module's job is to spawn a gevent greenlet at import time iff
``socketio_backend == "python"`` is set in common_site_config.json. The
greenlet subscribes to ``jarvis:chat:send:<site>`` on Redis pub/sub and
dispatches each message to ``jarvis.chat.turn_handler.handle_chat_send``
in its own greenlet (so many concurrent turns share one realtime process).

Tests cover:
- spawning is gated correctly on the backend config
- maybe_start_chat_subscriber is idempotent (a second call is a no-op)
- the per-message dispatch decodes JSON, calls the shared handler, and
  swallows exceptions instead of taking down the loop
- the channel name format matches dispatch.publish_chat_send
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.realtime import handlers


class _FakeConf:
	"""Minimal stand-in for frappe.conf in tests. The handler only reads
	via ``frappe.conf.get(key)``, so we just need a .get implementation."""

	def __init__(self, mapping):
		self._d = dict(mapping)

	def get(self, key, default=None):
		return self._d.get(key, default)


class TestMaybeStartChatSubscriber(FrappeTestCase):
	"""maybe_start_chat_subscriber is the gate; every other behaviour
	flows from whether it spawns or not."""

	def setUp(self):
		# Each test exercises a fresh start state.
		handlers._SUBSCRIBER_SPAWNED = False

	def test_no_spawn_when_socketio_backend_unset(self):
		# _read_common_config patched: the gate falls back to the real
		# common_site_config.json, which may legitimately have the flag on
		# (it does on the Path B dev bench).
		with (
			patch.object(frappe, "conf", _FakeConf({})),
			patch.object(handlers, "_read_common_config", return_value={}),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertFalse(spawned)
		spawn.assert_not_called()
		self.assertFalse(handlers._SUBSCRIBER_SPAWNED)

	def test_no_spawn_when_socketio_backend_is_node(self):
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "node"})),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertFalse(spawned)
		spawn.assert_not_called()
		self.assertFalse(handlers._SUBSCRIBER_SPAWNED)

	def test_spawn_when_socketio_backend_is_python(self):
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertTrue(spawned)
		# The watchdog is bound to the site resolved at spawn time (the
		# open test-site context here) so greenlets never re-resolve it.
		spawn.assert_called_once_with(handlers._watchdog_loop, frappe.local.site)
		self.assertTrue(handlers._SUBSCRIBER_SPAWNED)

	def test_no_spawn_when_site_unresolvable(self):
		"""socketio_backend=python but neither an open site context nor a
		default_site: log an error and skip the spawn instead of raising -
		a raise at import would take down the whole realtime server."""
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch.object(handlers, "_read_common_config", return_value={}),
			patch.object(frappe.local, "site", None),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertFalse(spawned)
		spawn.assert_not_called()
		self.assertFalse(handlers._SUBSCRIBER_SPAWNED)

	def test_backend_flag_falls_back_to_common_site_config(self):
		"""The realtime process imports this module with no frappe context;
		the gate must still see the flag via common_site_config.json."""
		with (
			patch.object(frappe, "conf", _FakeConf({})),
			patch.object(
				handlers,
				"_read_common_config",
				return_value={"socketio_backend": "python"},
			),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertTrue(spawned)
		spawn.assert_called_once()

	def test_python_backend_match_is_case_insensitive(self):
		"""Operators may type 'Python' or 'PYTHON' in the config; the
		gate normalises before comparing."""
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "PYTHON"})),
			patch("gevent.spawn") as spawn,
		):
			spawned = handlers.maybe_start_chat_subscriber()
		self.assertTrue(spawned)
		spawn.assert_called_once()

	def test_second_call_is_noop(self):
		"""Idempotent: once the watchdog is running, a second call must
		not spawn a duplicate greenlet (which would double-subscribe to
		Redis and cause every chat turn to run twice)."""
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch("gevent.spawn") as spawn,
		):
			first = handlers.maybe_start_chat_subscriber()
			second = handlers.maybe_start_chat_subscriber()
		self.assertTrue(first)
		self.assertFalse(second)
		spawn.assert_called_once()


class TestRunOne(FrappeTestCase):
	"""_run_one is the per-message dispatcher: it opens a per-turn site
	context, decodes the JSON payload, calls handle_chat_send, and
	swallows any exception so one bad turn doesn't kill the loop.

	Tests substitute a null context for _turn_context: the real
	frappe_context would init/destroy the test runner's own connection.
	The test process already has a site context, so frappe.log_error
	behaves exactly as it does inside the real per-turn context."""

	SITE = "site.example.com"

	def _null_ctx(self):
		from contextlib import nullcontext

		return patch.object(handlers, "_turn_context", return_value=nullcontext())

	def test_dispatches_decoded_payload_to_handle_chat_send(self):
		payload = {"conversation_id": "c1", "message_id": "m1", "run_id": "r1"}
		raw = json.dumps(payload).encode("utf-8")
		with self._null_ctx() as ctx, patch("jarvis.chat.turn_handler.handle_chat_send") as mock_handle:
			handlers._run_one(raw, self.SITE)
		mock_handle.assert_called_once_with(payload)
		ctx.assert_called_once_with(self.SITE)

	def test_accepts_str_payload_as_well_as_bytes(self):
		"""Redis usually returns bytes but some configurations decode
		strings; accept both."""
		payload = {"conversation_id": "c2", "message_id": "m2", "run_id": "r2"}
		raw = json.dumps(payload)
		with self._null_ctx(), patch("jarvis.chat.turn_handler.handle_chat_send") as mock_handle:
			handlers._run_one(raw, self.SITE)
		mock_handle.assert_called_once_with(payload)

	def test_swallows_handler_exception(self):
		"""A crashed turn must not propagate out of _run_one - else the
		watchdog would restart the loop and we'd lose pending messages.
		The exception is logged via frappe.log_error instead."""
		payload = {"conversation_id": "c", "message_id": "m", "run_id": "r"}
		raw = json.dumps(payload).encode("utf-8")
		with (
			self._null_ctx(),
			patch("jarvis.chat.turn_handler.handle_chat_send", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as mock_log,
		):
			handlers._run_one(raw, self.SITE)  # must not raise
		mock_log.assert_called_once()

	def test_swallows_invalid_json(self):
		"""A malformed payload (corrupt publish, schema drift) must not
		take down the loop. Log and move on."""
		with (
			self._null_ctx(),
			patch("jarvis.chat.turn_handler.handle_chat_send") as mock_handle,
			patch("frappe.log_error") as mock_log,
		):
			handlers._run_one(b"not-json", self.SITE)  # must not raise
		mock_handle.assert_not_called()
		mock_log.assert_called_once()

	def test_swallows_non_dict_payload(self):
		"""Defensive: a JSON value that isn't an object can't be a turn
		payload. Reject + log."""
		with (
			self._null_ctx(),
			patch("jarvis.chat.turn_handler.handle_chat_send") as mock_handle,
			patch("frappe.log_error") as mock_log,
		):
			handlers._run_one(b'["not", "a", "dict"]', self.SITE)
		mock_handle.assert_not_called()
		mock_log.assert_called_once()

	def test_swallows_context_bootstrap_failure(self):
		"""If the site context itself cannot open (bad site, db down), the
		failure is logged to the module logger and swallowed - the loop
		must survive."""
		with (
			patch.object(handlers, "_turn_context", side_effect=RuntimeError("no such site")),
			patch("jarvis.chat.turn_handler.handle_chat_send") as mock_handle,
		):
			handlers._run_one(b"{}", self.SITE)  # must not raise
		mock_handle.assert_not_called()


class TestDispatch(FrappeTestCase):
	"""Path B publisher semantics - both caught by the Stage A live smoke.

	1. frappe.cache() IS a redis.Redis subclass; publish directly (the
	   previously-assumed .get_redis_connection() accessor does not exist).
	2. _dispatch_turn must publish AFTER the request transaction commits:
	   pub/sub delivery is instant, so a mid-transaction publish lets the
	   subscriber greenlet start the turn before the conversation and
	   user-message rows are visible (LinkValidationError on the
	   placeholder insert)."""

	def test_publish_goes_through_cache_client_directly(self):
		from jarvis.chat import dispatch

		fake_cache = MagicMock()
		payload = {"conversation_id": "c1", "message_id": "m1", "run_id": "r1"}
		with patch.object(frappe, "cache", return_value=fake_cache):
			dispatch.publish_chat_send(payload)
		fake_cache.publish.assert_called_once_with(dispatch._channel(frappe.local.site), json.dumps(payload))

	def test_dispatch_turn_publishes_only_after_commit(self):
		from jarvis.chat import api as chat_api

		payload = {"conversation_id": "c1", "message_id": "m1", "run_id": "r1"}
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch("jarvis.chat.dispatch.subscriber_count", return_value=1),
			patch("jarvis.chat.dispatch.publish_chat_send") as mock_pub,
			patch("frappe.enqueue") as mock_enqueue,
		):
			chat_api._dispatch_turn(payload)
			mock_pub.assert_not_called()  # nothing until the transaction lands
			frappe.db.commit()
		mock_pub.assert_called_once_with(payload)
		mock_enqueue.assert_not_called()

	def test_dispatch_turn_falls_back_to_rq_when_no_subscriber(self):
		"""The mismatch guard: socketio_backend=python but zero live
		subscribers (config/process mismatch - e.g. Frappe Cloud pins
		node - or the realtime process died) must route the turn to RQ
		and log loudly, never publish into the void."""
		from jarvis.chat import api as chat_api

		payload = {"conversation_id": "c2", "message_id": "m2", "run_id": "r2"}
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch("jarvis.chat.dispatch.subscriber_count", return_value=0),
			patch("jarvis.chat.dispatch.publish_chat_send") as mock_pub,
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.log_error") as mock_log,
		):
			chat_api._dispatch_turn(payload)
			frappe.db.commit()  # even after commit: no publish was registered
		mock_pub.assert_not_called()
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.kwargs["method"],
			"jarvis.chat.worker.run_agent_turn",
		)
		mock_log.assert_called_once()

	def test_dispatch_turn_falls_back_to_rq_when_numsub_check_fails(self):
		"""Any doubt (redis hiccup during the NUMSUB probe) also falls
		back to RQ - both dispatch flows are first-class, so RQ is always
		a correct executor."""
		from jarvis.chat import api as chat_api

		payload = {"conversation_id": "c3", "message_id": "m3", "run_id": "r3"}
		with (
			patch.object(frappe, "conf", _FakeConf({"socketio_backend": "python"})),
			patch("jarvis.chat.dispatch.subscriber_count", side_effect=RuntimeError("redis hiccup")),
			patch("jarvis.chat.dispatch.publish_chat_send") as mock_pub,
			patch("frappe.enqueue") as mock_enqueue,
		):
			chat_api._dispatch_turn(payload)
			frappe.db.commit()
		mock_pub.assert_not_called()
		mock_enqueue.assert_called_once()

	def test_subscriber_count_reads_pubsub_numsub(self):
		from jarvis.chat import dispatch

		fake_cache = MagicMock()
		fake_cache.pubsub_numsub.return_value = [(b"jarvis:chat:send:x", 2)]
		with patch.object(frappe, "cache", return_value=fake_cache):
			self.assertEqual(dispatch.subscriber_count(), 2)
		fake_cache.pubsub_numsub.return_value = []
		with patch.object(frappe, "cache", return_value=fake_cache):
			self.assertEqual(dispatch.subscriber_count(), 0)


class TestChannelFormat(FrappeTestCase):
	"""The publisher (jarvis.chat.dispatch) and subscriber (this module)
	must agree on the channel name verbatim, else messages drop on the
	floor with no error. Pin the format here."""

	def test_subscriber_and_publisher_use_same_channel(self):
		from jarvis.chat import dispatch

		self.assertEqual(
			handlers._CHANNEL_TEMPLATE.format(site="site.example.com"),
			dispatch._channel("site.example.com"),
		)
