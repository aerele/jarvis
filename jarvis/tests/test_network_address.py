"""Address rules for the private-network onboarding (design spec section 9).

The bench mirrors admin's jarvis_admin_v2/fleet/address_rules.py. The PARITY_* tables below
are admin's own case table (tests/test_private_address_rules.py) plus the cases the
pre-flight found divergent. They are module-level lists on purpose: the SPA's JS mirror
(Task 11) is built from the same rows. Same input -> same accept/refuse and same canonical form.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import network_address as na
from jarvis.network_address import check_public_address, private_test_mode

# (given, canonical form) - accepted outside test mode.
PARITY_ACCEPT = [
	("https://erp.acme.com", "https://erp.acme.com"),
	("https://ERP.Acme.com/", "https://erp.acme.com"),
	("https://erp.acme.com:8443/jarvis", "https://erp.acme.com:8443/jarvis"),
	("https://8.8.8.8", "https://8.8.8.8"),
	("https://8.8.8.8:8443/x", "https://8.8.8.8:8443/x"),
	("https://8.8.8.8:8443/erp/", "https://8.8.8.8:8443/erp"),
	("https://xn--bcher-kva.example.com", "https://xn--bcher-kva.example.com"),
	("https://a-b.co.uk", "https://a-b.co.uk"),
	("https://a.com/", "https://a.com"),
	("https://a.com/a/b", "https://a.com/a/b"),
	("https://a.com/a/b/", "https://a.com/a/b"),
	("HTTPS://ERP.Acme.com:443/Jarvis/", "https://erp.acme.com/Jarvis"),
	("  https://erp.example.com/ ", "https://erp.example.com"),
	("https://erp.example.com:443", "https://erp.example.com"),
	("https://1.1.1.1", "https://1.1.1.1"),
]

# (given, admin reason code) - refused outside test mode.
PARITY_REFUSE = [
	("http://erp.acme.com", na.REASON_SCHEME),
	("ftp://x.com", na.REASON_SCHEME),
	("https://localhost", na.REASON_LOCAL_NAME),
	("https://a.localhost", na.REASON_LOCAL_NAME),
	("https://x.local", na.REASON_LOCAL_NAME),
	("https://x.test", na.REASON_RESERVED_TLD),
	("https://x.corp", na.REASON_RESERVED_TLD),
	("https://x.private", na.REASON_RESERVED_TLD),
	("https://x.onion", na.REASON_RESERVED_TLD),
	("https://x.arpa", na.REASON_RESERVED_TLD),
	("https://x.localdomain", na.REASON_RESERVED_TLD),
	("https://x.alt", na.REASON_RESERVED_TLD),
	("https://x.home.arpa", na.REASON_RESERVED_TLD),
	("https://intranet", na.REASON_SINGLE_LABEL),
	("https://erp", na.REASON_SINGLE_LABEL),
	("https://127.0.0.1", na.REASON_NOT_GLOBAL),
	("https://10.0.0.5", na.REASON_NOT_GLOBAL),
	("https://192.168.1.1", na.REASON_NOT_GLOBAL),
	("https://169.254.169.254", na.REASON_NOT_GLOBAL),
	("https://100.64.1.1", na.REASON_NOT_GLOBAL),
	("https://203.0.113.24", na.REASON_NOT_GLOBAL),
	("https://0.0.0.0", na.REASON_NOT_GLOBAL),
	("https://255.255.255.255", na.REASON_NOT_GLOBAL),
	("https://224.0.0.1", na.REASON_NOT_GLOBAL),
	("https://239.1.1.1", na.REASON_NOT_GLOBAL),
	("https://u@erp.acme.com", na.REASON_USERINFO),
	("https://good.com@evil.com", na.REASON_USERINFO),
	("https://erp.acme.com?x=1", na.REASON_QUERY),
	("https://erp.acme.com#f", na.REASON_QUERY),
	("https://erp.acme.com:0", na.REASON_PORT),
	("https://erp.acme.com:99999", na.REASON_PORT),
	("https://erp.acme.com:abc", na.REASON_PORT),
	("https://[::1]", na.REASON_IPV6),
	("https://2130706433", na.REASON_SINGLE_LABEL),
	("https://127.1", na.REASON_HOST),
	("https://0x7f.0.0.1", na.REASON_HOST),
	("https://010.1.1.1", na.REASON_HOST),
	("https://1.2.3.4.5", na.REASON_HOST),
	("https://erp.acme.com.", na.REASON_HOST),
	("https://localhost.", na.REASON_HOST),
	("https://a..com", na.REASON_HOST),
	("https://-a.com", na.REASON_HOST),
	("https://a-.com", na.REASON_HOST),
	("https://a_b.com", na.REASON_HOST),
	("https://.a.com", na.REASON_HOST),
	("https://erp.acme.com/a b", na.REASON_BAD_CHARS),
	("https://erp.acme.com\\x", na.REASON_BAD_CHARS),
	("https://exämple.com", na.REASON_BAD_CHARS),
	("https://erp.acme.com/..", na.REASON_PATH),
	("https://erp.acme.com/a/../b", na.REASON_PATH),
	("https://erp.acme.com/%2e%2e/x", na.REASON_PATH),
	("https://erp.acme.com/a%20b", na.REASON_PATH),
	("https://erp.acme.com/a;b", na.REASON_PATH),
	("https://a.com/.", na.REASON_PATH),
	("https://a.com/./", na.REASON_PATH),
	("https://a.com/a/./b", na.REASON_PATH),
	("https://a.com/a//b", na.REASON_PATH),
	("https://a.com//x", na.REASON_PATH),
	("https://a.com//", na.REASON_PATH),
	("https://erp.example.com/a//", na.REASON_PATH),
	("", na.REASON_EMPTY),
	("   ", na.REASON_EMPTY),
	(None, na.REASON_EMPTY),
	("https://", na.REASON_BAD_URL),
	("erp.acme.com", na.REASON_BAD_URL),
	("https://a.com/" + "x" * 300, na.REASON_TOO_LONG),
]

# Accepted ONLY in test mode: exactly admin's three relaxations.
TEST_MODE_ACCEPT = [
	("http://jarvis.localhost:8000", "http://jarvis.localhost:8000"),
	("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
	("https://jarvis.localhost", "https://jarvis.localhost"),
	("http://localhost", "http://localhost"),
	("http://127.5.5.5", "http://127.5.5.5"),
	("http://erp.acme.com", "http://erp.acme.com"),
	("http://8.8.8.8:80", "http://8.8.8.8"),
]

# Still refused in test mode.
TEST_MODE_REFUSE = [
	("http://10.0.0.1", na.REASON_NOT_GLOBAL),
	("http://192.168.1.1", na.REASON_NOT_GLOBAL),
	("https://203.0.113.24", na.REASON_NOT_GLOBAL),
	("http://203.0.113.5", na.REASON_NOT_GLOBAL),
	("http://169.254.169.254", na.REASON_NOT_GLOBAL),
	("http://x.local", na.REASON_LOCAL_NAME),
	("http://erp", na.REASON_SINGLE_LABEL),
	("http://u@jarvis.localhost", na.REASON_USERINFO),
	("http://jarvis.localhost?x=1", na.REASON_QUERY),
	("http://jarvis.localhost//x", na.REASON_PATH),
	("ftp://x.com", na.REASON_SCHEME),
	("", na.REASON_EMPTY),
]


class TestAddressParity(FrappeTestCase):
	def test_accepted_addresses_are_canonicalised(self):
		for given, expected in PARITY_ACCEPT:
			with self.subTest(given=given):
				self.assertEqual(check_public_address(given), (expected, ""))

	def test_refused_addresses_carry_admins_reason(self):
		for given, reason in PARITY_REFUSE:
			with self.subTest(given=given):
				self.assertEqual(na.address_reason((given or "").strip() or given), reason)
				expected = na._message_for(reason, (given or "").strip())
				self.assertEqual(check_public_address(given), ("", expected))

	def test_test_mode_accepts_exactly_admins_three_relaxations(self):
		for given, expected in TEST_MODE_ACCEPT:
			with self.subTest(given=given):
				self.assertEqual(check_public_address(given, test_mode=True), (expected, ""))
		for given, _ in TEST_MODE_ACCEPT:
			if given.startswith("http://8.8.8.8") or given.startswith("http://erp"):
				continue
			with self.subTest(outside_test_mode=given):
				self.assertNotEqual(check_public_address(given)[1], "")

	def test_test_mode_still_refuses_everything_else(self):
		for given, reason in TEST_MODE_REFUSE:
			with self.subTest(given=given):
				self.assertEqual(
					check_public_address(given, test_mode=True), ("", na._message_for(reason, given))
				)


class TestCustomerCopy(FrappeTestCase):
	def test_refusals_carry_the_mockup_copy(self):
		cases = [
			("", na.MSG_EMPTY),
			(None, na.MSG_EMPTY),
			("http://erp.example.com", na.MSG_HTTP),
			("ftp://erp.example.com", na.MSG_SCHEME),
			("erp.example.com", na.MSG_SCHEME),
			("https://", na.MSG_NO_HOST),
			("https://user:pw@erp.example.com", na.MSG_USERINFO),
			("https://erp.example.com?x=1", na.MSG_EXTRAS),
			("https://erp.example.com/#frag", na.MSG_EXTRAS),
			("https://erp.example.com:99999", na.MSG_PORT),
			("https://erp.example.com:0", na.MSG_PORT),
			("https://erp.example.com:abc", na.MSG_PORT),
			("https://[::1]", na.MSG_IPV6),
			("https://ex ample.com", na.MSG_CHARS),
			("https://a.com/" + "x" * 300, na.MSG_LONG),
			("https://10.0.0.5", na.MSG_PRIVATE),
			("https://localhost", na.MSG_PRIVATE),
			("https://erp.local", na.MSG_PRIVATE),
			("https://erp.corp", na.MSG_PRIVATE),
			("https://intranet", na.MSG_DOMAIN),
			("https://010.1.1.1", na.MSG_DOMAIN),
			("https://0x7f.1", na.MSG_DOMAIN),
			("https://a.com/a//b", na.MSG_PATH),
		]
		for given, expected in cases:
			with self.subTest(given=given):
				self.assertEqual(check_public_address(given), ("", expected))

	def test_every_reason_code_has_copy(self):
		for reason in PARITY_REFUSE:
			self.assertTrue(na._message_for(reason[1], "https://x"))


class TestPrivateTestModeSwitch(FrappeTestCase):
	def test_off_by_default(self):
		with patch.dict(frappe.conf, {}, clear=False):
			frappe.conf.pop("jarvis_private_network_test_mode", None)
			self.assertFalse(private_test_mode())

	def test_reads_the_site_config_integer(self):
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False):
			self.assertTrue(private_test_mode())
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 0}, clear=False):
			self.assertFalse(private_test_mode())
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": "1"}, clear=False):
			self.assertTrue(private_test_mode())
