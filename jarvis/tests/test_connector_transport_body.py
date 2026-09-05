"""The OAuth transport's body read is bounded by the wall-clock deadline even when
the server drips one byte at a time.

``resp.stream(n)`` -> ``read(n)`` loops recvs until ``n`` bytes arrive, so a
per-chunk deadline check never runs mid-read against a byte-dripping server.
``_read_capped`` therefore reads with ``read1`` (one syscall per iteration) and
re-arms the socket timeout to the remaining budget before each read. Hermetic:
a fake response models a one-byte-per-read drip that advances a fake clock.
"""

from __future__ import annotations

import unittest

from jarvis.connectors.mcp_oauth import transport
from jarvis.connectors.mcp_oauth.errors import OAuthTransportError


class _Sock:
	def __init__(self):
		self.timeouts: list[float] = []

	def settimeout(self, value):
		self.timeouts.append(value)


class _Conn:
	def __init__(self):
		self.sock = _Sock()


class _DripResponse:
	"""One byte per ``read1``; each read costs ``step`` seconds of fake time."""

	def __init__(self, total_bytes: int, step: float, now: list[float]):
		self._left = total_bytes
		self._step = step
		self._now = now
		self.connection = _Conn()
		self.closed = False

	def read1(self, amt=-1, decode_content=None):
		if self._left <= 0:
			return b""
		self._left -= 1
		self._now[0] += self._step
		return b"x"

	def stream(self, amt, decode_content=True):  # must NOT be used when read1 exists
		raise AssertionError("stream() used although read1 is available")

	def close(self):
		self.closed = True


class TestBodyDripIsBoundedByTheDeadline(unittest.TestCase):
	def test_a_byte_drip_trips_the_deadline_not_the_byte_count(self):
		now = [0.0]
		resp = _DripResponse(total_bytes=10_000, step=0.5, now=now)  # would take 5000s
		with self.assertRaises(OAuthTransportError) as ctx:
			transport._read_capped(resp, deadline=2.0, clock=lambda: now[0])
		self.assertEqual(ctx.exception.code, "timeout")
		self.assertLessEqual(now[0], 2.5, f"stopped at {now[0]}s against a 2s deadline")

	def test_the_socket_is_rearmed_to_the_remaining_budget_before_each_read(self):
		now = [0.0]
		resp = _DripResponse(total_bytes=3, step=0.5, now=now)
		body = transport._read_capped(resp, deadline=10.0, clock=lambda: now[0])
		self.assertEqual(body, b"xxx")
		timeouts = resp.connection.sock.timeouts
		self.assertEqual(len(timeouts), 4, "one re-arm per read, including the final empty read")
		# Strictly decreasing: each read is armed with what is LEFT, never the
		# original read timeout.
		self.assertEqual(timeouts, [10.0, 9.5, 9.0, 8.5])

	def test_a_fake_without_read1_still_streams(self):
		class _Legacy:
			def __init__(self):
				self.chunks = [b"ab", b"cd"]

			def stream(self, amt, decode_content=True):
				yield from self.chunks

		now = [0.0]
		self.assertEqual(transport._read_capped(_Legacy(), deadline=5.0, clock=lambda: now[0]), b"abcd")


if __name__ == "__main__":
	unittest.main()
