"""Tests for jarvis.onboarding sync + wrappers (admin_client mocked)."""

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
			patch(
				"jarvis.onboarding.admin_client.reset_workspace",
				return_value={"status": "Applied", "tenant": "t-new"},
			),
			patch("jarvis.account._bust_chat_gate"),
		):
			onboarding.request_workspace_reset(revoke_llm=True)
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

	def test_an_unmigrated_bench_degrades_instead_of_breaking(self):
		"""Between a code deploy and its ``bench migrate`` the context field does
		not exist yet, and Frappe REFUSES both reads and writes of a field its
		installed doctype has never heard of. Signup must survive that window:
		losing a display snapshot is a bad day, taking signup down is a worse
		one."""
		import frappe.model.document as document_module

		real_get = frappe.db.get_single_value
		real_db_set = document_module.Document.db_set

		def unmigrated_get(doctype, fieldname, *a, **kw):
			if doctype == "Jarvis Settings" and fieldname == onboarding_contract.CONTEXT_FIELD:
				frappe.throw("Field signup_context does not exist on Jarvis Settings")
			return real_get(doctype, fieldname, *a, **kw)

		def unmigrated_db_set(self, fieldname, value=None, *a, **kw):
			if fieldname == onboarding_contract.CONTEXT_FIELD:
				frappe.throw("Field signup_context does not exist on Jarvis Settings")
			return real_db_set(self, fieldname, value, *a, **kw)

		with (
			patch.object(frappe.db, "get_single_value", unmigrated_get),
			patch.object(document_module.Document, "db_set", unmigrated_db_set),
			self._signup_raises(self._duplicate_coded()),
			self._resume_ok("order_RM") as resume,
		):
			out = onboarding.start_signup("resume-me@example.com", "Co", "some-plan")
		resume.assert_called_once()
		self.assertEqual(out["razorpay_order_id"], "order_RM")

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
		plan it was about to charge for."""
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
					"subscription_status": "Active",
					"attempt_id": "att_7c2",
				},
				http_status=409,
			),
		):
			out = onboarding.initiate_signup_payment()
		self.assertEqual(out["context"]["code"], "PAYMENT_ALREADY_ACTIVE")
		self.assertEqual(out["context"]["subscription_status"], "Active")

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
