"""The SSRF client's ``deadline`` is ONE wall clock across DNS, connect and the
header wait - not a value computed once per hop and then spent three times over.

Hermetic: no network, no frappe. DNS and the pinned open are replaced by fakes
that stall for exactly the timeout they are handed and advance a fake clock, so
the assertions are about the arithmetic the real code does, not about sockets.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from jarvis.connectors import ssrf

URL = "https://mcp.example.test/mcp"


class _Resp:
	def __init__(self):
		self.status = 200
		self.headers = {}
		self.closed = False

	def close(self):
		self.closed = True


class _Pool:
	def __init__(self):
		self.closed = False

	def close(self):
		self.closed = True


class TestDeadlineSpansEveryPhase(unittest.TestCase):
	def _run(self, budget: float, dns_stall_to_cap: bool = True):
		now = [0.0]
		seen: dict = {}

		def fake_validate(url, egress_allowed, resolve_timeout):
			seen["resolve_timeout"] = resolve_timeout
			if dns_stall_to_cap:
				now[0] += resolve_timeout  # a resolver that only gives up at its cap
			return urlparse(url), "93.184.216.34"

		def fake_open(
			parsed, ip, connect_timeout, read_timeout, *, method, body, extra_headers, total_timeout=None
		):
			seen["connect_timeout"] = connect_timeout
			seen["read_timeout"] = read_timeout
			seen["total_timeout"] = total_timeout
			# A real urllib3 pool stops at ``total``; model the worst case: it uses
			# every second it was allowed.
			now[0] += total_timeout if total_timeout is not None else (connect_timeout + read_timeout)
			return _Resp(), _Pool()

		with (
			patch.object(ssrf, "_validate_url", fake_validate),
			patch.object(ssrf, "_open_pinned", fake_open),
		):
			ssrf.open_pinned_request(
				URL,
				method="POST",
				connect_timeout=5.0,
				read_timeout=8.0,
				deadline=budget,
				clock=lambda: now[0],
			)
		return now[0], seen

	def test_dns_connect_and_header_wait_share_one_budget(self):
		# Before the fix: resolve 5 + connect 5 + read 8 = 18s against a 10s budget.
		elapsed, seen = self._run(budget=10.0)
		self.assertLessEqual(elapsed, 10.0, f"hop took {elapsed}s against a 10s deadline")
		self.assertEqual(seen["resolve_timeout"], 5.0)
		# DNS spent 5s; what reaches the pinned open must be clamped to the 5s left.
		self.assertIsNotNone(seen["total_timeout"])
		self.assertLessEqual(seen["total_timeout"], 5.0)
		self.assertLessEqual(seen["connect_timeout"], 5.0)
		self.assertLessEqual(seen["read_timeout"], 5.0)

	def test_a_resolver_that_eats_the_whole_budget_never_opens_a_socket(self):
		now = [0.0]
		opened = []

		def fake_validate(url, egress_allowed, resolve_timeout):
			now[0] += 10.0  # DNS blew the entire budget
			return urlparse(url), "93.184.216.34"

		def fake_open(*a, **k):
			opened.append(1)
			return _Resp(), _Pool()

		with (
			patch.object(ssrf, "_validate_url", fake_validate),
			patch.object(ssrf, "_open_pinned", fake_open),
			self.assertRaises(ssrf.SsrfError) as ctx,
		):
			ssrf.open_pinned_request(
				URL, connect_timeout=5.0, read_timeout=8.0, deadline=10.0, clock=lambda: now[0]
			)
		self.assertEqual(ctx.exception.kind, ssrf.ERR_CONNECT_FAILED)
		self.assertEqual(opened, [], "no connection is attempted once the budget is gone")

	def test_without_a_deadline_nothing_is_clamped(self):
		# Callers that pass no deadline (the pre-existing contract) get the plain
		# per-phase timeouts and no ``total``.
		now = [0.0]
		seen: dict = {}

		def fake_validate(url, egress_allowed, resolve_timeout):
			seen["resolve_timeout"] = resolve_timeout
			return urlparse(url), "93.184.216.34"

		def fake_open(
			parsed, ip, connect_timeout, read_timeout, *, method, body, extra_headers, total_timeout=None
		):
			seen.update(
				connect_timeout=connect_timeout, read_timeout=read_timeout, total_timeout=total_timeout
			)
			return _Resp(), _Pool()

		with (
			patch.object(ssrf, "_validate_url", fake_validate),
			patch.object(ssrf, "_open_pinned", fake_open),
		):
			ssrf.open_pinned_request(URL, connect_timeout=5.0, read_timeout=8.0, clock=lambda: now[0])
		self.assertEqual(
			seen, {"resolve_timeout": 5.0, "connect_timeout": 5.0, "read_timeout": 8.0, "total_timeout": None}
		)


if __name__ == "__main__":
	unittest.main()
