"""Unit tests for ``jarvis.connectors.ssrf`` - the SSRF-guarded, IP-pinned seam
the MCP connector client uses.

These use a plain ``unittest.TestCase`` (NOT ``FrappeTestCase``) on purpose: the
module under test imports no frappe, so the whole file runs under a bare
``python -m unittest jarvis.tests.test_connector_ssrf`` with no bench, DB or site
- which is exactly how the SSRF logic wants to be exercised (fast, hermetic, no
live network). ``socket.getaddrinfo`` and the pinned-open seam are mocked in
every test that would otherwise touch the wire.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from jarvis.connectors import ssrf

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"
PRIVATE_IP = "10.0.0.5"
LOOPBACK_IP = "127.0.0.1"
LINK_LOCAL_IP = "169.254.1.1"
METADATA_IP = "169.254.169.254"


def _addrinfo(ip: str):
	return [(2, 1, 6, "", (ip, 443))]


class _FakeResp:
	def __init__(self, status=200, headers=None, chunks=None):
		self.status = status
		self.headers = headers or {}
		self._chunks = chunks or []
		self.closed = False

	def stream(self, amt=8192, decode_content=True):
		yield from self._chunks

	def close(self):
		self.closed = True


def _open(resp):
	return (resp, mock.MagicMock())


class TestBlockedIp(unittest.TestCase):
	def test_private_loopback_linklocal_metadata_are_blocked(self):
		for ip in (PRIVATE_IP, LOOPBACK_IP, LINK_LOCAL_IP, METADATA_IP, "0.0.0.0", "224.0.0.1", "::1"):
			self.assertTrue(ssrf._is_blocked_ip(ip), ip)

	def test_public_ip_is_allowed(self):
		self.assertFalse(ssrf._is_blocked_ip(PUBLIC_IP))

	def test_unparsable_address_fails_closed(self):
		self.assertTrue(ssrf._is_blocked_ip("not-an-ip"))

	def test_parity_with_link_fetch_guard(self):
		# The connector guard is a deliberate copy of link_fetch's; if the two
		# ever disagree on a classification that is drift worth catching. Imported
		# lazily because link_fetch drags in bs4, which need not be present for the
		# hermetic run; the parity check simply skips where it is unavailable.
		try:
			from jarvis.chat import link_fetch
		except Exception as exc:  # pragma: no cover - environment-dependent
			self.skipTest(f"link_fetch not importable here ({exc})")
		for ip in (PUBLIC_IP, PRIVATE_IP, LOOPBACK_IP, LINK_LOCAL_IP, METADATA_IP, "8.8.8.8", "172.16.0.1"):
			self.assertEqual(ssrf._is_blocked_ip(ip), link_fetch._is_blocked_ip(ip), ip)


class TestValidateUrl(unittest.TestCase):
	def test_rejects_non_http_scheme(self):
		with self.assertRaises(ssrf.SsrfError) as cm:
			ssrf._validate_url("file:///etc/passwd", None)
		self.assertEqual(cm.exception.kind, ssrf.ERR_INVALID_URL)

	def test_rejects_embedded_credentials(self):
		with self.assertRaises(ssrf.SsrfError) as cm:
			ssrf._validate_url("https://user:secret@example.com/mcp", None)
		self.assertEqual(cm.exception.kind, ssrf.ERR_INVALID_URL)

	def test_dns_rebind_to_private_is_blocked(self):
		# A public-looking host that resolves to a private address (the classic
		# DNS-rebind shape) is refused.
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PRIVATE_IP)):
			with self.assertRaises(ssrf.SsrfError) as cm:
				ssrf._validate_url("https://evil.example.com/mcp", None)
		self.assertEqual(cm.exception.kind, ssrf.ERR_BLOCKED_ADDRESS)

	def test_metadata_host_is_blocked(self):
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(METADATA_IP)):
			with self.assertRaises(ssrf.SsrfError) as cm:
				ssrf._validate_url("https://metadata.example.com/mcp", None)
		self.assertEqual(cm.exception.kind, ssrf.ERR_BLOCKED_ADDRESS)

	def test_egress_hook_deny_short_circuits(self):
		called = {"resolved": False}

		def _no_resolve(*a, **k):
			called["resolved"] = True
			return _addrinfo(PUBLIC_IP)

		with mock.patch.object(ssrf.socket, "getaddrinfo", _no_resolve):
			with self.assertRaises(ssrf.SsrfError) as cm:
				ssrf._validate_url("https://blocked.example.com/mcp", lambda host: False)
		self.assertEqual(cm.exception.kind, ssrf.ERR_EGRESS_DENIED)
		self.assertFalse(called["resolved"], "egress deny should short-circuit before DNS")

	def test_egress_hook_allow_passes(self):
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			parsed, ip = ssrf._validate_url("https://ok.example.com/mcp", lambda host: True)
		self.assertEqual(ip, PUBLIC_IP)


class TestOpenPinnedRequest(unittest.TestCase):
	def test_plain_200_is_returned(self):
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(ssrf, "_open_pinned", return_value=_open(_FakeResp(200))):
				resp, pool, final = ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(resp.status, 200)
		self.assertEqual(final, "https://example.com/mcp")

	def test_301_is_not_followed_and_returned_as_is(self):
		# Only 307/308 are followed; a 301/302/303 on a POST is handed back, not
		# silently downgraded to a GET.
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(
				ssrf,
				"_open_pinned",
				return_value=_open(_FakeResp(301, {"Location": "https://example.com/x"})),
			):
				resp, pool, final = ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(resp.status, 301)

	def test_307_same_host_is_followed_and_repinned(self):
		responses = [
			_open(_FakeResp(307, {"Location": "https://example.com/moved"})),
			_open(_FakeResp(200)),
		]
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)) as gai:
			with mock.patch.object(ssrf, "_open_pinned", side_effect=responses):
				resp, pool, final = ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(resp.status, 200)
		self.assertEqual(final, "https://example.com/moved")
		# Re-validated on the second hop too (host resolved again).
		self.assertGreaterEqual(gai.call_count, 2)

	def test_307_https_to_http_downgrade_refused(self):
		# A same-host redirect that downgrades https -> http would re-POST the
		# Authorization: Bearer header in cleartext. It must be refused.
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(
				ssrf,
				"_open_pinned",
				return_value=_open(_FakeResp(307, {"Location": "http://example.com/x"})),
			):
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(cm.exception.kind, ssrf.ERR_INSECURE_REDIRECT)

	def test_307_http_to_https_upgrade_is_followed(self):
		# The opposite direction (an UPGRADE) is safe and must still be followed.
		responses = [
			_open(_FakeResp(307, {"Location": "https://example.com/secure"})),
			_open(_FakeResp(200)),
		]
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(ssrf, "_open_pinned", side_effect=responses):
				resp, pool, final = ssrf.open_pinned_request("http://example.com/mcp", body=b"{}")
		self.assertEqual(resp.status, 200)
		self.assertEqual(final, "https://example.com/secure")

	def test_307_cross_host_is_refused(self):
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(
				ssrf,
				"_open_pinned",
				return_value=_open(_FakeResp(307, {"Location": "https://other.example.net/x"})),
			):
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(cm.exception.kind, ssrf.ERR_CROSS_HOST_REDIRECT)

	def test_redirect_target_to_private_is_blocked(self):
		# First hop resolves public; the redirect target resolves private -> the
		# re-validation on the second hop must catch it.
		gai = mock.Mock(side_effect=[_addrinfo(PUBLIC_IP), _addrinfo(PRIVATE_IP)])
		with mock.patch.object(ssrf.socket, "getaddrinfo", gai):
			with mock.patch.object(
				ssrf,
				"_open_pinned",
				return_value=_open(_FakeResp(307, {"Location": "https://example.com/internal"})),
			):
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(cm.exception.kind, ssrf.ERR_BLOCKED_ADDRESS)

	def test_too_many_redirects(self):
		loop = _open(_FakeResp(308, {"Location": "https://example.com/again"}))
		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(ssrf, "_open_pinned", return_value=loop):
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(cm.exception.kind, ssrf.ERR_TOO_MANY_REDIRECTS)

	def test_connect_failure_is_classified(self):
		import urllib3

		with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)):
			with mock.patch.object(ssrf, "_open_pinned", side_effect=urllib3.exceptions.HTTPError("boom")):
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request("https://example.com/mcp", body=b"{}")
		self.assertEqual(cm.exception.kind, ssrf.ERR_CONNECT_FAILED)


class TestConnectionPinning(unittest.TestCase):
	"""The socket must connect to the vetted IP while TLS verifies the ORIGINAL
	hostname. This is exactly the class of bug a wholesale-mocked seam hides (see
	link_fetch's ``_build_pool`` docstring), so assert on the pool construction
	directly."""

	def test_https_pool_pins_ip_and_verifies_hostname(self):
		with mock.patch.object(ssrf.urllib3, "HTTPSConnectionPool") as pool_cls:
			with mock.patch.object(ssrf.certifi, "where", return_value="/ca.pem"):
				ssrf._build_pool("https", PUBLIC_IP, 443, "example.com", 5.0, 20.0)
		_, kwargs = pool_cls.call_args
		self.assertEqual(kwargs["host"], PUBLIC_IP)
		self.assertEqual(kwargs["server_hostname"], "example.com")
		self.assertEqual(kwargs["assert_hostname"], "example.com")
		self.assertEqual(kwargs["cert_reqs"], "CERT_REQUIRED")

	def test_open_pinned_sends_original_host_header_and_no_redirect(self):
		from urllib.parse import urlparse

		pool = mock.MagicMock()
		pool.urlopen.return_value = _FakeResp(200)
		with mock.patch.object(ssrf, "_build_pool", return_value=pool):
			ssrf._open_pinned(
				urlparse("https://example.com/mcp"),
				PUBLIC_IP,
				5.0,
				20.0,
				method="POST",
				body=b"{}",
				extra_headers={"Authorization": "Bearer x"},
			)
		_, kwargs = pool.urlopen.call_args
		self.assertEqual(kwargs["headers"]["Host"], "example.com")
		self.assertFalse(kwargs["redirect"])
		self.assertFalse(kwargs["preload_content"])


class TestDnsTimeout(unittest.TestCase):
	"""``socket.getaddrinfo`` has no timeout; a black-holed nameserver would
	otherwise pin a worker for the OS resolver's full retry budget. Resolution
	must be hard-bounded and fail closed."""

	def test_resolve_timeout_fails_closed_promptly(self):
		release = threading.Event()

		def _slow(*a, **k):
			# Block until released (with a safety cap) to model a hung resolver.
			release.wait(5)
			return _addrinfo(PUBLIC_IP)

		try:
			with mock.patch.object(ssrf.socket, "getaddrinfo", _slow):
				start = time.monotonic()
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf._validate_host("slow.example.com", resolve_timeout=0.1)
				elapsed = time.monotonic() - start
			# A hung resolver is an endpoint-health signal -> ERR_CONNECT_FAILED so
			# the broker's circuit breaker opens on it (never ERR_UNRESOLVED).
			self.assertEqual(cm.exception.kind, ssrf.ERR_CONNECT_FAILED)
			# We returned on the timeout, not after waiting out getaddrinfo.
			self.assertLess(elapsed, 2.0)
		finally:
			release.set()

	def test_resolve_exception_is_unresolved(self):
		def _boom(*a, **k):
			raise OSError("nxdomain")

		with mock.patch.object(ssrf.socket, "getaddrinfo", _boom):
			with self.assertRaises(ssrf.SsrfError) as cm:
				ssrf._validate_host("bad.example.com", resolve_timeout=1.0)
		self.assertEqual(cm.exception.kind, ssrf.ERR_UNRESOLVED)

	def test_open_pinned_bounds_resolve_by_budget(self):
		# The DNS bound is min(connect_timeout, read_timeout) so it can never
		# outlast the request the caller asked for.
		release = threading.Event()

		def _slow(*a, **k):
			release.wait(5)
			return _addrinfo(PUBLIC_IP)

		try:
			with mock.patch.object(ssrf.socket, "getaddrinfo", _slow):
				start = time.monotonic()
				with self.assertRaises(ssrf.SsrfError) as cm:
					ssrf.open_pinned_request(
						"https://slow.example.com/mcp", body=b"{}", connect_timeout=0.1, read_timeout=0.1
					)
				elapsed = time.monotonic() - start
			self.assertEqual(cm.exception.kind, ssrf.ERR_CONNECT_FAILED)
			self.assertLess(elapsed, 2.0)
		finally:
			release.set()


if __name__ == "__main__":
	unittest.main()
