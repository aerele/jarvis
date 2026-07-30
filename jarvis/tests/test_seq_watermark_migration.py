"""Migration test for v2_10: openclaw_seq_watermark -> agent_seq_watermark copy.

Exercises the real patch against populated rows on both columns (the old column
survives model-sync as an orphan, so both exist during the transition). Verifies
the copy fills the new column and is clobber-safe on re-run.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.patches.v2_10_rename_openclaw_seq_watermark import execute

MSG = "Jarvis Chat Message"


class TestSeqWatermarkMigration(FrappeTestCase):
	def setUp(self):
		# The old column only exists on sites migrated from the pre-rename schema (model-sync
		# keeps it as an orphan). On a fresh install the JSON ships only agent_seq_watermark, so
		# v2_10 is a documented no-op (its own has_column guard) and there is no transition to
		# exercise - skip rather than error (or mutate the shared schema) on the missing column.
		if "openclaw_seq_watermark" not in frappe.db.get_table_columns(MSG):
			self.skipTest("no orphan openclaw_seq_watermark column (fresh install); v2_10 is a no-op")
		self.conv = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "wm", "session_key": "wm-migration-test"}
		).insert(ignore_permissions=True)
		self.msg = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv.name,
				"seq": 1,
				"role": "assistant",
				"content": "x",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(MSG, {"conversation": self.conv.name})
		frappe.db.delete("Jarvis Conversation", {"name": self.conv.name})
		frappe.db.commit()

	def _set_cols(self, old, new):
		frappe.db.sql(
			"UPDATE `tabJarvis Chat Message` SET openclaw_seq_watermark=%s, agent_seq_watermark=%s WHERE name=%s",
			(old, new, self.msg.name),
		)

	def _new(self):
		return frappe.db.get_value(MSG, self.msg.name, "agent_seq_watermark")

	def test_copies_old_watermark_when_new_is_zero(self):
		self._set_cols(old=42, new=0)
		execute()
		self.assertEqual(self._new(), 42)

	def test_idempotent_does_not_clobber_a_migrated_row(self):
		# already migrated: new set, old since zeroed -> a re-run must leave it alone
		self._set_cols(old=0, new=5)
		execute()
		self.assertEqual(self._new(), 5)

	def test_does_not_downgrade_when_new_already_fresher(self):
		# new carries a fresher value than the stale old column -> never overwrite
		self._set_cols(old=3, new=9)
		execute()
		self.assertEqual(self._new(), 9)
