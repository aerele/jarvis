"""The SPA reads two boot flags: jarvis_public_host_ok keeps meaning "a public host is
configured"; jarvis_network_mode is "private" only for a completed private setup."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.www import jarvis as www_desktop


class TestPrivateNetworkBootFlags(FrappeTestCase):
	def _boot(self, public_ok=True, private=False, public_raises=False, private_raises=False):
		ctx = frappe._dict()
		with (
			patch.object(www_desktop, "has_jarvis_access", return_value=True),
			patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
			patch.object(www_desktop, "is_skill_reviewer", return_value=False),
			patch.object(www_desktop, "support_scope", return_value=None),
			patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
			patch.object(
				admin_client,
				"_public_host_configured",
				side_effect=RuntimeError("boom") if public_raises else None,
				return_value=public_ok,
			),
			patch.object(
				admin_client,
				"is_private_mode",
				side_effect=RuntimeError("boom") if private_raises else None,
				return_value=private,
			),
		):
			www_desktop.get_context(ctx)
		return ctx.boot

	def test_public_bench(self):
		boot = self._boot(public_ok=True, private=False)
		self.assertIs(boot["jarvis_public_host_ok"], True)
		self.assertEqual(boot["jarvis_network_mode"], "")

	def test_completed_private_setup_keeps_public_host_ok_false(self):
		boot = self._boot(public_ok=False, private=True)
		self.assertIs(boot["jarvis_public_host_ok"], False)
		self.assertEqual(boot["jarvis_network_mode"], "private")

	def test_private_bench_before_the_setup(self):
		boot = self._boot(public_ok=False, private=False)
		self.assertIs(boot["jarvis_public_host_ok"], False)
		self.assertEqual(boot["jarvis_network_mode"], "")

	def test_probe_errors_fall_to_the_non_blocking_defaults(self):
		boot = self._boot(public_raises=True, private_raises=True)
		self.assertIs(boot["jarvis_public_host_ok"], True)  # never falsely block the wizard
		self.assertEqual(boot["jarvis_network_mode"], "")
