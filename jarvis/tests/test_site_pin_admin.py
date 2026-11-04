"""Site pin: tenant-authenticated admin calls refuse on a copied site."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.exceptions import AdminSiteIsCopyError, AdminUnreachableError
from jarvis.tests.test_admin_client import _FakeSettings, _mock_response


def _set_pin(value: str) -> None:
	frappe.db.set_single_value("Jarvis Settings", "paired_site", value, update_modified=False)


class TestSitePinAdmin(FrappeTestCase):
	def setUp(self):
		# FrappeTestCase rolls back per class, so clear what a refusal wrote.
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"paired_site": "", "pairing_blocked": 0, "pairing_blocked_reason": ""},
			update_modified=False,
		)
		# Fake Settings seam: no tabSingles/__Auth writes, no real admin credentials.
		fake = _FakeSettings()
		fake.jarvis_admin_url = "https://admin.example.com"
		fake.jarvis_admin_api_key = "k"
		fake.jarvis_admin_api_secret = "s"
		real = frappe.get_single

		def _get_single(doctype, *args, **kwargs):
			return fake if doctype == "Jarvis Settings" else real(doctype, *args, **kwargs)

		patcher = patch("frappe.get_single", side_effect=_get_single)
		patcher.start()
		self.addCleanup(patcher.stop)
		admin_client.clear_cached_token()
		self.addCleanup(admin_client.clear_cached_token)

	def test_post_refuses_on_copy_site(self):
		_set_pin("other.site")
		with patch("jarvis.admin_client.requests.post") as post:
			with self.assertRaises(AdminSiteIsCopyError) as cm:
				admin_client.get_connection()
		self.assertIsInstance(cm.exception, AdminUnreachableError)
		post.assert_not_called()

	def test_authenticated_raw_refuses_on_copy_site(self):
		_set_pin("other.site")
		with patch("jarvis.admin_client.requests.post") as post:
			with self.assertRaises(AdminSiteIsCopyError):
				admin_client._authenticated_raw("/api/method/x", {}, timeout_s=5)
		post.assert_not_called()

	def test_post_unaffected_when_pin_matches(self):
		_set_pin(frappe.local.site)
		resp = _mock_response(200, {"message": {"ok": True}})
		with patch("jarvis.admin_client.requests.post", return_value=resp) as post:
			admin_client.get_connection()
		post.assert_called()

	def test_guest_post_unaffected_on_copy_site(self):
		_set_pin("other.site")
		resp = _mock_response(200, {"message": {"replaced": False}})
		with patch("jarvis.admin_client.requests.post", return_value=resp) as post:
			admin_client.site_replacement()
		post.assert_called_once()
