"""The real site pins itself on its first verified plugin call_tool.

Plugin verification is patched at ``jarvis.api.validate_plugin_request`` so
these tests write nothing but the pin (rolled back by FrappeTestCase) and
never touch the agent token or commit.
"""

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._plugin_auth import PluginAuthError
from jarvis.api import call_tool
from jarvis.tests.test_api import _FakeRequest

SESSION_KEY = "agent:test:site-pin"


def _pin() -> str:
	return (frappe.db.get_single_value("Jarvis Settings", "paired_site") or "").strip()


class TestCallToolPinsSite(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Jarvis Settings", "paired_site", "", update_modified=False)
		# The hotfix call_tool resolves the user from this row; the test
		# rollback removes it, so nothing is committed.
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": SESSION_KEY, "user": "Administrator"}
		).insert(ignore_permissions=True)

	@contextlib.contextmanager
	def _plugin_request(self, *, verified=True, extra_headers=None):
		headers = {"X-Jarvis-Token": "t", "X-Jarvis-Session": SESSION_KEY, **(extra_headers or {})}
		if verified:
			verify = patch("jarvis.api.validate_plugin_request", return_value=SESSION_KEY)
		else:
			err = PluginAuthError(http_status=401, code="AuthenticationError", message="bad signature")
			verify = patch("jarvis.api.validate_plugin_request", side_effect=err)
		with (
			patch.object(frappe, "request", _FakeRequest(headers), create=True),
			verify,
			patch("jarvis.api.dispatch", return_value={"doctype": "Customer", "fields": []}),
			patch("jarvis.api._persist_and_publish_tool_call"),
		):
			yield

	def _call(self):
		return call_tool(tool="get_schema", args={"doctype": "Customer"})

	def test_verified_call_pins_receiving_site(self):
		with self._plugin_request():
			result = self._call()
		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(_pin(), frappe.local.site)

	def test_pin_ignores_site_name_header(self):
		with self._plugin_request(extra_headers={"X-Frappe-Site-Name": "evil.example.com"}):
			self._call()
		self.assertEqual(_pin(), frappe.local.site)

	def test_bad_signature_does_not_pin(self):
		with self._plugin_request(verified=False):
			result = self._call()
		self.assertFalse(result["ok"])
		self.assertEqual(_pin(), "")

	def test_pin_failure_never_breaks_call_tool(self):
		with self._plugin_request():
			baseline = self._call()
		with (
			self._plugin_request(),
			patch("jarvis.chat.site_pin.pin_if_unset", side_effect=Exception("db down")),
			patch("frappe.log_error") as log_error,
		):
			result = self._call()
		self.assertEqual(result, baseline)
		log_error.assert_called_once_with(title="site_pin: pin failed")
