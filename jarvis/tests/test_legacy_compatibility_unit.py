"""Bundled compatibility decoding and request caching fail predictably."""

import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import legacy_compatibility as compat


class TestCompatibilityContract(unittest.TestCase):
	def test_modified_bundle_fails_checksum(self):
		compat._embedded.cache_clear()
		try:
			with patch.object(compat, "_PAYLOAD", "e30="), self.assertRaisesRegex(ValueError, "checksum"):
				compat._embedded()
		finally:
			compat._embedded.cache_clear()

	def test_contract_is_immutable(self):
		contract = compat._embedded()[1]
		with self.assertRaises(FrozenInstanceError):
			contract.watermark_column = "changed"
		self.assertIsInstance(contract.settings_renames, tuple)
		self.assertTrue(all(isinstance(pair, tuple) for pair in contract.settings_renames))

	def test_request_reads_database_once_for_valid_missing_and_corrupt_mirrors(self):
		for value in (compat._embedded()[0], None, "invalid"):
			with self.subTest(value_type=type(value)):
				db = Mock()
				db.get_value.return_value = value
				with (
					patch.object(compat.frappe, "db", db, create=True),
					patch.object(compat.frappe, "local", SimpleNamespace()),
				):
					first = compat.get_contract()
					self.assertIs(first, compat.get_contract())
					self.assertEqual(first, compat._embedded()[1])
					db.get_value.assert_called_once()
					db.set_value.assert_not_called()
