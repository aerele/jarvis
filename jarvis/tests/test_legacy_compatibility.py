"""The bundled legacy-schema contract matches the reviewed upgrade fixture."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import legacy_compatibility as compat
from jarvis.tests._legacy_migration_fixtures import load


class TestLegacyCompatibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.fixture = load()
		super().setUpClass()

	def setUp(self):
		frappe.db.savepoint("legacy_compatibility_test")
		self._forget()

	def tearDown(self):
		try:
			frappe.db.rollback(save_point="legacy_compatibility_test")
		finally:
			self._forget()

	def _forget(self):
		if hasattr(frappe.local, compat._MEMO):
			delattr(frappe.local, compat._MEMO)

	def test_contract_matches_independent_upgrade_fixture(self):
		contract = compat.get_contract()
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
