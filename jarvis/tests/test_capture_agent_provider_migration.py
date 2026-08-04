"""Transition test for the capture provider-key rename (v2_12 copy patch).

Same shape as test_seq_watermark_migration: on an upgraded site the old
openclaw_provider column survives model-sync as an orphan; on a fresh install it
never exists. The upgrade shape is the one the patch exists for, so setUpClass
CREATES the legacy column to simulate it rather than skipping (which would make
this suite vacuous on every fresh CI site), and tearDownClass drops it again if we
added it, leaving a fresh site pristine.

Why it matters: agent_provider is what _revoke_token uses to find the provider's
revocation endpoint. A row that loses it revokes NOTHING and returns the terminal
"unsupported" state, so a live refresh token stays valid upstream while the local
ciphertext is erased.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.patches.v2_12_rename_capture_agent_provider import execute

DT = "Jarvis Pending OAuth Capture"


class TestCaptureAgentProviderMigration(FrappeTestCase):
	_added_legacy_col = False

	@staticmethod
	def _legacy_col_exists() -> bool:
		# Ask the SERVER, not get_table_columns - that helper is redis/client cached
		# and the DDL below makes the cache lie (the same staleness once made the
		# watermark patch's own has-column guard no-op on CI).
		return bool(frappe.db.sql(f"SHOW COLUMNS FROM `tab{DT}` LIKE 'openclaw_provider'"))

	@staticmethod
	def _bust_columns_cache():
		key = f"table_columns::tab{DT}"
		frappe.cache.delete_value(key)
		try:
			frappe.client_cache.delete_value(key)
		except Exception:
			pass

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not cls._legacy_col_exists():
			frappe.db.sql_ddl(f"ALTER TABLE `tab{DT}` ADD COLUMN openclaw_provider VARCHAR(140)")
			cls._added_legacy_col = True
		cls._bust_columns_cache()
		assert cls._legacy_col_exists(), "simulated-upgrade column did not stick"

	@classmethod
	def tearDownClass(cls):
		if cls._added_legacy_col and cls._legacy_col_exists():
			frappe.db.sql_ddl(f"ALTER TABLE `tab{DT}` DROP COLUMN openclaw_provider")
		cls._bust_columns_cache()
		super().tearDownClass()

	def setUp(self):
		self.doc = frappe.get_doc(
			{
				"doctype": DT,
				"capture_id": "oacap_migration_test",
				"owner_user": frappe.session.user,
				"expires_at": add_to_date(now_datetime(), minutes=30),
				"revocation_state": "pending",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DT, {"capture_id": "oacap_migration_test"})
		frappe.db.commit()

	def _set_cols(self, old, new):
		frappe.db.sql(
			f"UPDATE `tab{DT}` SET openclaw_provider=%s, agent_provider=%s WHERE name=%s",
			(old, new, self.doc.name),
		)

	def _new(self):
		return frappe.db.get_value(DT, self.doc.name, "agent_provider")

	def test_copies_old_provider_when_new_is_blank(self):
		self._set_cols(old="google-gemini-cli", new="")
		execute()
		self.assertEqual(self._new(), "google-gemini-cli")

	def test_copies_when_new_is_null(self):
		self._set_cols(old="xai", new=None)
		execute()
		self.assertEqual(self._new(), "xai")

	def test_idempotent_does_not_clobber_a_migrated_row(self):
		# already migrated: new set, old since blanked -> a re-run must leave it alone
		self._set_cols(old="", new="openai")
		execute()
		self.assertEqual(self._new(), "openai")

	def test_never_overwrites_a_live_value_with_a_stale_one(self):
		self._set_cols(old="openai", new="kimi")
		execute()
		self.assertEqual(self._new(), "kimi")
