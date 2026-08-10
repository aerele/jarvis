"""save_llm_pool returns the durable apply-operation descriptor (plan-05 D2,
review P0-02 / §10.4) and adopts the OAuth capture exactly once.

The pool push is now SYNCHRONOUS for this endpoint (Fable ruling): the local
async queue is suppressed and admin's descriptor is threaded straight back.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis import onboarding
from jarvis.exceptions import AdminUnreachableError
from jarvis.oauth import pending_capture as pc
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

CAP_DT = "Jarvis Pending OAuth Capture"

_DESCRIPTOR = {
	"operation_id": "llmapply_test01",
	"state": "applied_waiting_readiness",
	"code": "LLM_APPLIED_READINESS_PENDING",
	"message": "Your AI connection is saved. Jarvis is finishing setup.",
	"tenant": "7f3a9c1e4b2d8a6f0e5c3b1a9d7e2f40",
	"tenant_authority_generation": 7,
	"desired_version": 12,
	"applied_version": 12,
	"chat_readiness": "Configuring",
	"chat_readiness_reason": "Applying the ERP plugin environment.",
	"retryable": True,
	"retry_after_seconds": 0,
}


def _pool_ok(**over):
	res = {"action": "pool_update", "status": "applied", "apply_operation": dict(_DESCRIPTOR)}
	res.update(over)
	return res


class _SaveOpBase(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("preset", "", update_modified=False)
		s.db_set("routing_mode", "failover", update_modified=False)
		s.db_set("proxy_active", 0, update_modified=False)
		# jarvis#715 gives a LONE subscription its own routing decision, keyed
		# partly on "has this workspace ever synced through the pool leg". This
		# class's subscriptions use upstream "kimi" precisely so that decision
		# stays out of these tests' way (see _capture/_sub_models below), but
		# clear both markers anyway for determinism against a shared, DB-polluting
		# site.
		s.db_set("llm_pool_synced_at", None, update_modified=False)
		s.db_set("llm_direct_synced_at", None, update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		for name in frappe.get_all(CAP_DT, pluck="name"):
			frappe.delete_doc(CAP_DT, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _capture(self, ref="SUB_op0001", subject="acct-op-1"):
		# Kimi (Moonshot), DELIBERATELY: it has no agent-native auth flow, so a
		# lone Kimi subscription always stays on the pool leg regardless of
		# jarvis#715's new lone-renderable-subscription exception. These tests are
		# about POOL-leg plumbing (capture consumption, resumability, idempotency
		# threading, the apply-operation descriptor) - not about which leg a
		# subscription takes, which is covered on its own in
		# test_unified_llm_config.py and test_llm_pool_endpoints.py.
		blob = {"type": "oauth", "provider": "kimi", "access": "AT-op", "refresh": "RT-op"}
		return pc.create_capture(
			provider="Kimi (Moonshot)",
			upstream="kimi",
			agent_provider="kimi",
			oauth_blob=json.dumps(blob),
			account_email="op@example.com",
			account_ref=ref,
			safe_label="op@example.com",
			provider_subject=subject,
		)

	def _sub_models(self, cap_id, ref="SUB_op0001"):
		return [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"order": 0,
				"subscription": {
					"rotation": "sticky",
					"accounts": [
						{
							"upstream": "kimi",
							"account_ref": ref,
							"label": "op@example.com",
							"capture_id": cap_id,
						}
					],
				},
			}
		]


class TestSaveReturnsDescriptor(_SaveOpBase):
	def test_pool_save_consumes_capture_and_returns_descriptor(self):
		view = self._capture()
		calls = []
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=lambda **kw: calls.append(kw) or _pool_ok(),
		):
			out = onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				preset=None,
				routing_mode="failover",
				idempotency_key="idem-123",
			)
		# Descriptor threaded straight back for the SPA to follow.
		self.assertEqual(out["mode"], "operation")
		self.assertEqual(out["apply_operation"]["operation_id"], "llmapply_test01")
		self.assertEqual(out["apply_operation"]["state"], "applied_waiting_readiness")
		self.assertEqual(out["idempotency_key"], "idem-123")
		self.assertFalse(out["resumable"])
		# SYNCHRONOUS: exactly ONE admin push (the async enqueue was suppressed;
		# in tests it would have run inline and doubled this count).
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].get("idempotency_key"), "idem-123")
		# The capture was adopted exactly once: consumed + ciphertext erased.
		name = frappe.db.get_value(CAP_DT, {"capture_id": view["capture_id"]}, "name")
		self.assertIsNotNone(frappe.db.get_value(CAP_DT, name, "consumed_at"))
		self.assertEqual(frappe.db.get_value(CAP_DT, name, "consumed_by_operation"), "llmapply_test01")
		# And the blob moved into the saved pool config (encrypted at rest).
		s = frappe.get_single("Jarvis Settings")
		accts = json.loads(s.models[0].get_password("subscription_accounts"))
		self.assertEqual(json.loads(accts[0]["oauth_blob"])["refresh"], "RT-op")

	def test_resume_with_same_payload_after_consume_does_not_hard_fail(self):
		view = self._capture()
		with patch("jarvis.admin_client.post_update_llm_pool", side_effect=lambda **kw: _pool_ok()):
			onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				idempotency_key="idem-123",
			)
			# The SPA re-calls with the SAME payload (capture already consumed).
			out2 = onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				idempotency_key="idem-123",
			)
		self.assertEqual(out2["mode"], "operation")
		self.assertEqual(out2["apply_operation"]["operation_id"], "llmapply_test01")
		# The blob survived (fell back to the stored copy the first save wrote).
		s = frappe.get_single("Jarvis Settings")
		accts = json.loads(s.models[0].get_password("subscription_accounts"))
		self.assertEqual(json.loads(accts[0]["oauth_blob"])["refresh"], "RT-op")

	def test_admin_timeout_returns_resumable_no_descriptor(self):
		view = self._capture()
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=AdminUnreachableError("read timed out"),
		):
			out = onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				idempotency_key="idem-t",
			)
		self.assertIsNone(out["apply_operation"])
		self.assertTrue(out["resumable"], "an admin timeout is resumable via the idempotency key")
		self.assertEqual(out["idempotency_key"], "idem-t")

	def test_single_api_key_is_legacy_mode_no_operation(self):
		models = [{"provider": "openai", "model": "gpt-5.5", "api_key": "sk-x", "order": 0}]
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or _pool_ok(),
			),
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			out = onboarding.save_llm_pool(frappe.as_json(models), idempotency_key="idem-legacy")
		# A single BYO api-key model is the creds path: no durable operation exists.
		self.assertEqual(out["mode"], "legacy")
		self.assertIsNone(out["apply_operation"])
		self.assertFalse(out["resumable"])
		self.assertEqual(pool_calls, [], "single-model must NOT hit /llm-pool")
		creds.assert_called()


class TestSaveLlmPoolForceProbe(_SaveOpBase):
	"""jarvis_admin_v2#297: force_probe reaches admin's update_llm_pool ONLY when
	the caller (the subscription Test button) explicitly asks for it, so an
	ordinary save's request is unchanged."""

	def test_force_probe_true_reaches_post_update_llm_pool(self):
		view = self._capture()
		calls = []
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=lambda **kw: calls.append(kw) or _pool_ok(),
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				preset=None,
				routing_mode="failover",
				idempotency_key="idem-force-1",
				force_probe=True,
			)
		self.assertEqual(len(calls), 1)
		self.assertIs(calls[0].get("force_probe"), True)

	def test_force_probe_default_is_false_and_ordinary_saves_never_send_it_true(self):
		view = self._capture()
		calls = []
		with patch(
			"jarvis.admin_client.post_update_llm_pool",
			side_effect=lambda **kw: calls.append(kw) or _pool_ok(),
		):
			# No force_probe kwarg at all - the same call shape "Start chatting"
			# and every settings-pane save already make.
			onboarding.save_llm_pool(
				frappe.as_json(self._sub_models(view["capture_id"])),
				preset=None,
				routing_mode="failover",
				idempotency_key="idem-ordinary",
			)
		self.assertEqual(len(calls), 1)
		self.assertIs(calls[0].get("force_probe"), False)

	def test_force_probe_accepts_the_wire_strings_a_frappe_call_actually_sends(self):
		# Same allowlist convention as jarvis.chat.api.set_star: an HTTP form post
		# arrives as a string, and only these exact spellings mean true.
		view = self._capture()
		for wire_value, expected in (
			("1", True),
			("true", True),
			(1, True),
			(True, True),
			("0", False),
			("false", False),
			(0, False),
			(False, False),
			("", False),
		):
			calls = []
			with patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: calls.append(kw) or _pool_ok(),
			):
				onboarding.save_llm_pool(
					frappe.as_json(self._sub_models(view["capture_id"])),
					preset=None,
					routing_mode="failover",
					idempotency_key=f"idem-wire-{wire_value!r}",
					force_probe=wire_value,
				)
			self.assertIs(
				calls[0].get("force_probe"),
				expected,
				f"force_probe={wire_value!r} should coerce to {expected!r}",
			)
