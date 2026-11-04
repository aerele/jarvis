"""The unsigned-plugin switch is gone: signatures are always enforced."""

import unittest

from jarvis import _plugin_auth


class TestUnsignedSwitchRemoved(unittest.TestCase):
	def test_switch_and_its_helpers_no_longer_exist(self):
		for attr in (
			"_ALLOW_UNSIGNED_CONF_KEY",
			"_allow_unsigned",
			"_record_unsigned_allowed",
			"_unsigned_allowed_recent",
			"unsigned_escape_hatch_status",
		):
			self.assertFalse(hasattr(_plugin_auth, attr), attr)
