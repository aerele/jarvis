"""Password fields on Jarvis Settings must never land in tabSingles as plaintext.

Frappe only encrypts a Password field (into __Auth) when the value passes
through Document._save_passwords() - i.e. a real doc.save(). Several flows
here deliberately write via db_set to avoid re-triggering on_update's admin
sync (onboarding.write_connection, chat/device.py, api.rotate_agent_token);
before the fix each of those wrote the raw secret straight into the Single's
row in tabSingles.

These tests pin the fixed behavior of jarvis._password_utils and every
call site:
- the RAW tabSingles column (read via direct SQL, no ORM decryption) holds
  only an all-asterisk mask, never the secret;
- get_password() still returns the real secret (readers are unchanged);
- clear paths (chat clear_credentials) also drop the __Auth row so a
  revoked secret cannot be resurrected by get_password;
- the v1_11 migration patch moves an already-plaintext column value into
  __Auth, masks the column, and is idempotent (a second run must NOT
  encrypt the mask over the real secret).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

SETTINGS = "Jarvis Settings"

_ALL_PASSWORD_FIELDS = (
	"llm_api_key",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_password",
	"agent_token",
	"chat_device_private_key",
	"chat_device_token",
)

# Plain (non-Password) fields the exercised flows also write.
_PLAIN_FIELDS = (
	"jarvis_admin_customer_email",
	"agent_url",
	"agent_token_issued_at",
	"chat_device_id",
	"chat_device_public_key",
)


def _raw_singles_value(field: str) -> str | None:
	"""The value exactly as stored in tabSingles - no decryption, no cache."""
	rows = frappe.db.sql(
		"select `value` from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, field),
	)
	return rows[0][0] if rows else None


def _auth_row(field: str):
	"""The raw __Auth row for a Jarvis Settings password field (or None)."""
	rows = frappe.db.sql(
		"select `password`, `encrypted` from `__Auth` where doctype=%s and name=%s and fieldname=%s",
		(SETTINGS, SETTINGS, field),
	)
	return rows[0] if rows else None


def _is_masked(value: str | None) -> bool:
	return bool(value) and set(value) == {"*"}


class _SecretsSnapshotTestCase(FrappeTestCase):
	"""Snapshot/restore the raw tabSingles column AND the raw __Auth row for
	every Password field (plus the plain fields these flows touch), so the
	suite leaves the singleton exactly as found - byte-for-byte, whichever
	representation (plaintext-legacy or encrypted) the site had."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._raw_snapshot = {f: _raw_singles_value(f) for f in _ALL_PASSWORD_FIELDS}
		cls._auth_snapshot = {f: _auth_row(f) for f in _ALL_PASSWORD_FIELDS}
		s = frappe.get_single(SETTINGS)
		cls._plain_snapshot = {f: s.get(f) for f in _PLAIN_FIELDS}

	@classmethod
	def tearDownClass(cls):
		try:
			for field, raw in cls._raw_snapshot.items():
				frappe.db.set_single_value(SETTINGS, field, raw or "", update_modified=False)
			for field, row in cls._auth_snapshot.items():
				frappe.db.sql(
					"delete from `__Auth` where doctype=%s and name=%s and fieldname=%s",
					(SETTINGS, SETTINGS, field),
				)
				if row:
					frappe.db.sql(
						"insert into `__Auth` (doctype, name, fieldname, `password`, encrypted) "
						"values (%s, %s, %s, %s, %s)",
						(SETTINGS, SETTINGS, field, row[0], row[1]),
					)
			for field, value in cls._plain_snapshot.items():
				frappe.db.set_single_value(
					SETTINGS, field, value if value is not None else "", update_modified=False
				)
			frappe.db.commit()
		finally:
			super().tearDownClass()

	def setUp(self):
		super().setUp()
		# Each test starts with every password field fully absent (no column
		# value, no __Auth row) so cross-test residue can't mask a regression.
		from frappe.utils.password import remove_encrypted_password

		for field in _ALL_PASSWORD_FIELDS:
			frappe.db.set_single_value(SETTINGS, field, "", update_modified=False)
			remove_encrypted_password(SETTINGS, SETTINGS, field)
		frappe.db.commit()

	def assert_encrypted_at_rest(self, field: str, secret: str):
		"""The core Fix-1 invariant: raw column masked, get_password intact."""
		raw = _raw_singles_value(field)
		self.assertNotEqual(raw, secret, f"{field}: raw tabSingles column must NOT hold the plaintext secret")
		self.assertTrue(
			_is_masked(raw), f"{field}: raw tabSingles column must be an all-asterisk mask, got {raw!r}"
		)
		s = frappe.get_single(SETTINGS)
		self.assertEqual(
			s.get_password(field, raise_exception=False),
			secret,
			f"{field}: get_password must still return the secret",
		)
		auth = _auth_row(field)
		self.assertTrue(auth and auth[1] == 1, f"{field}: __Auth must hold an encrypted=1 row")
		self.assertNotEqual(
			auth[0], secret, f"{field}: __Auth.password must be the ENCRYPTED form, not plaintext"
		)


class TestOnboardingWriteConnection(_SecretsSnapshotTestCase):
	def test_write_connection_encrypts_all_password_fields(self):
		from jarvis import onboarding

		onboarding.write_connection(
			{
				"api_key": "native-key-abc123",
				"api_secret": "native-secret-def456",
				"customer": "cust@example.com",
				"customer_password": "grant-pass-xyz789",
				"agent_url": "http://127.0.0.1:19000",
				"agent_token": "agent-tok-0011223344",
			}
		)

		self.assert_encrypted_at_rest("jarvis_admin_api_key", "native-key-abc123")
		self.assert_encrypted_at_rest("jarvis_admin_api_secret", "native-secret-def456")
		self.assert_encrypted_at_rest("jarvis_admin_customer_password", "grant-pass-xyz789")
		self.assert_encrypted_at_rest("agent_token", "agent-tok-0011223344")
		# Plain fields still written verbatim.
		s = frappe.get_single(SETTINGS)
		self.assertEqual(s.jarvis_admin_customer_email, "cust@example.com")
		self.assertEqual(s.agent_url, "http://127.0.0.1:19000")

	def test_write_connection_skips_absent_fields(self):
		"""Partial payloads (email-only signup response) must not touch the
		other credentials."""
		from jarvis import onboarding

		onboarding.write_connection({"customer": "only-email@example.com"})
		for field in (
			"jarvis_admin_api_key",
			"jarvis_admin_api_secret",
			"jarvis_admin_customer_password",
			"agent_token",
		):
			self.assertFalse(_raw_singles_value(field), f"{field}: must stay empty on a partial payload")
			self.assertIsNone(_auth_row(field))


class TestChatDeviceCredentialWrites(_SecretsSnapshotTestCase):
	def test_save_credentials_encrypts_private_key_and_token(self):
		from jarvis.chat import device as chat_device

		s = frappe.get_single(SETTINGS)
		chat_device._save_credentials(
			settings_doc=s,
			private_key_b64u="priv-key-material-b64u",
			public_key_b64u="pub-key-b64u",
			device_id="device-id-hex",
			device_token="device-bearer-tok-1",
		)

		self.assert_encrypted_at_rest("chat_device_private_key", "priv-key-material-b64u")
		self.assert_encrypted_at_rest("chat_device_token", "device-bearer-tok-1")
		s = frappe.get_single(SETTINGS)
		self.assertEqual(s.chat_device_id, "device-id-hex")
		self.assertEqual(s.chat_device_public_key, "pub-key-b64u")

	def test_clear_credentials_removes_auth_rows(self):
		"""db_set('') alone leaves __Auth intact and get_password keeps
		returning the revoked secret - the clear must drop the __Auth rows."""
		from jarvis.chat import device as chat_device

		s = frappe.get_single(SETTINGS)
		chat_device._save_credentials(
			settings_doc=s,
			private_key_b64u="priv-to-revoke",
			public_key_b64u="pub",
			device_id="dev-revoked",
			device_token="tok-to-revoke",
		)
		# Pre-condition: secrets are retrievable.
		s = frappe.get_single(SETTINGS)
		self.assertEqual(s.get_password("chat_device_token", raise_exception=False), "tok-to-revoke")

		chat_device.clear_credentials()

		s = frappe.get_single(SETTINGS)
		self.assertFalse(
			s.get_password("chat_device_private_key", raise_exception=False),
			"revoked private key must NOT be readable after clear_credentials",
		)
		self.assertFalse(
			s.get_password("chat_device_token", raise_exception=False),
			"revoked device token must NOT be readable after clear_credentials",
		)
		self.assertIsNone(
			_auth_row("chat_device_private_key"), "__Auth row for chat_device_private_key must be removed"
		)
		self.assertIsNone(_auth_row("chat_device_token"), "__Auth row for chat_device_token must be removed")
		self.assertFalse(s.chat_device_id)
		self.assertFalse(s.chat_device_public_key)

	def test_update_device_token_encrypts_rotated_token(self):
		from jarvis.chat import device as chat_device

		s = frappe.get_single(SETTINGS)
		chat_device._save_credentials(
			settings_doc=s,
			private_key_b64u="priv-x",
			public_key_b64u="pub-x",
			device_id="dev-current",
			device_token="tok-original",
		)

		self.assertTrue(chat_device.update_device_token("tok-rotated-by-gateway", device_id="dev-current"))
		self.assert_encrypted_at_rest("chat_device_token", "tok-rotated-by-gateway")


class TestRotateAgentToken(_SecretsSnapshotTestCase):
	def test_rotate_agent_token_encrypts_new_token(self):
		from jarvis import api as jarvis_api

		with patch("jarvis.admin_client.post_rotate_agent_token", return_value={}):
			out = jarvis_api.rotate_agent_token()

		self.assertTrue(out["ok"], f"rotation must succeed, got: {out}")
		s = frappe.get_single(SETTINGS)
		new_token = s.get_password("agent_token", raise_exception=False) or ""
		self.assertEqual(len(new_token), 64, "rotated token must be 64 hex chars")
		self.assertFalse(_is_masked(new_token))
		self.assert_encrypted_at_rest("agent_token", new_token)


class TestV111EncryptPlaintextPatch(_SecretsSnapshotTestCase):
	"""The v1_11 migration: plaintext column values move to __Auth + mask."""

	def _execute_patch(self):
		from jarvis.patches import v1_13_encrypt_plaintext_settings_passwords as patch_mod

		patch_mod.execute()

	def test_plaintext_values_are_encrypted_and_masked(self):
		# Simulate the pre-fix state: raw plaintext written straight into
		# tabSingles (what the old db_set call sites produced), no __Auth row.
		seeded = {
			"agent_token": "plain-agent-token-1",
			"jarvis_admin_api_secret": "plain-admin-secret-2",
			"chat_device_private_key": "plain-priv-key-3",
		}
		for field, value in seeded.items():
			frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
		frappe.db.commit()

		self._execute_patch()

		for field, value in seeded.items():
			self.assert_encrypted_at_rest(field, value)

	def test_patch_skips_empty_and_masked_fields(self):
		# One field already in the CORRECT post-fix state...
		from frappe.utils.password import set_encrypted_password

		set_encrypted_password(SETTINGS, SETTINGS, "already-good-secret", "agent_token")
		frappe.db.set_single_value(SETTINGS, "agent_token", "*" * 10, update_modified=False)
		# ...everything else empty.
		frappe.db.commit()

		self._execute_patch()

		# The masked field must be untouched: encrypting the MASK over the
		# stored secret would destroy the credential.
		s = frappe.get_single(SETTINGS)
		self.assertEqual(
			s.get_password("agent_token", raise_exception=False),
			"already-good-secret",
			"patch must NOT re-encrypt the mask over the real secret",
		)
		# Empty fields stay empty - no __Auth rows conjured from nothing.
		for field in _ALL_PASSWORD_FIELDS:
			if field == "agent_token":
				continue
			self.assertIsNone(
				_auth_row(field), f"{field}: patch must not create __Auth rows for empty fields"
			)

	def test_patch_is_idempotent(self):
		frappe.db.set_single_value(
			SETTINGS, "jarvis_admin_api_key", "plain-key-run-twice", update_modified=False
		)
		frappe.db.commit()

		self._execute_patch()
		self._execute_patch()

		self.assert_encrypted_at_rest("jarvis_admin_api_key", "plain-key-run-twice")

	def test_patch_purges_stale_auth_row_behind_empty_column(self):
		"""The pre-fix clear path (clear_credentials)
		db_set("") the column WITHOUT remove_encrypted_password, so the
		REVOKED secret survived in __Auth - and get_password falls through to
		__Auth whenever the column is falsy, resurrecting it. The patch must
		purge __Auth rows behind empty columns."""
		from frappe.utils.password import set_encrypted_password

		# Simulate the pre-fix cleared state: secret in __Auth, empty column
		# (setUp already blanked every column).
		set_encrypted_password(SETTINGS, SETTINGS, "revoked-secret", "chat_device_token")
		frappe.db.commit()

		# Precondition: the revoked secret is still served via the fallback.
		s = frappe.get_single(SETTINGS)
		self.assertEqual(
			s.get_password("chat_device_token", raise_exception=False),
			"revoked-secret",
			"precondition: stale __Auth secret must be readable pre-patch",
		)

		self._execute_patch()

		s = frappe.get_single(SETTINGS)
		self.assertFalse(
			s.get_password("chat_device_token", raise_exception=False),
			"patch must purge the stale __Auth row behind an empty column",
		)
		self.assertIsNone(_auth_row("chat_device_token"), "__Auth row must be gone after the patch")
		self.assertFalse(
			_raw_singles_value("chat_device_token"), "column must stay empty - nothing to backfill"
		)
