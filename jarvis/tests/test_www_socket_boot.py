"""Both app shells tell socket.js where this site's socket.io server is: dev_server says
whether `bench start` runs it on a port of its own, socketio_port is this site's port
(site config over common, as Desk reads it in frappe/boot.py). Without them the
browser dials the build-time common port, which a per-site port (the VM's nginx
setup) or a production page on a custom port never answers."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.www import jarvis as www_desktop
from jarvis.www import jarvis_mobile as www_mobile


class TestWwwSocketBoot(FrappeTestCase):
	def _boots(self, dev_server: int, socketio_port: int | None) -> list[dict]:
		# patch.dict with no values snapshots the site conf and restores it on exit.
		with patch.object(frappe, "_dev_server", dev_server), patch.dict(frappe.local.conf):
			frappe.local.conf.pop("socketio_port", None)
			if socketio_port is not None:
				frappe.local.conf["socketio_port"] = socketio_port
			return [self._desktop_boot(), self._mobile_boot()]

	def _desktop_boot(self) -> dict:
		ctx = frappe._dict()
		with (
			patch.object(www_desktop, "has_jarvis_access", return_value=True),
			patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
			patch.object(www_desktop, "is_skill_reviewer", return_value=False),
			patch.object(www_desktop, "support_scope", return_value=None),
			patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
		):
			www_desktop.get_context(ctx)
		return ctx.boot

	def _mobile_boot(self) -> dict:
		ctx = frappe._dict()
		with (
			patch.object(www_mobile, "has_jarvis_access", return_value=True),
			patch.object(www_mobile, "has_jarvis_admin_access", return_value=False),
		):
			www_mobile.get_context(ctx)
		return ctx.boot

	def test_bench_start_sends_dev_server_and_the_sites_port(self):
		for boot in self._boots(dev_server=1, socketio_port=9081):
			self.assertIs(boot["dev_server"], True)
			self.assertEqual(boot["socketio_port"], 9081)

	def test_production_sends_dev_server_false(self):
		for boot in self._boots(dev_server=0, socketio_port=9000):
			self.assertIs(boot["dev_server"], False)

	def test_no_socketio_port_configured_sends_null(self):
		for boot in self._boots(dev_server=1, socketio_port=None):
			self.assertIsNone(boot["socketio_port"])
