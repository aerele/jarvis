import frappe
from frappe.tests.utils import FrappeTestCase

# Fields these tests mutate on the Jarvis Settings singleton. Snapshotted in
# setUpClass / restored in tearDownClass so a stray failure or new test that
# forgets to clean up cannot leak fixture values into the live UI.
_SNAPSHOT_PLAIN_FIELDS = (
	"jarvis_admin_url",
	"agent_url",
	"llm_provider",
	"llm_model",
	"llm_base_url",
	"last_sync_status",
	"last_sync_at",
)
_SNAPSHOT_PASSWORD_FIELDS = (
	"jarvis_admin_api_key",
	# Must be snapshotted/restored alongside the key: TestOnUpdateAdminDispatch
	# sets it in setUp, so without restoring it here that value would leak into
	# __Auth and become the cross-test pollution that #192 is about.
	"jarvis_admin_api_secret",
	"agent_token",
	"llm_api_key",
)


class _SettingsSnapshotTestCase(FrappeTestCase):
	"""Snapshot the Jarvis Settings singleton at class entry; restore at exit.

	Password fields are read via get_password() (cleartext) - settings.get(...)
	returns the masked "*****" string for those and would round-trip wrong.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		s = frappe.get_single("Jarvis Settings")
		snapshot: dict[str, object] = {f: s.get(f) for f in _SNAPSHOT_PLAIN_FIELDS}
		for f in _SNAPSHOT_PASSWORD_FIELDS:
			snapshot[f] = s.get_password(f, raise_exception=False) or ""
		cls._jarvis_settings_snapshot = snapshot

	@classmethod
	def tearDownClass(cls):
		try:
			s = frappe.get_single("Jarvis Settings")
			for field, value in cls._jarvis_settings_snapshot.items():
				s.db_set(field, value)
			frappe.db.commit()
		finally:
			super().tearDownClass()


class TestJarvisSettings(FrappeTestCase):
	def test_settings_is_single(self):
		meta = frappe.get_meta("Jarvis Settings")
		self.assertTrue(meta.issingle, "Jarvis Settings must be a Single DocType")

	def test_settings_has_expected_fields(self):
		meta = frappe.get_meta("Jarvis Settings")
		fieldnames = {f.fieldname for f in meta.fields}
		required = (
			"jarvis_admin_api_key",
			"jarvis_admin_url",
			"token_budget_monthly",
			"llm_provider",
			"llm_model",
			"llm_api_key",
			"llm_base_url",
			"llm_temperature",
			"llm_max_output_tokens",
		)
		for fieldname in required:
			self.assertIn(fieldname, fieldnames, f"missing field: {fieldname}")

	def test_api_keys_are_password_fields(self):
		meta = frappe.get_meta("Jarvis Settings")
		for fieldname in ("jarvis_admin_api_key", "llm_api_key"):
			field = next(f for f in meta.fields if f.fieldname == fieldname)
			self.assertEqual(field.fieldtype, "Password", f"{fieldname} must be Password")

	def test_jarvis_admin_fields_are_readonly(self):
		"""Populated by the signup wizard / staff; never customer-edited."""
		meta = frappe.get_meta("Jarvis Settings")
		for fieldname in ("jarvis_admin_url", "jarvis_admin_api_key"):
			field = next(f for f in meta.fields if f.fieldname == fieldname)
			self.assertTrue(
				field.read_only,
				f"{fieldname} must be read-only (populated by signup wizard)",
			)

	# (test_llm_provider_options_cover_paid_and_open_weight removed: the
	# unified LLM config turned legacy llm_provider into a read-only Data
	# mirror of models[0].provider — provider choice now lives in the models
	# table/preset catalog. Contract covered by test_unified_llm_config.)

	def test_tab_structure(self):
		"""Two tabs: Configuration (editable) and System (read-only)."""
		meta = frappe.get_meta("Jarvis Settings")
		fields_by_name = {f.fieldname: f for f in meta.fields}

		self.assertEqual(fields_by_name["config_tab"].fieldtype, "Tab Break")
		self.assertEqual(fields_by_name["config_tab"].label, "Configuration")
		self.assertEqual(fields_by_name["system_tab"].fieldtype, "Tab Break")
		self.assertEqual(fields_by_name["system_tab"].label, "System")

	def test_configuration_tab_sections(self):
		"""Configuration tab has Account, Language Model, Sampling sections."""
		meta = frappe.get_meta("Jarvis Settings")
		fields_by_name = {f.fieldname: f for f in meta.fields}

		for fieldname in ("account_section", "llm_section", "llm_advanced_section"):
			self.assertEqual(fields_by_name[fieldname].fieldtype, "Section Break")

	def test_system_tab_sections(self):
		"""System tab has Jarvis Admin Connection, Agent Operator, Last Sync sections."""
		meta = frappe.get_meta("Jarvis Settings")
		fields_by_name = {f.fieldname: f for f in meta.fields}

		for fieldname in ("admin_connection_section", "operator_section", "last_sync_section"):
			self.assertEqual(fields_by_name[fieldname].fieldtype, "Section Break")

	def test_operator_fields_are_readonly(self):
		"""All 5 operator fields are system-populated and must be read-only."""
		meta = frappe.get_meta("Jarvis Settings")
		fields_by_name = {f.fieldname: f for f in meta.fields}

		for fieldname, expected_type in (
			("agent_url", "Data"),
			("agent_token", "Password"),
		):
			self.assertEqual(fields_by_name[fieldname].fieldtype, expected_type)
			self.assertTrue(
				fields_by_name[fieldname].read_only,
				f"{fieldname} must be read-only (system-populated by admin signup)",
			)

	def test_last_sync_fields_are_readonly(self):
		meta = frappe.get_meta("Jarvis Settings")
		fields_by_name = {f.fieldname: f for f in meta.fields}

		self.assertEqual(fields_by_name["last_sync_at"].fieldtype, "Datetime")
		self.assertTrue(fields_by_name["last_sync_at"].read_only)
		self.assertEqual(fields_by_name["last_sync_status"].fieldtype, "Long Text")
		self.assertTrue(fields_by_name["last_sync_status"].read_only)


class TestOnUpdateAdminDispatch(_SettingsSnapshotTestCase):
	"""Tests for the admin-path branch added in Plan 3.2.2b.

	When jarvis_admin_url is set, on_update routes through
	jarvis.admin_client.post_update_llm_creds instead of agent_push.
	Errors land in last_sync_status; save itself never raises.
	"""

	def setUp(self):
		self.settings = frappe.get_single("Jarvis Settings")
		self.settings.db_set("jarvis_admin_url", "https://admin.example.com")
		self.settings.db_set("jarvis_admin_api_key", "test-token")
		# The authed admin path (admin_client._post_authed) requires BOTH
		# jarvis_admin_api_key AND jarvis_admin_api_secret, or it raises
		# "not onboarded". This setUp only ever set the key, so the restart
		# dispatch test passed only when an earlier test happened to leave a
		# secret in __Auth (the #192 Singles-snapshot / __Auth-pollution flake) —
		# order-dependent, and it fails whenever test discovery order shifts.
		# Set the secret explicitly so these tests are self-contained. (db_set on
		# a Password field round-trips through get_password — verified.)
		self.settings.db_set("jarvis_admin_api_secret", "test-secret")
		self.settings.db_set("llm_provider", "Anthropic")
		self.settings.db_set("llm_model", "claude-sonnet-4-6")
		self.settings.db_set("llm_base_url", "https://api.anthropic.com")
		self.settings.db_set("llm_api_key", "sk-original")
		# Force DIRECT (single-model) dispatch. on_update routes into the POOL path
		# whenever the models child table has ANY row (compute_proxy_active), and an
		# earlier test class (e.g. test_unified_llm_config) can leave a COMMITTED
		# Jarvis LLM Pool Model row that Frappe's per-class rollback cannot undo. With
		# a leftover row, this test's provider-change save routes to the unmocked
		# post_update_llm_pool → "admin unreachable" instead of the restart path it
		# mocks — the real, order-dependent cause of this flake (the clear_document_cache
		# below is inert for it: the classifier's before-save read is an uncached
		# get_doc). Same fix already shipped for the same bug in
		# test_settings_on_update._reset_settings (commit 6fc9b74).
		frappe.db.delete(
			"Jarvis LLM Pool Model",
			{"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"},
		)
		self.settings.db_set("preset", "")
		self.settings.db_set("proxy_active", 0, update_modified=False)
		frappe.db.commit()
		# Finish the isolation hardening #300 started: the restart-vs-reload
		# classifier (_classify_llm_change) compares the new provider/model/
		# base_url against get_doc_before_save(), which loads through the
		# single-doc cache. A prior test can leave that cache holding the exact
		# OpenAI/gpt-4o values test_admin_path_restart switches TO, so the change
		# reads as "no structural change" → misclassified "reload" → the unmocked
		# post_rotate_llm_secret → "admin unreachable" (order-dependent flake).
		# Clear the cache so the before-save baseline is the Anthropic state just
		# committed above.
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")

	def _save_with_new_api_key(self, new_key="sk-new"):
		# Re-fetch + change llm_api_key, then save (triggers on_update).
		s = frappe.get_doc("Jarvis Settings", "Jarvis Settings")
		s.llm_api_key = new_key
		s.save(ignore_permissions=True)
		frappe.db.commit()
		return frappe.get_doc("Jarvis Settings", "Jarvis Settings")

	def test_admin_path_reload_success_updates_last_sync_status(self):
		from unittest.mock import patch

		# Reload-action saves (api_key rotation) now go through the rotate
		# endpoint per the prod-wiring spec's unified action-based dispatch.
		with patch(
			"jarvis.admin_client.post_rotate_llm_secret", return_value={"action": "reload", "result": "ok"}
		) as mock_post:
			s = self._save_with_new_api_key("sk-after-reload-test")
		mock_post.assert_called_once()
		self.assertEqual(s.last_sync_status, "ok (reload via admin)")
		self.assertIsNotNone(s.last_sync_at)

	def test_admin_path_restart_returned_action_reflects_in_status(self):
		from unittest.mock import patch

		with patch(
			"jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart", "result": "ok"}
		):
			# Trigger via a change that classifies as restart (provider change)
			s = frappe.get_doc("Jarvis Settings", "Jarvis Settings")
			s.llm_provider = "OpenAI"
			s.llm_model = "gpt-4o"
			s.llm_base_url = "https://api.openai.com"
			s.llm_api_key = "sk-openai"
			s.save(ignore_permissions=True)
			frappe.db.commit()
			s = frappe.get_doc("Jarvis Settings", "Jarvis Settings")
		self.assertEqual(s.last_sync_status, "ok (restart via admin)")

	def test_admin_path_auth_error_surfaces_in_status(self):
		from unittest.mock import patch

		from jarvis.exceptions import AdminAuthError

		# api_key rotation is now a reload-action → post_rotate_llm_secret
		with patch("jarvis.admin_client.post_rotate_llm_secret", side_effect=AdminAuthError("invalid token")):
			s = self._save_with_new_api_key("sk-auth-fail-test")
		self.assertTrue(s.last_sync_status.startswith("failed: auth:"))
		self.assertIn("invalid token", s.last_sync_status)

	def test_admin_path_unreachable_surfaces_in_status(self):
		from unittest.mock import patch

		from jarvis.exceptions import AdminUnreachableError

		with patch(
			"jarvis.admin_client.post_rotate_llm_secret",
			side_effect=AdminUnreachableError("connection refused"),
		):
			s = self._save_with_new_api_key("sk-unreach-test")
		self.assertTrue(s.last_sync_status.startswith("failed: admin unreachable:"))
		self.assertIn("connection refused", s.last_sync_status)


# TestOnUpdateLocalDispatchWhenAdminUrlEmpty removed: post-unification (2026-05-29)
# on_update always routes through _sync_via_admin. The bench-local push path
# is gone; the test exercising it is no longer meaningful.
