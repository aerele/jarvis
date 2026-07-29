"""jarvis.onboarding.disconnect_llm - the customer-side half of Disconnect.

The interesting assertions here are about what is GONE afterwards, and the one
that matters most is __Auth: a Password field keeps its real value in that table
and only a mask in the doctype row, so a "cleared" credential that still has an
__Auth row is not cleared at all. Frappe cleans __Auth in frappe.delete_doc,
which never runs for child rows of a Single (Document.update_child_table deletes
them with a bare SQL DELETE), so these tests read the table directly rather than
trusting get_password to tell the truth about it.
"""

from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password

from jarvis import account, admin_client, onboarding
from jarvis.exceptions import AdminUnreachableError
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

_POOL = [
	{
		"provider": "openai",
		"model": "gpt-5.5",
		"api_key": "sk-first",
		"base_url": "",
		"tier": "strong",
		"order": 0,
	},
	{
		"provider": "anthropic",
		"model": "claude-sonnet-4",
		"api_key": "sk-second",
		"base_url": "",
		"tier": "strong",
		"order": 1,
	},
]


def _pool_auth_rows() -> int:
	"""__Auth rows holding a models[] child-row secret (api_key /
	subscription_accounts). Jarvis LLM Pool Model is a child of the Jarvis
	Settings Single and of nothing else, so this counts exactly the pool's
	encrypted credentials."""
	return frappe.db.sql("select count(*) from `__Auth` where doctype = %s", ("Jarvis LLM Pool Model",))[0][0]


class TestDisconnectLlm(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		frappe.db.commit()

	def _seed_pool(self):
		"""Write a real two-model pool through the ordinary save path, so the
		credentials under test were stored exactly the way a customer's are."""
		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
		):
			onboarding.save_llm_pool(frappe.as_json(_POOL), preset=None, routing_mode="failover")

	# ---- the credentials really go -------------------------------------- #

	def test_deletes_pool_rows_and_their_auth_secrets(self):
		self._seed_pool()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(len(settings.get("models")), 2)
		self.assertEqual(settings.models[0].get_password("api_key"), "sk-first")
		self.assertGreaterEqual(_pool_auth_rows(), 2, "seeding must produce encrypted row secrets")

		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.get("models") or [], [])
		# The point of the whole test: the rows are gone AND so is the ciphertext.
		# Clearing models[] the ordinary way leaves these behind.
		self.assertEqual(_pool_auth_rows(), 0)

	def test_clears_the_legacy_flat_api_key_including_its_auth_row(self):
		self._seed_pool()
		# on_update mirrors models[0]'s key into the legacy Password field.
		self.assertEqual(
			get_decrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key", False),
			"sk-first",
		)

		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		self.assertFalse(
			get_decrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key", False),
			"llm_api_key must not survive in __Auth after a disconnect",
		)
		settings = frappe.get_single("Jarvis Settings")
		self.assertFalse(settings.get_password("llm_api_key", raise_exception=False))

	def test_clears_the_mirrored_legacy_fields(self):
		self._seed_pool()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.get("llm_provider") or "", "")
		self.assertEqual(settings.get("llm_model") or "", "")
		self.assertEqual(settings.get("llm_base_url") or "", "")
		self.assertEqual(settings.get("llm_auth_mode") or "", "api_key")
		self.assertEqual(settings.get("llm_oauth_account_email") or "", "")
		self.assertIsNone(settings.get("llm_oauth_connected_at"))
		self.assertEqual(settings.get("preset") or "", "")
		self.assertFalse(settings.get("proxy_active"))
		# The apply markers go too: is_ready_for_chat reads a stamped marker as "this
		# tenant has applied before, keep chat open through a pending re-save", which
		# would wrongly open chat for the NEXT connection before its first apply.
		self.assertIsNone(settings.get("llm_pool_synced_at"))
		self.assertIsNone(settings.get("llm_direct_synced_at"))

	def test_clears_the_oauth_blob_of_a_subscription_pool(self):
		models = [
			{
				"provider": "openai",
				"model": "gpt-5.5",
				"order": 0,
				"subscription": {
					"rotation": "sticky",
					"accounts": [
						{
							"upstream": "openai",
							"account_ref": "SUB_a",
							"label": "a@x.com",
							"oauth_blob": '{"refresh_token": "rt-secret"}',
						}
					],
				},
			},
			{
				"provider": "openai",
				"model": "gpt-5.4",
				"api_key": "sk-backup",
				"order": 1,
			},
		]
		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
		):
			onboarding.save_llm_pool(frappe.as_json(models), preset=None, routing_mode="failover")
		settings = frappe.get_single("Jarvis Settings")
		self.assertIn("rt-secret", settings.models[0].get_password("subscription_accounts") or "")

		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		self.assertEqual(_pool_auth_rows(), 0)

	# ---- the container is told -------------------------------------------- #

	def test_calls_admin_so_the_deletion_reaches_the_container(self):
		self._seed_pool()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}) as m:
			onboarding.disconnect_llm()
		m.assert_called_once_with()

	def test_admin_failure_aborts_and_keeps_the_credentials(self):
		"""Admin first, and its failure is terminal. The other order would leave a
		bench reading "disconnected" in front of a container still holding live keys
		- telling the customer their credentials were deleted when they were not."""
		self._seed_pool()
		with patch(
			"jarvis.admin_client.post_disconnect_llm",
			side_effect=AdminUnreachableError("read timeout"),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_llm()

		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(len(settings.get("models")), 2)
		self.assertEqual(settings.models[0].get_password("api_key"), "sk-first")
		self.assertGreaterEqual(_pool_auth_rows(), 2)

	# ---- idempotence ------------------------------------------------------ #

	def test_disconnecting_twice_succeeds(self):
		self._seed_pool()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}) as m:
			onboarding.disconnect_llm()
			out = onboarding.disconnect_llm()
		self.assertEqual(m.call_count, 2)
		self.assertTrue(out["disconnected"])
		self.assertEqual(_pool_auth_rows(), 0)

	def test_disconnecting_an_unconfigured_tenant_succeeds(self):
		settings = frappe.get_single("Jarvis Settings")
		for field in ("llm_provider", "llm_model", "llm_base_url", "llm_api_key"):
			settings.db_set(field, "", update_modified=False)
		frappe.db.commit()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			out = onboarding.disconnect_llm()
		self.assertTrue(out["disconnected"])

	def test_skips_the_admin_call_when_the_bench_is_not_onboarded(self):
		"""No control plane to call: admin_client would raise
		AdminAuthError("not onboarded") on what is otherwise a valid local wipe.

		The predicate is patched rather than the fields cleared: this suite shares
		the Jarvis Settings Single with the live site, and really clearing the admin
		credentials means dropping their __Auth rows - which the class teardown
		cannot put back for jarvis_admin_customer_password. _has_admin_credentials
		itself is covered below."""
		with (
			patch("jarvis.onboarding._has_admin_credentials", return_value=False),
			patch("jarvis.admin_client.post_disconnect_llm") as m,
		):
			out = onboarding.disconnect_llm()
		m.assert_not_called()
		self.assertTrue(out["disconnected"])

	def test_admin_credentials_predicate_accepts_either_auth_shape(self):
		class _Stub:
			def __init__(self, **values):
				self._values = values

			def get_password(self, field, raise_exception=True):
				return self._values.get(field, "")

		self.assertFalse(onboarding._has_admin_credentials(_Stub()))
		self.assertFalse(
			onboarding._has_admin_credentials(
				_Stub(jarvis_admin_api_key="  ", jarvis_admin_customer_password="")
			)
		)
		self.assertTrue(onboarding._has_admin_credentials(_Stub(jarvis_admin_api_key="ak-1")))
		# OAuth password only: the shape a verified signup lands on.
		self.assertTrue(onboarding._has_admin_credentials(_Stub(jarvis_admin_customer_password="pw-1")))

	# ---- what the rest of the app sees afterwards -------------------------- #

	def test_chat_readiness_reports_llm_credentials(self):
		"""Pins the behaviour the disconnected banner keys off. is_ready_for_chat
		already returned this reason for a blank api_key config; this makes sure a
		disconnect lands on that path rather than on llm_pool_provisioning (which
		would send the customer to the full-screen onboarding gate instead)."""
		self._seed_pool()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		with patch.object(admin_client, "get_connection", return_value={}):
			verdict = account.is_ready_for_chat()
		self.assertFalse(verdict["ready"])
		self.assertEqual(verdict["reason"], "llm_credentials")

	def test_connection_status_reports_disconnected_without_an_admin_call(self):
		self._seed_pool()
		with patch("jarvis.admin_client.post_disconnect_llm", return_value={"ok": True}):
			onboarding.disconnect_llm()

		with patch.object(admin_client, "post_llm_auth_status") as m:
			out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertTrue(out["disconnected"])
		self.assertFalse(out["proxy_active"])
		self.assertEqual(out["default_model"], "")

	def test_a_configured_direct_tenant_is_not_disconnected(self):
		"""The Direct short-circuit returns before the admin round-trip, so the
		disconnected state has to be computed ahead of it - but not AT its expense."""
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_active", 0, update_modified=False)
		settings.db_set("llm_provider", "openai", update_modified=False)
		settings.db_set("llm_model", "gpt-4o", update_modified=False)
		frappe.db.commit()

		with patch.object(admin_client, "post_llm_auth_status") as m:
			out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertFalse(out["disconnected"])
		self.assertEqual(out["default_model"], "gpt-4o")
