"""Tests for jarvis.chat.policy - the subscription/credits validation seam."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

import jarvis.account as account
from jarvis.chat import policy
from jarvis.chat.policy import validate_can_send

# Entitled verdict. Patched in by default so these tests never depend on the
# live control plane's current subscription state.
_ENTITLED = {"ready": True, "reason": None}
_SUSPENDED = {"ready": False, "reason": "subscription_suspended", "detail": "Your subscription has expired."}


def _patch_llm_configured(test):
	"""Neutralize the llm-configured gate so tests don't depend on whether the
	test site happens to have an LLM set up. Its own coverage lives in
	TestLlmConfiguredGate."""
	p = patch("jarvis.chat.policy._llm_not_configured", return_value=False)
	p.start()
	test.addCleanup(p.stop)


class TestValidateCanSend(FrappeTestCase):
	def setUp(self):
		self._gate = patch.object(account, "_admin_chat_gate", return_value=_ENTITLED)
		self._gate.start()
		self.addCleanup(self._gate.stop)
		# Default: no release rollout, so these tests don't depend on whatever the
		# site's mirrored notice happens to say.
		self._rn = patch("jarvis.release_notice.boot_payload", return_value={"active": False})
		self._rn.start()
		self.addCleanup(self._rn.stop)
		_patch_llm_configured(self)

	def test_release_notice_blocks_send(self):
		"""Server-side half of the gate: the full-page block only latches at boot,
		so an already-open tab must still be refused here."""
		with patch("jarvis.release_notice.boot_payload", return_value={"active": True}):
			ok, reason = validate_can_send("Administrator")
		self.assertFalse(ok)
		self.assertEqual(reason, "release_update_required")

	def test_release_notice_check_fails_open(self):
		with patch("jarvis.release_notice.boot_payload", side_effect=RuntimeError("boom")):
			ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_administrator_can_send(self):
		ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_any_entitled_user_can_send(self):
		ok, reason = validate_can_send("nobody@example.invalid")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_empty_user_is_rejected(self):
		ok, reason = validate_can_send("")
		self.assertFalse(ok)
		self.assertIsNotNone(reason)

	def test_guest_is_rejected(self):
		ok, reason = validate_can_send("Guest")
		self.assertFalse(ok)
		self.assertIsNotNone(reason)

	def test_model_kwarg_defaults_to_no_gate(self):
		ok, reason = validate_can_send("nobody@example.invalid", model=None)
		self.assertTrue(ok)
		self.assertIsNone(reason)


class TestSubscriptionGate(FrappeTestCase):
	"""The entitlement check. Rejecting HERE is the whole point: without it the
	message is persisted and enqueued, and the worker spends the full WS-open
	deadline retrying a socket into a stopped container - surfacing to the
	customer as "the assistant may be starting up"."""

	def setUp(self):
		_patch_llm_configured(self)

	def test_suspended_subscription_rejects_the_send(self):
		with patch.object(account, "_admin_chat_gate", return_value=_SUSPENDED):
			ok, reason = validate_can_send("someone@example.invalid")
		self.assertFalse(ok)
		self.assertEqual(reason, "subscription_suspended")

	def test_provisioning_does_not_reject(self):
		"""A container still coming up is NOT a billing block - the send must go
		through so the existing retry can ride out a dormant container."""
		with patch.object(
			account,
			"_admin_chat_gate",
			return_value={"ready": False, "reason": "container_provisioning", "detail": ""},
		):
			ok, reason = validate_can_send("someone@example.invalid")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_fails_open_when_the_gate_raises(self):
		"""A control-plane hiccup must never block a paying customer."""
		with patch.object(account, "_admin_chat_gate", side_effect=RuntimeError("admin down")):
			ok, reason = validate_can_send("someone@example.invalid")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_guest_rejected_before_the_admin_round_trip(self):
		"""Cheap local checks come first - a Guest must not cost an admin call."""
		with patch.object(account, "_admin_chat_gate") as gate:
			ok, _ = validate_can_send("Guest")
		self.assertFalse(ok)
		gate.assert_not_called()


class TestWorkspaceResetGate(FrappeTestCase):
	"""The mid-reset send gate: transport cleared + resetting marker set."""

	def setUp(self):
		self._gate = patch.object(account, "_admin_chat_gate", return_value=_ENTITLED)
		self._gate.start()
		self.addCleanup(self._gate.stop)
		self._rn = patch("jarvis.release_notice.boot_payload", return_value={"active": False})
		self._rn.start()
		self.addCleanup(self._rn.stop)
		_patch_llm_configured(self)
		import frappe

		s = frappe.get_single("Jarvis Settings")
		self._prev = {"agent_url": s.agent_url or "", "last_sync_status": s.last_sync_status or ""}
		self.addCleanup(self._restore)

	def _restore(self):
		import frappe

		s = frappe.get_single("Jarvis Settings")
		for f, v in self._prev.items():
			s.db_set(f, v, update_modified=False)
		frappe.db.commit()

	def _set(self, agent_url, status):
		import frappe

		s = frappe.get_single("Jarvis Settings")
		s.db_set("agent_url", agent_url, update_modified=False)
		s.db_set("last_sync_status", status, update_modified=False)
		frappe.db.commit()

	def test_resetting_blocks_send(self):
		from jarvis.onboarding import _RESETTING_STATUS

		self._set("", _RESETTING_STATUS)
		ok, reason = validate_can_send("Administrator")
		self.assertFalse(ok)
		self.assertEqual(reason, "workspace_resetting")

	def test_reconnected_workspace_sends_again(self):
		from jarvis.onboarding import _RESETTING_STATUS

		# agent_url back (connection re-synced) -> gate opens even before the
		# marker flips, matching the fail-open posture.
		self._set("ws://localhost:19000", _RESETTING_STATUS)
		ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)


class TestLlmConfiguredGate(FrappeTestCase):
	"""The no-LLM send gate (fresh workspace / post-revoke): reject cleanly
	instead of queuing a turn against the container's stub key."""

	_FIELDS = ("llm_provider", "llm_model", "llm_auth_mode", "proxy_active")

	def setUp(self):
		self._gate = patch.object(account, "_admin_chat_gate", return_value=_ENTITLED)
		self._gate.start()
		self.addCleanup(self._gate.stop)
		self._rn = patch("jarvis.release_notice.boot_payload", return_value={"active": False})
		self._rn.start()
		self.addCleanup(self._rn.stop)
		import frappe

		# The gate is a no-op under test unless a test opts in (the CI site has
		# no LLM configured); these tests ARE its coverage, so opt in.
		frappe.flags.test_llm_configured_gate = True
		self.addCleanup(lambda: setattr(frappe.flags, "test_llm_configured_gate", False))
		s = frappe.get_single("Jarvis Settings")
		self._prev = {f: s.get(f) or ("" if f != "proxy_active" else 0) for f in self._FIELDS}
		self.addCleanup(self._restore)

	def _restore(self):
		import frappe

		s = frappe.get_single("Jarvis Settings")
		for f, v in self._prev.items():
			s.db_set(f, v, update_modified=False)
		frappe.db.commit()

	def _set(self, **kv):
		import frappe

		s = frappe.get_single("Jarvis Settings")
		for f, v in kv.items():
			s.db_set(f, v, update_modified=False)
		frappe.db.commit()

	def test_no_llm_configured_rejects_the_send(self):
		import frappe.model.document

		self._set(llm_provider="", llm_model="", llm_auth_mode="", proxy_active=0)
		with patch.object(frappe.model.document.Document, "get_password", return_value=""):
			ok, reason = validate_can_send("Administrator")
		self.assertFalse(ok)
		self.assertEqual(reason, "llm_not_configured")

	def test_configured_direct_llm_sends(self):
		import frappe.model.document

		self._set(
			llm_provider="Anthropic", llm_model="claude-sonnet-5", llm_auth_mode="api_key", proxy_active=0
		)
		with patch.object(frappe.model.document.Document, "get_password", return_value="k"):
			ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_pool_tenant_is_not_gated_locally(self):
		self._set(llm_provider="", llm_model="", llm_auth_mode="", proxy_active=1)
		ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_subscription_row_with_no_connected_account_still_rejects_the_send(self):
		"""jarvis#755 review: bare models[] presence used to be the whole check,
		so a subscription row enabled but not yet connected (mid-OAuth-handshake,
		before it has an account) read as configured and let a send queue
		against a container with no credential, where it hangs. Now the gate
		must require a connected account too."""
		self._set(llm_provider="", llm_model="", llm_auth_mode="subscription", proxy_active=0)
		with patch("jarvis.jarvis.pool_serialize.has_configured_subscription_model", return_value=False):
			ok, reason = validate_can_send("Administrator")
		self.assertFalse(ok)
		self.assertEqual(reason, "llm_not_configured")

	def test_subscription_with_a_connected_account_sends(self):
		self._set(llm_provider="", llm_model="", llm_auth_mode="subscription", proxy_active=0)
		with patch("jarvis.jarvis.pool_serialize.has_configured_subscription_model", return_value=True):
			ok, reason = validate_can_send("Administrator")
		self.assertTrue(ok)
		self.assertIsNone(reason)


class TestInsufficientWorkers(FrappeTestCase):  # reuse module base
	def setUp(self):
		frappe.flags.test_worker_gate = True  # opt into the real gate under test

	def tearDown(self):
		frappe.flags.test_worker_gate = False

	def test_blocked_returns_reason(self):
		with patch(
			"jarvis.chat.pump.chat_worker_status",
			return_value={"blocked": True, "degraded": True, "workers": 0},
		):
			self.assertTrue(policy._insufficient_workers())

	def test_healthy_returns_false(self):
		with patch(
			"jarvis.chat.pump.chat_worker_status",
			return_value={"blocked": False, "degraded": False, "workers": 4},
		):
			self.assertFalse(policy._insufficient_workers())

	def test_fails_open_on_error(self):
		with patch("jarvis.chat.pump.chat_worker_status", side_effect=RuntimeError):
			self.assertFalse(policy._insufficient_workers())

	def test_validate_can_send_surfaces_reason(self):
		# A user that clears every OTHER gate, but has 0 workers, is blocked here.
		with (
			patch.object(policy, "_over_total_limit", return_value=False),
			patch.object(policy, "_subscription_suspended", return_value=False),
			patch.object(policy, "_release_update_required", return_value=False),
			patch.object(policy, "_workspace_resetting", return_value=False),
			patch.object(policy, "_llm_not_configured", return_value=False),
			patch.object(policy, "_insufficient_workers", return_value=True),
		):
			ok, reason = policy.validate_can_send("someone@acme.com")
			self.assertFalse(ok)
			self.assertEqual(reason, "insufficient_workers")
