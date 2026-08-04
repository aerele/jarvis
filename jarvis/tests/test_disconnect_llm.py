"""jarvis.onboarding.disconnect_llm - the customer-side half of Disconnect.

The interesting assertions here are about what is GONE afterwards, and the one
that matters most is __Auth: a Password field keeps its real value in that table
and only a mask in the doctype row, so a "cleared" credential that still has an
__Auth row is not cleared at all. Frappe cleans __Auth in frappe.delete_doc,
which never runs for child rows of a Single (Document.update_child_table deletes
them with a bare SQL DELETE), so these tests read the table directly rather than
trusting get_password to tell the truth about it.

TestDisconnectReconcile below covers the other half of the feature: the scheduled
convergence that finishes a disconnect whose synchronous admin call died after
admin had already processed it (#534).
"""

from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password

from jarvis import account, admin_client, onboarding
from jarvis.exceptions import AdminUnreachableError
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_admin_says_llm_gone,
	reconcile_pending_llm_sync,
)
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


# What admin's chat_readiness_reason really looks like the moment it has processed
# a disconnect: its own no-credentials clause, plus the stale-generation clause the
# stub re-render is still working through. Composed, not a bare sentence, because
# that is the shape the reconcile has to survive - see compute_chat_readiness in
# jarvis-admin-v2 (fleet/pool.py).
_ADMIN_DISCONNECTED_REASON = "waiting for an LLM key or subscription; applying your LLM configuration"


class TestDisconnectReconcile(_RT3SettingsTestCase):
	"""#534: the disconnect must survive a worker that dies mid-call.

	``disconnect_llm`` asks admin synchronously and clears local secrets only if it
	answers. Admin commits its blanked row BEFORE it touches the container, so a
	worker killed anywhere after that commit - by gunicorn's -t, a dropped
	connection, a restart - leaves admin and the container disconnected while this
	bench still holds live keys and still advertises a model.

	Every test here drives ``reconcile_pending_llm_sync`` directly. The scheduler is
	paused on the dev bench and the cron cadence is not what is under test.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		frappe.db.commit()

	def _seed_connected_pool(self):
		"""A pool the fleet CONFIRMED it applied - i.e. an ordinary working customer.

		The confirmed-apply marker is not decoration here: it is the discriminator
		the reconcile uses to tell "admin deleted these" from "admin never received
		these", so seeding it is what makes this a connected workspace rather than a
		half-onboarded one.
		"""
		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}),
		):
			onboarding.save_llm_pool(frappe.as_json(_POOL), preset=None, routing_mode="failover")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_pool_synced_at", frappe.utils.now(), update_modified=False)
		settings.db_set("last_sync_status", "ok", update_modified=False)
		frappe.db.commit()

	def _assert_pool_intact(self, why: str):
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(len(settings.get("models") or []), 2, why)
		self.assertEqual(settings.models[0].get_password("api_key"), "sk-first", why)
		self.assertGreaterEqual(_pool_auth_rows(), 2, why)
		self.assertNotEqual(settings.get("last_sync_status") or "", "disconnected", why)

	def _assert_fully_disconnected(self):
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(settings.get("models") or [], [])
		self.assertEqual(_pool_auth_rows(), 0, "the ciphertext has to go too, not just the rows")
		self.assertFalse(
			get_decrypted_password("Jarvis Settings", "Jarvis Settings", "llm_api_key", False),
			"the legacy flat key must not survive in __Auth",
		)
		self.assertEqual(settings.get("llm_provider") or "", "")
		self.assertEqual(settings.get("llm_model") or "", "")
		self.assertEqual(settings.get("last_sync_status") or "", "disconnected")
		self.assertIsNone(settings.get("llm_pool_synced_at"))
		self.assertIsNone(settings.get("llm_direct_synced_at"))

	def _split_the_planes(self):
		"""Reproduce the defect: admin processed the disconnect, the bench never
		learned. AdminUnreachableError is what a killed read looks like from here,
		and disconnect_llm aborts on it BEFORE clearing anything locally."""
		self._seed_connected_pool()
		with patch(
			"jarvis.admin_client.post_disconnect_llm",
			side_effect=AdminUnreachableError("read timeout"),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.disconnect_llm()
		self._assert_pool_intact("the split state is the premise of these tests")

	# ---- it converges ----------------------------------------------------- #

	def test_reconcile_finishes_a_disconnect_the_worker_did_not_survive(self):
		self._split_the_planes()
		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "Configuring",
				"chat_readiness_reason": _ADMIN_DISCONNECTED_REASON,
			},
		):
			reconcile_pending_llm_sync()
		self._assert_fully_disconnected()

	def test_reconcile_converges_without_re_driving_the_admin_disconnect(self):
		"""Admin is already done - it said so. Calling its disconnect again would
		spend the customer's shared 20/hour rotate-ops bucket on a container admin's
		own reconcile is converging."""
		self._split_the_planes()
		with (
			patch.object(
				admin_client,
				"get_connection",
				return_value={
					"chat_readiness": "Configuring",
					"chat_readiness_reason": _ADMIN_DISCONNECTED_REASON,
				},
			),
			patch.object(admin_client, "post_disconnect_llm") as post,
		):
			reconcile_pending_llm_sync()
		post.assert_not_called()
		self._assert_fully_disconnected()

	def test_reconcile_is_idempotent(self):
		self._split_the_planes()
		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "Configuring",
				"chat_readiness_reason": _ADMIN_DISCONNECTED_REASON,
			},
		) as conn:
			reconcile_pending_llm_sync()
			reconcile_pending_llm_sync()
			reconcile_pending_llm_sync()
		self._assert_fully_disconnected()
		self.assertEqual(
			conn.call_count,
			1,
			"a converged workspace holds no credential, so later ticks must not even probe admin",
		)

	# ---- and it does NOT clobber a working customer ------------------------ #

	def test_a_connected_workspace_admin_reports_ready_is_untouched(self):
		self._seed_connected_pool()
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Ready", "chat_readiness_reason": ""},
		):
			reconcile_pending_llm_sync()
		self._assert_pool_intact("admin says Ready; there is nothing to converge")

	def test_credentials_admin_never_received_are_not_destroyed(self):
		"""THE clobber case, and the reason the confirmed-apply marker is required.

		A customer types a key into a bench that cannot reach admin. Admin holds no
		credential and says so in exactly the same words it uses after a disconnect -
		but here that means "never received", not "deleted", and the key is real and
		wanted. Without the marker gate the reconcile reads the two states
		identically and wipes this one.
		"""
		self._seed_connected_pool()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		settings.db_set("last_sync_status", "failed: admin unreachable", update_modified=False)
		frappe.db.commit()

		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "Configuring",
				"chat_readiness_reason": _ADMIN_DISCONNECTED_REASON,
			},
		):
			reconcile_pending_llm_sync()
		self._assert_pool_intact("an unproven first apply is not a disconnect")

	def test_other_configuring_reasons_are_not_a_disconnect(self):
		"""Every other Configuring reason describes a tenant whose credentials admin
		still HAS. Reading any of them as "gone" would delete a working key."""
		for reason in (
			"applying your LLM configuration",
			"ERP tools not connected",
			"pool spec rejected: invalid_spec",
			"still verifying your subscription route",
			"",
		):
			with self.subTest(reason=reason):
				self._clear_models()
				_reset_settings()
				self._seed_connected_pool()
				with patch.object(
					admin_client,
					"get_connection",
					return_value={"chat_readiness": "Configuring", "chat_readiness_reason": reason},
				):
					reconcile_pending_llm_sync()
				self._assert_pool_intact(f"reason {reason!r} says nothing about credentials")

	def test_a_probe_failure_is_never_read_as_disconnected(self):
		self._seed_connected_pool()
		with patch.object(admin_client, "get_connection", side_effect=AdminUnreachableError("boom")):
			reconcile_pending_llm_sync()
		self._assert_pool_intact("unknown is not disconnected")

	# ---- the predicate itself --------------------------------------------- #

	def test_admin_says_llm_gone_requires_the_configuring_state(self):
		clause = _ADMIN_DISCONNECTED_REASON
		self.assertTrue(_admin_says_llm_gone("Configuring", clause))
		# Case-insensitive: the clause is admin's prose, not a protocol token.
		self.assertTrue(_admin_says_llm_gone("Configuring", clause.upper()))
		# A state that says nothing about credentials must never qualify - a
		# suspended tenant in particular still owns its keys and gets them back.
		for state in ("Ready", "Provisioning", "Suspended", "SupportRequired", "", None):
			with self.subTest(state=state):
				self.assertFalse(_admin_says_llm_gone(state, clause))
		self.assertFalse(_admin_says_llm_gone("Configuring", None))
		self.assertFalse(_admin_says_llm_gone("Configuring", "ERP tools not connected"))

	def test_a_reconnect_during_the_probe_discards_the_stale_verdict(self):
		"""The narrow race the revision snapshot closes. Admin's answer is about the
		connection the customer just replaced; acting on it would delete the
		credential they entered while we were asking."""
		self._split_the_planes()

		def _reconnect_then_answer(*args, **kwargs):
			# Stands in for the customer saving a new key between the probe leaving
			# and its answer arriving. Any of the gate-revision fields will do.
			settings = frappe.get_single("Jarvis Settings")
			settings.db_set("last_sync_status", "pending: admin applying config", update_modified=False)
			frappe.db.commit()
			return {
				"chat_readiness": "Configuring",
				"chat_readiness_reason": _ADMIN_DISCONNECTED_REASON,
			}

		with patch.object(admin_client, "get_connection", side_effect=_reconnect_then_answer):
			reconcile_pending_llm_sync()
		self._assert_pool_intact("a verdict about a replaced connection must not be acted on")
