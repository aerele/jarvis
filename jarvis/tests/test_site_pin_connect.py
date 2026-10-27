"""Site pin: gateway connections refuse on a copied site."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_ws
from jarvis.chat.agent_client import AgentSession
from jarvis.exceptions import SiteIsCopyError
from jarvis.tests.test_chat_agent_client import _challenge, _frame, _make_creds, _ScriptedWS


def _set_pin(value: str) -> None:
	frappe.db.set_single_value("Jarvis Settings", "paired_site", value, update_modified=False)


def _scripted_ws() -> _ScriptedWS:
	def _ok():
		return _frame({"type": "res", "id": scripted.sent[-1]["id"], "ok": True, "payload": {}})

	scripted = _ScriptedWS([_challenge(), _ok])
	return scripted


class TestSitePinConnect(FrappeTestCase):
	def setUp(self):
		# FrappeTestCase rolls back per class, so clear what a refusal wrote.
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"paired_site": "", "pairing_blocked": 0, "pairing_blocked_reason": ""},
			update_modified=False,
		)

	def test_connect_refuses_on_copy_site(self):
		_set_pin("other.site")
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection") as create,
			patch("jarvis.chat.agent_client.ensure_paired") as paired,
		):
			with self.assertRaises(SiteIsCopyError):
				AgentSession.connect("ws://t")
		create.assert_not_called()
		paired.assert_not_called()

	def test_connect_unaffected_when_pin_matches(self):
		_set_pin(frappe.local.site)
		scripted = _scripted_ws()
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=_make_creds()),
		):
			sess = AgentSession.connect("ws://t")
		sess.close()

	def test_connect_does_not_pin(self):
		_set_pin("")
		scripted = _scripted_ws()
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=_make_creds()),
		):
			sess = AgentSession.connect("ws://t")
		sess.close()
		self.assertFalse(frappe.db.get_single_value("Jarvis Settings", "paired_site"))

	def test_ping_refuses_on_copy_site(self):
		_set_pin("other.site")
		with patch("jarvis.agent_ws.create_connection") as create:
			with self.assertRaises(SiteIsCopyError):
				agent_ws.ping("ws://t", "tok")
		create.assert_not_called()
