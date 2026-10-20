"""Desk shortcut opt-in: boot normalization, persisted settings and user boot cache."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.boot import set_jarvis_boot
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

FIELD = "hide_shortcut_without_access"


class TestShortcutBoot(FrappeTestCase):
	def _boot(self, value=None, fail=False, access=False):
		def read_setting(doctype, field, *args, **kwargs):
			if field == FIELD:
				if fail:
					raise RuntimeError("settings unavailable")
				return value
			return None

		boot = frappe._dict()
		with (
			patch("frappe.db.get_single_value", side_effect=read_setting),
			patch("jarvis.account.is_ready_for_chat", return_value={"ready": True}),
			patch("jarvis.account.has_been_chat_ready", return_value=True),
			patch("jarvis.permissions.has_jarvis_access", return_value=access),
			patch("jarvis.maintenance_notice.boot_payload", return_value={"active": False, "message": ""}),
		):
			set_jarvis_boot(boot)
		return boot

	def test_setting_is_a_boolean_even_for_single_string_values(self):
		for value, expected in [(None, False), (0, False), ("0", False), (1, True), ("1", True)]:
			with self.subTest(value=value):
				self.assertIs(self._boot(value).jarvis_hide_shortcut_without_access, expected)

	def test_failed_setting_read_preserves_old_visibility_without_granting_access(self):
		boot = self._boot(fail=True)
		self.assertIs(boot.jarvis_hide_shortcut_without_access, False)
		self.assertIs(boot.jarvis_has_access, False)

	def test_visibility_setting_does_not_change_access(self):
		for access in (False, True):
			with self.subTest(access=access):
				boot = self._boot(1, access=access)
				self.assertIs(boot.jarvis_has_access, access)
				self.assertIs(boot.jarvis_hide_shortcut_without_access, True)


class TestShortcutSettings(FrappeTestCase):
	def test_checkbox_defaults_off_and_is_in_the_account_section(self):
		meta = frappe.get_meta("Jarvis Settings")
		field = meta.get_field(FIELD)
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Check")
		self.assertEqual(str(field.default), "0")
		self.assertEqual(field.permlevel, 0)
		fields = [f.fieldname for f in meta.fields]
		self.assertLess(fields.index("account_section"), fields.index(FIELD))
		self.assertLess(fields.index(FIELD), fields.index("llm_tab"))

	def _save(self, value):
		settings = frappe.get_single("Jarvis Settings")
		settings.set(FIELD, value)
		with (
			patch.object(JarvisSettings, "_on_update_unified_llm"),
			patch.object(JarvisSettings, "_on_update_single_model_legacy"),
			patch("frappe.enqueue"),
		):
			settings.save()

	def test_both_toggle_directions_persist_and_invalidate_other_users_boot(self):
		frappe.db.set_single_value("Jarvis Settings", FIELD, 0)
		try:
			for value in (1, 0):
				frappe.cache.hset("bootinfo", "shortcut-cache-test", {"stale": True})
				self._save(value)
				self.assertEqual(frappe.db.get_single_value("Jarvis Settings", FIELD), value)
				self.assertIsNone(frappe.cache.hget("bootinfo", "shortcut-cache-test"))
		finally:
			frappe.cache.hdel("bootinfo", "shortcut-cache-test")

	def test_unchanged_setting_save_preserves_other_users_boot(self):
		frappe.db.set_single_value("Jarvis Settings", FIELD, 0)
		try:
			frappe.cache.hset("bootinfo", "shortcut-cache-test", {"stale": False})
			self._save(0)
			self.assertEqual(frappe.cache.hget("bootinfo", "shortcut-cache-test"), {"stale": False})
		finally:
			frappe.cache.hdel("bootinfo", "shortcut-cache-test")

	def test_commit_hook_clears_boot_repopulated_before_the_save_commits(self):
		from frappe.utils import CallbackManager

		frappe.db.set_single_value("Jarvis Settings", FIELD, 0)
		callbacks = CallbackManager()
		try:
			with patch.object(frappe.db, "after_commit", callbacks):
				self._save(1)
				# Another connection can cache the old committed preference after
				# on_update's immediate clear. Run the real registered commit hooks.
				frappe.cache.hset("bootinfo", "shortcut-cache-test", {"stale": True})
				callbacks.run()
			self.assertIsNone(frappe.cache.hget("bootinfo", "shortcut-cache-test"))
		finally:
			frappe.cache.hdel("bootinfo", "shortcut-cache-test")
