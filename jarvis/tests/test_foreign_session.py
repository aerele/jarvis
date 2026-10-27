from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.api import call_tool
from jarvis.chat import foreign_session
from jarvis.exceptions import AdminUnreachableError

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


class TestForeignSession(FrappeTestCase):
	def setUp(self):
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
		with patch("jarvis.admin_client._post", return_value={}) as post:
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
		):
			foreign_session.report()
		enq.assert_not_called()

	def test_report_notifies_system_managers_once_per_day(self):
		def logs():
			return frappe.db.count("Notification Log", {"subject": _NOTICE_SUBJECT})

		before = logs()
		with patch("jarvis.admin_client._post", return_value={}), patch("frappe.enqueue"):
			foreign_session.report()
			after_first = logs()
			foreign_session.report()
		self.assertGreater(after_first, before)
		self.assertEqual(logs(), after_first)


class TestCallToolRecordsForeignSession(FrappeTestCase):
	def setUp(self):
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
