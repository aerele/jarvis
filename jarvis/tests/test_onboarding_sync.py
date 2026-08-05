"""Tests for jarvis.onboarding sync + wrappers (admin_client mocked)."""

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding, onboarding_contract


def _set_token(value, secret="secret"):
	"""Set both native credentials for tests that exercise the authenticated
	admin path. value="" clears them (simulates 'not onboarded').

	Clearing must also drop the __Auth rows: the production write path
	(write_connection -> set_settings_password) stores the secret in __Auth
	with a masked column, so a column-only db_set("") would let get_password
	fall back to a previous test's __Auth value."""
	from frappe.utils.password import remove_encrypted_password

	s = frappe.get_single("Jarvis Settings")
	s.db_set("jarvis_admin_api_key", value)
	s.db_set("jarvis_admin_api_secret", secret if value else "")
	s.db_set("agent_url", "")
	if not value:
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_key")
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "jarvis_admin_api_secret")
	frappe.db.commit()


# Fields these tests write to. Snapshot in setUp, restore in tearDown so
# tests run against a real site (e.g. jarvis.localhost) don't clobber the
# operator's actual onboarded state - Frappe Singles aren't transactionally
# rolled back between tests.
_SNAPSHOTTED_FIELDS = (
	"jarvis_admin_url",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_email",
	"jarvis_admin_customer_password",
	"signup_context",
	"agent_url",
	"agent_token",
	"release_notice_active",
	"latest_jarvis_version",
	"release_notice_message",
)

# admin's prepare_bench_disconnect report for a teardown that fully succeeded.
# Spelled out as a literal rather than a MagicMock's truthy default, because the
# bench BRANCHES on these keys: a bare Mock() would make every key truthy and
# every abort path silently untestable. See onboarding._teardown_container_via_admin.
_TEARDOWN_OK = {
	"profile_cleared": True,
	"devices_unpaired": True,
	"removed": 1,
	"detail": "",
}


def _snapshot_settings() -> dict:
	s = frappe.get_single("Jarvis Settings")
	snap = {}
	for f in _SNAPSHOTTED_FIELDS:
		# Password fields → get_password; plain → attribute. Both safe.
		v = (
			s.get_password(f, raise_exception=False)
			if f.endswith(("_key", "_secret", "_token", "_password"))
			else s.get(f)
		)
		snap[f] = v or ""
	return snap


def _restore_settings(snap: dict) -> None:
	"""Restore the snapshot. Password fields also get their __Auth row
	dropped: the production write path stores secrets there (masked column),
	and restoring only the column would leave a test's secret readable via
	get_password's __Auth fallback in the NEXT test. The snapshot value
	itself is written to the column (get_password short-circuits on a
	non-masked column value), matching this helper's original semantics."""
	from frappe.utils.password import remove_encrypted_password

	for f, v in snap.items():
		if f.endswith(("_key", "_secret", "_token", "_password")):
			remove_encrypted_password("Jarvis Settings", "Jarvis Settings", f)
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", f, v)
	frappe.db.commit()


class TestSyncConnection(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot_settings()

	def tearDown(self):
		_restore_settings(self._snap)

	def test_sync_writes_connection_when_assigned(self):
		_set_token("tok")
		with patch(
			"jarvis.onboarding.admin_client.get_connection",
			return_value={
				"agent_url": "ws://localhost:19000",
				"agent_token": "k",
				"tenant_status": "running",
			},
		):
			out = onboarding.sync_connection()
		self.assertTrue(out["synced"])
		self.assertEqual(frappe.get_single("Jarvis Settings").agent_url, "ws://localhost:19000")

	def test_sync_persists_release_notice(self):
		"""An active operator notice on the connection payload is mirrored onto
		Jarvis Settings so boot can read it with no admin round-trip."""
		_set_token("tok")
		with patch(
			"jarvis.onboarding.admin_client.get_connection",
			return_value={
				"agent_url": "ws://localhost:19000",
				"agent_token": "k",
				"tenant_status": "running",
				"release_notice": {
					"active": True,
					"version": "0.0.2",
					"message": "Faster search.",
				},
			},
		):
			onboarding.sync_connection()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 1)
		self.assertEqual(s.latest_jarvis_version, "0.0.2")
		self.assertEqual(s.release_notice_message, "Faster search.")

	def test_sync_clears_release_notice(self):
		"""A payload without a notice clears a previously-mirrored one - the
		operator switching it off must reach the tenant."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("release_notice_active", 1)
		s.db_set("release_notice_message", "stale")
		s.db_set("latest_jarvis_version", "0.0.2")
		frappe.db.commit()
		_set_token("tok")
		with patch(
			"jarvis.onboarding.admin_client.get_connection",
			return_value={
				"agent_url": "ws://localhost:19000",
				"agent_token": "k",
				"tenant_status": "running",
			},
		):
			onboarding.sync_connection()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 0)
		self.assertEqual(s.release_notice_message, "")

	def test_sync_persists_release_notice_without_agent_url(self):
		"""A payload with no agent_url still has to raise/clear the notice - for an
		idle bench this daily sync is the only refresh it gets."""
		_set_token("tok")
		with patch(
			"jarvis.onboarding.admin_client.get_connection",
			return_value={
				"agent_url": "",
				"tenant_status": "pending",
				"release_notice": {"active": True, "version": "0.0.2", "message": "Please update."},
			},
		):
			onboarding.sync_connection()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 1)
		self.assertEqual(s.latest_jarvis_version, "0.0.2")

	def test_sync_noop_when_pending(self):
		_set_token("tok")
		with patch(
			"jarvis.onboarding.admin_client.get_connection",
			return_value={"agent_url": "", "tenant_status": "pending"},
		):
			out = onboarding.sync_connection()
		self.assertFalse(out["synced"])

	def test_sync_skips_when_not_onboarded(self):
		import frappe.model.document

		with (
			patch.object(frappe.model.document.Document, "get_password", return_value=""),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				side_effect=AssertionError("admin must not be called when not onboarded"),
			),
		):
			out = onboarding.sync_connection()
		self.assertFalse(out["synced"])
		self.assertEqual(out["reason"], "not onboarded")

	def test_start_signup_throws_only_when_no_admin_url_resolves(self):
		"""start_signup blocks only when even the bench-wide default resolves
		empty; a normal blank-config deployment rides the default (below)."""
		_set_token("")
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_url", "")
		frappe.db.commit()
		with (
			patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}),
			patch("jarvis.onboarding.get_default_admin_url", return_value=""),
			patch("jarvis.onboarding.admin_client.signup") as mock_signup,
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.start_signup("e4@x.com", "Co", "Annual Plan")
			mock_signup.assert_not_called()

	def test_require_admin_url_allows_default_fallback(self):
		"""With no explicit jarvis_admin_url, onboarding rides the bench-wide
		default instead of blocking (deliberate change)."""
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", "jarvis_admin_url", "")
		frappe.db.commit()
		with patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}):
			onboarding._require_admin_url()  # non-empty default → must not raise

	def test_write_connection_ignores_legacy_api_token(self):
		"""If admin returns the old api_token key, write_connection should NOT
		write it (no accidental cross-population - that field is gone now)."""
		_set_token("")
		onboarding.write_connection({"api_token": "legacy", "agent_url": "ws://h:1"})
		s = frappe.get_single("Jarvis Settings")
		stored = s.get_password("jarvis_admin_api_key", raise_exception=False) or ""
		self.assertEqual(stored, "")

	def test_save_llm_creds_writes_settings_and_fires_save(self):
		"""Step 4 of onboarding: provider/model/api_key land in Jarvis Settings
		and the save triggers the push pipeline (post-unification 2026-05-29:
		always via admin; admin call mocked here, may AdminAuthError if no
		api_key on settings - both paths set last_sync_status)."""
		_set_token("")
		with patch(
			"jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart", "result": "ok"}
		):
			out = onboarding.save_llm_creds(
				provider="Anthropic",
				model="claude-sonnet-4-6",
				api_key="sk-test",
				base_url="https://api.anthropic.com",
			)
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.llm_provider, "Anthropic")
		self.assertEqual(s.llm_model, "claude-sonnet-4-6")
		self.assertEqual(s.get_password("llm_api_key"), "sk-test")
		self.assertEqual(s.llm_base_url, "https://api.anthropic.com")
		self.assertIn("last_sync_status", out)

	def test_save_llm_creds_rejects_missing_required_fields(self):
		with self.assertRaises(frappe.ValidationError):
			onboarding.save_llm_creds(provider="", model="m", api_key="k")
		with self.assertRaises(frappe.ValidationError):
			onboarding.save_llm_creds(provider="Anthropic", model="", api_key="k")
		with self.assertRaises(frappe.ValidationError):
			onboarding.save_llm_creds(provider="Anthropic", model="m", api_key="")

	def test_save_llm_creds_oauth_mode_allows_empty_api_key(self):
		"""REV-1: auth_mode=oauth doesn't require api_key - credentials live in
		the container's auth-profiles.json (pushed separately)."""
		_set_token("")
		with patch(
			"jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart", "result": "ok"}
		):
			out = onboarding.save_llm_creds(
				provider="OpenAI",
				model="gpt-4o",
				api_key="",
				base_url="",
				auth_mode="oauth",
			)
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.llm_auth_mode, "oauth")
		self.assertEqual(s.llm_provider, "OpenAI")
		self.assertEqual(s.llm_model, "gpt-4o")
		self.assertIn("last_sync_status", out)

	def test_save_llm_creds_rejects_unknown_auth_mode(self):
		with self.assertRaises(frappe.ValidationError):
			onboarding.save_llm_creds(provider="OpenAI", model="gpt-4o", api_key="", auth_mode="token")


class TestSignupEmailVerification(FrappeTestCase):
	"""Customer-bench half of the Sprint-1 punch-list email-verification
	work. Pairs with the admin-side flag on
	Jarvis Admin Settings.require_email_verification.

	Bench-side surfaces:
	  start_signup persists api_key + api_secret regardless of which
	    response shape it got, so the poll endpoint can authenticate.
	  check_signup_payment_state wraps admin's get_signup_payment_state.
	"""

	def setUp(self):
		self._snap = _snapshot_settings()
		# Both paths need a non-empty admin URL to pass the pre-flight
		# guard at start_signup; tests don't actually hit it because
		# admin_client.signup is mocked.
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_url",
			"https://admin.example.com",
		)
		frappe.db.commit()
		# The https pre-flight guard (production signup requires an https
		# site URL) would trip on the test bench's plain-http URL; stub
		# get_url so these tests stay focused on response persistence. The
		# guard has its own coverage in TestOnboardingSync.
		self._get_url_patch = patch("frappe.utils.get_url", return_value="https://erp.example.com")
		self._get_url_patch.start()

	def tearDown(self):
		self._get_url_patch.stop()
		_restore_settings(self._snap)

	def test_start_signup_persists_api_key_secret_on_verification_response(self):
		# When admin returns pending_verification=True with api_key+secret,
		# the bench MUST store both so the subsequent poll endpoint can
		# authenticate during the verification window. Without this, the
		# wizard would call check_signup_payment_state with no creds and
		# admin would 401.
		with patch(
			"jarvis.onboarding.admin_client.signup",
			return_value={
				"ok": True,
				"api_key": "verify-key",
				"api_secret": "verify-secret",
				# Contract truth: ``customer`` is admin's SYNTHETIC OAuth login
				# (_synthetic_login), not a deliverable address. The bench stores
				# it as the grant username and must never render it.
				"customer": "cust-3a91f0c2b7de@jarvis.invalid",
				"email": "alice@example.com",
				"company": "Acme",
				"pending_verification": True,
			},
		):
			out = onboarding.start_signup(
				"verify-test@example.com",
				"Acme",
				"Annual Plan",
			)
		self.assertTrue(out["pending_verification"])
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("jarvis_admin_api_key", raise_exception=False),
			"verify-key",
		)
		self.assertEqual(
			s.get_password("jarvis_admin_api_secret", raise_exception=False),
			"verify-secret",
		)
		# The OAuth grant username is persisted now; the password is deliberately
		# absent on the verify-on response (admin defers it to the verified
		# poll). What is stored is the synthetic login — which is exactly why no
		# code may treat this field as the customer's address.
		self.assertEqual(s.get("jarvis_admin_customer_email"), "cust-3a91f0c2b7de@jarvis.invalid")
		self.assertFalse(s.get_password("jarvis_admin_customer_password", raise_exception=False) or "")
		# The address a resumed wizard renders lives in the signup context, from
		# admin's server truth.
		context = onboarding_contract.load()
		self.assertEqual(context["email"], "alice@example.com")
		self.assertEqual(context["company"], "Acme")

	def test_start_signup_legacy_response_still_persists_key_secret(self):
		# Regression pin: the flag-off (legacy) response shape must keep
		# persisting api_key + api_secret on the bench - all subsequent
		# admin calls (finish_payment, get_connection, sync_connection,
		# rotate-secret, push_oauth_blob) authenticate via these.
		with patch(
			"jarvis.onboarding.admin_client.signup",
			return_value={
				"ok": True,
				"api_key": "legacy-key",
				"api_secret": "legacy-secret",
				"customer": "cust-b0b7e1d2c3f4@jarvis.invalid",
				"customer_password": "bob-pw",
				"email": "bob@example.com",
				"company": "Bob Inc",
				"razorpay_key_id": "rzp_test_X",
				"razorpay_order_id": "order_LEGACY",
				"amount_inr": 12000,
			},
		):
			out = onboarding.start_signup(
				"legacy-test@example.com",
				"Bob Inc",
				"Annual Plan",
			)
		self.assertEqual(out["razorpay_order_id"], "order_LEGACY")
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("jarvis_admin_api_key", raise_exception=False),
			"legacy-key",
		)
		# Flag-off path carries the OAuth password in the signup response; the
		# bench persists it (+ the synthetic grant username) for bearer auth on
		# subsequent calls.
		self.assertEqual(s.get("jarvis_admin_customer_email"), "cust-b0b7e1d2c3f4@jarvis.invalid")
		self.assertEqual(
			s.get_password("jarvis_admin_customer_password", raise_exception=False),
			"bob-pw",
		)
		# No credential ever reaches the plain-text context field.
		context = onboarding_contract.load()
		self.assertEqual(context["email"], "bob@example.com")
		self.assertNotIn("api_key", context)
		self.assertNotIn("api_secret", context)
		self.assertNotIn("customer", context)
		self.assertNotIn("customer_password", context)

	def test_check_signup_payment_state_returns_pending(self):
		# Customer hasn't clicked the link yet - admin returns
		# pending_verification: True. Wizard keeps showing the "check
		# your email" screen.
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value={"pending_verification": True},
		):
			out = onboarding.check_signup_payment_state()
		self.assertTrue(out["pending_verification"])

	def test_check_signup_payment_state_returns_razorpay_payload(self):
		# Customer clicked the link - admin returns the deferred order
		# details. Wizard transitions to Razorpay Checkout.
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value={
				"pending_verification": False,
				"razorpay_order_id": "order_VERIFIED",
				"razorpay_key_id": "rzp_test_X",
				"amount_inr": 12000,
			},
		):
			out = onboarding.check_signup_payment_state()
		self.assertFalse(out["pending_verification"])
		self.assertEqual(out["razorpay_order_id"], "order_VERIFIED")

	def test_check_signup_payment_state_persists_customer_password(self):
		# On the verified poll admin delivers the OAuth password once. The
		# bench must persist it so later admin calls use bearer auth.
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value={
				"pending_verification": False,
				"razorpay_order_id": "order_VERIFIED",
				"razorpay_key_id": "rzp_test_X",
				"amount_inr": 12000,
				"customer_password": "verified-pw",
			},
		):
			out = onboarding.check_signup_payment_state()
		self.assertFalse(out["pending_verification"])
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			s.get_password("jarvis_admin_customer_password", raise_exception=False),
			"verified-pw",
		)

	def test_check_signup_payment_state_requires_admin_url(self):
		# Same pre-flight guard as start_signup: blocks only when even the
		# bench-wide default admin URL resolves empty.
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_url",
			"",
		)
		frappe.db.commit()
		with (
			patch.dict(frappe.local.conf, {"jarvis_admin_url": ""}),
			patch("jarvis.onboarding.get_default_admin_url", return_value=""),
			patch(
				"jarvis.onboarding.admin_client.get_signup_payment_state",
			) as mock_call,
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.check_signup_payment_state()
			mock_call.assert_not_called()


class TestGetLlmSyncStatus(FrappeTestCase):
	"""The polling endpoint that the onboarding + account pages hit while
	the background admin sync is running."""

	def setUp(self):
		self._snap = _snapshot_settings()
		_set_token("admin-key")

	def tearDown(self):
		_restore_settings(self._snap)

	def test_returns_pending_true_when_status_starts_with_pending(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "pending: provisioning container", update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(out["last_sync_status"], "pending: provisioning container")
		self.assertTrue(out["pending"])

	def test_returns_pending_false_for_ok_status(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "ok (restart via admin)", update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertFalse(out["pending"])

	def test_returns_pending_false_for_failed_status(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "failed: admin unreachable: boom", update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertFalse(out["pending"])

	def test_shape_has_expected_keys(self):
		out = onboarding.get_llm_sync_status()
		self.assertIn("last_sync_at", out)
		self.assertIn("last_sync_status", out)
		self.assertIn("pending", out)
		self.assertIn("subscription_status", out)
		self.assertIn("warnings", out)
		self.assertIn("model_statuses", out)

	# -- Apply-warning propagation (subscription_status + warnings) -------

	def test_returns_parsed_warnings_and_subscription_status(self):
		"""The pool sync worker stores warnings as a JSON array string;
		get_llm_sync_status must hand back a parsed list of dicts, plus
		the raw subscription_status string, to the SPA poller."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_subscription_status", "unverified", update_modified=False)
		s.db_set(
			"last_sync_warnings",
			'[{"code": "subscription_unverified", "message": "probe failed"}]',
			update_modified=False,
		)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(out["subscription_status"], "unverified")
		self.assertEqual(
			out["warnings"],
			[{"code": "subscription_unverified", "message": "probe failed"}],
		)

	def test_empty_warnings_and_subscription_status_default_cleanly(self):
		"""No pool sync has run yet (or the fleet is on a pre-warnings
		contract) - both fields are empty and must degrade to "" / []
		rather than raise."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_subscription_status", "", update_modified=False)
		s.db_set("last_sync_warnings", "", update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(out["subscription_status"], "")
		self.assertEqual(out["warnings"], [])

	def test_corrupt_warnings_json_degrades_to_empty_list(self):
		"""A malformed last_sync_warnings value must never 500 this poller -
		it must degrade to an empty list."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_warnings", "{not valid json", update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(out["warnings"], [])

	def test_non_list_warnings_json_degrades_to_empty_list(self):
		"""Valid JSON that isn't a list (e.g. a stray object) must also
		degrade to [] - the SPA always expects a list of {code, message}."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_warnings", '{"code": "x", "message": "y"}', update_modified=False)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(out["warnings"], [])

	# -- Per-model verdicts (model_statuses, contract 1.11) --------------

	def test_returns_parsed_model_statuses(self):
		"""The pool sync worker stores the fleet's per-model verdicts as a JSON
		array string; get_llm_sync_status must hand back a parsed list of dicts
		so the AI-models list can key each api-key row's health off it."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set(
			"last_model_statuses",
			'[{"provider": "openai_compat", "model": "claude-sonnet-4-6", "status": "failed"}]',
			update_modified=False,
		)
		frappe.db.commit()
		out = onboarding.get_llm_sync_status()
		self.assertEqual(
			out["model_statuses"],
			[{"provider": "openai_compat", "model": "claude-sonnet-4-6", "status": "failed"}],
		)

	def test_empty_model_statuses_defaults_to_empty_list(self):
		"""No pool sync yet, or a pre-1.11 fleet - the field is empty and must
		degrade to [] rather than raise."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_model_statuses", "", update_modified=False)
		frappe.db.commit()
		self.assertEqual(onboarding.get_llm_sync_status()["model_statuses"], [])

	def test_corrupt_or_non_list_model_statuses_degrades_to_empty_list(self):
		"""A malformed or non-list last_model_statuses must never 500 this poller."""
		s = frappe.get_single("Jarvis Settings")
		for bad in ("{not valid json", '{"model": "m", "status": "failed"}'):
			s.db_set("last_model_statuses", bad, update_modified=False)
			frappe.db.commit()
			self.assertEqual(onboarding.get_llm_sync_status()["model_statuses"], [])


class TestWorkspaceReset(FrappeTestCase):
	"""Self-serve reset: transport cleared, admin creds + synced markers kept."""

	# Everything the reset/revoke paths mutate. llm_auth_mode is MANDATORY on the
	# doctype: leaving it blank (db_set bypasses validation) breaks the next full
	# Jarvis Settings save in an unrelated suite (test_part4_security's fence).
	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"last_sync_status",
		"llm_pool_synced_at",
		"llm_direct_synced_at",
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"llm_provider",
		"llm_model",
		"llm_base_url",
		"llm_auth_mode",
		"llm_api_key",
		"llm_oauth_account_email",
		"llm_oauth_connected_at",
		"preset",
		"proxy_active",
		"proxy_recommended",
		# Only the L4-completion test reaches _clear_admin_connection (CONNECTION
		# spec), which touches these too - matches TestDisconnectBench's list.
		"tenant_authority_handle",
		"tenant_authority_generation",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			v = (
				s.get_password(f, raise_exception=False)
				if f.endswith(("_key", "_secret", "_token", "_password"))
				else s.get(f)
			)
			self._snap[f] = v or ""
		# The revoke path deletes the models[] child rows — snapshot to re-insert.
		self._pool_rows = frappe.get_all(
			"Jarvis LLM Pool Model", filters={"parent": "Jarvis Settings"}, fields=["*"]
		)
		_set_token("tok")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("chat_device_id", "dev-1")
		s.db_set("llm_pool_synced_at", "2026-01-01 00:00:00")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.delete("Jarvis LLM Pool Model", {"parent": "Jarvis Settings"})
		for row in self._pool_rows:
			doc = frappe.get_doc(dict(row, doctype="Jarvis LLM Pool Model", name=None))
			doc.flags.ignore_mandatory = True
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
		frappe.db.commit()

	def test_reset_disconnects_transport_and_keeps_creds(self):
		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect") as disc,
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				return_value={"status": "Applied", "tenant": "t-new"},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.request_workspace_reset(reason="stuck")
		disc.assert_called_once()
		self.assertEqual(out["status"], "Applied")
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.agent_url)
		self.assertFalse(s.chat_device_id)
		self.assertEqual(s.last_sync_status, onboarding._RESETTING_STATUS)
		# The control plane carries these; clearing them would eject to the wizard.
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))
		self.assertTrue(s.llm_pool_synced_at)

	def test_reset_admin_failure_leaves_transport(self):
		from jarvis.admin_client import AdminUnreachableError

		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				side_effect=AdminUnreachableError("down"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.request_workspace_reset()
		self.assertEqual(frappe.get_single("Jarvis Settings").agent_url, "ws://localhost:19000")

	def test_poll_converges_when_ready(self):
		frappe.get_single("Jarvis Settings").db_set("last_sync_status", onboarding._RESETTING_STATUS)
		frappe.db.commit()
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Ready",
					"agent_url": "ws://localhost:19100",
					"agent_token": "t2",
					"tenant_status": "running",
				},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.workspace_reset_state()
		self.assertTrue(out["ready"])
		self.assertFalse(out["resetting"])
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.agent_url, "ws://localhost:19100")
		self.assertEqual(s.last_sync_status, "ok (workspace reset)")

	def test_poll_reports_resetting_while_not_ready(self):
		frappe.get_single("Jarvis Settings").db_set("last_sync_status", onboarding._RESETTING_STATUS)
		frappe.db.commit()
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Pending Capacity", "message": "provisioning shortly"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"chat_readiness": "Configuring"},
			),
		):
			out = onboarding.workspace_reset_state()
		self.assertFalse(out["ready"])
		self.assertTrue(out["resetting"])
		self.assertEqual(out["status"], "Pending Capacity")

	def test_poll_reconnects_and_repushes_pool_when_container_up(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS)
		s.db_set("agent_url", "")
		s.db_set("proxy_active", 1)
		frappe.db.commit()
		try:
			with (
				patch(
					"jarvis.onboarding.admin_client.reset_workspace_state",
					return_value={"status": "Applied"},
				),
				patch(
					"jarvis.onboarding.admin_client.get_connection",
					return_value={
						"chat_readiness": "Configuring",
						"agent_url": "ws://localhost:19100",
						"agent_token": "t2",
					},
				),
				patch(
					"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings._enqueue_pool_sync"
				) as push,
			):
				out = onboarding.workspace_reset_state()
			push.assert_called_once()
			self.assertFalse(out["ready"])
			self.assertEqual(frappe.get_single("Jarvis Settings").agent_url, "ws://localhost:19100")
		finally:
			frappe.get_single("Jarvis Settings").db_set("proxy_active", 0)
			frappe.db.commit()

	def test_reset_wipe_data_deletes_content(self):
		frappe.db.delete("Jarvis Macro", {"macro_name": "wipe-me"})
		frappe.db.commit()
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "wipe-me"})
		conv.flags.ignore_mandatory = True
		conv.flags.ignore_links = True
		conv.insert(ignore_permissions=True)
		macro = frappe.get_doc(
			{"doctype": "Jarvis Macro", "macro_name": "wipe-me", "steps": [{"prompt": "hello"}]}
		)
		macro.flags.ignore_mandatory = True
		macro.flags.ignore_links = True
		macro.insert(ignore_permissions=True)
		frappe.db.commit()
		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				return_value={"status": "Applied", "tenant": "t-new"},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset(wipe_data=True)
		self.assertEqual(frappe.db.count("Jarvis Conversation"), 0)
		self.assertEqual(frappe.db.count("Jarvis Macro"), 0)

	def test_reset_revoke_llm_clears_connections(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("llm_model", "claude-sonnet-5")
		s.db_set("proxy_active", 1)
		s.db_set("llm_pool_synced_at", "2026-01-01 00:00:00")
		pm = frappe.get_doc(
			{
				"doctype": "Jarvis LLM Pool Model",
				"parent": "Jarvis Settings",
				"parenttype": "Jarvis Settings",
				"parentfield": "models",
				"model": "gpt-5.5",
			}
		)
		pm.flags.ignore_mandatory = True
		pm.flags.ignore_links = True
		pm.insert(ignore_permissions=True)
		frappe.db.commit()
		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			# L3 includes L2. The ladder is cumulative and the server now enforces
			# what only the SPA's radio group used to (round-4 MINOR 4). The wipe is
			# patched out because this test is about the LLM revoke, and
			# test_reset_wipe_data_deletes_content already owns the wipe.
			patch("jarvis.onboarding._wipe_workspace_content"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				return_value={"status": "Applied", "tenant": "t-new"},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset(wipe_data=True, revoke_llm=True)
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.llm_model)
		self.assertFalse(s.proxy_active)
		self.assertFalse(s.llm_pool_synced_at)
		self.assertEqual(s.llm_provider, "Anthropic")
		self.assertEqual(frappe.db.count("Jarvis LLM Pool Model", {"parent": "Jarvis Settings"}), 0)
		self.assertEqual(s.last_sync_status, onboarding._RESETTING_RECONNECT_LLM_STATUS)
		# Admin creds must survive — only the LLM connections are revoked.
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))

	def test_poll_completes_on_container_up_when_llm_revoked(self):
		frappe.get_single("Jarvis Settings").db_set(
			"last_sync_status", onboarding._RESETTING_RECONNECT_LLM_STATUS
		)
		frappe.db.commit()
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Configuring",
					"agent_url": "ws://localhost:19100",
					"agent_token": "t2",
				},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.workspace_reset_state()
		self.assertTrue(out["ready"])
		self.assertEqual(frappe.get_single("Jarvis Settings").last_sync_status, "ok (workspace reset)")

	def test_poll_tears_down_container_before_clearing_connection_on_l4(self):
		"""BLOCKER 2 regression. _workspace_reset_poll used to bind ``settings``
		before ``write_connection`` persisted the rebuilt container's agent_url -
		write_connection takes its OWN uncached frappe.get_single() doc, so the
		poll's copy kept the "" that _disconnect_agent_transport committed at
		request time. That made _clear_admin_connection's
		``if (settings.get("agent_url") or "").strip():`` guard deterministically
		false, so the container teardown was silently skipped on every L4 and the
		rebuilt container kept its OAuth auth-profile forever, unreachable once the
		bench destroyed the credentials that could have torn it down.

		Assert the teardown call actually HAPPENS - not that the guard's inputs
		look right, which is what let this ship undetected.

		T14 replaced the two current_customer-gated calls with the single
		authenticated prepare_bench_disconnect, so that is what must fire here."""
		s = frappe.get_single("Jarvis Settings")
		# Simulate the state _disconnect_agent_transport leaves behind: agent_url
		# already cleared, marker carrying the L4 (disconnect_after) suffix.
		s.db_set("agent_url", "")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX)
		frappe.db.commit()
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Ready",
					"agent_url": "ws://localhost:19100",
					"agent_token": "t2",
					"tenant_status": "running",
				},
			),
			# T21 re-validates eligibility immediately before the deferred clear -
			# minutes after the request-time precheck, on the far side of a rebuild.
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": False, "reason": ""},
			),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=_TEARDOWN_OK,
			) as teardown,
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.workspace_reset_state()
		teardown.assert_called_once()
		self.assertTrue(out["ready"])
		self.assertFalse(out["resetting"])
		s = frappe.get_single("Jarvis Settings")
		# CONNECTION cleared afterwards: the agent_url the poll just wrote is gone
		# again, along with the rest of the admin connection.
		self.assertFalse(s.agent_url)
		self.assertFalse(s.get_password("jarvis_admin_api_key", raise_exception=False))


class TestDisconnectBench(FrappeTestCase):
	"""'Disconnect this bench': the terminal, no-rebuild counterpart to L4.
	No poll, no resetting marker — leaving, not resetting."""

	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"tenant_authority_handle",
		"tenant_authority_generation",
		"last_sync_status",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			v = (
				s.get_password(f, raise_exception=False)
				if f.endswith(("_key", "_secret", "_token", "_password"))
				else s.get(f)
			)
			self._snap[f] = v or ""
		_set_token("tok")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("jarvis_admin_customer_email", "cust@example.com")
		s.db_set("chat_device_id", "dev-1")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_disconnects_tears_down_and_clears_connection(self):
		with (
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=_TEARDOWN_OK,
			) as teardown,
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": False, "reason": ""},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.disconnect_bench()
		teardown.assert_called_once()
		self.assertTrue(out["disconnected"])
		self.assertFalse(out["already_disconnected"])
		self.assertFalse(out["needs_company"])
		self.assertIn("jarvis_admin_customer_email", out["cleared"])
		self.assertIn("agent_url", out["cleared"])
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.agent_url)
		self.assertFalse(s.jarvis_admin_customer_email)
		self.assertFalse(s.get_password("jarvis_admin_api_key", raise_exception=False))

	def test_already_disconnected_is_idempotent_and_harmless(self):
		"""No email, no agent_url: this bench already left. Must succeed WITHOUT
		reaching _recovery_outlook - that predicate needs a registered email and
		would throw "no registered customer email" on a bench that already has
		none, turning a harmless repeat click into an error."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_customer_email", "")
		s.db_set("agent_url", "")
		frappe.db.commit()
		with patch("jarvis.onboarding._recovery_outlook") as outlook:
			out = onboarding.disconnect_bench()
		outlook.assert_not_called()
		self.assertTrue(out["disconnected"])
		self.assertTrue(out["already_disconnected"])
		self.assertEqual(out["cleared"], [])
		self.assertFalse(out["needs_company"])

	def test_refuses_when_not_recoverable_and_clears_nothing(self):
		with (
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={
					"recoverable": False,
					"needs_company": False,
					"reason": "Subscription is Cancelled; reconnect is not available.",
				},
			),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect") as teardown,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				onboarding.disconnect_bench()
		self.assertEqual(str(ctx.exception), "Subscription is Cancelled; reconnect is not available.")
		teardown.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.agent_url, "ws://localhost:19000")
		self.assertEqual(s.jarvis_admin_customer_email, "cust@example.com")
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))

	def test_needs_company_is_recoverable_not_refused(self):
		with (
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": True, "reason": ""},
			),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=_TEARDOWN_OK,
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.disconnect_bench()
		self.assertTrue(out["disconnected"])
		self.assertFalse(out["already_disconnected"])
		self.assertTrue(out["needs_company"])
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.agent_url)

	def _recoverable(self):
		return patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": True, "needs_company": False, "reason": ""},
		)

	def _assert_nothing_cleared(self):
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.agent_url, "ws://localhost:19000")
		self.assertEqual(s.jarvis_admin_customer_email, "cust@example.com")
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))

	# -- T14: refusal vs unreachability -------------------------------------
	#
	# Round-2 BLOCKER 1 / round-3 BLOCKER 3, and plan edge case 18. The old code
	# caught BOTH in one bare ``except Exception`` and logged them in the same
	# sentence, so the case where the teardown was POSSIBLE and got skipped was
	# indistinguishable from the case where it was impossible - and the bench
	# went on to destroy the credentials either way. These four pin the split.

	def test_dead_admin_still_completes_the_clear(self):
		"""Genuine unreachability - the ONLY thing that authorizes proceeding
		without a confirmed teardown. Without this escape hatch a bench whose
		control plane is gone could never leave its tenancy."""
		from jarvis.admin_client import AdminUnreachableError

		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				side_effect=AdminUnreachableError("down"),
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.disconnect_bench()
		self.assertTrue(out["disconnected"])
		self.assertFalse(out["already_disconnected"])
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.agent_url)
		self.assertFalse(s.jarvis_admin_customer_email)

	def test_admin_refusal_aborts_the_clear(self):
		"""T14's acceptance criterion. admin ANSWERED and said no (a 4xx -
		ResetLocked, MoveInFlight, UnknownProvider all arrive as this class).
		A retry can still succeed, so nothing may be destroyed."""
		from jarvis.admin_client import AdminValidationError

		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				side_effect=AdminValidationError("another workspace operation is in progress"),
			),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_bench()
		self._assert_nothing_cleared()

	def test_permanent_rejection_aborts_even_though_it_subclasses_unreachable(self):
		"""AdminRejectedError IS an AdminUnreachableError subclass
		(jarvis.exceptions), so an except-clause ordered the other way would
		turn every permanent rejection into a proceed. It is a refusal: admin
		was reached and said no."""
		from jarvis.admin_client import AdminRejectedError

		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				side_effect=AdminRejectedError("refused", code="FleetConfigError", detail="refused"),
			),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_bench()
		self._assert_nothing_cleared()

	def test_failed_device_unpair_aborts_the_clear(self):
		"""admin was reached and reported the teardown honestly: the auth
		profile went, the DEVICE unpair did not. Every paired chat-device token
		is still live, and clearing CONNECTION now destroys the only credentials
		that could ever revoke them. Round-3 BLOCKER 1's consequence, reached
		through a reported failure instead of skipped control flow."""
		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value={
					"profile_cleared": True,
					"devices_unpaired": False,
					"removed": 0,
					"detail": "unpair: TimeoutError: agent unreachable",
				},
			),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_bench()
		self._assert_nothing_cleared()

	def test_dead_container_still_completes_the_clear(self):
		"""The inverse, and the case the disconnect exists for (plan edge case
		6, T3's acceptance criterion). The container is dead: the auth-profile
		clear runs doctor + restart and cannot finish, but the device unpair is
		a file operation that survives a stopped container. Devices provably
		unpaired, so no third party keeps chat access - proceed. Refusing here
		would block exactly the customer most likely to be disconnecting."""
		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value={
					"profile_cleared": False,
					"devices_unpaired": True,
					"removed": 2,
					"detail": "auth_profile: TimeoutError: container stopped",
				},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.disconnect_bench()
		self.assertTrue(out["disconnected"])
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.agent_url)
		self.assertFalse(s.jarvis_admin_customer_email)


class TestRecoveryOutlookPrecheck(FrappeTestCase):
	"""Amendment 1 BLOCKER 1: the disconnect precheck must resolve identity
	SERVER-SIDE via ``admin_client.reconnect_eligibility_me()`` - never by
	sending ``jarvis_admin_customer_email``, which holds admin's synthetic
	OAuth login (``cust-<hash>@jarvis.invalid`` from ``signup._synthetic_login``),
	not a contact address. The earlier version passed it to the guest
	``can_reconnect``, which resolves on the real address, so the lookup could
	never match: L4 and disconnect_bench() refused for 100% of real benches and
	rendered a value the field is documented as never-to-be-shown into a
	customer-facing toast."""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status",)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""
		_set_token("tok")
		# The synthetic OAuth login this field really holds, modelled exactly -
		# if the precheck ever leaked it, this is the shape that would appear.
		s.db_set("jarvis_admin_customer_email", "cust-deadbeef@jarvis.invalid")
		s.db_set("agent_url", "ws://localhost:19000")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_sends_no_identity_argument(self):
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": True, "needs_company": False, "reason": ""},
		) as elig:
			onboarding._recovery_outlook()
		# Zero args, zero kwargs - not even jarvis_admin_customer_email.
		elig.assert_called_once_with()

	def test_recoverable_true_yields_a_recoverable_outlook(self):
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": True, "needs_company": False, "reason": ""},
		):
			outlook = onboarding._recovery_outlook()
		self.assertEqual(outlook, onboarding._RecoveryOutlook(True, False, ""))

	def test_needs_company_true_is_recoverable_not_a_refusal(self):
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": True, "needs_company": True, "reason": ""},
		):
			outlook = onboarding._recovery_outlook()
		self.assertTrue(outlook.recoverable)
		self.assertTrue(outlook.needs_company)

	def test_recoverable_false_refuses_with_admins_own_reason(self):
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={
				"recoverable": False,
				"needs_company": False,
				"reason": "Subscription is Cancelled; reconnect is not available.",
			},
		):
			outlook = onboarding._recovery_outlook()
		self.assertFalse(outlook.recoverable)
		self.assertEqual(outlook.reason, "Subscription is Cancelled; reconnect is not available.")

	def test_no_customer_facing_string_contains_the_synthetic_login(self):
		"""Plan edge case 13 - asserted by test, not by inspection."""
		# Sanity: the fixture really does model the never-to-be-shown shape.
		settings = frappe.get_single("Jarvis Settings")
		self.assertIn("@jarvis.invalid", settings.jarvis_admin_customer_email)
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": False, "needs_company": False, "reason": ""},
		):
			outlook = onboarding._recovery_outlook()
		self.assertNotIn("@jarvis.invalid", outlook.reason)
		# The disconnect endpoint's thrown message is what actually reaches the
		# customer's toast - assert the same on that surface, not just the
		# helper's return value.
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": False, "needs_company": False, "reason": ""},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				onboarding.disconnect_bench()
		self.assertNotIn("@jarvis.invalid", str(ctx.exception))

	def test_admin_unreachable_fails_closed(self):
		"""Fails CLOSED, deliberately: an unverified recovery path is treated as
		no recovery path, and disconnect_bench() must refuse rather than clear
		on a blip it could not confirm."""
		from jarvis.admin_client import AdminUnreachableError

		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			side_effect=AdminUnreachableError("down"),
		):
			outlook = onboarding._recovery_outlook()
		self.assertFalse(outlook.recoverable)
		self.assertNotIn("@jarvis.invalid", outlook.reason)
		with patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			side_effect=AdminUnreachableError("down"),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_bench()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.agent_url, "ws://localhost:19000")
		self.assertTrue(s.jarvis_admin_customer_email)
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))


class TestDisconnectBenchInFlightGuard(FrappeTestCase):
	"""Amendment 1 BLOCKER 3 / MAJOR-1 (T8): disconnect_bench() must refuse
	SERVER-SIDE while a reset is in flight. The SPA's :disabled button is not
	enforcement - a second Settings tab never learns a reset started, and once
	CONNECTION is cleared the resetting marker is blanked with it, leaving
	reconcile_pending_workspace_reset nothing to converge and the rebuilt
	container stranded."""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status",)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""
		_set_token("tok")
		s.db_set("jarvis_admin_customer_email", "cust-deadbeef@jarvis.invalid")
		# The state _disconnect_agent_transport leaves mid-reset: agent_url
		# blanked but the email still present - this is what DEFEATS
		# _admin_connection_absent's AND (it requires BOTH empty), so the
		# idempotency short-circuit does not fire here and the in-flight guard
		# is the only thing standing between this call and a stranding clear.
		s.db_set("agent_url", "")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS)
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_disconnect_mid_reset_is_refused_and_clears_nothing(self):
		# Patches the endpoint the disconnect path ACTUALLY calls. It used to
		# patch post_subscription_disconnect / unpair_chat_devices, which T14
		# removed from this path entirely - assert_not_called() on a target the
		# code can no longer reach passes whatever the guard does.
		with (
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect") as teardown,
			patch("jarvis.onboarding.admin_client.reconnect_eligibility_me") as elig,
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_bench()
		teardown.assert_not_called()
		# Refused by the in-flight guard, before the recovery precheck even runs.
		elig.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, onboarding._RESETTING_STATUS)
		self.assertTrue(s.jarvis_admin_customer_email)


class TestRequestWorkspaceResetInFlightGuard(FrappeTestCase):
	"""Amendment 1 BLOCKER 3 / MAJOR-1 (T8), plan edge case 15: a second
	request_workspace_reset submission while a reset is already in flight must
	not silently change its depth. Admin's own named_lock makes the ADMIN-side
	request idempotent, but the DEPTH lives on this bench's marker, so this
	bench has to guard it separately."""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status",)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""
		_set_token("tok")
		# Transport already cleared by the in-flight reset's own request.
		s.db_set("agent_url", "")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_different_depth_while_in_flight_throws_and_does_not_relabel(self):
		s = frappe.get_single("Jarvis Settings")
		in_flight_marker = onboarding._reset_marker(reconnect_llm=False, disconnect_after=False)  # L1
		s.db_set("last_sync_status", in_flight_marker)
		frappe.db.commit()
		with patch("jarvis.onboarding.admin_client.reset_workspace") as rw:
			with self.assertRaises(frappe.ValidationError):
				onboarding.request_workspace_reset(revoke_llm=True)  # L3 - a different depth
		rw.assert_not_called()
		self.assertEqual(frappe.get_single("Jarvis Settings").last_sync_status, in_flight_marker)

	def test_a_non_cumulative_depth_is_refused_server_side(self):
		"""Round-4 MINOR 4. The ladder is cumulative — each level ADDS to the one
		above — but the server took three independent booleans, so
		``disconnect_after=1, revoke_llm=0`` was accepted and produced a depth the
		ladder has no name for: the marker is "pending: resetting workspace
		(disconnect)", for which ``_reconnect_llm()`` is false, so the poll demands
		full chat-Ready before converging while the customer has asked for the
		connection to be destroyed at the end.

		Only the SPA's radio group enforced this, and a radio group is not
		enforcement — it computes ``depth >= N`` for each flag, which is exactly the
		rule now applied here. Nothing that reaches this endpoint by any other route
		can pick a rung that does not exist."""
		with patch("jarvis.onboarding.admin_client.reset_workspace") as rw:
			with self.assertRaises(frappe.ValidationError):
				onboarding.request_workspace_reset(disconnect_after=True)  # L4 without L2/L3
			with self.assertRaises(frappe.ValidationError):
				onboarding.request_workspace_reset(revoke_llm=True)  # L3 without L2
			with self.assertRaises(frappe.ValidationError):
				onboarding.request_workspace_reset(wipe_data=True, disconnect_after=True)  # L4 without L3
		rw.assert_not_called()

	def test_same_depth_resubmission_stays_idempotent(self):
		# L1 vs L1 (no flags) - deliberately avoids revoke_llm/wipe_data here, so
		# this stays a guard-only test and does not also exercise (and need to
		# snapshot/restore) settings_reset.LLM's field list and pool-model wipe,
		# which TestWorkspaceReset already covers on its own terms.
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._reset_marker(reconnect_llm=False, disconnect_after=False))
		frappe.db.commit()
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				return_value={"status": "Applied", "tenant": "t-new"},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.request_workspace_reset()  # same L1 depth: not refused
		self.assertEqual(out["status"], "Applied")


class TestL4RevalidatesBeforeTheDeferredClear(FrappeTestCase):
	"""T21 + T24 / round-4 MAJOR 8 and MAJOR 2, Amendment 3.

	request_workspace_reset refuses up front if the customer could not reconnect,
	which satisfies plan edge case 2 AT REQUEST TIME. But for L4 the clear happens
	minutes later in the poll, on the far side of a container rebuild - and a
	subscription that moved to Cancelled in between (NOT Suspended; Cancelled is
	outside billing.reconnect._ELIGIBLE_STATUSES) would have its credentials
	destroyed with no emailed-code reconnect able to restore them. One precheck
	"before clearing anything" cannot cover a gap it sits minutes before."""

	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"last_sync_status",
		"workspace_reset_claimed_at",
		"reconnect_needs_company",
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"tenant_authority_handle",
		"tenant_authority_generation",
		"llm_oauth_connected_at",
		"llm_oauth_account_email",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			v = (
				s.get_password(f, raise_exception=False)
				if f.endswith(("_key", "_secret", "_token", "_password"))
				else s.get(f)
			)
			self._snap[f] = v or ""
		_set_token("tok")
		s.db_set("jarvis_admin_customer_email", "cust@example.com")
		s.db_set("agent_url", "")
		s.db_set("reconnect_needs_company", 0)
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX)
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		s = frappe.get_single("Jarvis Settings")
		s.db_set("workspace_reset_claimed_at", None)
		s.db_set("reconnect_needs_company", 0)
		frappe.db.commit()

	def _poll(self, *, eligibility, teardown=None):
		return (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Ready",
					"agent_url": "ws://localhost:19100",
					"agent_token": "t2",
					"tenant_status": "running",
				},
			),
			patch("jarvis.onboarding.admin_client.reconnect_eligibility_me", return_value=eligibility),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=teardown if teardown is not None else _TEARDOWN_OK,
			),
			patch("jarvis.account._bust_chat_gate"),
		)

	def test_a_subscription_cancelled_mid_rebuild_does_not_lose_its_credentials(self):
		"""The permanent lockout the plan calls "mandatory, not advisory" to
		prevent. Eligible at request time, Cancelled by the time the rebuild
		finishes - the clear must be abandoned, not completed.

		Fails against pre-T21 code, which cleared unconditionally."""
		reason = "This account cannot be reconnected afterwards. Contact support."
		with contextlib.ExitStack() as stack:
			for cm in self._poll(
				eligibility={"recoverable": False, "needs_company": False, "reason": reason}
			):
				stack.enter_context(cm)
			out = onboarding.workspace_reset_state()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(out["disconnected"], "the clear must be abandoned")
		self.assertTrue(s.jarvis_admin_customer_email, "credentials must survive")
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))
		# The RESET still completed - this is the L3 outcome, not a failed reset.
		self.assertEqual(s.last_sync_status, "ok (workspace reset)")
		self.assertEqual(s.agent_url, "ws://localhost:19100")

	def test_the_downgrade_is_reported_not_just_logged(self):
		"""T24 / round-4 MAJOR 2. The customer chose "reset and disconnect", was
		shown an irreversibility warning and confirmed it. Silently giving them a
		shallower reset and toasting "Workspace is back" is not acceptable; an
		Error Log entry is not telling them."""
		reason = "This account cannot be reconnected afterwards. Contact support."
		with contextlib.ExitStack() as stack:
			for cm in self._poll(
				eligibility={"recoverable": False, "needs_company": False, "reason": reason}
			):
				stack.enter_context(cm)
			out = onboarding.workspace_reset_state()
		self.assertEqual(out["disconnect_blocked"], reason)

	def test_a_refused_teardown_is_reported_on_the_same_field(self):
		"""The other way an L4 becomes an L3. One field, so the SPA has one branch
		rather than two ways to say the same thing."""
		with contextlib.ExitStack() as stack:
			for cm in self._poll(
				eligibility={"recoverable": True, "needs_company": False, "reason": ""},
				teardown={
					"profile_cleared": True,
					"devices_unpaired": False,
					"removed": 0,
					"detail": "unpair: TimeoutError",
				},
			):
				stack.enter_context(cm)
			out = onboarding.workspace_reset_state()
		self.assertFalse(out["disconnected"])
		self.assertTrue(out["disconnect_blocked"])
		self.assertTrue(frappe.get_single("Jarvis Settings").jarvis_admin_customer_email)

	def test_a_successful_l4_reports_no_blockage(self):
		with contextlib.ExitStack() as stack:
			for cm in self._poll(eligibility={"recoverable": True, "needs_company": False, "reason": ""}):
				stack.enter_context(cm)
			out = onboarding.workspace_reset_state()
		self.assertTrue(out["disconnected"])
		self.assertEqual(out["disconnect_blocked"], "")

	def test_the_l4_clear_holds_a_durable_claim_while_it_runs(self):
		"""Round-5 BLOCKER. The poll used to retire the marker to
		"ok (workspace reset)" and commit BEFORE starting the clear - which runs up
		to ~264s (prepare_bench_disconnect's 240s budget plus the eligibility
		re-check) while this poll's _RESET_LOCK expires at 60s.

		So from t=60s there was neither lock NOR marker: a second tab's Reset passed
		every guard, called admin and started a genuine rebuild, and the clear still
		in flight here then blanked CONNECTION underneath it. That is round-4
		BLOCKER 1's stranding, reached through the one long section that got the
		short lock without a durable claim.

		Read from INSIDE the teardown, which is the window that was unguarded."""
		seen = {}

		def _capture():
			seen["status"] = frappe.db.get_single_value("Jarvis Settings", "last_sync_status")
			seen["claimed_at"] = frappe.db.get_single_value("Jarvis Settings", "workspace_reset_claimed_at")
			seen["blocks_a_reset"] = bool(
				onboarding._workspace_op_in_flight(frappe.get_single("Jarvis Settings"))
			)
			return _TEARDOWN_OK

		with contextlib.ExitStack() as stack:
			for cm in self._poll(eligibility={"recoverable": True, "needs_company": False, "reason": ""}):
				stack.enter_context(cm)
			stack.enter_context(
				patch("jarvis.onboarding.admin_client.prepare_bench_disconnect", side_effect=_capture)
			)
			onboarding.workspace_reset_state()
		self.assertEqual(seen["status"], onboarding._DISCONNECTING_STATUS)
		self.assertTrue(seen["claimed_at"], "an unstamped claim cannot be expired later")
		self.assertTrue(
			seen["blocks_a_reset"],
			"a concurrent reset would have passed every guard and stranded this clear",
		)
		# ...and the completed clear leaves the terminal state, not the claim.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, onboarding._DISCONNECTED_STATUS)
		self.assertFalse(s.workspace_reset_claimed_at)

	def test_a_downgraded_l4_gives_the_claim_back(self):
		"""Nothing is going to clear anything, so the claim must not sit there
		blocking the customer's next reset for the whole expiry window."""
		with contextlib.ExitStack() as stack:
			for cm in self._poll(
				eligibility={
					"recoverable": False,
					"needs_company": False,
					"reason": "Subscription is Cancelled.",
				}
			):
				stack.enter_context(cm)
			onboarding.workspace_reset_state()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, "ok (workspace reset)")
		self.assertFalse(onboarding._workspace_op_in_flight(s))
		self.assertFalse(s.workspace_reset_claimed_at)

	def test_needs_company_is_persisted_through_the_l4_clear(self):
		"""T22 / round-4 MAJOR 9. The value is knowable ONLY at this moment - after
		the sweep there are no credentials left to ask with. bench_connection_state
		hardcoded False before, so an L4 customer whose address owns several
		eligible accounts was sent to a reconnect they could not complete."""
		with contextlib.ExitStack() as stack:
			for cm in self._poll(eligibility={"recoverable": True, "needs_company": True, "reason": ""}):
				stack.enter_context(cm)
			onboarding.workspace_reset_state()
		# Survives the CONNECTION sweep, like _DISCONNECTED_STATUS does...
		self.assertTrue(frappe.get_single("Jarvis Settings").reconnect_needs_company)
		# ...and every later page load can now read it back, with no admin call.
		self.assertTrue(onboarding.bench_connection_state()["needs_company"])


class TestDisconnectClaimsBeforeItTearsDown(FrappeTestCase):
	"""T29 / Amendment 4. disconnect_bench used to hold _RESET_LOCK across its whole
	critical section - including a teardown budgeted at 240s. redis_lock's release
	is a `finally` a SIGKILLed worker never runs, so under a ~120s gunicorn
	http_timeout the ordinary end of a slow teardown left the lock held for the full
	TTL, freezing every convergence including the */5 reconcile.

	The lock now covers check-and-claim only, and a durable claim
	(_DISCONNECTING_STATUS) carries in-flight-ness afterwards, exactly as the
	reset's pre-flight marker does. That gives the claim three new jobs: block a
	concurrent reset, block a second disconnect, and be recoverable when its own
	worker dies."""

	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"last_sync_status",
		"workspace_reset_claimed_at",
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"tenant_authority_handle",
		"tenant_authority_generation",
		"llm_oauth_connected_at",
		"llm_oauth_account_email",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			v = (
				s.get_password(f, raise_exception=False)
				if f.endswith(("_key", "_secret", "_token", "_password"))
				else s.get(f)
			)
			self._snap[f] = v or ""
		_set_token("tok")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("jarvis_admin_customer_email", "cust@example.com")
		s.db_set("last_sync_status", "ok")
		s.db_set("workspace_reset_claimed_at", None)
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.get_single("Jarvis Settings").db_set("workspace_reset_claimed_at", None)
		frappe.db.commit()

	def _recoverable(self):
		return patch(
			"jarvis.onboarding.admin_client.reconnect_eligibility_me",
			return_value={"recoverable": True, "needs_company": False, "reason": ""},
		)

	def test_the_claim_is_written_before_the_teardown_runs(self):
		"""Read from INSIDE the teardown call - the exact window the lock no longer
		covers. With no claim there, a concurrent reset would sail past its guard
		while this bench was mid-teardown."""
		seen = {}

		def _capture():
			seen["status"] = frappe.db.get_single_value("Jarvis Settings", "last_sync_status")
			seen["claimed_at"] = frappe.db.get_single_value("Jarvis Settings", "workspace_reset_claimed_at")
			return _TEARDOWN_OK

		with (
			self._recoverable(),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect", side_effect=_capture),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.disconnect_bench()
		self.assertEqual(seen["status"], onboarding._DISCONNECTING_STATUS)
		self.assertTrue(seen["claimed_at"], "an unstamped claim cannot be expired later")
		# ...and the completed clear overwrites it, leaving no stale claim behind.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, onboarding._DISCONNECTED_STATUS)
		self.assertFalse(s.workspace_reset_claimed_at)

	def test_a_reset_is_refused_while_a_disconnect_is_mid_teardown(self):
		"""The race the shortened lock opens. The disconnect's lock is already
		released by the time its teardown runs, so this marker is the only thing
		stopping a reset from starting on a bench about to lose its credentials."""
		outcome = {}

		def _reentrant():
			try:
				onboarding.request_workspace_reset()
				outcome["refused"] = False
			except frappe.ValidationError as exc:
				outcome["refused"] = True
				outcome["why"] = str(exc)
			return _TEARDOWN_OK

		with (
			self._recoverable(),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect", side_effect=_reentrant),
			patch("jarvis.onboarding.admin_client.reset_workspace") as rw,
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.disconnect_bench()
		self.assertTrue(outcome.get("refused"), outcome)
		self.assertIn("being disconnected", outcome.get("why", ""))
		rw.assert_not_called()

	def test_a_second_disconnect_is_refused_while_one_is_mid_teardown(self):
		outcome = {}

		def _reentrant():
			try:
				onboarding.disconnect_bench()
				outcome["refused"] = False
			except frappe.ValidationError as exc:
				outcome["refused"] = True
				outcome["why"] = str(exc)
			return _TEARDOWN_OK

		with (
			self._recoverable(),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect", side_effect=_reentrant),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.disconnect_bench()
		self.assertTrue(outcome.get("refused"), outcome)
		self.assertIn("already being disconnected", outcome.get("why", ""))

	def test_a_refused_teardown_gives_the_claim_back(self):
		"""Nothing was torn down or cleared, so the claim must not linger and block
		the customer's next attempt."""
		with (
			self._recoverable(),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value={
					"profile_cleared": True,
					"devices_unpaired": False,
					"removed": 0,
					"detail": "unpair: TimeoutError",
				},
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.disconnect_bench()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, "ok")
		self.assertFalse(onboarding._workspace_op_in_flight(s))
		self.assertFalse(s.workspace_reset_claimed_at)
		self.assertTrue(s.jarvis_admin_customer_email)

	def test_reconcile_expires_an_orphaned_disconnect_claim(self):
		"""The SIGKILL case. Nothing else can ever clear this marker and it blocks
		BOTH entry points, so without the expiry the customer is locked out of
		resetting AND disconnecting, permanently. Safe to retry: the claim precedes
		anything destructive, and a completed clear would have overwritten it with
		_DISCONNECTED_STATUS."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._DISCONNECTING_STATUS)
		s.db_set(
			"workspace_reset_claimed_at",
			frappe.utils.add_to_date(
				frappe.utils.now_datetime(), minutes=-(onboarding._PREFLIGHT_EXPIRY_MINUTES + 1)
			),
		)
		frappe.db.commit()
		with patch("jarvis.onboarding._workspace_reset_poll") as poll:
			onboarding.reconcile_pending_workspace_reset()
		# Never polled: a disconnect is not a reset and nothing converges it.
		poll.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(onboarding._workspace_op_in_flight(s))
		self.assertIn("did not complete", s.last_sync_status)
		self.assertTrue(s.jarvis_admin_customer_email, "credentials must be intact")

	def test_reconcile_leaves_a_young_disconnect_claim_alone(self):
		"""The teardown is budgeted at 240s. Expiring a claim whose worker is merely
		slow would let a reset start on top of a live teardown."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._DISCONNECTING_STATUS)
		s.db_set("workspace_reset_claimed_at", frappe.utils.now_datetime())
		frappe.db.commit()
		onboarding.reconcile_pending_workspace_reset()
		self.assertEqual(
			frappe.get_single("Jarvis Settings").last_sync_status, onboarding._DISCONNECTING_STATUS
		)


class TestDisconnectClearsTheOAuthMarkers(FrappeTestCase):
	"""T17 / plan edge case 21: a reconnected bench must be routed back through
	LLM setup, not skip it on a marker whose credential the disconnect destroyed.

	prepare_bench_disconnect tears the container's OAuth auth-profile down, and an
	L4 rebuild would drop it regardless (OAuth creds never ride a rebuild). Either
	way the container can no longer answer a turn on an OAuth grant. Left set,
	llm_oauth_connected_at makes account.is_ready_for_chat skip the whole LLM step
	for auth_mode oauth/subscription - so the customer reconnects with the emailed
	code and lands in a chat whose container holds no credential."""

	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"last_sync_status",
		"llm_auth_mode",
		"llm_oauth_connected_at",
		"llm_oauth_account_email",
		"llm_direct_synced_at",
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"tenant_authority_handle",
		"tenant_authority_generation",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			v = (
				s.get_password(f, raise_exception=False)
				if f.endswith(("_key", "_secret", "_token", "_password"))
				else s.get(f)
			)
			self._snap[f] = v or ""
		_set_token("tok")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("jarvis_admin_customer_email", "cust@example.com")
		s.db_set("llm_auth_mode", "oauth")
		s.db_set("llm_oauth_connected_at", "2026-01-01 00:00:00")
		s.db_set("llm_oauth_account_email", "person@example.com")
		s.db_set("llm_direct_synced_at", "2026-01-01 00:00:00")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def _disconnect(self):
		with (
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": False, "reason": ""},
			),
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=_TEARDOWN_OK,
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			return onboarding.disconnect_bench()

	def test_the_oauth_markers_are_cleared(self):
		out = self._disconnect()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.llm_oauth_connected_at)
		self.assertFalse(s.llm_oauth_account_email)
		# Reported, so the caller's "what was cleared" list is not a lie.
		self.assertIn("llm_oauth_connected_at", out["cleared"])
		self.assertIn("llm_oauth_account_email", out["cleared"])

	def test_readiness_no_longer_reports_a_usable_credential(self):
		"""The property edge case 21 actually asks for, asserted through the real
		predicate rather than by re-reading the field the test just cleared."""
		from jarvis import account

		self._disconnect()
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(account._has_llm_config(s))

	def test_a_disconnect_is_not_a_silent_llm_revoke(self):
		"""OAUTH_MARKERS is deliberately narrower than the LLM spec. The disconnect
		destroys the container's auth PROFILE; it does not touch an api-key
		tenant's /secrets/llm.key or a pool's own keys. Clearing llm_api_key,
		models[] or the direct/pool sync markers here would turn every disconnect
		into an L3 the customer never asked for."""
		self._disconnect()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.llm_direct_synced_at)
		# llm_auth_mode is reqd AND load-bearing: it is what routes readiness to
		# the LLM step rather than to the "unknown auth_mode" verdict.
		self.assertEqual(s.llm_auth_mode, "oauth")

	def test_the_spec_is_a_strict_subset_of_the_llm_spec(self):
		"""So the reset-onboarding CLI (which applies FULL = CONNECTION | LLM)
		cannot drift from the self-serve disconnect. If a marker were ever added
		to OAUTH_MARKERS without also being in LLM, FULL would stop clearing it
		and the two paths would silently diverge - the exact class of bug
		settings_reset exists to prevent."""
		from jarvis import settings_reset

		full = set(settings_reset.cleared_fields(settings_reset.FULL))
		for field in settings_reset.cleared_fields(settings_reset.OAUTH_MARKERS):
			self.assertIn(field, full)


class TestResetClaimIsAtomicAndEarly(FrappeTestCase):
	"""T15 / Amendment 2 BLOCKER 2: the in-flight guard was defeated by ordering.

	``request_workspace_reset`` READ the marker first and WROTE it LAST, inside
	``_disconnect_agent_transport``, after ``_recovery_outlook`` (8s),
	``post_subscription_disconnect`` (180s budget), ``admin_client.reset_workspace``
	(180s, and it STARTS the rebuild) and the content wipe. For that entire
	multi-minute window ``disconnect_bench``'s guard saw no marker and let the
	disconnect through - blanking ``last_sync_status`` (it is in
	``settings_reset.CONNECTION``), leaving ``reconcile_pending_workspace_reset``
	nothing to converge, and stranding the rebuilt container with no bench able to
	reach it.

	A guard that reads state it has not yet claimed is not a guard.
	"""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status", "workspace_reset_claimed_at")

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""
		_set_token("tok")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("jarvis_admin_customer_email", "cust@example.com")
		s.db_set("last_sync_status", "ok")
		frappe.db.commit()
		self.addCleanup(self._release_lock)

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	@staticmethod
	def _release_lock():
		"""Belt and braces: a test that fails mid-lock must not leave the key set
		for the rest of the suite.

		Deletes through the RAW redis client, not ``frappe.cache().delete_value``.
		That was round-4 MINOR 5: ``delete_value`` routes through
		``RedisWrapper.make_key``, which prefixes the site's db name, while
		redis-py's ``cache.lock(key)`` uses the key VERBATIM. So the old cleanup
		deleted a key that never existed, and this safety net caught nothing.
		Pinned by a test below rather than left to inspection — a no-op cleanup
		looks exactly like a working one until the day it matters."""
		from jarvis._redis_lock import LOCK_PREFIX

		try:
			frappe.cache().delete(LOCK_PREFIX + onboarding._RESET_LOCK)
		except Exception:
			pass

	def test_the_release_lock_helper_deletes_the_key_the_lock_actually_uses(self):
		"""Round-4 MINOR 5. Takes the real lock, runs the cleanup, and asserts the
		lock can then be re-acquired. Fails against the delete_value version, which
		deleted a db-name-prefixed key redis-py never wrote."""
		from jarvis._redis_lock import redis_lock

		lock = frappe.cache().lock(f"jarvis:lock:{onboarding._RESET_LOCK}", timeout=60, blocking_timeout=None)
		self.assertTrue(lock.acquire(blocking=False), "could not take the lock under test")
		self._release_lock()
		with redis_lock(onboarding._RESET_LOCK, timeout_s=10) as reacquired:
			self.assertTrue(reacquired, "the cleanup did not actually free the lock")

	def test_marker_is_claimed_before_the_rebuild_is_requested(self):
		"""Reads ``last_sync_status`` from INSIDE the reset_workspace call - the
		exact moment the old code had already started the rebuild with no marker
		set. Fails against pre-T15 code, where the marker was written afterwards."""
		seen = {}

		def _capture(reason=""):
			seen["status"] = frappe.db.get_single_value("Jarvis Settings", "last_sync_status")
			return {"status": "Applied", "tenant": "t-new"}

		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch("jarvis.onboarding.admin_client.reset_workspace", side_effect=_capture),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset()
		# PRE-FLIGHT at this point: claimed, but admin has not yet accepted the
		# request, so no poll may converge on it. Promotion happens after
		# reset_workspace returns.
		self.assertEqual(seen["status"], onboarding._reset_marker(preflight=True))

	def test_the_l4_depth_is_on_the_claim_not_just_the_final_write(self):
		"""The marker is where a reset's DEPTH lives. Claiming a bare L1 marker
		early and only labelling it L4 at the end would leave the whole window
		looking like a shallower reset to anything that read it."""
		seen = {}

		def _capture(reason=""):
			seen["status"] = frappe.db.get_single_value("Jarvis Settings", "last_sync_status")
			return {"status": "Applied", "tenant": "t-new"}

		with (
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": False, "reason": ""},
			),
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			# Patched for the same reason as above: the assertion is the marker.
			patch("jarvis.onboarding._wipe_workspace_content"),
			patch("jarvis.onboarding._revoke_llm_connections"),
			patch("jarvis.onboarding.admin_client.reset_workspace", side_effect=_capture),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset(wipe_data=True, revoke_llm=True, disconnect_after=True)
		# A real L4 necessarily carries L3 as well (the ladder is cumulative and the
		# server enforces it), so the marker carries BOTH suffixes - which is the
		# combined "(reconnect llm) (disconnect)" case _reset_marker's docstring
		# calls out by name.
		l4 = onboarding._reset_marker(reconnect_llm=True, disconnect_after=True)
		self.assertEqual(
			seen["status"],
			onboarding._reset_marker(reconnect_llm=True, disconnect_after=True, preflight=True),
		)
		# The depth survives the pre-flight suffix: stripping it must give back
		# exactly the L4 marker the poll will later converge on.
		self.assertEqual(onboarding._strip_preflight(seen["status"]), l4)
		self.assertTrue(onboarding._strip_preflight(seen["status"]).endswith(onboarding._DISCONNECT_SUFFIX))
		# ...and it is promoted once admin accepts, or the poll could never finish.
		self.assertEqual(frappe.get_single("Jarvis Settings").last_sync_status, l4)

	def test_a_refused_request_gives_the_claim_back(self):
		"""admin was REACHED and declined (a 4xx), so nothing was started and the
		claim must not survive. Left set, it shows a reset that does not exist,
		blocks disconnect_bench on a guard protecting nothing, and hands the */5
		reconcile a reset it can never converge."""
		from jarvis.admin_client import AdminValidationError

		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				side_effect=AdminValidationError("no subscription found"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.request_workspace_reset()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, "ok")
		self.assertFalse(onboarding._reset_in_flight(s))
		# And the transport is untouched, as it was before T15.
		self.assertEqual(s.agent_url, "ws://localhost:19000")

	def test_a_timeout_KEEPS_the_claim_because_the_rebuild_may_be_running(self):
		"""Round-4 MAJOR 5. Admin runs the destroy + reprovision SYNCHRONOUSLY
		inside the HTTP request, committing its request row before it starts, while
		this bench gives up at timeout_s=180. So AdminUnreachableError here is the
		ordinary shape of a slow rebuild that IS running - not evidence that none
		is.

		Releasing the claim there would blank the marker mid-rebuild, leaving
		reconcile_pending_workspace_reset nothing to converge and disconnect_bench
		unguarded: the exact stranding the claim exists to prevent, reached through
		the release path instead of the claim path.

		Fails against the pre-fix code, which released on every exception."""
		from jarvis.admin_client import AdminUnreachableError

		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				side_effect=AdminUnreachableError("read timeout"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.request_workspace_reset()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.last_sync_status, onboarding._reset_marker(preflight=True))
		self.assertTrue(
			onboarding._reset_in_flight(s),
			"the marker must survive so reconcile can resolve a rebuild that may be running",
		)

	def test_a_transient_failure_leaves_a_claim_no_poll_will_converge_on(self):
		"""The defect the two-state marker exists for, and it was INTRODUCED by the
		round-4 MAJOR 5 fix.

		Keeping the claim on a timeout is right (admin rebuilds synchronously, so a
		bench timeout is the ordinary shape of a rebuild that IS running). But the
		raise unwinds `with redis_lock(...)`, whose `finally` DOES release - so if
		the rebuild never actually started, reconcile arrives five minutes later to
		a free lock, an old container still answering Ready, and a marker saying
		"resetting". It converged: a silent no-op for L1-L3, and for L4 a container
		teardown plus a credential clear against a container that was never rebuilt.

		Pre-flight is what closes it. The claim survives (so the guards hold and
		reconcile can resolve it), but no poll may converge on it until admin has
		positively accepted the request.

		Fails against the pre-fix code, where the kept marker was convergeable."""
		from jarvis.admin_client import AdminUnreachableError

		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "ok")
		frappe.db.commit()

		with (
			# L4, so the recovery precheck runs FIRST and has to pass - otherwise the
			# request is refused before it ever claims and this test proves nothing.
			patch(
				"jarvis.onboarding.admin_client.reconnect_eligibility_me",
				return_value={"recoverable": True, "needs_company": False, "reason": ""},
			),
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			# The two destructive legs are patched out: this test is about the
			# CLAIM. Cumulativity (round-4 MINOR 4) means an L4 must send them, but
			# actually running them here would delete workspace content and revoke
			# LLM config on the real site for no assertion's benefit.
			patch("jarvis.onboarding._wipe_workspace_content"),
			patch("jarvis.onboarding._revoke_llm_connections"),
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				side_effect=AdminUnreachableError("connection reset by peer"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.request_workspace_reset(wipe_data=True, revoke_llm=True, disconnect_after=True)

		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(onboarding._is_preflight(s), "the claim must be left PRE-FLIGHT")
		self.assertTrue(onboarding._reset_in_flight(s), "guards must still see a claim")
		self.assertTrue(s.workspace_reset_claimed_at, "reconcile needs the claim timestamp")

		# Now the reconcile that used to destroy the credentials: the OLD container
		# is up and Ready, because it was never rebuilt.
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Ready",
					"agent_url": "ws://localhost:19000",
					"agent_token": "t",
					"tenant_status": "running",
				},
			),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect") as teardown,
			patch("jarvis.account._bust_chat_gate"),
		):
			out = onboarding.workspace_reset_state()

		teardown.assert_not_called()
		self.assertFalse(out["disconnected"])
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.jarvis_admin_customer_email, "credentials must survive")
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))
		self.assertTrue(onboarding._is_preflight(s), "still unresolved, not silently completed")

	# -- T27: no claim may be left unresolvable --------------------------------
	#
	# The plan's invariant is "no path may leave last_sync_status on a resetting
	# marker nothing can clear". Pre-flight satisfies the safety half (nothing
	# converges on it) but would violate that invariant outright without a
	# resolver: a worker SIGKILLed between the claim and the promote leaves a
	# marker no poll acts on and nothing clears. These three cover every branch.

	def _preflight_claim(self, *, age_minutes=0):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._reset_marker(preflight=True))
		s.db_set(
			"workspace_reset_claimed_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-age_minutes),
		)
		frappe.db.commit()
		return frappe.get_single("Jarvis Settings")

	def test_a_claim_admin_really_accepted_is_promoted(self):
		"""The common case: the bench's HTTP call died but admin went on
		rebuilding. The claim must become convergeable, or the customer's rebuild
		completes on the control plane and this bench never notices."""
		s = self._preflight_claim(age_minutes=1)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			return_value={
				"request": "REQ-1",
				"status": "Applying",
				"requested_at": str(frappe.utils.now_datetime()),
				# Admin measures this on ADMIN's clock; the bench compares it against
				# its own claim's age. Two durations, so no timezone can enter it.
				"requested_age_seconds": 5,
			},
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(onboarding._is_preflight(s), "must be promoted")
		self.assertEqual(s.last_sync_status, onboarding._reset_marker())

	def test_a_settings_save_cannot_clobber_a_claim(self):
		"""Round-5 MAJOR 6. The whole post-lock design rests on the claim being
		durable, but Jarvis Settings.on_update writes its own status to the SAME
		field on every save while pool_mode - and _should_skip_admin_sync requires
		last_sync_status.startswith("ok"), which a claim never satisfies, so the
		skip could never apply while one was held.

		One save from a second tab (save_llm_creds, save_llm_pool, or a System
		Manager on the desk form) destroyed it: a pre-flight claim left
		workspace_reset_claimed_at with nothing to resolve it; a converging one left
		the rebuild unconverged, agent_url blank and chat dead until the daily
		sync_connection.

		All three claim states, because they are written by different code paths and
		only one of them starts with the resetting prefix."""
		for claim in (
			onboarding._reset_marker(preflight=True),
			onboarding._reset_marker(),
			onboarding._DISCONNECTING_STATUS,
		):
			with self.subTest(claim=claim):
				frappe.get_single("Jarvis Settings").db_set("last_sync_status", claim)
				frappe.db.commit()
				s = frappe.get_single("Jarvis Settings")
				s.flags.ignore_mandatory = True
				s.save(ignore_permissions=True)
				frappe.db.commit()
				self.assertEqual(
					frappe.db.get_single_value("Jarvis Settings", "last_sync_status"),
					claim,
					"a Jarvis Settings save destroyed the workspace-operation claim",
				)

	def test_a_claim_does_not_disable_llm_validation_or_the_key_write(self):
		"""Round-6 MAJOR 2, a regression the round-5 MAJOR 6 fix INTRODUCED.

		That guard returned early from on_update itself, which skipped far more than
		the admin sync it was aimed at: validate_models (the app's ONLY call site),
		the derived proxy flags, the legacy models[0] mirror, and the encrypted
		llm_api_key write. save_llm_creds then returned SUCCESS while the credential
		was never stored and an invalid config was never rejected.

		The guard now sits at the two PUSH sites instead. This asserts the local
		bookkeeping still runs while a claim is held - which is what none of the 164
		tests noticed, because they all assert on the sync, never on validation."""
		from jarvis.jarvis.pool_serialize import validate_models

		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._reset_marker(preflight=True))
		frappe.db.commit()

		# validate_models must still REFUSE a bad config while a claim is held.
		s = frappe.get_single("Jarvis Settings")
		s.set(
			"models",
			[{"provider": "OpenAI", "model": "gpt-5.5", "credential_type": "api_key", "enabled": 1}],
		)
		self.assertTrue(
			validate_models(s),
			"an api_key model with no key must not validate - if this passes, the "
			"fixture stopped modelling the case the regression was about",
		)
		with self.assertRaises(frappe.ValidationError):
			s.save(ignore_permissions=True)
		frappe.db.rollback()

		# ...and the claim is still standing afterwards.
		self.assertEqual(
			frappe.db.get_single_value("Jarvis Settings", "last_sync_status"),
			onboarding._reset_marker(preflight=True),
		)

	def test_a_failed_request_does_not_promote_a_claim(self):
		"""Round-5. Promotion means "a rebuild is happening, go converge it", and a
		row that already reached a terminal FAILURE proves the opposite. Promoting
		on one hands the poll a reset that can never complete and, for an L4, a
		marker that eventually authorises clearing the credentials.

		The timestamp alone was the whole test before, so a Failed row promoted."""
		s = self._preflight_claim(age_minutes=1)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			return_value={
				"request": "REQ-1",
				"status": "Failed",
				"requested_at": str(frappe.utils.now_datetime()),
			},
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(onboarding._is_preflight(s), "a Failed request is not evidence of a rebuild")

	def test_the_card_is_not_ready_while_the_claim_is_pre_flight(self):
		"""Round-5. `ready` for an L3/L4 is `agent_url and (Ready or
		_reconnect_llm())`, and _reconnect_llm() matches straight THROUGH the
		pre-flight suffix - so during pre-flight `ready` collapsed to "the OLD
		container is up", which is unconditionally true. GeneralPane.pollReset acts
		on it with a hard reload announcing "Workspace is back", and T33 now starts
		that poll after a timed-out initiate: the very first tick would announce a
		completed reset for one that never started."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._reset_marker(reconnect_llm=True, preflight=True))
		frappe.db.commit()
		with (
			patch("jarvis.onboarding.admin_client.reset_workspace_state", return_value={}),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"chat_readiness": "Configuring", "agent_url": "ws://old:19000"},
			),
		):
			out = onboarding.workspace_reset_state()
		self.assertFalse(out["ready"], "a pre-flight claim must never read as a finished reset")
		self.assertTrue(out["resetting"], "but the card still shows progress")

	def test_a_young_claim_with_no_request_is_left_alone(self):
		"""The call may still be in flight. Deciding now would be guessing, and
		guessing wrong expires a live rebuild's claim."""
		s = self._preflight_claim(age_minutes=1)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			return_value={"request": None, "status": None, "requested_at": None},
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(onboarding._is_preflight(s), "a young claim must not be expired")

	def test_an_expired_claim_with_no_request_is_given_up_honestly(self):
		"""Past the deadline with no matching request the reset never started.
		The marker must not mean "resetting" forever - this is the plan's
		clear-the-marker invariant, and the claim is written before any
		destructive step, so retrying is safe."""
		s = self._preflight_claim(age_minutes=onboarding._PREFLIGHT_EXPIRY_MINUTES + 1)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			return_value={"request": None, "status": None, "requested_at": None},
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(onboarding._reset_in_flight(s), "no marker may outlive its claim")
		self.assertFalse(s.workspace_reset_claimed_at)
		self.assertIn("did not start", s.last_sync_status)

	def test_an_old_request_does_not_promote_a_new_claim(self):
		"""The customer who ran an L1 at 10:00 and an L4 at 10:05. A bare "is
		there a request row" check would promote the new claim on the OLD row and
		converge against a container this reset never rebuilt - which is the
		defect pre-flight exists to stop, re-entered through the resolver."""
		s = self._preflight_claim(age_minutes=onboarding._PREFLIGHT_EXPIRY_MINUTES + 1)
		stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-3)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			return_value={
				"request": "REQ-OLD",
				"status": "Applied",
				"requested_at": str(stale),
				# THREE HOURS old against a claim of ~16 minutes: admin's row is
				# created moments AFTER a genuine claim, so a row far older than the
				# claim provably predates it.
				"requested_age_seconds": 3 * 60 * 60,
			},
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(onboarding._reset_in_flight(s))
		self.assertIn("did not start", s.last_sync_status)

	def test_admin_unreachable_is_not_a_verdict(self):
		""" "Cannot ask" must never be read as "no row" - that would expire a live
		rebuild's claim on a network blip. Leave it; the next tick retries."""
		from jarvis.admin_client import AdminUnreachableError

		s = self._preflight_claim(age_minutes=onboarding._PREFLIGHT_EXPIRY_MINUTES + 1)
		with patch(
			"jarvis.onboarding.admin_client.reset_workspace_state",
			side_effect=AdminUnreachableError("down"),
		):
			onboarding._resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(onboarding._is_preflight(s), "an unanswerable question resolves nothing")

	def test_reconcile_does_not_poll_a_preflight_claim(self):
		"""The */5 backstop must resolve first and only poll a promoted claim.
		Polling a pre-flight marker converges nothing, forever."""
		self._preflight_claim(age_minutes=1)
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"request": None, "status": None, "requested_at": None},
			),
			patch("jarvis.onboarding._workspace_reset_poll") as poll,
		):
			onboarding.reconcile_pending_workspace_reset()
		poll.assert_not_called()

	def test_reconcile_polls_in_the_same_tick_it_promotes(self):
		"""Promotion and convergence in one pass, or every recovered reset waits
		another five minutes for no reason."""
		self._preflight_claim(age_minutes=1)
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={
					"request": "REQ-1",
					"status": "Applying",
					"requested_at": str(frappe.utils.now_datetime()),
					"requested_age_seconds": 5,
				},
			),
			patch("jarvis.onboarding._workspace_reset_poll") as poll,
		):
			onboarding.reconcile_pending_workspace_reset()
		poll.assert_called_once()

	def test_the_card_still_says_resetting_while_pre_flight(self):
		"""Display and convergence are different questions. The customer's card
		must say "resetting" from the moment they click - not from the moment admin
		confirms - or a slow initiate looks like nothing happened."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._reset_marker(preflight=True))
		frappe.db.commit()
		with (
			patch("jarvis.onboarding.admin_client.reset_workspace_state", return_value={}),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"chat_readiness": "Configuring", "agent_url": "ws://x:1"},
			),
		):
			out = onboarding.workspace_reset_state()
		self.assertTrue(out["resetting"])

	def test_a_disconnect_landing_in_the_pre_rebuild_window_is_refused(self):
		"""The race itself, end to end: disconnect_bench is called from INSIDE
		reset_workspace - i.e. after the reset was authorised but before the
		transport teardown that used to be the first thing to write the marker.

		Against pre-T15 code this window let the disconnect straight through.
		Both guards close it here: the redis lock (held by the request) and the
		marker (already claimed)."""
		outcome = {}

		def _reentrant(reason=""):
			try:
				onboarding.disconnect_bench()
				outcome["refused"] = False
			except frappe.ValidationError as exc:
				outcome["refused"] = True
				outcome["why"] = str(exc)
			return {"status": "Applied", "tenant": "t-new"}

		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch("jarvis.onboarding.admin_client.reset_workspace", side_effect=_reentrant),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect") as teardown,
			patch("jarvis.onboarding.admin_client.reconnect_eligibility_me") as elig,
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset()
		self.assertTrue(outcome.get("refused"), outcome)
		# Refused before it could reach either the eligibility precheck or the
		# container teardown, so nothing about the tenancy was touched.
		elig.assert_not_called()
		teardown.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(s.jarvis_admin_customer_email)

	def test_the_guard_sees_a_marker_committed_by_another_connection(self):
		"""Round-4 BLOCKER 1, and the only test here that is not single-transaction.

		Frappe holds ONE MariaDB transaction per request at REPEATABLE READ, so
		every consistent read returns the snapshot pinned by the FIRST read in it.
		``settings.reload()`` is a plain consistent read and therefore CANNOT see
		another worker's commit - which is what the guard has to see. Verified on
		this bench: a second connection's committed write is invisible to a
		re-read, and visible immediately after frappe.db.commit().

		Every other T15 test writes the marker itself, in its own transaction, so
		the guard always sees it and none of them can catch this. This one uses a
		SECOND CONNECTION to model the other worker, which is the whole point.

		Fails against the pre-fix code (`settings.reload()`), passes with
		`_reread_inside_lock`'s commit."""
		from frappe.database import get_db

		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", "ok")
		frappe.db.commit()

		# Pin THIS request's read snapshot before the marker exists, exactly as
		# disconnect_bench's pre-lock read does.
		frappe.get_single("Jarvis Settings")

		marker = onboarding._reset_marker()
		other = get_db(
			socket=frappe.conf.db_socket,
			host=frappe.conf.db_host,
			port=frappe.conf.db_port,
			user=frappe.conf.db_name,
			password=frappe.conf.db_password,
			cur_db_name=frappe.conf.db_name,
		)
		try:
			other.connect()
			other.sql(
				"UPDATE `tabSingles` SET `value`=%s WHERE `doctype`='Jarvis Settings' "
				"AND `field`='last_sync_status'",
				(marker,),
			)
			other.commit()
		finally:
			try:
				other.close()
			except Exception:
				pass

		# A plain re-read still cannot see it - this is the defect, asserted so the
		# test fails loudly if someone "simplifies" _reread_inside_lock back to a
		# reload().
		stale = frappe.get_single("Jarvis Settings")
		stale.reload()
		self.assertEqual(
			stale.get("last_sync_status"),
			"ok",
			"reload() unexpectedly saw the other connection - the isolation "
			"assumption this fix rests on no longer holds; re-derive it",
		)

		fresh = onboarding._reread_inside_lock()
		self.assertEqual(fresh.get("last_sync_status"), marker)
		self.assertTrue(
			onboarding._reset_in_flight(fresh),
			"the in-flight guard must see a marker another worker committed",
		)

	def test_the_poll_will_not_converge_while_a_request_holds_the_lock(self):
		"""The hazard the early claim CREATES, and the reason the poll takes the
		lock too. Between the claim and the rebuild the marker is set while the
		OLD container is still up and Ready - so a */5
		reconcile_pending_workspace_reset landing there would declare the reset
		complete against a container that was never replaced, and for an L4 go on
		to clear the credentials."""
		from jarvis._redis_lock import redis_lock

		s = frappe.get_single("Jarvis Settings")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX)
		s.db_set("agent_url", "")
		frappe.db.commit()
		with (
			redis_lock(onboarding._RESET_LOCK, timeout_s=30) as held,
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Applied"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={
					"chat_readiness": "Ready",
					"agent_url": "ws://localhost:19100",
					"agent_token": "t2",
					"tenant_status": "running",
				},
			),
			patch("jarvis.onboarding.admin_client.prepare_bench_disconnect") as teardown,
			patch("jarvis.account._bust_chat_gate"),
		):
			self.assertTrue(held, "test could not take the lock it is asserting about")
			out = onboarding.workspace_reset_state()
		# Round-4 MINOR 7: NOT reported ready. This assertion used to read "readiness
		# is still REPORTED - it is an observation, not a mutation", which was the
		# defect: GeneralPane.pollReset ACTS on `ready` with stopPoll() and a hard
		# reload announcing "Workspace is back", so a poll landing during a long L4
		# clear reloaded the page mid-clear and that tab never saw the terminal
		# `disconnected: true`. "Ready" is a claim about the workspace being back,
		# and while a claimed reset has not been finished it is not yet true.
		self.assertFalse(out["ready"])
		# The card still says "resetting", so the customer sees progress, not a stall.
		self.assertTrue(out["resetting"])
		# And nothing converged - above all, nothing was cleared.
		self.assertFalse(out["disconnected"])
		teardown.assert_not_called()
		s = frappe.get_single("Jarvis Settings")
		self.assertTrue(onboarding._reset_in_flight(s))
		self.assertTrue(s.jarvis_admin_customer_email)


class TestResetMarkerRoundTrip(FrappeTestCase):
	"""``_reset_marker`` / ``_reset_in_flight``: one definition each, so a
	reset's depth cannot be silently downgraded by two call sites deriving it
	separately. All four (reconnect_llm, disconnect_after) combinations,
	including the combined "(reconnect llm) (disconnect)" case the docstring
	calls out by name."""

	def setUp(self):
		self._snap_status = frappe.get_single("Jarvis Settings").get("last_sync_status") or ""

	def tearDown(self):
		frappe.get_single("Jarvis Settings").db_set("last_sync_status", self._snap_status)
		frappe.db.commit()

	def test_all_four_depth_combinations_round_trip(self):
		s = frappe.get_single("Jarvis Settings")
		cases = (
			(False, False, onboarding._RESETTING_STATUS),
			(True, False, onboarding._RESETTING_RECONNECT_LLM_STATUS),
			(False, True, onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX),
			(True, True, onboarding._RESETTING_RECONNECT_LLM_STATUS + onboarding._DISCONNECT_SUFFIX),
		)
		for reconnect_llm, disconnect_after, expected in cases:
			with self.subTest(reconnect_llm=reconnect_llm, disconnect_after=disconnect_after):
				marker = onboarding._reset_marker(reconnect_llm, disconnect_after)
				self.assertEqual(marker, expected)
				s.db_set("last_sync_status", marker)
				frappe.db.commit()
				self.assertEqual(onboarding._reset_in_flight(s), marker)


class TestDeferredConnectionClear(FrappeTestCase):
	"""T2 (plan edge case 1, the central risk the whole L4 design exists to
	avoid): the CONNECTION clear must be deferred until the poll has observed
	Ready. Nothing in the suite referenced ``disconnect_after``,
	``_DISCONNECT_SUFFIX`` or ``_clear_admin_connection`` before this task.
	The Ready-completes-the-clear half is already covered by
	``test_poll_tears_down_container_before_clearing_connection_on_l4`` above
	(BLOCKER 2's regression test); this covers the other half the "ONLY"
	requires - that a not-ready poll must not clear early."""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status",)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""
		_set_token("tok")
		s.db_set("agent_url", "")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX)
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_marker_survives_and_clear_does_not_fire_while_not_ready(self):
		with (
			patch(
				"jarvis.onboarding.admin_client.reset_workspace_state",
				return_value={"status": "Pending Capacity"},
			),
			patch(
				"jarvis.onboarding.admin_client.get_connection",
				return_value={"chat_readiness": "Configuring"},  # no agent_url yet: not ready
			),
			patch("jarvis.onboarding._clear_admin_connection") as clear,
		):
			out = onboarding.workspace_reset_state()
		clear.assert_not_called()
		self.assertFalse(out["ready"])
		self.assertFalse(out["disconnected"])
		self.assertTrue(out["resetting"])
		s = frappe.get_single("Jarvis Settings")
		# The disconnect intent is still recorded on the marker - the clear has
		# not consumed it, so a later poll can still finish the job.
		self.assertEqual(s.last_sync_status, onboarding._RESETTING_STATUS + onboarding._DISCONNECT_SUFFIX)
		self.assertTrue(s.get_password("jarvis_admin_api_key", raise_exception=False))


class TestDisconnectedMarkerIsNotAResetToConverge(FrappeTestCase):
	"""T11 item 1-3: ``_clear_admin_connection`` writes a DURABLE
	``"disconnected"`` marker (not a blank field) so a reload can tell "lost
	admin credentials" apart from "never onboarded" - and that marker must
	never be mistaken for a reset still in flight, or the */5
	``reconcile_pending_workspace_reset`` backstop would try to poll a bench
	holding no admin credentials to poll with."""

	# Matches TestDisconnectBench's list: test_clear_admin_connection_leaves_the_
	# disconnected_marker below calls the real _clear_admin_connection (CONNECTION
	# spec), which touches all of these on the SAME Jarvis Settings singleton
	# TestDisconnectBench exercises - not just last_sync_status.
	_FIELDS = _SNAPSHOTTED_FIELDS + (
		"chat_device_id",
		"chat_device_public_key",
		"chat_device_private_key",
		"chat_device_token",
		"tenant_authority_handle",
		"tenant_authority_generation",
		"last_sync_status",
	)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_clear_admin_connection_leaves_the_disconnected_marker(self):
		settings = frappe.get_single("Jarvis Settings")
		frappe.db.commit()
		# The teardown is patched, not skipped. Blanking agent_url used to skip it,
		# which is exactly the guard round-4 BLOCKER 2 removed: admin resolves the
		# tenant itself, so the bench's agent_url never had any bearing on whether
		# a container needed tearing down.
		with (
			patch(
				"jarvis.onboarding.admin_client.prepare_bench_disconnect",
				return_value=_TEARDOWN_OK,
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding._clear_admin_connection(settings)
		self.assertEqual(
			frappe.get_single("Jarvis Settings").last_sync_status, onboarding._DISCONNECTED_STATUS
		)

	def test_reset_in_flight_returns_empty_for_the_disconnected_marker(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("last_sync_status", onboarding._DISCONNECTED_STATUS)
		frappe.db.commit()
		self.assertEqual(onboarding._reset_in_flight(settings), "")

	def test_reconcile_does_not_poll_a_disconnected_bench(self):
		frappe.get_single("Jarvis Settings").db_set("last_sync_status", onboarding._DISCONNECTED_STATUS)
		frappe.db.commit()
		with patch("jarvis.onboarding._workspace_reset_poll") as poll:
			onboarding.reconcile_pending_workspace_reset()
		poll.assert_not_called()


class TestBenchConnectionState(FrappeTestCase):
	"""T11 item 4: ``bench_connection_state()`` is the durable,
	admin-round-trip-free answer a reload / second tab / tab reconciled later
	by the */5 backstop falls back to. Its predicate is
	``_admin_connection_absent`` - deliberately NOT
	``last_sync_status == "disconnected"``. That literal string is ALSO
	written by ``jarvis.oauth.api.disconnect`` / ``onboarding.disconnect_llm``
	for an LLM-only disconnect that leaves admin credentials fully intact, so
	testing equality against it would tell a customer who merely unplugged
	their AI model that they need the emailed-code reconnect."""

	_FIELDS = _SNAPSHOTTED_FIELDS + ("last_sync_status",)

	def setUp(self):
		s = frappe.get_single("Jarvis Settings")
		self._snap = _snapshot_settings()
		for f in self._FIELDS:
			if f in self._snap:
				continue
			self._snap[f] = s.get(f) or ""

	def tearDown(self):
		_restore_settings(self._snap)
		frappe.db.commit()

	def test_disconnected_true_when_both_email_and_agent_url_are_empty(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_customer_email", "")
		s.db_set("agent_url", "")
		frappe.db.commit()
		self.assertEqual(onboarding.bench_connection_state(), {"disconnected": True, "needs_company": False})

	def test_disconnected_false_when_marker_says_so_but_credentials_survive(self):
		"""Regression: oauth.api.disconnect() / onboarding.disconnect_llm()
		write the literal "disconnected" to last_sync_status for an LLM-only
		disconnect that leaves admin credentials intact. Without this, a
		customer who merely unplugged their LLM would be told to use the
		emailed-code reconnect - this is the collision T11 found."""
		_set_token("tok")
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_customer_email", "cust-deadbeef@jarvis.invalid")
		s.db_set("agent_url", "ws://localhost:19000")
		s.db_set("last_sync_status", onboarding._DISCONNECTED_STATUS)
		frappe.db.commit()
		self.assertFalse(onboarding.bench_connection_state()["disconnected"])

	def test_disconnected_false_mid_reset(self):
		_set_token("tok")
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_customer_email", "cust-deadbeef@jarvis.invalid")
		s.db_set("agent_url", "")
		s.db_set("last_sync_status", onboarding._RESETTING_STATUS)
		frappe.db.commit()
		self.assertFalse(onboarding.bench_connection_state()["disconnected"])

	def test_makes_no_admin_call(self):
		"""Must work with ZERO credentials - a genuinely disconnected bench
		holds none, so any authenticated admin call would simply fail."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_customer_email", "")
		s.db_set("agent_url", "")
		frappe.db.commit()
		with patch("jarvis.onboarding.admin_client.reconnect_eligibility_me") as elig:
			onboarding.bench_connection_state()
		elig.assert_not_called()

	def test_a_caller_without_jarvis_access_is_refused(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				onboarding.bench_connection_state()
		finally:
			frappe.set_user("Administrator")

	def test_a_non_admin_jarvis_user_gets_an_answer(self):
		"""Round-5 MINOR 4, and the POINT of moving this off require_jarvis_admin.

		OnboardingGate is the screen a disconnected bench lands on for EVERY user,
		and it carries copy telling a non-admin teammate to ask their workspace
		admin. Behind the admin gate this call 403s for exactly that person, the
		catch left `disconnected` false, and the generic first-time-setup poster
		rendered instead — so the branch could never fire in production and its spec
		test passed only by mocking the API.

		The Guest test above cannot show this: Guest is refused under
		require_jarvis_access too, so it passes either way. This one needs a real
		user WITH Jarvis access and WITHOUT System Manager."""
		email = "minor4-jarvis-user@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Minor4",
					"send_welcome_email": 0,
					"roles": [{"role": "Jarvis User"}],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.delete_doc, "User", email, force=True, ignore_permissions=True)
		self.addCleanup(frappe.set_user, "Administrator")

		frappe.set_user(email)
		self.assertFalse(
			"System Manager" in frappe.get_roles(), "fixture must not be an admin, or it proves nothing"
		)
		out = onboarding.bench_connection_state()
		self.assertIn("disconnected", out)
		self.assertIn("needs_company", out)


class TestSignupResumeFallback(FrappeTestCase):
	"""Failed-payment retry against CONTRACT-TRUE mocks.

	The dead end had two independent kills and this class used to encode both:

	1. the resume gate string-matched "already registered or pending"; admin
	   reworded that sentence on 2026-07-26 and the gate stopped matching. The
	   only surviving copy of the old wording in this tree was THIS suite's
	   ``_DUP`` fixture, which is why all four tests stayed green while the
	   feature was dead;
	2. the gate then compared the typed email against
	   ``jarvis_admin_customer_email`` — which holds admin's SYNTHETIC OAuth
	   login ``cust-<hash>@jarvis.invalid``, never a real address. The old mocks
	   returned a real email there, so the fiction that hid this kill lived in
	   the fixtures too.

	These mocks now say what admin says: the duplicate arrives as a CODE (or as
	Frappe's exception class), and ``customer`` is the synthetic login with the
	real address nowhere in Settings."""

	# What admin's _synthetic_login actually mints, and what write_connection
	# stores verbatim as jarvis_admin_customer_email.
	_SYNTHETIC_LOGIN = "cust-9f2b1c4d5e6a@jarvis.invalid"
	# Admin's current duplicate copy. Pinned ONLY to prove nothing branches on
	# it: every test below passes a code or an exc_type, and the one that passes
	# neither must NOT resume.
	_DUP_COPY = "An account for this email and company already exists."

	def setUp(self):
		self._snap = _snapshot_settings()
		s = frappe.get_single("Jarvis Settings")
		_set_token("tok")
		s.db_set("jarvis_admin_url", "https://fleet.example.test")
		# Contract truth: the stored "customer email" is admin's synthetic OAuth
		# login. Comparing a typed address against it can only ever be False.
		s.db_set("jarvis_admin_customer_email", self._SYNTHETIC_LOGIN)
		s.db_set("signup_context", "")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)

	def _signup_raises(self, err):
		return patch("jarvis.onboarding.admin_client.signup", side_effect=err)

	def _duplicate_coded(self):
		"""The RETURNED/THROWN contract shape: a stable code beside exc_type."""
		from jarvis.exceptions import AdminContractError

		return AdminContractError(
			self._DUP_COPY,
			code="ACCOUNT_ALREADY_EXISTS",
			recovery="authenticate_or_reconnect",
			error={
				"code": "ACCOUNT_ALREADY_EXISTS",
				"message": self._DUP_COPY,
				"recovery": "authenticate_or_reconnect",
			},
			exc_type="DuplicateEntryError",
			http_status=409,
		)

	def _duplicate_legacy(self):
		"""A control plane older than the contract: Frappe's exception class and
		a sentence, and no code anywhere."""
		from jarvis.exceptions import AdminValidationError

		return AdminValidationError(
			"An account with this email is already registered or pending.",
			exc_type="DuplicateEntryError",
		)

	def _resume_ok(self, order="order_R2"):
		return patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value={
				"payment_provider": "razorpay",
				"razorpay_order_id": order,
				"code": "PAYMENT_CONFIRMATION_PENDING",
				"attempt_id": "att_7c2",
				"generation": 2,
				# Server truth for the identity: admin's real contact email, NOT
				# the synthetic login and NOT what the wizard typed.
				"email": "owner@acme.example",
				"company": "Acme",
			},
		)

	def test_coded_duplicate_resumes(self):
		"""The headline. A coded duplicate reaches the resume with no prose read
		and no email compared — the retry a declined card needs."""
		with self._signup_raises(self._duplicate_coded()), self._resume_ok() as resume:
			out = onboarding.start_signup("Resume-Me@example.com ", "Co", "some-plan")
		self.assertEqual(resume.call_count, 1)
		self.assertEqual(resume.call_args.args[0], "some-plan")
		self.assertIsNone(resume.call_args.kwargs.get("provider"))
		self.assertIsNone(resume.call_args.kwargs.get("billing"))
		self.assertEqual(out["razorpay_order_id"], "order_R2")

	def test_duplicate_resumes_though_the_stored_login_can_never_match(self):
		"""Kill #2, pinned. Settings hold the synthetic login; the customer types
		a real address; the two are structurally never equal. Possession of the
		credentials is the ownership proof, so the retry still succeeds."""
		stored = frappe.db.get_single_value("Jarvis Settings", "jarvis_admin_customer_email")
		self.assertEqual(stored, self._SYNTHETIC_LOGIN)
		self.assertNotEqual(stored, "resume-me@example.com")
		with self._signup_raises(self._duplicate_coded()), self._resume_ok("order_R9") as resume:
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["razorpay_order_id"], "order_R9")

	def test_any_typed_address_resumes_this_benchs_own_attempt(self):
		"""No caller-supplied identifier chooses the record. Whatever was typed,
		the resume authenticates as THIS bench and admin answers about the
		account those credentials belong to — whose identity the response then
		reports."""
		with self._signup_raises(self._duplicate_coded()), self._resume_ok("order_RX") as resume:
			out = onboarding.start_signup("somebody-completely-else@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["email"], "owner@acme.example")

	def test_legacy_admin_duplicate_still_resumes_on_exc_type(self):
		"""A fleet mid-upgrade: no contract code, only Frappe's exception class.
		Every admin ever deployed throws DuplicateEntryError here, which is why
		the prose fallback could be deleted rather than widened."""
		with self._signup_raises(self._duplicate_legacy()), self._resume_ok("order_R4") as resume:
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["razorpay_order_id"], "order_R4")

	def test_the_duplicate_sentence_alone_never_resumes(self):
		"""The regression that started all of this, inverted: prose is not a
		signal. An error carrying admin's exact duplicate copy but NO machine
		marker must not divert into the resume — that is the branch that rotted
		the moment somebody improved the wording."""
		from jarvis.exceptions import AdminValidationError

		with (
			self._signup_raises(AdminValidationError(self._DUP_COPY)),
			patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume,
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_not_called()

	def test_no_credentials_no_resume(self):
		"""Ownership is proven by credentials, so a bench holding none has
		nothing to prove it with — a wiped site's path is reconnect, not resume."""
		_set_token("")
		with (
			self._signup_raises(self._duplicate_coded()),
			patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume,
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_not_called()

	def test_a_double_submit_reuses_one_idempotency_key(self):
		"""Two clicks, one gateway object: admin returns the intent a key it has
		already seen created, so the second submit must arrive under the SAME
		key. The key is persisted before the call, which is what covers the case
		it exists for — the response that never comes back."""
		with self._signup_raises(self._duplicate_coded()), self._resume_ok() as resume:
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		self.assertEqual(resume.call_count, 2)
		first = resume.call_args_list[0].kwargs["idempotency_key"]
		second = resume.call_args_list[1].kwargs["idempotency_key"]
		self.assertTrue(first)
		self.assertEqual(first, second)

	def test_a_retry_after_a_decline_mints_a_new_key(self):
		"""...and the opposite, because reusing it there is worse than not
		having one: admin would hand back the very intent the gateway refused,
		and the customer could never escape it."""
		with self._signup_raises(self._duplicate_coded()), self._resume_ok() as resume:
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
			first = resume.call_args.kwargs["idempotency_key"]
			# The gateway refuses that intent; the wizard's status check records it.
			onboarding_contract.update(code=onboarding_contract.PAYMENT_DECLINED)
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
			second = resume.call_args.kwargs["idempotency_key"]
		self.assertNotEqual(first, second)

	def test_frappe_really_does_refuse_a_field_the_doctype_lacks(self):
		"""The premise of the guard, asserted against REAL frappe rather than a
		mock — and the two write layers do not agree, which is why the guard
		checks instead of catching:

		  - a READ of a field the installed doctype lacks THROWS
		    (``get_single_value`` looks the field up in meta and refuses);
		  - ``frappe.db.set_single_value`` validates NOTHING and silently writes
		    an orphan ``tabSingles`` row that no migrate cleans up.

		So "just catch the exception" would have left orphan rows behind on every
		bench deployed ahead of its migrate."""
		scratch = "zz_signup_context_probe"
		try:
			with self.assertRaises(Exception):
				frappe.db.get_single_value("Jarvis Settings", scratch)
			frappe.db.set_single_value("Jarvis Settings", scratch, "written-anyway")
			rows = frappe.db.sql(
				"select value from tabSingles where doctype=%s and field=%s", ("Jarvis Settings", scratch)
			)
			self.assertTrue(rows, "set_single_value writes without validating - that is the hazard")
		finally:
			frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": scratch})
			frappe.db.commit()

	def test_an_unmigrated_bench_degrades_and_leaves_no_orphan_row(self):
		"""The deploy window itself: code is live, ``bench migrate`` has not run,
		so the installed doctype has no such field. Everything below is real
		frappe except the one fact that differs in that window.

		Signup must survive it — losing a display snapshot is a bad day, taking
		signup down is a worse one — and it must not write the row it cannot
		legitimately write."""
		before = frappe.db.get_single_value("Jarvis Settings", onboarding_contract.CONTEXT_FIELD)
		real_get_meta = frappe.get_meta

		class _MetaMissingTheField:
			def __init__(self, meta):
				self._meta = meta

			def has_field(self, fieldname):
				if fieldname == onboarding_contract.CONTEXT_FIELD:
					return False
				return self._meta.has_field(fieldname)

			def __getattr__(self, name):
				return getattr(self._meta, name)

		def unmigrated_meta(doctype, *a, **kw):
			meta = real_get_meta(doctype, *a, **kw)
			return _MetaMissingTheField(meta) if doctype == "Jarvis Settings" else meta

		frappe.cache().delete_value(onboarding_contract._MISSING_FIELD_LOG_KEY)
		with (
			patch.object(frappe, "get_meta", unmigrated_meta),
			self._signup_raises(self._duplicate_coded()),
			self._resume_ok("order_RM") as resume,
		):
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["razorpay_order_id"], "order_RM")
		self.assertEqual(
			frappe.db.get_single_value("Jarvis Settings", onboarding_contract.CONTEXT_FIELD),
			before,
			"nothing may be written to a field the installed doctype does not have",
		)

	def test_non_dedup_error_reraises(self):
		from jarvis.exceptions import AdminValidationError

		with (
			self._signup_raises(AdminValidationError("Unsupported payment provider.")),
			patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume,
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_not_called()

	def test_failed_resume_surfaces_the_original_duplicate(self):
		from jarvis.exceptions import AdminValidationError

		with (
			self._signup_raises(self._duplicate_coded()),
			patch(
				"jarvis.onboarding.admin_client.resume_pending_signup",
				side_effect=AdminValidationError("signup is not awaiting payment; nothing to resume"),
			),
			self.assertRaises(frappe.ValidationError) as ctx,
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		self.assertIn("already exists", str(ctx.exception))

	def test_the_fresh_signup_response_carries_no_credentials(self):
		"""start_signup's response is the one that CARRIES the account's
		credentials — api_key, api_secret and, on the flag-off path, the OAuth
		login password. write_connection stores every one of them before this
		returns; none of them belongs in an HTTP response body."""
		with patch(
			"jarvis.onboarding.admin_client.signup",
			return_value={
				"api_key": "k",
				"api_secret": "s",
				"customer": "cust-abc@jarvis.invalid",
				"customer_password": "pw",
				"agent_token": "tok",
				"razorpay_order_id": "order_FRESH",
				"email": "owner@acme.example",
			},
		):
			out = onboarding.start_signup("owner@acme.example", "Acme", "annual")
		for leaked in ("api_key", "api_secret", "customer_password", "agent_token"):
			self.assertNotIn(leaked, out, f"{leaked} must not reach the browser")
		self.assertEqual(out["razorpay_order_id"], "order_FRESH", "the checkout handles survive")
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_api_key", raise_exception=False), "k")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False), "pw")

	def test_the_resumed_signup_response_carries_no_credentials_either(self):
		"""Both paths return through the same statement, and the retry is the one
		a real customer hits."""
		with (
			self._signup_raises(self._duplicate_coded()),
			patch(
				"jarvis.onboarding.admin_client.resume_pending_signup",
				return_value={
					"api_key": "k2",
					"api_secret": "s2",
					"customer_password": "pw2",
					"razorpay_order_id": "order_RESUMED",
				},
			),
		):
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		for leaked in ("api_key", "api_secret", "customer_password", "agent_token"):
			self.assertNotIn(leaked, out)
		self.assertEqual(out["razorpay_order_id"], "order_RESUMED")

	def test_finish_payment_never_hands_back_the_agent_token(self):
		"""Not tidiness: admin's confirm_payment re-serves the connection payload
		for a payment id it has already recorded, so this endpoint is a REPEATABLE
		token read — behind a gate (require_jarvis_admin) that
		grant_onboarding_admin hands to every user who finishes an onboarding."""
		with patch(
			"jarvis.onboarding.admin_client.confirm_payment",
			return_value={
				"agent_url": "ws://container.example",
				"agent_token": "the-container-token",
				"tenant_status": "running",
			},
		):
			out = onboarding.finish_payment({"razorpay_payment_id": "pay_1"})
		self.assertNotIn("agent_token", out)
		# The two fields the SPA actually reads off this response survive.
		self.assertEqual(out["agent_url"], "ws://container.example")
		self.assertEqual(out["tenant_status"], "running")
		# ...and the bench still consumed the token internally.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("agent_token", raise_exception=False), "the-container-token")

	def test_parked_money_stops_the_whole_signup_not_just_the_resume(self):
		"""Refusing only the resume would let a FRESH signup through while a
		payment sits unplaceable — a second account on top of money nobody has
		managed to credit to the first, which is strictly worse than the retry it
		was meant to prevent."""
		onboarding_contract.update(awaiting_manual_reconciliation=True)
		with (
			patch("jarvis.onboarding.admin_client.signup") as signup,
			patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume,
			self.assertRaises(frappe.ValidationError) as ctx,
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		signup.assert_not_called()
		resume.assert_not_called()
		self.assertEqual(
			frappe.local.response.get("error", {}).get("code"),
			onboarding_contract.BENCH_AWAITING_RECONCILIATION,
		)
		self.assertEqual(
			frappe.local.response.get("error", {}).get("recovery"),
			onboarding_contract.RECOVERY_CHECK_STATUS,
		)
		self.assertEqual(
			getattr(type(ctx.exception), "http_status_code", None),
			409,
			"a conflict must reach the wire as 409, which comes off the exception CLASS",
		)

	def test_a_check_clears_the_parked_refusal_for_signup_too(self):
		onboarding_contract.update(awaiting_manual_reconciliation=True)
		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			return_value={"code": "PAYMENT_CONFIRMATION_PENDING", "gateway_consulted": True},
		):
			onboarding.check_signup_payment_status()
		with (
			self._signup_raises(self._duplicate_coded()),
			self._resume_ok("order_AFTER") as resume,
		):
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["razorpay_order_id"], "order_AFTER")

	def test_a_thrown_duplicate_still_delivers_its_code(self):
		"""A throw is how this endpoint reports failure, and the SPA still has to
		branch. The machine-readable error rides on the response next to Frappe's
		exc_type — the same mechanism admin uses, read from the other end."""
		from jarvis.exceptions import AdminValidationError

		with (
			self._signup_raises(self._duplicate_coded()),
			patch(
				"jarvis.onboarding.admin_client.resume_pending_signup",
				side_effect=AdminValidationError("nothing to resume"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		self.assertEqual(frappe.local.response.get("error", {}).get("code"), "ACCOUNT_ALREADY_EXISTS")
		self.assertEqual(frappe.local.response.get("error", {}).get("recovery"), "authenticate_or_reconnect")


class TestOnboardingFacadeEndpoints(FrappeTestCase):
	"""The three surfaces the payment page calls, and the local context they
	keep. Each returns admin's envelope UNFILTERED plus a non-secret context
	block; a failure comes back under a deliberate 4xx/5xx, never as an
	``ok: false`` body wearing a 200."""

	# One realistic passive-poll envelope, shaped exactly like admin's
	# reconcile_pending / state fixtures.
	_STATE = {
		"contract_version": 2,
		"code": "PAYMENT_CONFIRMATION_PENDING",
		"pending_verification": False,
		"payment_provider": "razorpay",
		"amount_inr": 12000.0,
		"plan": {"name": "annual", "label": "Annual"},
		"signup_fee_inr": 0.0,
		"due_today_inr": 12000.0,
		"email": "owner@acme.example",
		"company": "Acme",
		"razorpay_key_id": "rzp_test_X",
		"razorpay_order_id": "order_A1",
		"can_initiate_payment": True,
		"can_check_status": True,
		"can_reconnect": False,
		"attempt_id": "att_7c2",
		"generation": 1,
	}

	def setUp(self):
		self._snap = _snapshot_settings()
		s = frappe.get_single("Jarvis Settings")
		_set_token("tok")
		s.db_set("jarvis_admin_url", "https://fleet.example.test")
		s.db_set("signup_context", "")
		frappe.db.commit()
		frappe.local.response.pop("http_status_code", None)

	def tearDown(self):
		frappe.local.response.pop("http_status_code", None)
		_restore_settings(self._snap)

	def test_state_passes_the_envelope_through_untouched(self):
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=dict(self._STATE, some_future_field="from a newer admin"),
		):
			out = onboarding.get_onboarding_state()
		self.assertTrue(out["ok"])
		self.assertEqual(out["contract_version"], 2)
		self.assertEqual(out["data"]["code"], "PAYMENT_CONFIRMATION_PENDING")
		self.assertTrue(out["data"]["can_initiate_payment"])
		self.assertFalse(out["data"]["can_reconnect"])
		# Additive-only is admin's rule; not filtering is what makes it true here.
		self.assertEqual(out["data"]["some_future_field"], "from a newer admin")

	def test_state_records_the_display_context(self):
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=self._STATE,
		):
			out = onboarding.get_onboarding_state()
		context = out["context"]
		self.assertEqual(context["email"], "owner@acme.example")
		self.assertEqual(context["company"], "Acme")
		self.assertEqual(context["plan"], "annual")
		self.assertEqual(context["plan_label"], "Annual")
		self.assertEqual(context["attempt_id"], "att_7c2")
		self.assertEqual(context["due_today_inr"], 12000.0)
		self.assertEqual(context["code"], "PAYMENT_CONFIRMATION_PENDING")
		# Survives a restart: it is persisted, not assembled per request.
		self.assertEqual(onboarding_contract.load()["attempt_id"], "att_7c2")

	def test_state_persists_the_oauth_password_it_is_handed(self):
		"""Admin delivers the login password on whichever poll first runs after
		the email is confirmed. Whichever surface receives it must persist it or
		the bench never gets bearer auth."""
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=dict(self._STATE, customer_password="pw-once"),
		):
			onboarding.get_onboarding_state()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False), "pw-once")
		self.assertNotIn("customer_password", onboarding_contract.load())

	def test_a_failure_is_a_deliberate_4xx_not_an_ok_false_200(self):
		from jarvis.exceptions import AdminContractError

		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			side_effect=AdminContractError(
				"This signup is already paid. Continue setup.",
				code="PAYMENT_ALREADY_ACTIVE",
				recovery="continue_setup",
				error={
					"code": "PAYMENT_ALREADY_ACTIVE",
					"message": "This signup is already paid. Continue setup.",
					"recovery": "continue_setup",
					"subscription_status": "Active",
				},
				http_status=409,
			),
		):
			out = onboarding.get_onboarding_state()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "PAYMENT_ALREADY_ACTIVE")
		self.assertEqual(out["error"]["recovery"], "continue_setup")
		self.assertEqual(out["error"]["subscription_status"], "Active")
		self.assertEqual(frappe.local.response.http_status_code, 409)

	def test_a_rate_limit_is_not_reported_as_a_decline(self):
		from jarvis.exceptions import AdminRateLimitedError

		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			side_effect=AdminRateLimitedError(
				"We're still checking with your payment provider.",
				retry_after_seconds=30,
				code="PAYMENT_CHECK_RATE_LIMITED",
				recovery="retry",
				error={"code": "PAYMENT_CHECK_RATE_LIMITED", "retry_after_seconds": 30},
			),
		):
			out = onboarding.check_signup_payment_status()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "PAYMENT_CHECK_RATE_LIMITED")
		self.assertEqual(out["error"]["retry_after_seconds"], 30)
		self.assertEqual(frappe.local.response.http_status_code, 429)

	def test_the_check_carries_its_two_extra_facts(self):
		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			return_value=dict(
				self._STATE,
				gateway_consulted=True,
				awaiting_manual_reconciliation=True,
				can_initiate_payment=False,
			),
		):
			out = onboarding.check_signup_payment_status()
		self.assertTrue(out["data"]["gateway_consulted"])
		self.assertTrue(out["data"]["awaiting_manual_reconciliation"])
		self.assertFalse(out["data"]["can_initiate_payment"])

	def test_initiate_uses_the_plan_the_signup_was_started_with(self):
		onboarding_contract.update(plan="annual", payment_provider="cashfree")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			out = onboarding.initiate_signup_payment()
		self.assertTrue(out["ok"])
		self.assertEqual(resume.call_args.args[0], "annual")
		self.assertEqual(resume.call_args.kwargs["provider"], "cashfree")

	def test_initiate_double_click_reuses_one_key(self):
		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			onboarding.initiate_signup_payment()
			first = resume.call_args.kwargs["idempotency_key"]
			onboarding.initiate_signup_payment()
			second = resume.call_args.kwargs["idempotency_key"]
		self.assertTrue(first)
		self.assertEqual(first, second)

	def test_initiate_honours_a_supplied_key_verbatim(self):
		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			onboarding.initiate_signup_payment(idempotency_key="caller-chose-this")
		self.assertEqual(resume.call_args.kwargs["idempotency_key"], "caller-chose-this")

	def test_initiate_with_no_signup_refuses_with_a_code(self):
		"""Nothing local and nothing named: a coded refusal, never a guess.
		Initiating on the wrong plan is a wrong charge."""
		with patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume:
			out = onboarding.initiate_signup_payment()
		resume.assert_not_called()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_NO_SIGNUP_CONTEXT)
		self.assertEqual(frappe.local.response.http_status_code, 409)

	def test_a_refused_initiate_still_updates_what_the_page_renders(self):
		"""A wizard reloading onto "already paid" has to render that, not the
		plan it was about to charge for.

		The CODE and its recovery hint, and nothing else: a refusal is not a state
		read. The fields it happens to carry describe why this call was refused,
		not where the money is, and treating them as a state read is how a
		rate-limit answer would come to overwrite a real decline."""
		from jarvis.exceptions import AdminContractError

		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			side_effect=AdminContractError(
				"This signup is already paid. Continue setup.",
				code="PAYMENT_ALREADY_ACTIVE",
				error={
					"code": "PAYMENT_ALREADY_ACTIVE",
					"message": "This signup is already paid. Continue setup.",
					"recovery": "continue_setup",
					"subscription_status": "Active",
					"attempt_id": "att_7c2",
				},
				http_status=409,
			),
		):
			out = onboarding.initiate_signup_payment()
		self.assertEqual(out["context"]["code"], "PAYMENT_ALREADY_ACTIVE")
		self.assertEqual(out["context"]["recovery"], "continue_setup")
		# The full error object still reaches the page - it just does not become
		# the persisted state.
		self.assertEqual(out["error"]["subscription_status"], "Active")

	# ---- P0-1: credentials must not ride the response back to the browser ----

	def test_the_state_poll_never_returns_the_login_password(self):
		"""Admin delivers the OAuth password on the first poll after the email is
		confirmed. The bench PERSISTS it; the page has no use for it; returning
		admin's dict verbatim put a plaintext login secret in an HTTP response
		body on every verified poll, where it lands in browser caches, devtools
		and any proxy log on the way."""
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=dict(self._STATE, customer_password="pw-once", api_key="k", api_secret="s"),
		):
			out = onboarding.get_onboarding_state()
		for leaked in ("customer_password", "api_key", "api_secret"):
			self.assertNotIn(leaked, out["data"], f"{leaked} must never reach the browser")
		# ...and it was still persisted, which is the point of receiving it.
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False), "pw-once")

	def test_the_provider_check_never_returns_the_login_password(self):
		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			return_value=dict(self._STATE, customer_password="pw-once"),
		):
			out = onboarding.check_signup_payment_status()
		self.assertNotIn("customer_password", out["data"])
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False), "pw-once")

	def test_the_legacy_poll_never_returns_the_login_password(self):
		"""Same leak, older endpoint, still live: this is the one the shipped
		wizard actually calls."""
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=dict(self._STATE, customer_password="pw-once"),
		):
			out = onboarding.check_signup_payment_state()
		self.assertNotIn("customer_password", out)
		self.assertEqual(out["code"], "PAYMENT_CONFIRMATION_PENDING", "the rest of the payload is untouched")
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False), "pw-once")

	# ---- P0-2: a key that cannot work must not be stored ----

	def test_an_unusable_key_is_refused_locally_and_never_persisted(self):
		onboarding_contract.update(plan="annual")
		with patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume:
			out = onboarding.initiate_signup_payment(idempotency_key="x" * 400)
		resume.assert_not_called()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], onboarding_contract.INVALID_REQUEST)
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertNotIn("idempotency_key", onboarding_contract.load())

	def test_an_unusable_key_does_not_brick_the_next_attempt(self):
		"""The self-inflicted brick: the over-long key used to be PERSISTED, admin
		answered INVALID_REQUEST, and every later attempt replayed the same stored
		key into the same refusal with no way out but a settings reset. Three
		identical sends, then a normal one."""
		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			for _ in range(3):
				refused = onboarding.initiate_signup_payment(idempotency_key="x" * 400)
				self.assertEqual(refused["error"]["code"], onboarding_contract.INVALID_REQUEST)
			resume.assert_not_called()
			out = onboarding.initiate_signup_payment()
		self.assertTrue(out["ok"])
		self.assertTrue(onboarding_contract.load()["idempotency_key"])

	def test_a_stored_bad_key_heals_itself_end_to_end(self):
		"""The brick as an older build would leave it: a key admin refuses, ALREADY
		in the context, with no code planted beside it. Nothing here hand-writes
		the verdict — it has to come back from admin, through
		absorb_payment_outcome, and free the next attempt on its own.

		This is the test that failed to be a test the first time: it asserted the
		self-heal from a context state (``code: INVALID_REQUEST``) that no code
		path could actually produce, because INVALID_REQUEST was not absorbable.
		The brick survived underneath a green test."""
		from jarvis.exceptions import AdminContractError

		onboarding_contract.update(plan="annual", idempotency_key="x" * 400)
		self.assertNotIn("code", onboarding_contract.load(), "no verdict may be pre-planted")

		refusal = AdminContractError(
			"idempotency_key must be at most 128 characters",
			code="INVALID_REQUEST",
			recovery="retry",
			error={"code": "INVALID_REQUEST", "message": "idempotency_key must be at most 128 characters"},
			http_status=400,
		)
		with patch("jarvis.onboarding.admin_client.resume_pending_signup", side_effect=refusal) as resume:
			first = onboarding.initiate_signup_payment()
		self.assertEqual(resume.call_args.kwargs["idempotency_key"], "x" * 400, "the stored key was sent")
		self.assertEqual(first["error"]["code"], onboarding_contract.INVALID_REQUEST)
		self.assertEqual(
			onboarding_contract.load()["code"],
			onboarding_contract.INVALID_REQUEST,
			"admin's verdict on the key must be recorded, or the next attempt replays it",
		)

		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			second = onboarding.initiate_signup_payment()
		self.assertNotEqual(
			resume.call_args.kwargs["idempotency_key"], "x" * 400, "the refused key must not be replayed"
		)
		self.assertTrue(second["ok"])

	# ---- P1-2: money an operator is still placing ----

	def test_a_parked_payment_refuses_a_new_intent_without_a_network_call(self):
		"""The gateway holds a payment that could not be credited to this attempt
		and an operator is placing it. The CODE stays the ordinary pending one -
		deliberately, so a wizard does not invite a second payment - which means
		nothing in the code alone stops a retry. The flag does."""
		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			return_value=dict(
				self._STATE,
				gateway_consulted=True,
				awaiting_manual_reconciliation=True,
				can_initiate_payment=False,
			),
		):
			onboarding.check_signup_payment_status()
		with patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume:
			out = onboarding.initiate_signup_payment()
		resume.assert_not_called()
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_AWAITING_RECONCILIATION)
		self.assertEqual(out["error"]["recovery"], onboarding_contract.RECOVERY_CHECK_STATUS)
		self.assertEqual(frappe.local.response.http_status_code, 409)

	def test_a_later_check_clears_the_refusal(self):
		"""Admin sends the flag only when it is TRUE, so an ordinary absorb can
		raise it and nothing could ever lower it - the customer would be refused
		forever over an incident closed weeks ago. The check is the one surface
		entitled to say it is false, and it says so explicitly."""
		onboarding_contract.update(plan="annual", awaiting_manual_reconciliation=True)
		with patch(
			"jarvis.onboarding.admin_client.check_signup_payment_status",
			return_value=dict(self._STATE, gateway_consulted=True),
		):
			onboarding.check_signup_payment_status()
		self.assertFalse(onboarding_contract.awaiting_reconciliation())
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			out = onboarding.initiate_signup_payment()
		resume.assert_called_once()
		self.assertTrue(out["ok"])

	# ---- P1-3 / U1-2: only a payment-state code may become the payment's state ----

	def test_a_decline_confirmed_through_finish_payment_mints_a_fresh_key(self):
		"""Where a refusal is actually LEARNED: the browser comes back from
		checkout and confirm says the gateway refused it. If that verdict does not
		reach the context, the next Pay click reuses the key that bought the
		refused intent - and admin, correctly, hands the dead order back."""
		from jarvis.exceptions import AdminContractError

		onboarding_contract.update(plan="annual", idempotency_key="key-of-the-dead-intent")
		with (
			patch(
				"jarvis.onboarding.admin_client.confirm_payment",
				side_effect=AdminContractError(
					"This Cashfree mandate is not authorized.",
					code="PAYMENT_DECLINED",
					error={"code": "PAYMENT_DECLINED", "message": "This Cashfree mandate is not authorized."},
					http_status=402,
				),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.finish_payment({"provider": "cashfree"})
		self.assertEqual(onboarding_contract.load()["code"], onboarding_contract.PAYMENT_DECLINED)
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		) as resume:
			onboarding.initiate_signup_payment()
		self.assertNotEqual(resume.call_args.kwargs["idempotency_key"], "key-of-the-dead-intent")

	def test_a_backoff_is_not_absorbed_as_the_payments_state(self):
		"""Being told you are asking too often says nothing about the money.
		Absorbing it as the payment's state would overwrite a real decline and,
		worse, let a backoff decide whether the next click opens a gateway
		object."""
		from jarvis.exceptions import AdminRateLimitedError

		onboarding_contract.update(plan="annual", code=onboarding_contract.PAYMENT_DECLINED)
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			side_effect=AdminRateLimitedError(
				"try again shortly",
				retry_after_seconds=30,
				code="PAYMENT_CHECK_RATE_LIMITED",
				error={"code": "PAYMENT_CHECK_RATE_LIMITED"},
			),
		):
			out = onboarding.initiate_signup_payment()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "PAYMENT_CHECK_RATE_LIMITED")
		self.assertEqual(
			onboarding_contract.load()["code"],
			onboarding_contract.PAYMENT_DECLINED,
			"the real payment verdict must survive a rate-limit answer",
		)

	def test_an_unreachable_admin_is_not_absorbed_either(self):
		from jarvis.exceptions import AdminUnreachableError

		onboarding_contract.update(plan="annual", code=onboarding_contract.PAYMENT_CONFIRMATION_PENDING)
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			side_effect=AdminUnreachableError("admin is unreachable"),
		):
			out = onboarding.initiate_signup_payment()
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_ADMIN_UNREACHABLE)
		self.assertEqual(onboarding_contract.load()["code"], onboarding_contract.PAYMENT_CONFIRMATION_PENDING)

	# ---- U0: day one ----

	def test_a_site_that_never_signed_up_is_answered_locally(self):
		"""A bench with no credentials cannot authenticate anything, so the call
		is a guaranteed 401 that the facade would dress up as "admin
		authentication failed; contact support" - shown to somebody whose only
		mistake was opening the page early."""
		_set_token("")
		with patch("jarvis.onboarding.admin_client.get_signup_payment_state") as poll:
			out = onboarding.get_onboarding_state()
		poll.assert_not_called()
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_NO_SIGNUP_CONTEXT)
		self.assertNotIn("support", out["error"]["message"].lower())
		self.assertNotEqual(out["error"]["code"], onboarding_contract.BENCH_ADMIN_AUTH_FAILED)

	def test_the_provider_check_guards_day_one_the_same_way(self):
		_set_token("")
		with patch("jarvis.onboarding.admin_client.check_signup_payment_status") as check:
			out = onboarding.check_signup_payment_status()
		check.assert_not_called()
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_NO_SIGNUP_CONTEXT)

	def test_initiate_guards_cleared_credentials_with_a_surviving_context(self):
		"""The one endpoint that could still reach admin unauthenticated: the plan
		check would pass on a remembered plan, and the call would earn the exact
		401 the day-one guard exists to stop being shown as "contact support"."""
		onboarding_contract.update(plan="annual")
		_set_token("")
		with patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume:
			out = onboarding.initiate_signup_payment()
		resume.assert_not_called()
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_NO_SIGNUP_CONTEXT)
		self.assertNotEqual(out["error"]["code"], onboarding_contract.BENCH_ADMIN_AUTH_FAILED)

	# ---- P2: the bookkeeping must not cost more than it is worth ----

	def test_a_steady_poll_writes_nothing(self):
		"""Every answer carries a fresh payment_last_checked_at, so comparing
		whole dicts made a wizard polling every two seconds rewrite the Single on
		every tick - and every write clears the document cache for every other
		request on the site."""
		with patch(
			"jarvis.onboarding.admin_client.get_signup_payment_state",
			return_value=dict(self._STATE, payment_last_checked_at="2026-08-02 00:00:00.000000"),
		):
			onboarding.get_onboarding_state()
		with patch.object(onboarding_contract, "save", wraps=onboarding_contract.save) as save:
			for i in range(5):
				with patch(
					"jarvis.onboarding.admin_client.get_signup_payment_state",
					return_value=dict(self._STATE, payment_last_checked_at=f"2026-08-02 00:00:0{i}.000000"),
				):
					onboarding.get_onboarding_state()
			self.assertEqual(save.call_count, 0, "a steady state must cost no writes")
			# ...and something real still writes.
			with patch(
				"jarvis.onboarding.admin_client.get_signup_payment_state",
				return_value=dict(self._STATE, code="PAYMENT_DECLINED"),
			):
				onboarding.get_onboarding_state()
			self.assertEqual(save.call_count, 1)

	def test_save_sweeps_credentials_out_of_a_raw_payload(self):
		"""The allowlist in absorb() is the first line; this is the last. A future
		caller that hands the store a raw admin payload must fail safe, not park
		an api_secret in a plain-text Settings column."""
		onboarding_contract.save(
			{
				"email": "owner@acme.example",
				"api_key": "k",
				"api_secret": "s",
				"customer_password": "pw",
				"agent_token": "t",
				"customer": "cust-abc@jarvis.invalid",
			}
		)
		raw = frappe.db.get_single_value("Jarvis Settings", onboarding_contract.CONTEXT_FIELD) or ""
		for leaked in ("api_key", "api_secret", "customer_password", "agent_token", "cust-abc"):
			self.assertNotIn(leaked, raw)
		self.assertIn("owner@acme.example", raw)

	def test_a_rejected_plan_does_not_become_the_sticky_default(self):
		"""A REQUESTED plan is not a confirmed one. Writing it before the call
		meant a plan admin refused (disabled, zero-priced, a gateway switched off)
		became this site's default, and the next Pay click charged on it."""
		from jarvis.exceptions import AdminValidationError

		with (
			patch(
				"jarvis.onboarding.admin_client.signup",
				side_effect=AdminValidationError("This plan has no payable amount."),
			),
			self.assertRaises(frappe.ValidationError),
		):
			onboarding.start_signup("owner@acme.example", "Acme", "a-plan-admin-refuses")
		self.assertNotIn("plan", onboarding_contract.load())
		with patch("jarvis.onboarding.admin_client.resume_pending_signup") as resume:
			out = onboarding.initiate_signup_payment()
		resume.assert_not_called()
		self.assertEqual(out["error"]["code"], onboarding_contract.BENCH_NO_SIGNUP_CONTEXT)

	def test_the_context_never_leaves_the_idempotency_key_on_the_wire(self):
		onboarding_contract.update(plan="annual")
		with patch(
			"jarvis.onboarding.admin_client.resume_pending_signup",
			return_value=self._STATE,
		):
			out = onboarding.initiate_signup_payment()
		self.assertNotIn("idempotency_key", out["context"])
		self.assertTrue(onboarding_contract.load()["idempotency_key"])


class TestAccountReconnect(FrappeTestCase):
	"""Fresh-bench reconnect wrappers (admin_client mocked)."""

	def setUp(self):
		self._snap = _snapshot_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_url", "https://fleet.example.test")
		frappe.db.commit()

	def tearDown(self):
		_restore_settings(self._snap)

	def test_start_proxies_request(self):
		with patch(
			"jarvis.onboarding.admin_client.request_account_reconnect",
			return_value={"request": "rid-1", "message": "check your email"},
		) as req:
			out = onboarding.start_account_reconnect("someone@example.com", "Acme")
		req.assert_called_once_with("someone@example.com", "Acme")
		self.assertEqual(out["request"], "rid-1")

	def test_check_pending_writes_nothing(self):
		_set_token("")
		with patch(
			"jarvis.onboarding.admin_client.get_reconnect_state",
			return_value={"status": "pending"},
		):
			out = onboarding.check_account_reconnect("rid-1")
		self.assertEqual(out["status"], "pending")
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("jarvis_admin_api_key", raise_exception=False))

	def test_check_ready_persists_rotated_credentials(self):
		_set_token("")
		with patch(
			"jarvis.onboarding.admin_client.get_reconnect_state",
			return_value={
				"status": "ready",
				"api_key": "new-key",
				"api_secret": "new-secret",
				"customer": "someone@example.com",
				"customer_password": "new-pass",
			},
		):
			out = onboarding.check_account_reconnect("rid-1")
		self.assertEqual(out["status"], "connected")
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.get_password("jarvis_admin_api_key", raise_exception=False), "new-key")
		self.assertEqual(s.jarvis_admin_customer_email, "someone@example.com")

	def test_check_awaiting_code_writes_nothing(self):
		"""Confirmed but not yet unlocked: the code binds delivery to whoever
		clicked, so nothing is persisted until it matches."""
		_set_token("")
		with patch(
			"jarvis.onboarding.admin_client.get_reconnect_state",
			return_value={"status": "awaiting_code"},
		):
			out = onboarding.check_account_reconnect("rid-1")
		self.assertEqual(out["status"], "awaiting_code")
		s = frappe.get_single("Jarvis Settings")
		self.assertFalse(s.get_password("jarvis_admin_api_key", raise_exception=False))

	def test_check_passes_code_through(self):
		with patch(
			"jarvis.onboarding.admin_client.get_reconnect_state",
			return_value={"status": "awaiting_code"},
		) as poll:
			onboarding.check_account_reconnect("rid-1", "ABCD2345")
		poll.assert_called_once_with("rid-1", "ABCD2345")

	def test_check_expired_passthrough(self):
		with patch(
			"jarvis.onboarding.admin_client.get_reconnect_state",
			return_value={"status": "expired"},
		):
			out = onboarding.check_account_reconnect("rid-x")
		self.assertEqual(out["status"], "expired")


class TestCredentialChangeBustsTheTokenCache(FrappeTestCase):
	"""A cached bearer outlives the credentials it was minted from. Reconnecting a
	bench onto another account left it calling admin as the PREVIOUS customer -
	which reports no subscription and no tenant, so the wizard sat on "still being
	set up" while everything server-side looked healthy."""

	def setUp(self):
		from jarvis import admin_client

		frappe.cache().set_value(
			admin_client._OAUTH_CACHE_KEY,
			{"access_token": "stale-from-the-old-account", "access_expires_at": 9999999999},
		)

	def tearDown(self):
		from jarvis import admin_client

		frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)

	def _cached(self):
		from jarvis import admin_client

		return frappe.cache().get_value(admin_client._OAUTH_CACHE_KEY)

	def test_writing_a_new_login_drops_the_cached_token(self):
		onboarding.write_connection({"customer": "cust-newaccount@jarvis.invalid"})
		self.assertIsNone(self._cached())

	def test_writing_a_new_password_drops_the_cached_token(self):
		onboarding.write_connection({"customer_password": "s3cret"})
		self.assertIsNone(self._cached())

	def test_writing_api_keys_drops_the_cached_token(self):
		onboarding.write_connection({"api_key": "k", "api_secret": "s"})
		self.assertIsNone(self._cached())

	def test_a_payload_with_no_credentials_leaves_it_alone(self):
		onboarding.write_connection({"agent_url": "ws://127.0.0.1:19999"})
		self.assertIsNotNone(self._cached(), "an agent_url refresh is not a credential change")

	def test_reset_onboarding_drops_it_too(self):
		from jarvis.dev import reset_onboarding

		reset_onboarding()
		self.assertIsNone(self._cached())
