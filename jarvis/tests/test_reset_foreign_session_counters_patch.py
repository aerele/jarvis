"""The one-time reset of the polluted foreign-session counter (incident 2026-11-06)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.patches.v2_31_reset_false_foreign_session_counters import execute
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot

SETTINGS = "Jarvis Settings"


def _stored(field):
	"""The raw Singles value: get_single_value casts an empty Datetime to year 1."""
	singles = frappe.qb.DocType("Singles")
	rows = (
		frappe.qb.from_(singles)
		.select(singles.value)
		.where(singles.doctype == SETTINGS)
		.where(singles.field == field)
		.run()
	)
	return rows[0][0] if rows else None


class TestResetForeignSessionCounters(JarvisSettingsSnapshot, FrappeTestCase):
	def test_polluted_counter_is_zeroed(self):
		frappe.db.set_single_value(
			SETTINGS,
			{"foreign_sessions_seen": 180, "foreign_session_last_at": "2026-11-06 11:36:28"},
			update_modified=False,
		)
		execute()
		self.assertEqual(int(_stored("foreign_sessions_seen") or 0), 0)
		self.assertIsNone(_stored("foreign_session_last_at"))

	def test_running_twice_is_harmless(self):
		execute()
		execute()
		self.assertEqual(int(_stored("foreign_sessions_seen") or 0), 0)
		self.assertIsNone(_stored("foreign_session_last_at"))
