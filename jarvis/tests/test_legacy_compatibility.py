"""Direct upgrades retain historical data and patch completion semantics."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import legacy_compatibility as compat
from jarvis.patches import v1_0_migrate_legacy_agent_settings as migration
from jarvis.tests._legacy_migration_fixtures import load


class TestLegacyCompatibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.fixture = load()
		super().setUpClass()

	def setUp(self):
		frappe.db.savepoint("legacy_compatibility_test")
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()
		self.clear_cache = patch.object(frappe, "clear_cache")
		self.clear_cache.start()
		self._forget()

	def tearDown(self):
		try:
			frappe.db.rollback(save_point="legacy_compatibility_test")
		finally:
			self.commit.stop()
			self.clear_cache.stop()
			self._forget()

	def _forget(self):
		if hasattr(frappe.local, compat._MEMO):
			delattr(frappe.local, compat._MEMO)

	def test_contract_matches_independent_upgrade_fixture(self):
		contract = compat.get_contract()
		self.assertEqual(dict(contract.settings_renames), self.fixture["settings"]["renames"])
		self.assertEqual(contract.settings_patch, self.fixture["settings"]["legacy_patch"])
		self.assertEqual(contract.watermark_column, self.fixture["watermark"]["legacy_column"])
		self.assertEqual(contract.capture_provider_column, self.fixture["capture_provider"]["legacy_column"])

	def test_missing_mirror_bootstraps_offline_without_runtime_writes(self):
		frappe.db.delete("DefaultValue", {"name": compat.ROW})
		self.assertEqual(compat.get_contract(), compat._embedded()[1])
		self.assertFalse(frappe.db.exists("DefaultValue", compat.ROW))
		compat.seed()
		compat.seed()
		self.assertEqual(frappe.db.get_value("DefaultValue", compat.ROW, "defvalue"), compat._embedded()[0])
		self.assertEqual(frappe.db.count("DefaultValue", {"name": compat.ROW}), 1)

	def test_corrupt_mirror_cannot_change_identifiers_and_is_repaired(self):
		compat.seed()
		frappe.db.set_value("DefaultValue", compat.ROW, "defvalue", '{"watermark_column":"unsafe"}')
		self._forget()
		self.assertEqual(compat.get_contract(), compat._embedded()[1])
		compat.seed()
		self.assertEqual(frappe.db.get_value("DefaultValue", compat.ROW, "defvalue"), compat._embedded()[0])

	def test_storage_collision_does_not_overwrite_another_owner(self):
		compat.seed()
		frappe.db.set_value("DefaultValue", compat.ROW, "parent", "another_owner")
		with self.assertRaisesRegex(ValueError, "collision"):
			compat.seed()
		self.assertEqual(frappe.db.get_value("DefaultValue", compat.ROW, "parent"), "another_owner")

	def _reset_settings(self, mode):
		self.renames = self.fixture["settings"]["renames"]
		self.old_patch = self.fixture["settings"]["legacy_patch"]
		self.new_patch = self.fixture["settings"]["patch"].removesuffix(".execute")
		fields = tuple(set(self.renames) | set(self.renames.values()))
		frappe.db.sql("DELETE FROM tabSingles WHERE doctype=%s AND field IN %s", ("Jarvis Settings", fields))
		frappe.db.sql(
			"DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname IN %s",
			("Jarvis Settings", "Jarvis Settings", fields),
		)
		frappe.db.delete("Patch Log", {"patch": ["in", [self.old_patch, self.new_patch]]})
		for old, new in self.renames.items():
			for field, value in ((old, "previous"), (new, "current")):
				include = (field == old and mode in {"legacy", "both", "auth_only"}) or (
					field == new and mode in {"both", "current"}
				)
				if not include:
					continue
				if mode != "auth_only":
					frappe.db.sql(
						"INSERT INTO tabSingles (doctype,field,value) VALUES (%s,%s,%s)",
						("Jarvis Settings", field, value),
					)
				if new in {"agent_token", "jarvis_admin_api_key"}:
					frappe.db.sql(
						"INSERT INTO `__Auth` (doctype,name,fieldname,password,encrypted) VALUES (%s,%s,%s,%s,1)",
						("Jarvis Settings", "Jarvis Settings", field, value + "-ciphertext"),
					)

	def test_direct_upgrade_and_repeat_preserve_values_and_ciphertext(self):
		for mode in ("empty", "legacy", "both", "current", "auth_only"):
			with self.subTest(mode=mode):
				self._reset_settings(mode)
				migration.execute()
				migration.execute()
				value = "current" if mode in {"both", "current"} else "previous"
				for old, new in self.renames.items():
					self.assertFalse(
						frappe.db.exists("Singles", {"doctype": "Jarvis Settings", "field": old})
					)
					rows = frappe.db.sql(
						"SELECT value FROM tabSingles WHERE doctype=%s AND field=%s", ("Jarvis Settings", new)
					)
					self.assertEqual(rows, () if mode in {"empty", "auth_only"} else ((value,),))
					if new in {"agent_token", "jarvis_admin_api_key"}:
						rows = frappe.db.sql(
							"SELECT password,encrypted FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s",
							("Jarvis Settings", "Jarvis Settings", new),
						)
						self.assertEqual(rows, () if mode == "empty" else ((value + "-ciphertext", 1),))

	def test_historical_completion_prevents_resurrecting_settings(self):
		from frappe.modules.patch_handler import run_single, update_patch_log

		self._reset_settings("legacy")
		update_patch_log(self.old_patch)
		with patch.object(migration, "execute", wraps=migration.execute) as execute:
			run_single(self.new_patch)
			run_single(self.new_patch)
			self.assertEqual(execute.call_count, 1)
		for old, new in self.renames.items():
			self.assertTrue(
				frappe.db.sql(
					"SELECT 1 FROM tabSingles WHERE doctype=%s AND field=%s", ("Jarvis Settings", old)
				)
			)
			self.assertFalse(
				frappe.db.sql(
					"SELECT 1 FROM tabSingles WHERE doctype=%s AND field=%s", ("Jarvis Settings", new)
				)
			)
		self.assertTrue(frappe.db.exists("Patch Log", {"patch": self.old_patch, "skipped": 0}))

	def test_skipped_historical_patch_is_retried(self):
		self._reset_settings("legacy")
		frappe.get_doc({"doctype": "Patch Log", "patch": self.old_patch, "skipped": 1}).insert(
			ignore_permissions=True
		)
		migration.execute()
		self.assertTrue(frappe.db.exists("Patch Log", {"patch": self.old_patch, "skipped": 0}))
		for new in self.renames.values():
			self.assertEqual(
				frappe.db.sql(
					"SELECT value FROM tabSingles WHERE doctype=%s AND field=%s", ("Jarvis Settings", new)
				),
				(("previous",),),
			)
