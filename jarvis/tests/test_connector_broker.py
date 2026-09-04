"""Unit tests for the frappe-facing parts of ``jarvis.connectors.broker`` that
were hardened in the security pass: recursive argument redaction, fail-CLOSED
egress policy on a read error, and routing the Test-connection probe through the
SAME circuit breaker + concurrency cap as a real call.

Plain ``unittest`` (no bench): ``frappe`` is mocked at the module boundary where
touched, ``mcp_client.fetch_tools`` is mocked so no socket is opened, and the
breaker/cap run against the in-memory ``limits.InMemoryStore``. The pure gate /
SSRF / limits state machines have their own hermetic suites; this file covers
only the broker glue those fixes changed.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from jarvis.connectors import broker, ssrf
from jarvis.connectors.limits import InMemoryStore


class _Row(dict):
	"""Document-like stand-in: attribute access + ``.get``, so ``row.name`` and
	``row.get("base_url")`` both work like a real Jarvis Connector row."""

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc


class TestRecursiveRedaction(unittest.TestCase):
	def test_nested_secret_key_is_redacted(self):
		out = broker._redact_value({"config": {"token": "SECRET-abc", "name": "ok"}, "id": 3})
		self.assertEqual(out["config"]["token"], "***")
		self.assertEqual(out["config"]["name"], "ok")
		self.assertEqual(out["id"], 3)

	def test_secret_inside_a_list_is_redacted(self):
		out = broker._redact_value({"items": [{"password": "p"}, {"ok": 1}]})
		self.assertEqual(out["items"][0]["password"], "***")
		self.assertEqual(out["items"][1]["ok"], 1)

	def test_long_nested_string_is_clipped(self):
		out = broker._redact_value({"a": {"b": "x" * 200}})
		self.assertTrue(out["a"]["b"].endswith("..."))
		self.assertLessEqual(len(out["a"]["b"]), 84)

	def test_redact_args_serialized_summary_hides_nested_token(self):
		fake = mock.MagicMock()
		fake.as_json = lambda obj: json.dumps(obj)
		with mock.patch.object(broker, "frappe", fake):
			summary = broker._redact_args({"config": {"token": "SECRET-abc"}})
		self.assertIn("***", summary)
		self.assertNotIn("SECRET-abc", summary)


class TestEgressFailClosed(unittest.TestCase):
	def test_read_error_denies(self):
		fake = mock.MagicMock()
		fake.db.get_single_value.side_effect = RuntimeError("db down")
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			self.assertFalse(broker._egress_allowed("api.example.com"))

	def test_hook_error_denies(self):
		def _boom(_host):
			raise RuntimeError("hook broke")

		fake = mock.MagicMock()
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", _boom):
			self.assertFalse(broker._egress_allowed("api.example.com"))

	def test_empty_policy_still_allows(self):
		fake = mock.MagicMock()
		fake.db.get_single_value.return_value = None  # unset -> allow-all (design default)
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			self.assertTrue(broker._egress_allowed("api.example.com"))


class TestGuardedTestProbe(unittest.TestCase):
	"""``broker.test_connector`` must run the outbound probe inside the same
	breaker + concurrency cap as a real call, so the Test button cannot bypass the
	worker protection."""

	def _row(self, **kw):
		row = {"name": "conn-1", "base_url": "https://api.example.com/mcp"}
		row.update(kw)
		return _Row(row)

	def test_success_returns_tools(self):
		store = InMemoryStore()
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "fetch_tools", return_value=[{"name": "t"}]),
		):
			out = broker.test_connector(self._row())
		self.assertTrue(out["ok"])
		self.assertEqual(out["tools"], [{"name": "t"}])

	def test_repeated_transport_failures_open_the_circuit(self):
		store = InMemoryStore()
		boom = ssrf.SsrfError("unreachable", kind=ssrf.ERR_CONNECT_FAILED)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "fetch_tools", side_effect=boom),
		):
			row = self._row()
			for _ in range(broker.CB_THRESHOLD):
				out = broker.test_connector(row)
				self.assertEqual(out["error"]["code"], "transport_error")
			# The next probe is fast-failed by the now-open breaker, without a call.
			with mock.patch.object(broker.mcp_client, "fetch_tools") as fetch:
				blocked = broker.test_connector(row)
			fetch.assert_not_called()
		self.assertEqual(blocked["error"]["code"], "circuit_open")

	def test_ssrf_block_does_not_open_the_circuit(self):
		# A policy block (blocked address) is not endpoint health; it must not count
		# toward the breaker.
		store = InMemoryStore()
		boom = ssrf.SsrfError("blocked", kind=ssrf.ERR_BLOCKED_ADDRESS)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "fetch_tools", side_effect=boom),
		):
			row = self._row()
			for _ in range(broker.CB_THRESHOLD + 2):
				out = broker.test_connector(row)
				self.assertEqual(out["error"]["code"], "ssrf_blocked")

	def test_at_capacity_when_cap_full(self):
		store = InMemoryStore()
		# Pre-fill the per-connector concurrency counter to its limit so the probe's
		# own slot acquisition trips the cap.
		store.set(f"cc:{'conn-1'}", broker.CC_LIMIT, 60)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "fetch_tools", return_value=[{"name": "t"}]) as fetch,
		):
			out = broker.test_connector(self._row())
		fetch.assert_not_called()
		self.assertEqual(out["error"]["code"], "at_capacity")


if __name__ == "__main__":
	unittest.main()
