"""Transition tests for the watermark rename: the v2_10 copy patch AND the
dual-write/GREATEST-read compatibility layer (jarvis.chat.seq_watermark).

On an upgraded site the old column survives model-sync as an orphan; on a fresh
install it never exists. The upgrade shape is the one the migration exists for —
so instead of skipping when the column is absent (which made this suite vacuous
on every fresh CI site), setUpClass CREATES the legacy column to simulate the
upgrade, and tearDownClass drops it again if we added it (leaving a fresh site
pristine).
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import seq_watermark
from jarvis.chat.turn_recovery import _next_turn_watermark
from jarvis.patches.v2_10_rename_openclaw_seq_watermark import execute

MSG = "Jarvis Chat Message"


class TestSeqWatermarkMigration(FrappeTestCase):
	_added_legacy_col = False

	@staticmethod
	def _legacy_col_exists() -> bool:
		# Ask the SERVER, not get_table_columns — that helper is redis/client cached
		# and the DDL below makes the cache lie (this exact staleness broke CI once:
		# the patch's own has-column guard read the stale cache and no-op'd).
		return bool(frappe.db.sql(f"SHOW COLUMNS FROM `tab{MSG}` LIKE 'openclaw_seq_watermark'"))

	@staticmethod
	def _bust_columns_cache():
		key = f"table_columns::tab{MSG}"
		frappe.cache.delete_value(key)
		try:
			frappe.client_cache.delete_value(key)
		except Exception:
			pass
		frappe.local._jarvis_wm_legacy_col = None

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not cls._legacy_col_exists():
			frappe.db.sql_ddl(
				f"ALTER TABLE `tab{MSG}` ADD COLUMN openclaw_seq_watermark INT(11) NOT NULL DEFAULT 0"
			)
			cls._added_legacy_col = True
		cls._bust_columns_cache()
		assert cls._legacy_col_exists(), "simulated-upgrade column did not stick"

	@classmethod
	def tearDownClass(cls):
		if cls._added_legacy_col and cls._legacy_col_exists():
			frappe.db.sql_ddl(f"ALTER TABLE `tab{MSG}` DROP COLUMN openclaw_seq_watermark")
		cls._bust_columns_cache()
		super().tearDownClass()

	def setUp(self):
		# the per-request column cache must not leak between tests / the DDL above
		frappe.local._jarvis_wm_legacy_col = None
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
		frappe.local._jarvis_wm_legacy_col = None

	def _set_cols(self, old, new, name=None):
		frappe.db.sql(
			"UPDATE `tabJarvis Chat Message` SET openclaw_seq_watermark=%s, agent_seq_watermark=%s WHERE name=%s",
			(old, new, name or self.msg.name),
		)

	def _cols(self, name=None):
		return frappe.db.sql(
			"SELECT agent_seq_watermark, openclaw_seq_watermark FROM `tabJarvis Chat Message` WHERE name=%s",
			(name or self.msg.name,),
		)[0]

	def _new(self):
		return frappe.db.get_value(MSG, self.msg.name, "agent_seq_watermark")

	# ---- the v2_10 one-shot copy ----

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

	# ---- the transition compatibility layer (dual-write + GREATEST read) ----

	def test_stamp_watermark_dual_writes_while_legacy_column_exists(self):
		"""A rollback's old-code readers look only at the legacy column: while it
		exists, every new-code write must land on BOTH columns."""
		self.assertTrue(seq_watermark.has_legacy_column())
		seq_watermark.stamp_watermark(self.msg.name, 17)
		self.assertEqual(self._cols(), (17, 17))

	def test_read_expr_sees_a_legacy_only_write(self):
		"""The forward deploy window: an old-code worker stamped ONLY the legacy
		column after the one-shot copy ran. The read expression must surface that
		value — a plain new-column read would return 0 and disable the fence."""
		self._set_cols(old=23, new=0)
		val = frappe.db.sql(
			f"SELECT {seq_watermark.wm_expr()} FROM `tab{MSG}` WHERE name=%s", (self.msg.name,)
		)[0][0]
		self.assertEqual(int(val), 23)

	def test_next_turn_watermark_honors_legacy_only_later_turn(self):
		"""The real recovery read path: a LATER turn whose watermark exists only on
		the legacy column must still bound this turn's transcript window."""
		later = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv.name,
				"seq": 2,
				"role": "assistant",
				"content": "y",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self._set_cols(old=31, new=0, name=later.name)
		self.assertEqual(_next_turn_watermark(self.conv.name, 1), 31)
