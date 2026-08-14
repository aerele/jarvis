"""Tests for Jarvis Settings on_update classification + admin dispatch.

`Jarvis Settings` is a Single - there's exactly one row in the whole DB,
shared by tests and the live UI. `_SettingsSingletonTestCase` snapshots
the pre-test field values in setUpClass and restores them in
tearDownClass so the suite leaves no footprint on the user's real
credentials.

Post-unification (2026-05-29): on_update always dispatches via
`_sync_via_admin` - there is no longer a bench-local push shortcut. All
tests mock `jarvis.admin_client.*` accordingly.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

LLM_BASELINE = {
	"llm_provider": "Moonshot (Kimi)",
	"llm_model": "kimi-k2.6",
	"llm_api_key": "sk-original-1234",
	"llm_base_url": "",
}

# Plain-text fields the tests overwrite. Snapshotted via settings.get(...).
_SNAPSHOT_PLAIN_FIELDS = (
	"llm_provider",
	"llm_model",
	"llm_base_url",
	"llm_auth_mode",
	"last_sync_status",
	"last_sync_at",
	# Establishedness + apply-timing markers (jarvis C2). is_ready_for_chat now
	# consults these at its provisioning exits, so a test that stamps them must
	# not leave them set for the next class.
	"chat_was_ready_at",
	"chat_ready_authority",
	"last_sync_requested_at",
)

# Password fields the tests overwrite. Snapshotted via get_password() because
# settings.get(field) returns the masked "*****" string for these.
_SNAPSHOT_PASSWORD_FIELDS = (
	"llm_api_key",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
)


def _reset_settings():
	"""Reset settings to a known baseline. Seeds admin credentials so
	`_sync_via_admin` doesn't fail with `AdminAuthError` (callers mock
	the admin_client HTTP call separately).

	Also clears the unified-config pool state (models child rows + preset):
	these tests assert the LEGACY single-model routing, and a leftover pool
	row — from the v1_seed_llm_models migration on a configured site, or from
	another module's pool tests — flips on_update onto the pool-sync path and
	the legacy assertions fail order-dependently."""
	frappe.db.delete("Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"})
	settings = frappe.get_single("Jarvis Settings")
	base = {
		**LLM_BASELINE,
		"jarvis_admin_url": "http://127.0.0.1:8000",
		"jarvis_admin_api_key": "test-admin-key",
		"jarvis_admin_api_secret": "test-admin-secret",
		"last_sync_status": "",
		"last_sync_at": None,
		"preset": "",
		"proxy_active": 0,
	}
	for field, value in base.items():
		settings.db_set(field, value)
	frappe.db.commit()


class _SettingsSingletonTestCase(FrappeTestCase):
	"""Snapshots the Jarvis Settings singleton's pre-test state and restores
	it in tearDownClass so running the suite leaves no footprint."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		snapshot: dict[str, object] = {f: settings.get(f) for f in _SNAPSHOT_PLAIN_FIELDS}
		for f in _SNAPSHOT_PASSWORD_FIELDS:
			snapshot[f] = settings.get_password(f, raise_exception=False) or ""
		cls._jarvis_settings_snapshot = snapshot

	@classmethod
	def tearDownClass(cls):
		try:
			settings = frappe.get_single("Jarvis Settings")
			for field, value in cls._jarvis_settings_snapshot.items():
				settings.db_set(field, value)
			frappe.db.commit()
		finally:
			super().tearDownClass()

	def setUp(self):
		super().setUp()
		# Reset the establishedness + apply-timing markers before every test. The
		# class snapshot restores them only at tearDownClass, not between tests, and
		# is_ready_for_chat now reads them at its provisioning exits (jarvis C2): a
		# sibling case that stamps chat_was_ready_at (an established tenant) would
		# otherwise leave a later fresh-tenant case reading as established, flipping
		# its hard llm_pool_provisioning / llm_provisioning assertion to llm_applying.
		# Every test that needs establishedness sets it in its own body, so clearing
		# here only removes leaked state, never state a test set for itself.
		for _f in ("chat_was_ready_at", "chat_ready_authority", "last_sync_requested_at"):
			frappe.db.set_value("Jarvis Settings", "Jarvis Settings", _f, None, update_modified=False)
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")


class TestOnUpdateClassification(_SettingsSingletonTestCase):
	"""Classifier-output tests. The dispatcher always routes to
	`_sync_via_admin`; we mock that to verify the correct action arg."""

	def setUp(self):
		super().setUp()
		_reset_settings()

	def test_no_change_is_noop(self):
		settings = frappe.get_single("Jarvis Settings")
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_not_called()

	def test_force_admin_sync_flag_triggers_restart_even_without_diff(self):
		"""Save_llm_creds(force=True) sets flags.force_admin_sync. With no
		field change, the no-diff classifier would normally return None
		and skip the push, leaving the container with the previous (and
		in re-authorize cases, broken) auth state. The flag forces
		'restart' so admin re-renders openclaw.json + restarts the
		container. Verified-live failure mode 2026-06-11."""
		settings = frappe.get_single("Jarvis Settings")
		settings.flags.force_admin_sync = True
		# No structural field changes.
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_only_key_change_triggers_reload(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-new-key-9999"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("reload")

	def test_provider_change_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_model_only_change_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_model = "kimi-k2.5"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_base_url_change_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_base_url = "https://custom.example.com/v1"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_key_and_provider_change_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-anthropic-key"
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_temperature_change_is_noop(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_temperature = 0.5
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_not_called()

	def test_max_output_tokens_change_is_noop(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_max_output_tokens = 8192
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_not_called()


class TestOnUpdateAlwaysAdminPath(_SettingsSingletonTestCase):
	"""Unified architecture: on_update always uses `_sync_via_admin`,
	regardless of whether the admin api_key is configured. If admin isn't
	configured, the call fails with AdminAuthError (which we don't mock -
	the test only verifies the path is taken)."""

	def setUp(self):
		super().setUp()
		_reset_settings()

	def test_on_update_routes_to_admin(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-rotated"
		with patch(
			"jarvis.admin_client.post_rotate_llm_secret", return_value={"action": "reload"}
		) as admin_mock:
			settings.save()
		admin_mock.assert_called_once()


class TestOnUpdateStatus(_SettingsSingletonTestCase):
	def setUp(self):
		super().setUp()
		_reset_settings()

	def test_records_ok_reload_via_admin_on_success(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-new"
		with patch("jarvis.admin_client.post_rotate_llm_secret", return_value={"action": "reload"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_sync_status, "ok (reload via admin)")
		self.assertIsNotNone(settings.last_sync_at)

	def test_records_ok_restart_via_admin_on_success(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_sync_status, "ok (restart via admin)")

	def test_records_failure_when_admin_unreachable(self):
		from jarvis.exceptions import AdminUnreachableError

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-new"
		with patch(
			"jarvis.admin_client.post_rotate_llm_secret", side_effect=AdminUnreachableError("network is down")
		):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertIn("failed", settings.last_sync_status or "")
		self.assertIn("admin unreachable", settings.last_sync_status or "")

	# jarvis #542: on a "restart" the two failures below arrive as the SAME
	# exception class off the wire (admin answers a gateway fault and a config
	# refusal with the same 502), and they must end in opposite states - one
	# waits for the admin reconcile, the other can only ever be refused again.

	def test_transient_unreachable_on_restart_records_pending(self):
		"""F2, unchanged: admin persists a creds re-render desired-first and
		reconciles a late apply, so a timeout is pending, never a lost change."""
		from jarvis.exceptions import AdminUnreachableError

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with (
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=AdminUnreachableError("read timeout"),
			),
			patch("jarvis.admin_client.get_connection", return_value={"chat_readiness": "Configuring"}),
		):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.last_sync_status, "pending: admin applying config")

	def test_permanent_rejection_on_restart_records_failed_with_the_reason(self):
		"""admin threw during validation and persisted NOTHING, so there is no
		desired state for the */5 reconcile to converge to - "pending" would
		read as "Applying your changes" forever. Terminal, carrying admin's own
		reason, and get_connection is never even consulted."""
		from jarvis.exceptions import AdminRejectedError

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with (
			patch(
				"jarvis.admin_client.post_update_llm_creds",
				side_effect=AdminRejectedError(
					"admin returned a 502 error: unknown llm_provider: 'gemini'",
					code="FleetConfigError",
					detail="unknown llm_provider: 'gemini'",
				),
			),
			patch("jarvis.admin_client.get_connection") as get_conn,
		):
			settings.save()
			get_conn.assert_not_called()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.last_sync_status,
			"failed: Your AI configuration was rejected: unknown llm_provider: 'gemini'",
		)
		# last_sync_at stayed NULL on the pending path, which left support with
		# no signal either - a terminal verdict always stamps it.
		self.assertIsNotNone(settings.last_sync_at)

	def test_save_succeeds_even_when_admin_call_fails(self):
		from jarvis.exceptions import AdminUnreachableError

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-persisted"
		with patch("jarvis.admin_client.post_rotate_llm_secret", side_effect=AdminUnreachableError("boom")):
			try:
				settings.save()
			except Exception:
				self.fail("save() should not raise when admin call fails")
		settings = frappe.get_single("Jarvis Settings")
		# Password field still persisted.
		self.assertEqual(settings.get_password("llm_api_key"), "sk-persisted")


class TestValidateAuthMode(_SettingsSingletonTestCase):
	"""validate() requires the right credential per auth mode.

	REV-1: only api_key mode requires a bench-side credential. The
	subscription modes (oauth / legacy subscription) keep credentials on
	the container, so the bench's validate() has no check for them."""

	@classmethod
	def tearDownClass(cls):
		# Tests here mutate models/preset/oauth signals the base snapshot
		# doesn't cover; clear them so this class leaves no footprint on the
		# shared Jarvis Settings singleton (test-pollution guard).
		frappe.db.delete(
			"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
		)
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("llm_oauth_connected_at", None, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		from frappe.utils.password import remove_encrypted_password

		_reset_settings()
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key")
		# Start each test from a clean LEGACY-direct state: no models rows, no
		# preset, no oauth connection (the base snapshot doesn't cover these).
		frappe.db.delete(
			"Jarvis LLM Pool Model", {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"}
		)
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		settings.db_set("preset", "", update_modified=False)
		settings.db_set("llm_oauth_connected_at", None, update_modified=False)
		frappe.db.commit()

	def test_api_key_mode_requires_api_key(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "api_key"
		settings.llm_api_key = ""
		settings.llm_provider = "OpenAI"
		settings.llm_model = "gpt-4o-mini"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_api_key_mode_with_api_key_passes(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_api_key", "sk-xyz", update_modified=False)
		frappe.db.commit()
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "api_key"
		settings.llm_provider = "OpenAI"
		settings.llm_model = "gpt-4o-mini"
		settings.validate()  # should not raise

	def test_oauth_mode_no_bench_credential_required(self):
		"""REV-1: oauth mode credentials live on the container. The bench's
		validate() doesn't require any bench-side credential."""
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "oauth"
		settings.llm_provider = "OpenAI"
		settings.llm_model = "gpt-4o"
		settings.validate()  # should not raise

	def test_legacy_subscription_mode_no_bench_credential_required(self):
		"""Migrated tenants on the legacy 'subscription' value are treated
		the same as oauth - no bench-side credential required."""
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "subscription"
		settings.llm_provider = "OpenAI"
		settings.llm_model = "gpt-4o"
		settings.validate()  # should not raise

	def test_unconfigured_fresh_settings_does_not_require_api_key(self):
		"""Fresh/pre-onboarding Settings - no models, no preset, no direct
		model/base_url, no connected oauth account - must validate even though
		llm_auth_mode DEFAULTS to 'api_key' with no key set.

		Regression for the fresh-start save deadlock: on a brand-new site the
		customer must be able to save Jarvis Settings (e.g. to set an unrelated
		field) BEFORE onboarding configures an LLM. The credential is enforced
		the moment LLM is actually configured (see the api_key/oauth tests
		above)."""
		settings = frappe.get_single("Jarvis Settings")
		settings.set("models", [])
		settings.preset = ""
		settings.llm_auth_mode = "api_key"
		settings.llm_api_key = ""
		settings.llm_provider = ""
		settings.llm_model = ""
		settings.llm_base_url = ""
		settings.llm_oauth_connected_at = None
		settings.validate()  # must NOT raise - nothing is configured yet

	def test_legacy_direct_base_url_without_key_still_raises(self):
		"""A real legacy-direct api_key config (custom base_url set) with the
		key cleared must still raise even when llm_model is blank - it is a
		configured tenant, not a fresh/unconfigured site. Guards the boundary
		so narrowing the fresh-start gate doesn't let a keyless credential push
		through (review finding: keyless-direct bypass)."""
		settings = frappe.get_single("Jarvis Settings")
		settings.set("models", [])
		settings.preset = ""
		settings.llm_auth_mode = "api_key"
		settings.llm_api_key = ""
		settings.llm_provider = "Custom"
		settings.llm_model = ""
		settings.llm_base_url = "https://llm.internal.example.com/v1"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_models_table_present_skips_flat_api_key_check(self):
		"""When the models table drives config, validate_models() (in
		on_update) owns credential validation; the flat llm_* guard must NOT
		fire against the derived mirror. A models row present (even disabled,
		which before_validate won't mirror) with a stale/empty legacy
		llm_api_key + api_key mode must not raise from validate() - otherwise
		disabled-only tables / decrypt errors produce spurious throws that
		re-block saves (review findings: disabled-only, decrypt-error)."""
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "api_key"
		settings.llm_api_key = ""
		settings.append(
			"models",
			{
				"provider": "openai_compat",
				"model": "gpt-4o",
				"credential_type": "api_key",
				"api_key": "",
				"tier": "strong",
				"order": 0,
				"enabled": 0,
			},
		)
		settings.validate()  # must NOT raise the flat 'requires llm_api_key'


class TestClassifyAuthModeSwitch(_SettingsSingletonTestCase):
	"""Mode-switch is structural - always triggers restart."""

	def setUp(self):
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		frappe.db.commit()

	def test_api_key_to_oauth_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "oauth"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")

	def test_oauth_to_api_key_triggers_restart(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		frappe.db.commit()
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_auth_mode = "api_key"
		with patch.object(type(settings), "_sync_via_admin") as sync_mock:
			settings.save()
		sync_mock.assert_called_once_with("restart")


class TestResolveLlmSecretForPush(_SettingsSingletonTestCase):
	"""REV-1: _resolve_llm_secret_for_push returns llm_api_key only.
	Oauth credentials live on the container in auth-profiles.json, not
	in any Jarvis Settings password field."""

	def setUp(self):
		from frappe.utils.password import remove_encrypted_password

		_reset_settings()
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key")
		frappe.db.set_single_value("Jarvis Settings", "llm_api_key", None)
		frappe.db.commit()

	def test_api_key_mode_returns_api_key(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		settings.db_set("llm_api_key", "sk-1", update_modified=False)
		frappe.db.commit()
		settings.reload()
		self.assertEqual(settings._resolve_llm_secret_for_push(), "sk-1")

	def test_oauth_mode_returns_empty(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		frappe.db.commit()
		settings.reload()
		self.assertEqual(settings._resolve_llm_secret_for_push(), "")


class TestSyncViaAdminDispatch(_SettingsSingletonTestCase):
	"""_sync_via_admin dispatches to the right admin endpoint per action."""

	def setUp(self):
		_reset_settings()

	@patch("jarvis.admin_client.post_rotate_llm_secret")
	@patch("jarvis.admin_client.post_update_llm_creds")
	def test_reload_action_calls_rotate_llm_secret(self, mock_update, mock_rotate):
		mock_rotate.return_value = {"action": "reload"}
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_api_key", "sk-1", update_modified=False)
		frappe.db.commit()
		settings.reload()
		settings._sync_via_admin("reload")
		mock_rotate.assert_called_once_with(secret="sk-1")
		mock_update.assert_not_called()

	@patch("jarvis.admin_client.post_rotate_llm_secret")
	@patch("jarvis.admin_client.post_update_llm_creds")
	def test_restart_api_key_calls_update_with_api_key_mode(self, mock_update, mock_rotate):
		mock_update.return_value = {"action": "restart"}
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		settings.db_set("llm_api_key", "sk-1", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		frappe.db.commit()
		settings.reload()
		settings._sync_via_admin("restart")
		kw = mock_update.call_args.kwargs
		self.assertEqual(kw["auth_mode"], "api_key")
		self.assertEqual(kw["api_key"], "sk-1")
		mock_rotate.assert_not_called()

	@patch("jarvis.admin_client.post_rotate_llm_secret")
	@patch("jarvis.admin_client.post_update_llm_creds")
	def test_restart_oauth_threads_auth_mode(self, mock_update, mock_rotate):
		"""Oauth-mode restart still calls post_update_llm_creds; admin ignores
		api_key in oauth mode (credentials live in auth-profiles.json on the
		container). The bench just threads auth_mode through."""
		from frappe.utils.password import remove_encrypted_password

		mock_update.return_value = {"action": "restart"}
		# Clear llm_api_key so the wire payload reflects an unconfigured
		# api-key field (oauth mode tenants never set llm_api_key).
		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key")
		frappe.db.set_single_value("Jarvis Settings", "llm_api_key", None)
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		frappe.db.commit()
		settings.reload()
		settings._sync_via_admin("restart")
		kw = mock_update.call_args.kwargs
		self.assertEqual(kw["auth_mode"], "oauth")
		self.assertEqual(kw["api_key"], "")
		mock_rotate.assert_not_called()

	@patch("jarvis.admin_client.post_rotate_llm_secret")
	def test_rate_limited_writes_terminal_failure_status(self, mock_rotate):
		"""Sprint-3 (2026-06-16 review): rate-limit USED to be silently
		swallowed - last_sync_status stayed at 'pending:' forever and
		the UI poller spun on it indefinitely. Now we write a terminal
		failure status with the admin-provided retry_after hint so the
		UI can render a clean retry timer."""
		from jarvis.exceptions import AdminRateLimitedError

		mock_rotate.side_effect = AdminRateLimitedError(retry_after_seconds=600)
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_api_key", "sk-1", update_modified=False)
		frappe.db.commit()
		settings.reload()
		# Should not raise.
		settings._sync_via_admin("reload")
		# last_sync_status flips to a terminal-failure shape that
		# carries the retry hint.
		settings.reload()
		self.assertIn("failed", settings.last_sync_status or "")
		self.assertIn("rate-limited", settings.last_sync_status or "")
		self.assertIn("600", settings.last_sync_status or "")
		# last_sync_at gets bumped so the UI knows when the status was set.
		self.assertIsNotNone(settings.last_sync_at)

	@patch("jarvis.admin_client.post_rotate_llm_secret")
	def test_admin_auth_error_recorded_in_status(self, mock_rotate):
		from jarvis.exceptions import AdminAuthError

		mock_rotate.side_effect = AdminAuthError("not onboarded")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_api_key", "sk-1", update_modified=False)
		frappe.db.commit()
		settings.reload()
		settings._sync_via_admin("reload")
		settings.reload()
		self.assertIn("failed", settings.last_sync_status or "")
		self.assertIn("auth", settings.last_sync_status or "")


class TestOnUpdateAsyncPending(_SettingsSingletonTestCase):
	"""The async path writes ``last_sync_status = 'pending: ...'``
	synchronously, then enqueues the admin call. In tests we normally run
	the enqueue inline (via ``frappe.flags.in_test``), so the final status
	overwrites the pending marker. This class explicitly stops the inline
	behavior to verify the intermediate "pending:" write happens BEFORE
	``_sync_via_admin`` fires."""

	def setUp(self):
		super().setUp()
		_reset_settings()

	def test_pending_status_written_before_enqueue_for_reload(self):
		seen = []

		def capture_pending(*args, **kwargs):
			# Called from _enqueued_sync_via_admin via frappe.enqueue(now=True).
			# By the time this runs, the synchronous "pending:" db_set in
			# on_update should already be persisted.
			settings = frappe.get_single("Jarvis Settings")
			seen.append(settings.last_sync_status)
			# Now simulate the admin call's final status write.
			settings.db_set("last_sync_status", "ok (reload via admin)", update_modified=False)
			return {"action": "reload"}

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-rotation"
		with patch("jarvis.admin_client.post_rotate_llm_secret", side_effect=capture_pending):
			settings.save()
		self.assertEqual(len(seen), 1)
		self.assertTrue(
			(seen[0] or "").startswith("pending: rotating credentials"),
			f"expected 'pending: rotating credentials', got {seen[0]!r}",
		)

	def test_pending_status_written_before_enqueue_for_restart(self):
		seen = []

		def capture_pending(*args, **kwargs):
			settings = frappe.get_single("Jarvis Settings")
			seen.append(settings.last_sync_status)
			settings.db_set("last_sync_status", "ok (restart via admin)", update_modified=False)
			return {"action": "restart"}

		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with patch("jarvis.admin_client.post_update_llm_creds", side_effect=capture_pending):
			settings.save()
		self.assertEqual(len(seen), 1)
		self.assertTrue(
			(seen[0] or "").startswith("pending: provisioning container"),
			f"expected 'pending: provisioning container', got {seen[0]!r}",
		)


class TestEnqueueDedupAndJobId(_SettingsSingletonTestCase):
	"""Sprint-2 (2026-06-16): two close-together saves with the same
	action collapse into one enqueued job via job_id + deduplicate=True.
	Without this, two `reload` saves landed as two redundant rotate-
	secret round trips (or worse: stale-snapshot interleaved actions)."""

	def setUp(self):
		super().setUp()
		_reset_settings()
		# The async path runs inline under in_test - we need to peek at
		# the kwargs before the inline call collapses them, so stop the
		# inline shortcut just for this class.
		frappe.flags.in_test = False
		self.addCleanup(lambda: setattr(frappe.flags, "in_test", True))

	def test_enqueue_carries_action_keyed_job_id_and_dedupe_flag(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_api_key = "sk-rotation-1"
		with patch("frappe.enqueue") as enqueue:
			settings.save()
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(
			kwargs.get("job_id"),
			"jarvis_settings_sync:reload",
			"job_id must encode the action so identical-action saves dedup and different-action saves don't",
		)
		self.assertTrue(
			kwargs.get("deduplicate"),
			"deduplicate=True must be set so the second enqueue is a no-op",
		)

	def test_restart_action_uses_distinct_job_id(self):
		"""A restart save must NOT collapse into an in-flight reload save -
		they're different operations on the container side."""
		settings = frappe.get_single("Jarvis Settings")
		settings.llm_provider = "Anthropic"
		settings.llm_model = "claude-sonnet-4-6"
		with patch("frappe.enqueue") as enqueue:
			settings.save()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs.get("job_id"), "jarvis_settings_sync:restart")


class TestEnqueuedSyncRedisLock(_SettingsSingletonTestCase):
	"""_enqueued_sync_via_admin must run under a Redis lock so two queued
	jobs with different actions don't fire admin/fleet calls in parallel."""

	def setUp(self):
		super().setUp()
		_reset_settings()

	def test_lock_acquired_and_released_around_sync(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_enqueued_sync_via_admin,
		)

		sync_called = []

		def _fake_sync(action):
			sync_called.append(action)

		with patch(
			"jarvis.admin_client.post_rotate_llm_secret",
			side_effect=lambda **kw: sync_called.append("reload") or {"action": "reload"},
		):
			_enqueued_sync_via_admin("reload")
		# The admin call ran exactly once - confirms the lock acquired
		# the happy path and yielded into the sync.
		self.assertEqual(len(sync_called), 1)

	def test_lock_contention_schedules_retry_and_no_admin_call(self):
		"""If a prior worker is still holding the lock past blocking timeout,
		the late arrival must NOT call admin (the in-flight one is in
		charge) and must NOT terminal-fail: a sibling sync may now hold the
		lock legitimately for minutes (600s envelope), and terminally
		dropping this sync would silently lose a credential change. First
		loss -> pending + one retry enqueued under a per-level job_id."""
		# Patch the lock helper to simulate contention - context manager
		# yields False without ever acquiring.
		from contextlib import contextmanager

		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_enqueued_sync_via_admin,
		)

		@contextmanager
		def _held(*_a, **_kw):
			yield False

		captured = []
		with (
			patch("jarvis._redis_lock.redis_lock", side_effect=_held),
			patch("frappe.enqueue", side_effect=lambda *a, **kw: captured.append(kw)),
			patch("jarvis.admin_client.post_rotate_llm_secret") as admin_mock,
		):
			_enqueued_sync_via_admin("reload")
		admin_mock.assert_not_called()
		settings = frappe.get_single("Jarvis Settings")
		self.assertIn("pending: waiting for a concurrent sync", settings.last_sync_status or "")
		self.assertEqual(len(captured), 1)
		self.assertTrue(
			(captured[0].get("job_id") or "").startswith("jarvis_settings_sync:reload:retry:3:"),
			f"unexpected retry job_id: {captured[0].get('job_id')!r}",
		)
		self.assertEqual(captured[0].get("retry_left"), 3)

	def test_lock_contention_retries_exhausted_is_terminal(self):
		"""Only the LAST retry's loss writes the terminal 'failed: skipped'."""
		from contextlib import contextmanager

		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_enqueued_sync_via_admin,
		)

		@contextmanager
		def _held(*_a, **_kw):
			yield False

		captured = []
		with (
			patch("jarvis._redis_lock.redis_lock", side_effect=_held),
			patch("frappe.enqueue", side_effect=lambda *a, **kw: captured.append(kw)),
			patch("jarvis.admin_client.post_rotate_llm_secret") as admin_mock,
		):
			_enqueued_sync_via_admin("reload", retry_left=0)
		admin_mock.assert_not_called()
		self.assertFalse(captured, "no further retries after exhaustion")
		settings = frappe.get_single("Jarvis Settings")
		self.assertIn("failed", settings.last_sync_status or "")
		self.assertIn("skipped", settings.last_sync_status or "")


class TestCredsWireAuthMode(FrappeTestCase):
	"""The direct leg's auth-mode vocabulary translation (jarvis#715 step 1b).

	Why the two vocabularies differ, and why the translation is wire-only, lives
	in ``creds_wire_auth_mode``'s own docstring. Kept there rather than copied
	here: two prose copies of the same reasoning drift the moment one is edited.
	"""

	def test_a_subscription_is_sent_as_oauth(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		self.assertEqual(creds_wire_auth_mode("subscription"), "oauth")

	def test_api_key_is_unchanged(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		self.assertEqual(creds_wire_auth_mode("api_key"), "api_key")

	def test_an_already_oauth_value_passes_through(self):
		"""The legacy direct-OAuth path stores "oauth" already; translating twice
		must not corrupt it."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		self.assertEqual(creds_wire_auth_mode("oauth"), "oauth")

	def test_empty_defaults_to_api_key(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		self.assertEqual(creds_wire_auth_mode(""), "api_key")
		self.assertEqual(creds_wire_auth_mode(None), "api_key")

	def test_it_translates_the_WIRE_only_and_never_the_stored_value(self):
		"""The stored field stays "subscription" because other consumers read it,
		and widening those guards is what force-disconnected healthy pool tenants
		before (jarvis-admin-v2#89). This pins that the helper is pure."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		stored = "subscription"
		self.assertEqual(creds_wire_auth_mode(stored), "oauth")
		self.assertEqual(stored, "subscription")

	def test_a_whitespace_only_value_falls_back_to_api_key(self):
		"""A whitespace-only stored value is TRUTHY, so defaulting before stripping
		returned "" - not a mode fleet branches on, which lands the tenant on its
		undefined path instead of the api_key default this promises. Only a
		validation-bypassing write (db_set, a patch) can produce one, and those are
		exactly the writes that reach a Single."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		for blank in ("   ", "\t", "\n", " \t "):
			self.assertEqual(creds_wire_auth_mode(blank), "api_key", repr(blank))

	def test_surrounding_whitespace_does_not_defeat_the_translation(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import creds_wire_auth_mode

		self.assertEqual(creds_wire_auth_mode("  subscription  "), "oauth")
