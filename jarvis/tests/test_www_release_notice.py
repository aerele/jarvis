"""The desktop + mobile www shells expose release_notice in context.boot."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.www import jarvis as www_desktop
from jarvis.www import jarvis_mobile as www_mobile

_FIELDS = (
	"release_notice_active",
	"latest_jarvis_version",
	"release_notice_message",
)


class TestWwwReleaseNotice(FrappeTestCase):
	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = {f: s.get(f) for f in _FIELDS}
		s.db_set("release_notice_active", 1)
		s.db_set("latest_jarvis_version", "0.0.2")
		s.db_set("release_notice_message", "New dashboards.")
		frappe.db.commit()

	def tearDown(self):
		s = frappe.get_single("Jarvis Settings")
		for f, v in self._snap.items():
			s.db_set(f, v)
		frappe.db.commit()

	def test_desktop_boot_exposes_release_notice(self):
		ctx = frappe._dict()
		with (
			patch.object(www_desktop, "has_jarvis_access", return_value=True),
			patch.object(www_desktop, "has_jarvis_admin_access", return_value=False),
			patch.object(www_desktop, "grant_default_support", lambda: None),
			patch.object(www_desktop, "support_scope", return_value=None),
			patch.object(www_desktop, "_support_available", return_value=False),
		):
			www_desktop.get_context(ctx)
		rn = ctx.boot["release_notice"]
		self.assertTrue(rn["active"])
		self.assertEqual(rn["version"], "0.0.2")
		self.assertEqual(rn["message"], "New dashboards.")

	def test_mobile_boot_exposes_release_notice(self):
		ctx = frappe._dict()
		with patch.object(www_mobile, "has_jarvis_access", return_value=True):
			www_mobile.get_context(ctx)
		rn = ctx.boot["release_notice"]
		self.assertTrue(rn["active"])
		self.assertEqual(rn["version"], "0.0.2")
