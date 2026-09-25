"""Dedicated-site checks; run with an initialized *.test Frappe site.

Uses unique internal rows/cache keys, never rewrites the site's connection.
The tests intentionally commit to exercise real callbacks and concurrent writes.
"""

import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import runtime_profile as rp
from jarvis.tests.test_runtime_profile import connection, envelope


class TestRuntimeProfileDatabase(unittest.TestCase):
	def setUp(self):
		self.site = getattr(frappe.local, "site", "") or ""
		if not self.site.endswith(".test"):
			self.skipTest("requires initialized dedicated .test site")
		self.sites_path = frappe.local.sites_path
		self.user = frappe.session.user
		frappe.set_user("Administrator")
		self.suffix = uuid.uuid4().hex
		self.anchor = {
			**connection(),
			"admin_url": "https://admin.example.test",
			"customer": "test@example.test",
		}
		self.patches = [
			patch.object(rp, "ROW", f"runtime-test-{self.suffix}"),
			patch.object(rp, "PARENT", f"__runtime_test_{self.suffix}"),
			patch.object(rp, "CACHE_KEY", f"jarvis:runtime_test:{self.suffix}"),
			patch.object(rp, "_anchor", return_value=self.anchor),
		]
		for p in self.patches:
			p.start()
		rp._forget_snapshot()

	def tearDown(self):
		if not hasattr(self, "patches"):
			return
		frappe.db.rollback()
		frappe.db.delete("DefaultValue", {"name": rp.ROW})
		frappe.db.commit()
		rp._invalidate()
		rp._forget_snapshot()
		for p in reversed(self.patches):
			p.stop()
		frappe.set_user(self.user)

	def persist(self):
		rp.persist(envelope(), frappe.get_single("Jarvis Settings"))

	def test_commit_reload_and_global_user_default_exclusion(self):
		self.persist()
		frappe.db.commit()
		rp._forget_snapshot()
		rp._invalidate()
		self.assertEqual(rp.get_profile(), rp.legacy_profile())
		self.assertEqual(frappe.db.count("DefaultValue", {"parent": rp.PARENT, "defkey": rp.KEY}), 1)
		for user in ("Administrator", "Guest"):
			defaults = frappe.defaults.get_defaults(user)
			self.assertNotIn(rp.KEY, defaults)
			self.assertNotIn(rp.PARENT, json.dumps(defaults, default=str))
		from frappe.client import get

		frappe.set_user("Guest")
		with self.assertRaises((frappe.PermissionError, frappe.DoesNotExistError)):
			get("DefaultValue", rp.ROW)

	def test_rollback_never_leaves_published_profile(self):
		self.persist()
		frappe.db.rollback()
		self.assertIsNone(frappe.db.get_value("DefaultValue", rp.ROW))
		self.assertIsNone(frappe.cache().get_value(rp.CACHE_KEY))
		self.assertFalse(hasattr(frappe.local, rp._MEMO))

	def test_rolled_back_reset_cannot_leave_a_fallback_snapshot(self):
		rp.persist(None, frappe.get_single("Jarvis Settings"), unavailable=True)
		frappe.db.commit()
		rp.clear()
		self.assertEqual(rp.get_profile(), rp.legacy_profile())
		frappe.db.rollback()
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()

	def test_repeat_write_never_creates_hash_named_duplicates(self):
		for _ in range(3):
			self.persist()
			frappe.db.commit()
		self.assertEqual(frappe.db.count("DefaultValue", {"parent": rp.PARENT, "defkey": rp.KEY}), 1)

	def test_connection_and_profile_reject_delayed_assignment_together(self):
		from jarvis.onboarding import write_connection

		# No commit: teardown rolls these temporary Settings writes back.
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("tenant_authority_generation", 3)
		settings.db_set("tenant_authority_handle", "a" * 32)
		write_connection(rp.private_connection({**connection(), "runtime_profile": envelope()}))
		self.assertEqual(
			frappe.db.get_single_value("Jarvis Settings", "tenant_authority_generation", cache=False), 4
		)
		before = frappe.db.get_value("DefaultValue", rp.ROW, "defvalue")
		old = envelope()
		old["runtime_binding"]["generation"] = 3
		write_connection(
			rp.private_connection(
				{
					**connection(),
					"tenant_authority_generation": 3,
					"runtime_profile": old,
				}
			)
		)
		self.assertEqual(frappe.db.get_value("DefaultValue", rp.ROW, "defvalue"), before)
		self.assertEqual(json.loads(before)["profile"]["runtime_binding"]["generation"], 4)

	def test_concurrent_first_writers_create_one_record(self):
		# Release any setup read transaction before workers lock Settings.
		frappe.db.rollback()

		def write():
			try:
				frappe.init(site=self.site, sites_path=self.sites_path)
				frappe.connect()
				frappe.set_user("Administrator")
				rp.persist(envelope(), frappe.get_single("Jarvis Settings"))
				frappe.db.commit()
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			list(pool.map(lambda _: write(), range(2)))
		frappe.db.rollback()
		self.assertEqual(frappe.db.count("DefaultValue", {"parent": rp.PARENT, "defkey": rp.KEY}), 1)
		self.assertEqual(rp.get_profile(), rp.legacy_profile())

	def test_historical_canvas_is_sanitized_only_after_file_permission_check(self):
		from jarvis.chat import api, canvas

		marker = rp.legacy_profile().live_reload_route
		html = f'<script>chart()</script><script>new WebSocket("{marker}")</script>'
		file = Mock(name="test-file")
		file.get_content.return_value = html.encode()
		row = frappe._dict(
			conversation="test-conversation",
			canvas=json.dumps(
				[{"name": "test.html", "type": "html", "file_url": "/private/files/test.html"}]
			),
		)
		with (
			patch.object(api, "require_jarvis_access"),
			patch.object(api, "_get_owned_conversation"),
			patch.object(frappe.db, "get_value", return_value=row),
			patch.object(frappe, "get_doc", return_value=file),
			patch.object(frappe, "has_permission", return_value=False) as permission,
			patch.object(canvas, "get_profile", return_value=rp.legacy_profile()),
		):
			with self.assertRaises(frappe.PermissionError):
				api.get_canvas("test-message")
			file.get_content.assert_not_called()
			permission.return_value = True
			result = api.get_canvas("test-message")
			self.assertIn("chart()", result["content"])
			self.assertNotIn(marker, result["content"])

	def test_saved_dashboard_still_renders_when_runtime_is_unavailable(self):
		from jarvis.chat import canvas, dashboards_api

		marker = rp.legacy_profile().live_reload_route
		doc = frappe._dict(html=f'<script>chart()</script><script>new WebSocket("{marker}")</script>')
		with (
			patch.object(canvas, "get_profile", side_effect=rp.RuntimeProfileError("unavailable")),
			patch.object(dashboards_api, "_filter_defs_for_detail", return_value=[]),
			patch.object(dashboards_api.dashboard_permissions, "can_edit_dashboard", return_value=False),
		):
			result = dashboards_api._dashboard_detail(doc)
			self.assertIn("chart()", result["html"])
			self.assertNotIn(marker, result["html"])
