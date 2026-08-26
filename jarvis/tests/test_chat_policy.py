"""Tests for jarvis.chat.policy - the subscription/credits validation seam."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

import jarvis.account as account
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


# --- agent-token drift self-heal (fix/agent-token-selfheal-on-chat-gate) ---
#
# When a tenant is destroyed and reassigned, the new container mints a fresh
# gateway token and admin's Jarvis Tenant.agent_token is updated, but
# nothing pushes that to the customer bench - the bench only re-pulls
# agent_url/agent_token via the DAILY sync_connection cron. _admin_chat_gate
# already fetches the connection payload on every uncached readiness check, so
# it reconciles agent_url/agent_token from that SAME payload, diff-gated so a
# call that changed nothing writes nothing.

_RECONCILE_FIELDS = (
	"agent_url",
	"agent_token",
	"tenant_authority_generation",
	"tenant_authority_handle",
)


def _snapshot_reconcile_fields() -> dict:
	s = frappe.get_single("Jarvis Settings")
	snap = {}
	for f in _RECONCILE_FIELDS:
		v = s.get_password(f, raise_exception=False) if f == "agent_token" else s.get(f)
		snap[f] = v or ""
	return snap


def _restore_reconcile_fields(snap: dict) -> None:
	from frappe.utils.password import remove_encrypted_password

	# The production write path (write_connection -> set_settings_password)
	# stores agent_token in __Auth with a masked column, so a column-only
	# restore would leave a test's token readable via get_password's __Auth
	# fallback in the NEXT test - same trap test_onboarding_sync.py documents.
	remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "agent_token")
	s = frappe.get_single("Jarvis Settings")
	for f, v in snap.items():
		s.db_set(f, v, update_modified=False)
	frappe.db.commit()


class TestChatGateReconcilesAgentTokenDrift(FrappeTestCase):
	"""jarvis.account._admin_chat_gate's opportunistic agent_url/agent_token
	reconcile. admin_client.get_connection is mocked; the gate cache is busted
	in setUp/tearDown (same as TestAdminChatGate in test_account.py) so a
	cached verdict from an earlier test never short-circuits the admin call
	this reconcile depends on. Jarvis Settings is a Single (never rolled back
	between tests), so the fields this reconcile touches are snapshotted and
	restored like test_onboarding_sync.py's _snapshot_settings/_restore_settings."""

	def setUp(self):
		account._bust_chat_gate()
		self._snap = _snapshot_reconcile_fields()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("agent_url", "wss://old-container.example/ws", update_modified=False)
		from jarvis._password_utils import set_settings_password

		set_settings_password(s, "agent_token", "old-token")
		# A stored generation lower than every drift-heal payload's, so guard()
		# ACCEPTs (higher incoming generation) rather than HOLDing on an
		# equal-generation identity check.
		s.db_set("tenant_authority_generation", 1, update_modified=False)
		s.db_set("tenant_authority_handle", "handle-old", update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		account._bust_chat_gate()
		_restore_reconcile_fields(self._snap)

	def test_chat_gate_reconciles_drifted_agent_token(self):
		"""The drift-heal case: a fleet-side rotation changed the token (and
		bumped the authority generation, as a real reassign does) but the
		agent_url is unchanged. After one gate call the new token must be
		live on Jarvis Settings - no more waiting for the daily cron."""
		payload = {
			"agent_url": "wss://old-container.example/ws",
			"agent_token": "new-token",
			"tenant_authority_generation": 2,
			"tenant_authority_handle": "handle-new",
			"tenant_status": "running",
		}
		with patch.object(account.admin_client, "get_connection", return_value=payload):
			out = account._admin_chat_gate()
		self.assertTrue(out["ready"])
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("agent_token", raise_exception=False), "new-token")

	def test_no_drift_does_not_write(self):
		"""Steady state: every field in the payload matches what's already
		stored, so write_connection must never be called - the reconcile must
		not bust the chat-gate cache or churn the encrypted agent_token field
		on a call that changed nothing."""
		payload = {
			"agent_url": "wss://old-container.example/ws",
			"agent_token": "old-token",
			"tenant_authority_generation": 1,
			"tenant_authority_handle": "handle-old",
			"tenant_status": "running",
		}
		with (
			patch.object(account.admin_client, "get_connection", return_value=payload),
			patch("jarvis.onboarding.write_connection") as wc,
		):
			account._admin_chat_gate()
		wc.assert_not_called()

	def test_reconcile_failure_does_not_break_the_gate(self):
		"""A reconcile failure must never break the ready-gate: the gate's
		normal verdict still comes back even when write_connection raises."""
		payload = {
			"agent_url": "wss://old-container.example/ws",
			"agent_token": "new-token",
			"tenant_authority_generation": 2,
			"tenant_authority_handle": "handle-new",
			"tenant_status": "running",
		}
		with (
			patch.object(account.admin_client, "get_connection", return_value=payload),
			patch("jarvis.onboarding.write_connection", side_effect=RuntimeError("boom")),
		):
			out = account._admin_chat_gate()
		self.assertTrue(out["ready"])
		self.assertIsNone(out["reason"])
