import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._password_utils import set_settings_password
from jarvis.chat import site_pin
from jarvis.exceptions import AdminSiteIsCopyError, SiteIsCopyError

SETTINGS = "Jarvis Settings"


def _set(**values):
	frappe.db.set_single_value(SETTINGS, values, update_modified=False)


class TestSitePin(FrappeTestCase):
	def setUp(self):
		super().setUp()
		_set(paired_site="", pairing_blocked=0, pairing_blocked_reason="")

	def test_pin_if_unset_stamps_current_site(self):
		self.assertTrue(site_pin.pin_if_unset())
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), frappe.local.site)

	def test_pin_if_unset_keeps_existing_pin(self):
		_set(paired_site="other.site")
		self.assertFalse(site_pin.pin_if_unset())
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "paired_site"), "other.site")

	def test_status_ok_when_pin_matches(self):
		_set(paired_site=frappe.local.site)
		self.assertFalse(site_pin.status()["blocked"])

	def test_status_ok_when_pin_empty(self):
		self.assertFalse(site_pin.status()["blocked"])

	def test_status_blocked_when_pin_differs(self):
		_set(paired_site="erp.example.com")
		st = site_pin.status()
		self.assertTrue(st["blocked"])
		self.assertEqual(st["paired_site"], "erp.example.com")

	def test_status_works_without_request(self):
		saved = getattr(frappe.local, "request", None)
		frappe.local.request = None
		try:
			self.assertFalse(site_pin.status()["blocked"])
			_set(paired_site="erp.example.com")
			self.assertTrue(site_pin.status()["blocked"])
		finally:
			frappe.local.request = saved

	def test_assert_not_copy_raises_and_persists_block(self):
		_set(paired_site="erp.example.com")
		with self.assertRaises(SiteIsCopyError):
			site_pin.assert_not_copy()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "pairing_blocked"), 1)
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "pairing_blocked_reason"),
			site_pin.copy_message("erp.example.com"),
		)
		with self.assertRaises(AdminSiteIsCopyError):
			site_pin.assert_not_copy(admin=True)

	def test_block_never_wipes_secrets(self):
		doc = frappe.get_doc(SETTINGS)
		before_url = frappe.db.get_single_value(SETTINGS, "agent_url")
		_set(agent_url="ws://127.0.0.1:19999")
		set_settings_password(doc, "agent_token", "tok-123", update_modified=False)
		_set(paired_site="erp.example.com")
		with self.assertRaises(SiteIsCopyError):
			site_pin.assert_not_copy()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "agent_url"), "ws://127.0.0.1:19999")
		self.assertEqual(doc.get_password("agent_token"), "tok-123")
		_set(agent_url=before_url)

	def test_clear_pairing_block_resets_flag_and_pin(self):
		_set(paired_site="erp.example.com")
		with self.assertRaises(SiteIsCopyError):
			site_pin.assert_not_copy()
		st = site_pin.clear_pairing_block()
		self.assertFalse(st["blocked"])
		self.assertEqual(st["paired_site"], "")
