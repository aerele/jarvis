"""Round-trip test for the re-modeled LLM-pool subscription accounts.

Subscription accounts USED to be a grandchild Table (Jarvis Settings ->
models[] -> accounts[]) which Frappe's ORM neither persists nor auto-loads
(grandchildren are not saved/loaded), so subscription pools never provisioned.

They are now stored as a JSON string in the ENCRYPTED `subscription_accounts`
Password field ON the model row (a normal child-of-Single Password, exactly
like `api_key`). This test drives the real save -> serialize -> read path:

- save_llm_pool with a subscription model (2 accounts, each a fake oauth_blob)
  + an api_key model.
- build_pool_payload emits BOTH oauth_blobs keyed by their account_ref, the
  spec carries the subscription block with 2 accounts, and the api_key model's
  key is emitted.
- get_llm_config returns the 2 account labels WITHOUT any oauth_blob.
- the oauth_blob is stored ENCRYPTED (the raw DB column is masked, not the
  plaintext token) yet decryptable via get_password.
"""

import json
from unittest.mock import patch

import frappe

from jarvis import onboarding
from jarvis.jarvis.pool_serialize import build_pool_payload
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase

_BLOB_1 = '{"refresh_token":"fake-rt-ACC1-secret"}'
_BLOB_2 = '{"refresh_token":"fake-rt-ACC2-secret"}'
_BLOB_1_NEW = '{"refresh_token":"fake-rt-ACC1-RECONNECTED"}'


def _models_payload():
	return [
		{
			"model": "gpt-5.5",
			"tier": "cheap",
			"order": 0,
			"subscription": {
				"rotation": "round_robin",
				"accounts": [
					{
						"upstream": "openai",
						"account_ref": "ACC_1",
						"label": "alice@example.com",
						"oauth_blob": _BLOB_1,
					},
					{
						"upstream": "openai",
						"account_ref": "ACC_2",
						"label": "bob@example.com",
						"oauth_blob": _BLOB_2,
					},
				],
			},
		},
		{
			"provider": "openai",
			"model": "gpt-5.4",
			"api_key": "sk-apikey-roundtrip-xyz",
			"tier": "strong",
			"order": 1,
		},
	]


class TestSubscriptionAccountsRoundTrip(_RT3SettingsTestCase):
	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		s = frappe.get_single("Jarvis Settings")
		s.db_set("preset", "", update_modified=False)
		s.db_set("routing_mode", "failover", update_modified=False)
		s.db_set("proxy_active", 0, update_modified=False)
		frappe.db.commit()

	def _save(self):
		"""save_llm_pool the mixed pool; capture the admin pool push kwargs."""
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or {"action": "pool_update"},
			),
			patch("jarvis.admin_client.post_update_llm_creds") as creds,
		):
			onboarding.save_llm_pool(frappe.as_json(_models_payload()), preset=None, routing_mode="failover")
		creds.assert_not_called()
		self.assertTrue(pool_calls, "proxy pool path must fire for a 2-model pool")
		return pool_calls[0]

	# ------------------------------------------------------------------ #
	# (1) The admin push carries both oauth_blobs + the api_key + a
	#     subscription spec block with 2 accounts.
	# ------------------------------------------------------------------ #

	def test_admin_push_carries_blobs_spec_and_api_key(self):
		pushed = self._save()

		oauth_blobs = pushed["oauth_blobs"]
		self.assertEqual(oauth_blobs.get("ACC_1"), {"refresh_token": "fake-rt-ACC1-secret"})
		self.assertEqual(oauth_blobs.get("ACC_2"), {"refresh_token": "fake-rt-ACC2-secret"})

		# api_key emitted (keyed by a POOL_KEY_* ref).
		api_keys = pushed["api_keys"]
		self.assertIn("sk-apikey-roundtrip-xyz", api_keys.values())

		# Spec: subscription block with 2 accounts, secrets NOT in the spec.
		spec = pushed["spec"]
		sub_entries = [m for m in spec["models"] if "subscription" in m]
		self.assertEqual(len(sub_entries), 1, "exactly one subscription model in spec")
		sub = sub_entries[0]["subscription"]
		self.assertEqual(sub["upstream"], "openai")
		self.assertEqual(sub["rotation"], "round_robin")
		refs = {a["account_ref"] for a in sub["accounts"]}
		self.assertEqual(refs, {"ACC_1", "ACC_2"})
		self.assertNotIn(
			"fake-rt-ACC1-secret", json.dumps(spec), "oauth_blob secret must never appear in the spec"
		)
		self.assertNotIn(
			"sk-apikey-roundtrip-xyz", json.dumps(spec), "api_key secret must never appear in the spec"
		)

	# ------------------------------------------------------------------ #
	# (2) build_pool_payload, re-run on the persisted settings, is identical.
	# ------------------------------------------------------------------ #

	def test_build_pool_payload_from_persisted_settings(self):
		self._save()
		s = frappe.get_single("Jarvis Settings")
		spec, api_keys, oauth_blobs = build_pool_payload(s)
		self.assertEqual(oauth_blobs.get("ACC_1"), {"refresh_token": "fake-rt-ACC1-secret"})
		self.assertEqual(oauth_blobs.get("ACC_2"), {"refresh_token": "fake-rt-ACC2-secret"})
		self.assertIn("sk-apikey-roundtrip-xyz", api_keys.values())
		sub = [m for m in spec["models"] if "subscription" in m][0]["subscription"]
		self.assertEqual(len(sub["accounts"]), 2)

	# ------------------------------------------------------------------ #
	# (3) get_llm_config returns account labels WITHOUT oauth_blob.
	# ------------------------------------------------------------------ #

	def test_get_llm_config_returns_labels_without_secrets(self):
		self._save()
		cfg = onboarding.get_llm_config()
		sub_models = [m for m in cfg["models"] if m.get("credential_type") == "subscription"]
		self.assertEqual(len(sub_models), 1)
		accounts = sub_models[0]["accounts"]
		self.assertEqual(len(accounts), 2)
		labels = {a["label"] for a in accounts}
		self.assertEqual(labels, {"alice@example.com", "bob@example.com"})
		for a in accounts:
			self.assertNotIn("oauth_blob", a, "get_llm_config must NOT expose oauth_blob")
			self.assertIn("upstream", a)
			self.assertIn("account_ref", a)
		self.assertTrue(sub_models[0]["has_key"], "subscription model with accounts -> has_key True")
		# No secret anywhere in the returned config.
		blob = frappe.as_json(cfg)
		self.assertNotIn("fake-rt-ACC1-secret", blob)
		self.assertNotIn("fake-rt-ACC2-secret", blob)

	# ------------------------------------------------------------------ #
	# (4) oauth_blob is stored ENCRYPTED at rest (masked column) yet
	#     decryptable via get_password.
	# ------------------------------------------------------------------ #

	def test_accounts_stored_encrypted_not_plaintext(self):
		self._save()

		rows = frappe.db.sql(
			"""SELECT name, subscription_accounts FROM `tabJarvis LLM Pool Model`
               WHERE parent='Jarvis Settings' AND credential_type='subscription'""",
			as_dict=True,
		)
		self.assertEqual(len(rows), 1, "exactly one persisted subscription model row")
		raw_column = rows[0]["subscription_accounts"] or ""

		# The raw DB column must NOT contain the plaintext oauth_blob tokens.
		self.assertNotIn(
			"fake-rt-ACC1-secret",
			raw_column,
			"oauth_blob must be encrypted at rest, not plaintext in the column",
		)
		self.assertNotIn("fake-rt-ACC2-secret", raw_column)
		self.assertNotIn(
			"refresh_token", raw_column, "the accounts JSON must not sit in plaintext in the column"
		)

		# Decryptable via get_password on the reloaded child row.
		s = frappe.get_single("Jarvis Settings")
		sub_row = [m for m in s.models if m.credential_type == "subscription"][0]
		decrypted = sub_row.get_password("subscription_accounts", raise_exception=False)
		accounts = json.loads(decrypted)
		self.assertEqual(len(accounts), 2)
		refs = {a["account_ref"] for a in accounts}
		self.assertEqual(refs, {"ACC_1", "ACC_2"})
		blob_by_ref = {a["account_ref"]: a["oauth_blob"] for a in accounts}
		self.assertEqual(blob_by_ref["ACC_1"], _BLOB_1)
		self.assertEqual(blob_by_ref["ACC_2"], _BLOB_2)

	# ------------------------------------------------------------------ #
	# (5) Re-save with blanked secrets (the reloaded-editor case) preserves
	#     credentials the user did not re-enter. Regression for #200 review
	#     findings #2 (silent OAuth-blob loss) and #3 (can't edit without
	#     re-typing keys).
	# ------------------------------------------------------------------ #

	def _resave_blanked_payload(self):
		"""Same pool, but account ACC_1 reconnected (fresh blob), ACC_2 NOT
		reconnected (blank blob), and the api_key model's key blanked with
		has_key set — exactly what the SPA posts after a reload."""
		return [
			{
				"model": "gpt-5.5",
				"tier": "cheap",
				"order": 0,
				"subscription": {
					"rotation": "round_robin",
					"accounts": [
						{
							"upstream": "openai",
							"account_ref": "ACC_1",
							"label": "alice@example.com",
							"oauth_blob": _BLOB_1_NEW,
						},
						{
							"upstream": "openai",
							"account_ref": "ACC_2",
							"label": "bob@example.com",
							"oauth_blob": "",
						},
					],
				},
			},
			{
				"provider": "openai",
				"model": "gpt-5.4",
				"api_key": "",
				"has_key": True,
				"tier": "strong",
				"order": 1,
			},
		]

	def test_resave_preserves_unreentered_secrets(self):
		self._save()  # initial save with both blobs + the api_key
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or {"action": "pool_update"},
			),
			patch("jarvis.admin_client.post_update_llm_creds"),
		):
			onboarding.save_llm_pool(
				frappe.as_json(self._resave_blanked_payload()), preset=None, routing_mode="failover"
			)
		pushed = pool_calls[0]
		oauth_blobs = pushed["oauth_blobs"]
		# ACC_1 took the freshly-reconnected blob; ACC_2 kept its ORIGINAL blob
		# (blank posted value merged back), not wiped.
		self.assertEqual(oauth_blobs.get("ACC_1"), {"refresh_token": "fake-rt-ACC1-RECONNECTED"})
		self.assertEqual(oauth_blobs.get("ACC_2"), {"refresh_token": "fake-rt-ACC2-secret"})
		# The api_key survived the blank re-post (has_key → merge).
		self.assertIn("sk-apikey-roundtrip-xyz", pushed["api_keys"].values())

	# ------------------------------------------------------------------ #
	# (6) Provider label / alias is canonicalized before persist + wire, so a
	#     custom-mode pool (labels) and a preset pool (ids) converge. Regression
	#     for #200 review #4.
	# ------------------------------------------------------------------ #

	def test_provider_label_normalized_to_canonical_id(self):
		payload = [
			{"provider": "OpenAI", "model": "gpt-5.5", "api_key": "sk-x", "tier": "strong", "order": 0},
			{
				"provider": "Google Gemini",
				"model": "gemini-2.5-pro",
				"api_key": "sk-y",
				"tier": "strong",
				"order": 1,
			},
		]
		pool_calls = []
		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				side_effect=lambda **kw: pool_calls.append(kw) or {"action": "pool_update"},
			),
			patch("jarvis.admin_client.post_update_llm_creds"),
		):
			onboarding.save_llm_pool(frappe.as_json(payload), preset=None, routing_mode="failover")
		providers = {m.get("provider") for m in pool_calls[0]["spec"]["models"]}
		self.assertEqual(providers, {"openai", "gemini"})


class TestOrphanCaptureAdoption(_RT3SettingsTestCase):
	"""save_llm_pool adopts a live capture the client failed to cite.

	The editor blanks capture_id on every load and only re-attaches an ORPHAN one
	in singleMode, so a settings-pane reload after a failed save leaves a valid,
	unconsumed capture that no retry can cite. Without the fallback the account is
	permanently unsavable even though its credential is sitting on the server.
	"""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		frappe.db.delete("Jarvis Pending OAuth Capture")
		frappe.db.commit()

	def _mint(self, account_ref, blob='{"refresh_token":"fake-rt-ORPHAN"}'):
		from jarvis.oauth import pending_capture

		return pending_capture.create_capture(
			provider="openai",
			upstream="openai",
			openclaw_provider="openai",
			oauth_blob=blob,
			account_email="orphan@acme.com",
			account_ref=account_ref,
			safe_label="orphan@acme.com",
			provider_subject="orphan-subject",
		)

	def _payload(self, account_ref, **extra):
		acct = {"upstream": "openai", "account_ref": account_ref, "label": "orphan@acme.com"}
		acct.update(extra)
		return [
			{
				"model": "gpt-5.5",
				"tier": "strong",
				"order": 0,
				"subscription": {"rotation": "sticky", "accounts": [acct]},
			}
		]

	def _save(self, payload):
		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			patch("jarvis.admin_client.post_update_llm_creds"),
		):
			return onboarding.save_llm_pool(frappe.as_json(payload), preset=None, routing_mode="failover")

	def _stored_blob(self):
		s = frappe.get_single("Jarvis Settings")
		row = (s.get("models") or [])[0]
		accounts = json.loads(row.get_password("subscription_accounts", raise_exception=False) or "[]")
		return (accounts[0] or {}).get("oauth_blob") or ""

	def test_orphan_capture_is_adopted_without_capture_id(self):
		"""The regression: account_ref only, no capture_id, nothing stored."""
		view = self._mint("SUB_orphan1")
		self._save(self._payload("SUB_orphan1"))

		self.assertIn("fake-rt-ORPHAN", self._stored_blob())
		row = frappe.db.get_value(
			"Jarvis Pending OAuth Capture",
			{"capture_id": view["capture_id"]},
			["consumed_at", "revocation_state"],
			as_dict=True,
		)
		self.assertTrue(row.consumed_at, "the adopted capture must be burned")
		self.assertEqual(row.revocation_state, "consumed")

	def test_adopted_account_survives_a_resave(self):
		"""Second save cites nothing either; the now-STORED blob carries it."""
		self._mint("SUB_orphan2")
		self._save(self._payload("SUB_orphan2"))
		self._save(self._payload("SUB_orphan2"))
		self.assertIn("fake-rt-ORPHAN", self._stored_blob())

	def test_capture_bound_to_another_connection_is_refused(self):
		"""The F10 anchor fence still bites - the fallback must not bypass it."""
		self._mint("SUB_orphan3")
		frappe.db.set_value(
			"Jarvis Pending OAuth Capture",
			{"account_ref": "SUB_orphan3"},
			"bound_agent_url",
			"ws://some-other-workspace:1",
		)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save(self._payload("SUB_orphan3"))
		self.assertIn("no OAuth credential stored", str(cm.exception))

	def test_another_users_capture_is_never_adopted(self):
		"""Owner scoping: an account_ref from the request body must not reach a
		capture belonging to somebody else."""
		self._mint("SUB_orphan4")
		frappe.db.set_value(
			"Jarvis Pending OAuth Capture",
			{"account_ref": "SUB_orphan4"},
			"owner_user",
			"Guest",
		)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save(self._payload("SUB_orphan4"))
		self.assertIn("no OAuth credential stored", str(cm.exception))

	def test_no_capture_at_all_still_refuses(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save(self._payload("SUB_nothing_here"))
		self.assertIn("no OAuth credential stored", str(cm.exception))
