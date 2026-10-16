"""The desktop + mobile www shells ship the site's realtime port in context.boot.

socket.js prefers it over the bench-wide port baked in at build time: behind a
dev proxy each site has its own public socketio port, and dialing the internal
one leaves chat on "thinking" until a reload.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.www import jarvis as www_desktop
from jarvis.www import jarvis_mobile as www_mobile


class TestWwwSocketioPort(FrappeTestCase):
	def _desktop_boot(self):
		ctx = frappe._dict()
		with (
			patch.object(www_desktop, "has_jarvis_access", return_value=True),
			patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
			patch.object(www_desktop, "support_scope", return_value=None),
			patch.object(www_desktop, "_support_state", return_value=www_desktop.SUPPORT_OFF),
		):
			www_desktop.get_context(ctx)
		return ctx.boot

	def _mobile_boot(self):
		ctx = frappe._dict()
		with patch.object(www_mobile, "has_jarvis_access", return_value=True):
			www_mobile.get_context(ctx)
		return ctx.boot

	def test_both_shells_ship_the_sites_socketio_port(self):
		with patch.dict(frappe.conf, {"socketio_port": 9081}):
			self.assertEqual(self._desktop_boot()["socketio_port"], 9081)
			self.assertEqual(self._mobile_boot()["socketio_port"], 9081)

	def test_an_unset_port_ships_as_null_so_the_client_falls_back(self):
		conf = {k: v for k, v in frappe.conf.items() if k != "socketio_port"}
		with patch.dict(frappe.conf, conf, clear=True):
			self.assertIsNone(self._desktop_boot()["socketio_port"])
			self.assertIsNone(self._mobile_boot()["socketio_port"])
