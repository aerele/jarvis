"""Watermark reconciliation requires no legacy schema or setup on fresh sites."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.chat import seq_watermark as wm


class TestFreshWatermarkSchema(unittest.TestCase):
	def test_migrate_is_a_noop_without_legacy_column(self):
		db = Mock()
		db.sql.return_value = []
		with (
			patch.object(wm.frappe, "db", db, create=True),
			patch.object(wm.frappe, "local", SimpleNamespace()),
		):
			wm.reconcile_watermarks()
			db.sql.assert_called_once()
			self.assertTrue(db.sql.call_args.args[0].startswith("SHOW COLUMNS"))
			self.assertFalse(wm.has_legacy_column())
			self.assertEqual(wm.wm_expr("m."), "m.agent_seq_watermark")
			wm.stamp_watermark("test-message", 17)
			query, params = db.sql.call_args.args
			self.assertIn("SET agent_seq_watermark=%(w)s WHERE", query)
			self.assertEqual(params, {"w": 17, "n": "test-message"})

	def test_migrate_rechecks_schema_after_a_stale_negative_lookup(self):
		db = Mock()
		db.sql.side_effect = [[("existing-column",)], []]
		local = SimpleNamespace(_jarvis_wm_legacy_col=False)
		with patch.object(wm.frappe, "db", db, create=True), patch.object(wm.frappe, "local", local):
			wm.reconcile_watermarks()
			self.assertTrue(local._jarvis_wm_legacy_col)
			self.assertEqual(db.sql.call_count, 2)
			self.assertIn("WHERE Field =", db.sql.call_args_list[0].args[0])
			self.assertIn("GREATEST", db.sql.call_args_list[1].args[0])
