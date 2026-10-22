"""link_fetch blocks every address that is not globally routable."""

import unittest

from jarvis.chat import link_fetch


class TestLinkFetchGlobalOnly(unittest.TestCase):
	def test_cgnat_range_is_blocked(self):
		for ip in ("100.64.0.1", "100.100.100.100", "100.127.255.255"):
			self.assertTrue(link_fetch._is_blocked_ip(ip), ip)

	def test_nat64_prefix_is_blocked(self):
		for ip in ("64:ff9b::808:808", "64:ff9b::7f00:1"):
			self.assertTrue(link_fetch._is_blocked_ip(ip), ip)

	def test_public_addresses_are_allowed(self):
		for ip in ("8.8.8.8", "93.184.216.34", "2606:4700::1111"):
			self.assertFalse(link_fetch._is_blocked_ip(ip), ip)

	def test_existing_blocks_still_apply(self):
		for ip in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "not-an-ip"):
			self.assertTrue(link_fetch._is_blocked_ip(ip), ip)
