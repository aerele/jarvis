"""jarvis#841: the connect step's two byte-identical saves must apply ONCE.

On the onboarding connect step the sign-in step's save (the Test press's
save_llm_pool) applies the config and restarts the container; "Start chatting"
then re-posts the identical payload. _classify_llm_change's subscription branch
compares the plaintext pool_state_snapshot against the before-doc, whose row
secrets are '*'-masks, so the identical re-save always read as a change and
bounced the container a second time - underneath the customer's first chat
message.

The fix stamps a fingerprint of the enqueued config at enqueue time
(_on_update_single_model_legacy) and classifies an identical re-save as None
while that apply is recent-"ok" (600s) or still "pending" inside the SPA's own
300s readiness budget (_identical_apply_already_underway). These tests drive
the REAL save path twice, exactly as the wizard does - including the
consumed-capture -> stored-blob fallback that makes save #2's payload
byte-identical - and pin what must still restart: a refreshed token
(jarvis#755), a failed prior apply (the F5 retry lever), and a stale window.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis import onboarding
from jarvis.oauth import pending_capture as pc
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

CAP_DT = "Jarvis Pending OAuth Capture"

_BLOB = '{"provider":"openai","refresh_token":"fake-rt-841"}'
_BLOB_RECONNECTED = '{"provider":"openai","refresh_token":"fake-rt-841-RECONNECTED"}'


def _lone_subscription_models(blob=_BLOB, capture_id=""):
	"""One renderable openai subscription - the jarvis#715 direct leg. The
	account cites EITHER an inline blob (test shorthand) or a capture_id (what
	the wizard really posts; the blob then stays server-side)."""
	account = {
		"upstream": "openai",
		"account_ref": "SUB_841_ref1",
		"label": "me@example.com",
	}
	if capture_id:
		account["capture_id"] = capture_id
	else:
		account["oauth_blob"] = blob
	return [
		{
			"provider": "openai",
			"model": "gpt-5.5",
			"tier": "strong",
			"order": 0,
			"subscription": {"rotation": "sticky", "accounts": [account]},
		}
	]


class TestConnectDoubleApplyDedup(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("preset", "", update_modified=False)
		s.db_set("routing_mode", "failover", update_modified=False)
		s.db_set("proxy_active", 0, update_modified=False)
		# Fresh-connect workspace: jarvis#715's non-retroactivity gate keys the
		# direct leg off these markers (shared, DB-polluting site).
		s.db_set("llm_pool_synced_at", None, update_modified=False)
		s.db_set("llm_direct_synced_at", None, update_modified=False)
		s.db_set("last_sync_status", "", update_modified=False)
		s.db_set("last_sync_requested_at", None, update_modified=False)
		s.db_set("llm_last_apply_fingerprint", "", update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		for name in frappe.get_all(CAP_DT, pluck="name"):
			frappe.delete_doc(CAP_DT, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _capture(self):
		return pc.create_capture(
			provider="OpenAI",
			upstream="openai",
			agent_provider="openai",
			oauth_blob=_BLOB,
			account_email="me@example.com",
			account_ref="SUB_841_ref1",
			safe_label="me@example.com",
			provider_subject="acct-841",
		)

	def _save(self, models, connect_result=None):
		"""Drive the real save; return (connect_mock, save_result)."""
		with patch(
			"jarvis.admin_client.post_subscription_connect",
			return_value=connect_result or {"action": "restart", "status": "applied"},
		) as connect:
			out = onboarding.save_llm_pool(frappe.as_json(models), preset=None, routing_mode="failover")
		return connect, out

	# -- the jarvis#841 wizard sequence itself --------------------------------

	def test_start_chatting_resave_after_confirmed_signin_apply_is_a_noop(self):
		"""The exact wizard sequence: save #1 cites a capture and applies; save
		#2 re-posts the same payload (capture now consumed, so the stored blob
		is merged back) and must NOT reach admin again."""
		view = self._capture()
		connect1, _ = self._save(_lone_subscription_models(capture_id=view["capture_id"]))
		connect1.assert_called_once()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue((s.last_sync_status or "").startswith("ok"))
		self.assertTrue(s.llm_direct_synced_at)
		fingerprint = s.get("llm_last_apply_fingerprint") or ""
		self.assertEqual(len(fingerprint), 64, "enqueue must stamp the apply fingerprint")

		connect2, out2 = self._save(_lone_subscription_models(capture_id=view["capture_id"]))
		connect2.assert_not_called()
		self.assertEqual(out2["mode"], "legacy")
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(
			(s.last_sync_status or "").startswith("ok"),
			"a skipped re-save must not overwrite the confirmed status with pending:",
		)
		self.assertEqual(s.get("llm_last_apply_fingerprint"), fingerprint)
		# The blob itself survived the fallback merge (byte-identity's substrate).
		accounts = json.loads(s.models[0].get_password("subscription_accounts"))
		self.assertEqual(json.loads(accounts[0]["oauth_blob"])["refresh_token"], "fake-rt-841")

	def test_resave_while_the_first_apply_is_still_pending_is_a_noop(self):
		"""The fast clicker: "Start chatting" lands while the sign-in apply is
		still converging. The identical re-save must follow the in-flight apply,
		not enqueue a second one."""
		with patch(
			"jarvis.admin_client.get_connection",
			return_value={"chat_readiness": "Configuring", "chat_readiness_reason": ""},
		):
			connect1, _ = self._save(
				_lone_subscription_models(),
				connect_result={"action": "restart", "status": "applying"},
			)
		connect1.assert_called_once()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue((s.last_sync_status or "").startswith("pending"))

		connect2, _ = self._save(_lone_subscription_models())
		connect2.assert_not_called()

	# -- what must STILL restart ----------------------------------------------

	def test_refreshed_token_still_restarts(self):
		"""jarvis#755: a re-save carrying a genuinely refreshed blob changes the
		fingerprint and must re-apply, recent ok or not."""
		connect1, _ = self._save(_lone_subscription_models())
		connect1.assert_called_once()
		connect2, _ = self._save(_lone_subscription_models(blob=_BLOB_RECONNECTED))
		connect2.assert_called_once()

	def test_identical_resave_after_a_failed_apply_still_restarts(self):
		"""The F5 retry lever: a failed apply is retried by re-saving, and the
		fingerprint gate must never absorb that."""
		connect1, _ = self._save(_lone_subscription_models())
		connect1.assert_called_once()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "failed: admin unreachable: x", update_modified=False)
		connect2, _ = self._save(_lone_subscription_models())
		connect2.assert_called_once()

	def test_identical_resave_outside_the_windows_still_restarts(self):
		"""A stale stamp never dedups: past 600s for a confirmed "ok", past 300s
		for a "pending" (the SPA's readiness budget - its post-exhaustion Retry
		must fire a real apply even with the scheduler paused)."""
		connect1, _ = self._save(_lone_subscription_models())
		connect1.assert_called_once()
		s = frappe.get_single("Jarvis Settings")
		s.db_set(
			"last_sync_requested_at",
			add_to_date(now_datetime(), seconds=-601),
			update_modified=False,
		)
		connect2, _ = self._save(_lone_subscription_models())
		connect2.assert_called_once()

		# Same, on the pending window: 301s-old pending is past its budget.
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "pending: provisioning container", update_modified=False)
		s.db_set(
			"last_sync_requested_at",
			add_to_date(now_datetime(), seconds=-301),
			update_modified=False,
		)
		connect3, _ = self._save(_lone_subscription_models())
		connect3.assert_called_once()

	def test_every_non_direct_writer_clears_the_stamp(self):
		"""The stamp only ever describes the last DIRECT-leg enqueue. Every
		other writer that refreshes last_sync_* without going through the
		classifier (pool enqueue, disconnect blanks, tenancy reset spec) must
		clear it, or a stale stamp could absorb a later repair save against a
		container that never received the config (jarvis#841 review)."""
		from jarvis import settings_reset
		from jarvis.onboarding import _DISCONNECTED_LLM_FIELDS

		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_stamp_pool_applied_ok,
		)

		s = frappe.get_single("Jarvis Settings")
		s.db_set("llm_last_apply_fingerprint", "f" * 64, update_modified=False)
		with patch("frappe.enqueue"):
			s._enqueue_pool_sync()
		self.assertEqual(s.get("llm_last_apply_fingerprint"), "")
		# The synchronous pool push (sync_pool_now) suppresses that enqueue, so
		# its confirmed-apply stamp must clear the stamp itself (#846 review).
		s.db_set("llm_last_apply_fingerprint", "f" * 64, update_modified=False)
		self.assertTrue(_stamp_pool_applied_ok(s, {"action": "pool_update"}))
		self.assertEqual(s.get("llm_last_apply_fingerprint"), "")
		self.assertIn("llm_last_apply_fingerprint", settings_reset.CONNECTION.blank)
		self.assertEqual(_DISCONNECTED_LLM_FIELDS.get("llm_last_apply_fingerprint"), "")

	def test_forced_sync_still_restarts_through_an_identical_resave(self):
		"""flags.force_admin_sync (the explicit resync lever) still applies after
		a recent identical apply. (On this loaded-doc save the masked snapshot
		compares equal anyway, so the flag's own "restart" is what fires; the
		point pinned is the observable one - a forced resync always reaches
		admin, fingerprint stamp or not.)"""
		connect1, _ = self._save(_lone_subscription_models())
		connect1.assert_called_once()
		with patch(
			"jarvis.admin_client.post_subscription_connect",
			return_value={"action": "restart", "status": "applied"},
		) as connect2:
			s = frappe.get_single("Jarvis Settings")
			s.flags.force_admin_sync = True
			# Re-trigger on_update the way a forced save path does.
			s.save(ignore_permissions=True)
		connect2.assert_called_once()
