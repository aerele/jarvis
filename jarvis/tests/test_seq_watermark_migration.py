"""Transition tests for the watermark rename's dual-write/GREATEST-read
compatibility layer (jarvis.chat.seq_watermark).

On an upgraded site the old column survives model-sync as an orphan; on a fresh
install it never exists. The upgrade shape is the one the layer exists for, so
instead of skipping when the column is absent (which made this suite vacuous on
every fresh CI site), setUpClass CREATES the legacy column to simulate the
upgrade, and tearDownClass drops it again if we added it (leaving a fresh site
pristine).
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import seq_watermark
from jarvis.chat.turn_recovery import _next_turn_watermark
from jarvis.tests._legacy_migration_fixtures import load

MSG = "Jarvis Chat Message"


class TestSeqWatermarkMigration(FrappeTestCase):
	_added_legacy_col = False

	@classmethod
	def _legacy_col_exists(cls) -> bool:
		# Ask the SERVER, not get_table_columns — that helper is redis/client cached
		# and the DDL below makes the cache lie (this exact staleness broke CI once:
		# the patch's own has-column guard read the stale cache and no-op'd).
		return bool(frappe.db.sql(f"SHOW COLUMNS FROM `tab{MSG}` WHERE Field = %s", (cls._legacy_column,)))

	@staticmethod
	def _bust_columns_cache():
		key = f"table_columns::tab{MSG}"
		frappe.cache.delete_value(key)
		# Frappe 15 caches table columns in a redis HASH ("table_columns", field
		# "tab<doctype>"), not the "table_columns::tab<doctype>" string key Frappe
		# 16 uses. Clear both shapes, else the stale pre-DDL column list survives
		# and the patch's has-column guard no-ops. hdel is a harmless no-op on v16.
		frappe.cache.hdel("table_columns", f"tab{MSG}")
		try:
			frappe.client_cache.delete_value(key)
		except Exception:
			pass
		frappe.local._jarvis_wm_legacy_col = None

	@classmethod
	def setUpClass(cls):
		cls._legacy_column = load()["watermark"]["legacy_column"]
		cls._added_legacy_col = False
		super().setUpClass()
		if not cls._legacy_col_exists():
			frappe.db.sql_ddl(
				f"ALTER TABLE `tab{MSG}` ADD COLUMN `{cls._legacy_column}` INT(11) NOT NULL DEFAULT 0"
			)
			cls._added_legacy_col = True
		cls._bust_columns_cache()
		assert cls._legacy_col_exists(), "simulated-upgrade column did not stick"

	@classmethod
	def tearDownClass(cls):
		if cls._added_legacy_col and cls._legacy_col_exists():
			frappe.db.sql_ddl(f"ALTER TABLE `tab{MSG}` DROP COLUMN `{cls._legacy_column}`")
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
			f"UPDATE `tabJarvis Chat Message` SET `{self._legacy_column}`=%s, agent_seq_watermark=%s WHERE name=%s",
			(old, new, name or self.msg.name),
		)

	def _cols(self, name=None):
		return frappe.db.sql(
			f"SELECT agent_seq_watermark, `{self._legacy_column}` FROM `tabJarvis Chat Message` WHERE name=%s",
			(name or self.msg.name,),
		)[0]

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
