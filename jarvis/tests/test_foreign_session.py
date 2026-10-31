from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.api import call_tool
from jarvis.chat import foreign_session
from jarvis.exceptions import AdminUnreachableError
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot

_SETTINGS = "Jarvis Settings"
_NOTICE_SUBJECT = "Another site is using this site's Jarvis connection"


def _count() -> int:
	return int(frappe.db.get_single_value(_SETTINGS, "foreign_sessions_seen") or 0)


def _clear_cache(keys: list[str]) -> None:
	import hashlib

	cache = frappe.cache()
	for key in keys:
		cache.delete_value("jarvis:foreign_session:" + hashlib.sha256(key.encode()).hexdigest()[:12])
	cache.delete_value("jarvis:foreign_session:notice")


KEYS = ["agent:main:dashboard:x", "agent:main:dashboard:y", "agent:main:dashboard:legacy", "jarvis:shared:z"]


class TestForeignSession(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(_SETTINGS, "foreign_sessions_seen", 0, update_modified=False)
		frappe.db.set_single_value(_SETTINGS, "foreign_session_last_at", None, update_modified=False)
		_clear_cache(KEYS)
		self.addCleanup(_clear_cache, KEYS)

	def test_record_first_time_counts_and_enqueues(self):
		with patch("frappe.enqueue") as enq:
			self.assertTrue(foreign_session.record(KEYS[0]))
		self.assertEqual(_count(), 1)
		self.assertEqual(enq.call_count, 1)
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.foreign_session.report")

	def test_record_throttles_per_session_key(self):
		with patch("frappe.enqueue") as enq:
			results = [foreign_session.record(KEYS[0]) for _ in range(10)]
		self.assertEqual(results.count(True), 1)
		self.assertEqual(_count(), 1)
		self.assertEqual(enq.call_count, 1)

	def test_record_counts_distinct_keys(self):
		with patch("frappe.enqueue"):
			foreign_session.record(KEYS[0])
			foreign_session.record(KEYS[1])
		self.assertEqual(_count(), 2)

	def test_record_never_raises(self):
		with patch("frappe.db.set_single_value", side_effect=RuntimeError("db down")):
			self.assertFalse(foreign_session.record(KEYS[0]))

	def test_report_posts_count_without_session_key(self):
		with patch("frappe.enqueue"):
			foreign_session.record(KEYS[0])
		with (
			patch("jarvis.admin_client._post", return_value={}) as post,
			patch("jarvis.learning.lifecycle.notify_system_managers"),
		):
			foreign_session.report()
		body = post.call_args.kwargs["body"]
		self.assertEqual(set(body), {"report"})
		self.assertEqual(body["report"]["count"], 1)
		self.assertIsInstance(body["report"]["last_seen_at"], str)
		self.assertTrue(body["report"]["last_seen_at"])
		self.assertNotIn(KEYS[0], repr(post.call_args))

	def test_report_swallows_admin_errors(self):
		with (
			patch("jarvis.admin_client._post", side_effect=AdminUnreachableError("down")),
			patch("frappe.enqueue") as enq,
			patch("jarvis.learning.lifecycle.notify_system_managers"),
		):
			foreign_session.report()
		enq.assert_not_called()

	def test_report_notifies_system_managers_once_per_day(self):
		with (
			patch("jarvis.admin_client._post", return_value={}),
			patch("frappe.enqueue"),
			patch("jarvis.learning.lifecycle.notify_system_managers") as notify,
		):
			foreign_session.report()
			foreign_session.report()
		notify.assert_called_once_with(_NOTICE_SUBJECT, foreign_session.NOTICE_MESSAGE)


class TestForeignSessionHardening(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(_SETTINGS, "foreign_sessions_seen", 0, update_modified=False)
		_clear_cache(KEYS)
		self.addCleanup(_clear_cache, KEYS)

	def test_prefix_is_a_persistent_cache_key(self):
		from jarvis import hooks

		self.assertIn("jarvis:foreign_session:", hooks.persistent_cache_keys)

	def test_throttle_survives_a_full_cache_clear(self):
		with patch("frappe.enqueue"):
			self.assertTrue(foreign_session.record(KEYS[0]))
			frappe.clear_cache()
			self.assertIn("jarvis:foreign_session:", frappe.get_hooks("persistent_cache_keys"))
			self.assertFalse(foreign_session.record(KEYS[0]))
		self.assertEqual(_count(), 1)

	def test_known_keys_are_not_recorded(self):
		"""A match in ANY of the four tables is known, and the whole lookup is ONE query."""
		queries = []

		def sql(query, *args, **kwargs):
			queries.append(str(query))
			return [(1,)]

		with (
			patch("frappe.db.table_exists", return_value=True),
			patch("frappe.db.sql", side_effect=sql),
			patch("frappe.enqueue") as enq,
		):
			self.assertFalse(foreign_session.record(KEYS[0]))
			enq.assert_not_called()
		self.assertEqual(len(queries), 1)
		for table in (
			"Jarvis Agent Run",
			"Jarvis Conversation",
			"Jarvis Chat Session",
			"Jarvis Chat Binding",
		):
			self.assertIn(f"`tab{table}`", queries[0])
		self.assertIn(KEYS[0], queries[0])
		self.assertEqual(_count(), 0)

	def test_lookup_skips_tables_that_do_not_exist(self):
		queries = []

		def sql(query, *args, **kwargs):
			queries.append(str(query))
			return []

		with (
			patch("frappe.db.table_exists", side_effect=lambda dt: dt == "Jarvis Chat Session"),
			patch("frappe.db.sql", side_effect=sql),
		):
			self.assertFalse(foreign_session.has_row_for_key(KEYS[0]))
		self.assertEqual(len(queries), 1)
		self.assertIn("`tabJarvis Chat Session`", queries[0])
		self.assertNotIn("`tabJarvis Agent Run`", queries[0])

	def test_lookup_without_any_table_is_false_with_no_query(self):
		with patch("frappe.db.table_exists", return_value=False), patch("frappe.db.sql") as sql:
			self.assertFalse(foreign_session.has_row_for_key(KEYS[0]))
		sql.assert_not_called()

	def test_unknown_key_is_recorded(self):
		with patch("frappe.enqueue"):
			self.assertTrue(foreign_session.record(KEYS[1]))

	def test_lookup_error_does_not_record(self):
		with (
			patch("frappe.db.table_exists", return_value=True),
			patch("frappe.db.sql", side_effect=RuntimeError("boom")),
			patch("frappe.enqueue") as enq,
		):
			self.assertFalse(foreign_session.record(KEYS[1]))
		enq.assert_not_called()
		self.assertEqual(_count(), 0)

	def test_failed_write_releases_the_throttle(self):
		with (
			patch("frappe.db.set_single_value", side_effect=RuntimeError("db down")),
			patch("frappe.enqueue"),
		):
			self.assertFalse(foreign_session.record(KEYS[1]))
		with patch("frappe.enqueue"):
			self.assertTrue(foreign_session.record(KEYS[1]))

	def test_report_job_is_deduplicated(self):
		with patch("frappe.enqueue") as enq:
			foreign_session.record(KEYS[1])
		self.assertEqual(enq.call_args.kwargs["job_id"], "jarvis-foreign-session-report")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])

	def test_notice_is_claimed_before_notifying(self):
		with patch("jarvis.learning.lifecycle.notify_system_managers") as notify:
			foreign_session._notify_system_managers_once_a_day()
			foreign_session._notify_system_managers_once_a_day()
		notify.assert_called_once_with(_NOTICE_SUBJECT, foreign_session.NOTICE_MESSAGE)


class TestCallToolRecordsForeignSession(JarvisSettingsSnapshot, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(_SETTINGS, "foreign_sessions_seen", 0, update_modified=False)
		_clear_cache(KEYS)
		self.addCleanup(_clear_cache, KEYS)

	def _call(self, key):
		with (
			patch("jarvis.api._get_header", return_value="tok"),
			patch("jarvis.api.validate_plugin_request", return_value=key),
			patch("frappe.enqueue"),
		):
			return call_tool(tool="get_schema", args={"doctype": "Customer"})

	def test_unknown_legacy_session_is_recorded(self):
		result = self._call(KEYS[2])
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertEqual(_count(), 1)

	def test_unknown_shared_session_is_recorded(self):
		with (
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
			patch("jarvis.chat.collaboration.resolve_binding", side_effect=frappe.PermissionError),
		):
			result = self._call(KEYS[3])
		self.assertEqual(frappe.local.response.http_status_code, 403)
		self.assertEqual(result["error"]["code"], "PermissionError")
		self.assertEqual(_count(), 1)
