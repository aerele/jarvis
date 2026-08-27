"""Tests for jarvis.diagnostics - the three Jarvis Settings buttons."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import diagnostics


class TestPingAdmin(FrappeTestCase):
	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._orig = s.get_password("jarvis_admin_api_key", raise_exception=False) or ""

	def tearDown(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key", self._orig)
		frappe.db.commit()

	def test_no_key_returns_config_error(self):
		with patch.object(frappe.get_single("Jarvis Settings").__class__, "get_password", return_value=""):
			out = diagnostics.ping_admin()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "config")

	def test_happy_path(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key", "test-token")
		frappe.db.commit()
		with patch(
			"jarvis.admin_client.get_connection",
			return_value={"status": "active", "agent_url": "ws://localhost:19200"},
		):
			out = diagnostics.ping_admin()
		self.assertTrue(out["ok"])
		self.assertEqual(out["kind"], "ok")
		self.assertTrue(out["connected"])
		# Security review PART 4 TASK 34-R: the admin get_connection payload carries
		# the container agent_token + operator URLs; ping_admin consumes it to prove
		# reachability but must NEVER return it (to ANY role). Assert the redaction.
		self.assertNotIn("connection", out)
		self.assertNotIn("admin_url", out)
		self.assertNotIn("agent_url", out)
		self.assertNotIn("agent_token", out)

	def test_auth_failure_returns_kind_auth(self):
		from jarvis.exceptions import AdminAuthError

		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key", "bad-token")
		frappe.db.commit()
		with patch("jarvis.admin_client.get_connection", side_effect=AdminAuthError("admin returned 401")):
			out = diagnostics.ping_admin()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "auth")
		self.assertIn("401", out["error"])

	def test_unreachable_returns_kind_unreachable(self):
		from jarvis.exceptions import AdminUnreachableError

		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key", "tok")
		frappe.db.commit()
		with patch(
			"jarvis.admin_client.get_connection", side_effect=AdminUnreachableError("connection refused")
		):
			out = diagnostics.ping_admin()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "unreachable")


class TestPingAgent(FrappeTestCase):
	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._orig_url = s.get("agent_url") or ""
		self._orig_tok = s.get_password("agent_token", raise_exception=False) or ""

	def tearDown(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_url", self._orig_url)
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_token", self._orig_tok)
		frappe.db.commit()

	def test_missing_url_returns_config_error(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_url", "")
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_token", "t")
		frappe.db.commit()
		out = diagnostics.ping_agent()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "config")
		self.assertIn("agent_url", out["error"])

	def test_missing_token_returns_config_error(self):
		# Password fields ignore empty db_set; patch get_password to simulate
		# the "operator hasn't onboarded yet" state.
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_url", "ws://h:1")
		frappe.db.commit()
		with patch.object(frappe.get_single("Jarvis Settings").__class__, "get_password", return_value=""):
			out = diagnostics.ping_agent()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "config")
		self.assertIn("agent_token", out["error"])

	def test_happy_path(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_url", "ws://h:1")
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_token", "t")
		frappe.db.commit()
		with patch("jarvis.agent_ws.ping") as p:
			out = diagnostics.ping_agent()
		p.assert_called_once_with("ws://h:1", "t")
		self.assertTrue(out["ok"])
		# PART 4 TASK 34-R: the endpoint agent_url is an operator-disclosure surface
		# and is dropped from the response.
		self.assertNotIn("agent_url", out)

	def test_unreachable(self):
		from jarvis.exceptions import AgentUnreachableError

		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_url", "ws://h:1")
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "agent_token", "t")
		frappe.db.commit()
		with patch("jarvis.agent_ws.ping", side_effect=AgentUnreachableError("refused")):
			out = diagnostics.ping_agent()
		self.assertFalse(out["ok"])
		self.assertEqual(out["kind"], "unreachable")


class TestForceResync(FrappeTestCase):
	def test_rejects_invalid_action(self):
		with self.assertRaises(frappe.ValidationError):
			diagnostics.force_resync(action="blowup")

	def test_force_resync_always_uses_admin_path(self):
		"""Post-unification (2026-05-29): on_update always routes via admin.
		force_resync inherits the same path."""
		cls = frappe.get_single("Jarvis Settings").__class__
		with patch.object(cls, "_sync_via_admin") as sa:
			diagnostics.force_resync(action="reload")
		sa.assert_called_once_with("reload")

	def test_force_resync_survives_genuine_auth_error(self):
		"""#388: _sync_via_admin now re-raises a genuine admin auth failure.
		force_resync is the one SYNCHRONOUS, whitelisted caller with its own
		{ok/status} JSON contract - it must swallow the raise and still return
		its status dict (reflecting the terminal status _sync_via_admin wrote)
		rather than letting the exception blow through the endpoint."""
		from jarvis.exceptions import AdminAuthError

		cls = frappe.get_single("Jarvis Settings").__class__
		with patch.object(cls, "_sync_via_admin", side_effect=AdminAuthError("nope")):
			out = diagnostics.force_resync(action="reload")
		self.assertEqual(out["action"], "reload")
		self.assertIn("last_sync_status", out)


class TestChatRecoveryStats(FrappeTestCase):
	"""jarvis.diagnostics.chat_recovery_stats - operator visibility into
	how often snapshot recovery is rescuing turns."""

	MSG = "Jarvis Chat Message"
	CONV = "Jarvis Conversation"

	def setUp(self):
		self.conv = frappe.get_doc(
			{
				"doctype": self.CONV,
				"title": "diag-stats",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(self.MSG, {"conversation": self.conv.name})
		frappe.db.delete(self.CONV, {"name": self.conv.name})
		frappe.db.commit()

	def _add(self, *, was_recovered=0, streaming=0, recovering=0, error=""):
		frappe.get_doc(
			{
				"doctype": self.MSG,
				"conversation": self.conv.name,
				"seq": 1,
				"role": "assistant",
				"content": "x",
				"streaming": streaming,
				"recovering": recovering,
				"was_recovered": was_recovered,
				"error": error,
			}
		).insert(ignore_permissions=True)

	def test_shape_and_zero_rate_with_no_turns(self):
		out = diagnostics.chat_recovery_stats()
		for window in ("24h", "7d"):
			self.assertIn(window, out)
			for key in ("total", "recovered", "currently_recovering", "ceiling_errored"):
				self.assertIn(key, out[window])
		self.assertEqual(out["recovered_rate_24h"], 0)

	def test_counts_move_after_a_finalize(self):
		before = diagnostics.chat_recovery_stats()
		self._add(was_recovered=0)
		self._add(was_recovered=1)
		self._add(
			was_recovered=1,
			error="Run did not finish within the recovery window.",
		)
		self._add(streaming=1, recovering=1)
		frappe.db.commit()
		after = diagnostics.chat_recovery_stats()

		self.assertEqual(after["24h"]["total"], before["24h"]["total"] + 4)
		self.assertEqual(after["24h"]["recovered"], before["24h"]["recovered"] + 2)
		self.assertEqual(
			after["24h"]["currently_recovering"],
			before["24h"]["currently_recovering"] + 1,
		)
		self.assertEqual(
			after["24h"]["ceiling_errored"],
			before["24h"]["ceiling_errored"] + 1,
		)
		# 7d window includes the same new rows.
		self.assertEqual(after["7d"]["total"], before["7d"]["total"] + 4)
		self.assertEqual(after["7d"]["recovered"], before["7d"]["recovered"] + 2)
		self.assertGreater(after["recovered_rate_24h"], 0)
