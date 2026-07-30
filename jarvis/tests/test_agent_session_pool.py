"""Tests for jarvis.chat.agent_session_pool.

The pool itself talks only to ``AgentSession.connect`` (which we mock
with a fake session exposing ``_ws.connected`` + ``close()``). No real
WebSocket / Frappe involvement; tests are pure unit.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_session_pool
from jarvis.exceptions import AgentUnreachableError


def _fake_session(connected: bool = True) -> MagicMock:
	"""Build a session stub that satisfies the pool's healthcheck."""
	sess = MagicMock()
	sess._ws = MagicMock()
	sess._ws.connected = connected
	return sess


class TestAgentSessionPool(FrappeTestCase):
	"""Pool unit tests. Each test resets the module-level pool dict in
	``setUp`` so suite order doesn't matter."""

	def setUp(self):
		agent_session_pool._POOL.clear()

	def tearDown(self):
		agent_session_pool._POOL.clear()

	# ---- happy path -------------------------------------------------

	def test_checkout_creates_new_session(self):
		sess = _fake_session()
		with patch.object(agent_session_pool.AgentSession, "connect", return_value=sess) as mock_connect:
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess)
			mock_connect.assert_called_once_with("ws://gw")

	def test_checkout_reuses_pooled_session(self):
		sess = _fake_session()
		with patch.object(agent_session_pool.AgentSession, "connect", return_value=sess) as mock_connect:
			with agent_session_pool.checkout("ws://gw"):
				pass
			with agent_session_pool.checkout("ws://gw"):
				pass
			# One connect total: second checkout reused the pool entry.
			mock_connect.assert_called_once()

	def test_different_urls_get_separate_entries(self):
		sess_a = _fake_session()
		sess_b = _fake_session()
		with patch.object(
			agent_session_pool.AgentSession, "connect", side_effect=[sess_a, sess_b]
		) as mock_connect:
			with agent_session_pool.checkout("ws://a"):
				pass
			with agent_session_pool.checkout("ws://b"):
				pass
			self.assertEqual(mock_connect.call_count, 2)
			self.assertEqual(set(agent_session_pool._POOL), {"ws://a", "ws://b"})

	# ---- healthcheck + reconnect ------------------------------------

	def test_dead_ws_triggers_reconnect(self):
		"""When ``_ws.connected`` is False on second checkout, the pool
		discards the entry and opens a fresh session."""
		sess1 = _fake_session()
		sess2 = _fake_session()
		with patch.object(
			agent_session_pool.AgentSession, "connect", side_effect=[sess1, sess2]
		) as mock_connect:
			with agent_session_pool.checkout("ws://gw"):
				pass
			# Simulate gateway closing our WS.
			sess1._ws.connected = False
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess2)
			self.assertEqual(mock_connect.call_count, 2)
			# First session's close() called during eviction.
			sess1.close.assert_called_once()

	def test_idle_eviction(self):
		"""After ``IDLE_MAX_S`` seconds the next checkout reconnects."""
		sess1 = _fake_session()
		sess2 = _fake_session()
		with (
			patch.object(
				agent_session_pool.AgentSession, "connect", side_effect=[sess1, sess2]
			) as mock_connect,
			patch.object(agent_session_pool.time, "monotonic") as mock_time,
		):
			# T=1000: first checkout creates session.
			mock_time.return_value = 1000.0
			with agent_session_pool.checkout("ws://gw"):
				pass
			# T=1000 + IDLE_MAX_S + 1: stale, reconnect.
			mock_time.return_value = 1000.0 + agent_session_pool.IDLE_MAX_S + 1
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess2)
			self.assertEqual(mock_connect.call_count, 2)
			sess1.close.assert_called_once()

	# ---- error eviction ---------------------------------------------

	def test_unreachable_during_use_evicts(self):
		"""``AgentUnreachableError`` raised inside the ``with`` block
		evicts the entry from the pool so the next checkout reconnects."""
		sess1 = _fake_session()
		sess2 = _fake_session()
		with patch.object(
			agent_session_pool.AgentSession, "connect", side_effect=[sess1, sess2]
		) as mock_connect:
			with self.assertRaises(AgentUnreachableError):
				with agent_session_pool.checkout("ws://gw"):
					raise AgentUnreachableError("simulated stream failure")
			# Entry evicted; next checkout opens a fresh session.
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess2)
			self.assertEqual(mock_connect.call_count, 2)
			# Evicted session's close() was called.
			sess1.close.assert_called()

	def test_other_exception_does_not_evict(self):
		"""A non-Agent exception inside the block should NOT evict
		the pool — those are caller bugs, the connection is fine."""
		sess = _fake_session()
		with patch.object(agent_session_pool.AgentSession, "connect", return_value=sess) as mock_connect:
			with self.assertRaises(ValueError):
				with agent_session_pool.checkout("ws://gw"):
					raise ValueError("caller bug")
			# Next checkout reuses the same session — no eviction.
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess)
			mock_connect.assert_called_once()

	def test_close_failure_during_eviction_does_not_propagate(self):
		"""If ``sess.close()`` itself raises during eviction (e.g. socket
		already torn down), the pool swallows it. The eviction still
		removes the entry."""
		sess1 = _fake_session()
		sess1.close.side_effect = RuntimeError("already closed")
		sess2 = _fake_session()
		with patch.object(agent_session_pool.AgentSession, "connect", side_effect=[sess1, sess2]):
			with self.assertRaises(AgentUnreachableError):
				with agent_session_pool.checkout("ws://gw"):
					raise AgentUnreachableError("boom")
			# Entry should have been removed even though close raised.
			# (The gateway GROUP object stays in the dict; what matters
			# is that no dead entry remains inside it.)
			group = agent_session_pool._POOL["ws://gw"]
			self.assertEqual(len(group.entries), 0)

	# ---- concurrent checkout (Stage B: N entries per gateway) --------

	def _held_checkout(self, url, acquired_evt, release_evt, out, errors):
		"""Thread body: hold a checkout open until release_evt fires."""
		try:
			with agent_session_pool.checkout(url) as got:
				out.append(got)
				acquired_evt.set()
				release_evt.wait(timeout=5)
		except Exception as e:  # pragma: no cover - surfaced via assertion
			errors.append(e)
			acquired_evt.set()

	def test_concurrent_checkouts_get_distinct_sessions(self):
		"""Two checkouts held at the same time get two DIFFERENT pooled
		sessions (per-entry exclusivity kept, per-gateway exclusivity
		gone - the Stage B point)."""
		sessions = [_fake_session(), _fake_session(), _fake_session()]
		with patch.object(agent_session_pool.AgentSession, "connect", side_effect=sessions) as mock_connect:
			out, errors = [], []
			acq = [threading.Event() for _ in range(2)]
			rel = threading.Event()
			threads = [
				threading.Thread(
					target=self._held_checkout,
					args=("ws://gw", acq[i], rel, out, errors),
				)
				for i in range(2)
			]
			for t in threads:
				t.start()
			for e in acq:
				self.assertTrue(e.wait(timeout=5))
			# Both hold a session simultaneously, and they are distinct.
			self.assertEqual(len(out), 2)
			self.assertIsNot(out[0], out[1])
			self.assertEqual(mock_connect.call_count, 2)
			rel.set()
			for t in threads:
				t.join(timeout=5)
			self.assertEqual(errors, [])

	def test_cap_blocks_fourth_concurrent_checkout(self):
		"""With POOL_MAX_PER_GATEWAY sessions held, the next checkout
		waits; releasing one slot unblocks it and it reuses that entry
		(no fourth connect)."""
		cap = agent_session_pool.POOL_MAX_PER_GATEWAY
		sessions = [_fake_session() for _ in range(cap + 1)]
		with patch.object(agent_session_pool.AgentSession, "connect", side_effect=sessions) as mock_connect:
			out, errors = [], []
			acq = [threading.Event() for _ in range(cap)]
			rels = [threading.Event() for _ in range(cap)]
			holders = [
				threading.Thread(
					target=self._held_checkout,
					args=("ws://gw", acq[i], rels[i], out, errors),
				)
				for i in range(cap)
			]
			for t in holders:
				t.start()
			for e in acq:
				self.assertTrue(e.wait(timeout=5))
			self.assertEqual(mock_connect.call_count, cap)

			# Fourth checkout: must block while all slots are busy.
			acq4, rel4 = threading.Event(), threading.Event()
			fourth = threading.Thread(
				target=self._held_checkout,
				args=("ws://gw", acq4, rel4, out, errors),
			)
			fourth.start()
			self.assertFalse(acq4.wait(timeout=0.3))  # still blocked

			rels[0].set()  # free one slot
			self.assertTrue(acq4.wait(timeout=5))  # now acquired
			# Reused a pooled entry - no additional connect.
			self.assertEqual(mock_connect.call_count, cap)
			rel4.set()
			for e in rels[1:]:
				e.set()
			for t in [*holders, fourth]:
				t.join(timeout=5)
			self.assertEqual(errors, [])

	def test_eviction_of_one_entry_does_not_affect_others(self):
		"""An AgentUnreachableError on one held session evicts ONLY
		that entry; the concurrently-held sibling survives in the pool
		and is reused by the next checkout."""
		sess_a, sess_b = _fake_session(), _fake_session()
		with patch.object(
			agent_session_pool.AgentSession, "connect", side_effect=[sess_a, sess_b]
		) as mock_connect:
			out, errors = [], []
			acq_b, rel_b = threading.Event(), threading.Event()

			def failing_holder():
				try:
					with agent_session_pool.checkout("ws://gw"):
						# Hold until B has its own session, then die.
						self.assertTrue(acq_b.wait(timeout=5))
						raise AgentUnreachableError("stream died")
				except AgentUnreachableError:
					pass

			holder_b = threading.Thread(
				target=self._held_checkout,
				args=("ws://gw", acq_b, rel_b, out, errors),
			)
			holder_a = threading.Thread(target=failing_holder)
			holder_a.start()
			holder_b.start()
			holder_a.join(timeout=5)
			rel_b.set()
			holder_b.join(timeout=5)
			self.assertEqual(errors, [])

			group = agent_session_pool._POOL["ws://gw"]
			self.assertEqual(len(group.entries), 1)  # one evicted, one kept
			# Next checkout reuses the survivor - no third connect.
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIn(got, (sess_a, sess_b))
			self.assertEqual(mock_connect.call_count, 2)

	def test_connect_failure_frees_reserved_slot(self):
		"""A failed connect must release its reserved slot so the pool
		does not leak capacity (the 'connecting' counter)."""
		sess = _fake_session()
		with patch.object(
			agent_session_pool.AgentSession,
			"connect",
			side_effect=[AgentUnreachableError("refused"), sess],
		) as mock_connect:
			with self.assertRaises(AgentUnreachableError):
				with agent_session_pool.checkout("ws://gw"):
					pass  # pragma: no cover - never reached
			group = agent_session_pool._POOL["ws://gw"]
			self.assertEqual(group.connecting, 0)
			# Retry succeeds and occupies a normal slot.
			with agent_session_pool.checkout("ws://gw") as got:
				self.assertIs(got, sess)
			self.assertEqual(mock_connect.call_count, 2)

	# ---- drain ------------------------------------------------------

	def test_drain_all_closes_every_pooled_session(self):
		sess_a = _fake_session()
		sess_b = _fake_session()
		with patch.object(agent_session_pool.AgentSession, "connect", side_effect=[sess_a, sess_b]):
			with agent_session_pool.checkout("ws://a"):
				pass
			with agent_session_pool.checkout("ws://b"):
				pass
		agent_session_pool.drain_all()
		sess_a.close.assert_called_once()
		sess_b.close.assert_called_once()
		self.assertEqual(len(agent_session_pool._POOL), 0)

	def test_drain_all_is_idempotent(self):
		"""Calling drain_all on an empty pool should be a no-op, not
		raise."""
		agent_session_pool.drain_all()
		agent_session_pool.drain_all()
