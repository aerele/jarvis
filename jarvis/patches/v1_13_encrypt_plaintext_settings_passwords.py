"""Encrypt plaintext-written Password fields on Jarvis Settings.

Several call sites (jarvis/onboarding.py::write_connection,
jarvis/chat/device.py::_save_credentials/update_device_token,
jarvis/api.py::rotate_agent_token)
wrote Password field values via doc.db_set(...), which stores exactly what
it's given straight into the Single's row in tabSingles - no encryption.
Only Document.save()'s _save_passwords path (or a direct
set_encrypted_password call) puts a Password field's real value into the
__Auth table. So any of the seven Password fields on Jarvis Settings that
was ever written through one of those call sites has sat in tabSingles in
PLAINTEXT, readable by a bare SELECT despite every reader going through
get_password() and expecting encryption-at-rest.

This is a one-time cleanup for state written BEFORE the app-code fix landed
(same commit as this patch). For each Password field, read the RAW
tabSingles value - frappe.db.get_single_value does a plain SELECT against
Singles with no decryption, so it returns exactly what's stored, plaintext
or mask alike (frappe.db.get_value("Jarvis Settings", "Jarvis Settings",
field) resolves to the same Singles read for a Single doctype; either works,
get_single_value is used here as the more direct/documented API). Then
three branches:

- EMPTY column -> purge any __Auth row. The pre-fix clear paths
  (chat/device.clear_credentials) blanked
  the column WITHOUT remove_encrypted_password, and fields also saved via a
  full .save() at some point in history have __Auth rows - so a REVOKED
  secret can survive behind the empty column, and BaseDocument.get_password
  falls through to __Auth whenever the column is falsy, resurrecting it.
  Harmless no-op for never-set fields.
- MASKED (all-asterisk) column -> leave untouched. It is already in the
  correct post-fix representation; re-encrypting the mask over the stored
  secret would destroy the credential.
- PLAINTEXT column -> move the value into __Auth via set_encrypted_password
  and overwrite the column with a mask - exactly what
  Document._save_passwords does on a normal save().

Idempotent: after one run every field is empty-and-no-__Auth or
masked-with-__Auth, both of which the branches above leave unchanged.
"""

import frappe
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

from jarvis._password_utils import _MASK

SETTINGS = "Jarvis Settings"

# Every Password-fieldtype field on Jarvis Settings (jarvis_settings.json).
_PASSWORD_FIELDS = (
	"llm_api_key",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_password",
	"agent_token",
	"chat_device_private_key",
	"chat_device_token",
)


# Matches BaseDocument.is_dummy_password: non-empty and every char is '*'.
def _is_dummy_password(value: str) -> bool:
	return bool(value) and set(value) == {"*"}


def execute():
	for field in _PASSWORD_FIELDS:
		raw = frappe.db.get_single_value(SETTINGS, field, cache=False)
		if not raw:
			# Cleared/never-set column: drop any stale __Auth row hiding
			# behind it so get_password can't resurrect a revoked secret
			# (see module docstring, branch 1).
			remove_encrypted_password(SETTINGS, SETTINGS, field)
			continue
		if _is_dummy_password(raw):
			continue
		set_encrypted_password(SETTINGS, SETTINGS, raw, field)
		frappe.db.set_single_value(SETTINGS, field, _MASK, update_modified=False)
	frappe.db.commit()
