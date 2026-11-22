"""Shared fixtures for the user agent tests (flag on, two Jarvis users, sync mocked)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot
from jarvis.tests.test_part3_security import _as, _ensure_user

DOCTYPE = "Jarvis User Agent"
OWNER = "ua-owner@example.com"
OTHER = "ua-other@example.com"
AGENT_URL = "ws://127.0.0.1:1"


class UserAgentCase(JarvisSettingsSnapshot, FrappeTestCase):
	"""Flag on, a gateway URL set, the device paired and the runtime push mocked."""

	def setUp(self):
		super().setUp()
		_ensure_user(OWNER, ["Jarvis User"])
		_ensure_user(OTHER, ["Jarvis User"])
		frappe.db.set_single_value("Jarvis Settings", {"enable_user_agents": 1, "agent_url": AGENT_URL})
		# The caps count every row on the site; start each test from none.
		frappe.db.delete(DOCTYPE)
		self.sync = self._start(patch("jarvis.chat.user_agent_sync.request_sync"))
		self._start(patch("jarvis.chat.device.has_paired_token", return_value=True))

	def _start(self, p):
		mock = p.start()
		self.addCleanup(p.stop)
		return mock

	def flag(self, on: bool) -> None:
		frappe.db.set_single_value("Jarvis Settings", "enable_user_agents", 1 if on else 0)

	def make_agent(self, owner: str = OWNER, *, synced: bool = True, **values) -> frappe._dict:
		"""An agent owned by ``owner``, inserted through the normal permission path."""
		fields = {
			"doctype": DOCTYPE,
			"agent_name": "Overdue invoices",
			"instructions": "List overdue Sales Invoices and draft reminders.",
			"tools_json": '["get_list", "get_doc"]',
			**values,
		}
		with _as(owner):
			doc = frappe.get_doc(fields).insert()
		if synced:
			frappe.db.set_value(DOCTYPE, doc.name, {"sync_status": "Synced", "synced_at": frappe.utils.now()})
		return frappe.db.get_value(DOCTYPE, doc.name, "*", as_dict=True)


__all__ = ["AGENT_URL", "DOCTYPE", "OTHER", "OWNER", "UserAgentCase", "_as"]
