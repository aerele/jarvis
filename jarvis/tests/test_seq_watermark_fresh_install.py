"""The watermark compatibility layer needs no legacy schema on fresh sites."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.chat import seq_watermark as wm


class TestFreshWatermarkSchema(unittest.TestCase):
	def test_fresh_site_uses_only_the_current_column(self):
		db = Mock()
		db.sql.return_value = []
		with (
			patch.object(wm.frappe, "db", db, create=True),
			patch.object(wm.frappe, "local", SimpleNamespace()),
		):
			self.assertFalse(wm.has_legacy_column())
			self.assertIn("WHERE Field =", db.sql.call_args.args[0])
			self.assertEqual(wm.wm_expr("m."), "m.agent_seq_watermark")
			wm.stamp_watermark("test-message", 17)
			query, params = db.sql.call_args.args
			self.assertIn("SET agent_seq_watermark=%(w)s WHERE", query)
			self.assertEqual(params, {"w": 17, "n": "test-message"})
			# The schema lookup is memoized per request.
			self.assertEqual(db.sql.call_count, 2)
