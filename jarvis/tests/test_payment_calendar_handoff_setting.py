"""``Jarvis Settings.payment_calendar_chat_handoff`` (Payment Calendar chat hand-off, A1).

The switch is hidden, permlevel 2 and Administrator-only. A System Manager must not be
able to change it - not even after a permlevel-2 self-grant through the Role Permission
Manager - and flipping it must never queue an LLM sync.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_handoff_setting
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

SETTINGS = "Jarvis Settings"
FIELD = "payment_calendar_chat_handoff"
SYSMAN = "pc-handoff-sysman@example.com"


@contextmanager
def _as(user):
	original = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original)


def _ensure_system_manager(email):
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "pcsysman",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if "System Manager" not in frappe.get_roles(email):
		frappe.get_doc("User", email).add_roles("System Manager")
	frappe.db.commit()


def _switch():
	return frappe.utils.cint(frappe.db.get_single_value(SETTINGS, FIELD))


def _set_switch(value):
	frappe.db.set_single_value(SETTINGS, FIELD, value)
	frappe.db.commit()


@contextmanager
def _no_sync_side_effects():
	with (
		patch.object(JarvisSettings, "_on_update_unified_llm"),
		patch.object(JarvisSettings, "_on_update_single_model_legacy"),
		patch("frappe.enqueue"),
	):
		yield


class TestPaymentCalendarHandoffSetting(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_system_manager(SYSMAN)

	def setUp(self):
		frappe.set_user("Administrator")
		_set_switch(0)

	def tearDown(self):
		frappe.set_user("Administrator")
		_set_switch(0)

	def test_field_is_a_hidden_permlevel_two_check_that_defaults_off(self):
		df = frappe.get_meta(SETTINGS).get_field(FIELD)
		self.assertEqual(df.fieldtype, "Check")
		self.assertEqual(str(df.default), "0")
		self.assertEqual(df.permlevel, 2)
		self.assertEqual(df.hidden, 1)
		self.assertEqual(_switch(), 0)

	def test_a_system_manager_cannot_write_the_permlevel(self):
		meta = frappe.get_meta(SETTINGS)
		self.assertNotIn(2, meta.get_permlevel_access("write", user=SYSMAN))
		self.assertIn(2, meta.get_permlevel_access("write", user="Administrator"))

	def test_a_system_manager_save_leaves_the_switch_off(self):
		with _as(SYSMAN), _no_sync_side_effects():
			settings = frappe.get_single(SETTINGS)
			setattr(settings, FIELD, 1)
			settings.save()
		self.assertEqual(_switch(), 0)

	def test_a_self_granted_permlevel_write_is_still_refused(self):
		from frappe import permissions as frappe_permissions
		from frappe.core.page.permission_manager import permission_manager

		with _as(SYSMAN):
			permission_manager.update(SETTINGS, "System Manager", 2, "write", 1)
		frappe.db.commit()
		try:
			self.assertIn(2, frappe.get_meta(SETTINGS).get_permlevel_access("write", user=SYSMAN))
			with _as(SYSMAN), _no_sync_side_effects(), self.assertRaises(frappe.PermissionError):
				settings = frappe.get_single(SETTINGS)
				setattr(settings, FIELD, 1)
				settings.save()
			self.assertEqual(_switch(), 0)
		finally:
			frappe_permissions.reset_perms(SETTINGS)
			frappe.clear_cache(doctype=SETTINGS)
			frappe.db.commit()

	def test_a_self_granted_system_manager_may_not_switch_it_back_off_either(self):
		from frappe import permissions as frappe_permissions
		from frappe.core.page.permission_manager import permission_manager

		_set_switch(1)
		with _as(SYSMAN):
			permission_manager.update(SETTINGS, "System Manager", 2, "write", 1)
		frappe.db.commit()
		try:
			with _as(SYSMAN), _no_sync_side_effects(), self.assertRaises(frappe.PermissionError):
				settings = frappe.get_single(SETTINGS)
				setattr(settings, FIELD, 0)
				settings.save()
			self.assertEqual(_switch(), 1)
		finally:
			frappe_permissions.reset_perms(SETTINGS)
			frappe.clear_cache(doctype=SETTINGS)
			frappe.db.commit()

	def test_administrator_can_change_it(self):
		with _no_sync_side_effects():
			settings = frappe.get_single(SETTINGS)
			setattr(settings, FIELD, 1)
			settings.save()
		self.assertEqual(_switch(), 1)

	def test_a_save_that_does_not_touch_it_is_not_refused_for_a_system_manager(self):
		_set_switch(1)
		with _as(SYSMAN), _no_sync_side_effects():
			settings = frappe.get_single(SETTINGS)
			settings.save()  # the switch is unchanged: the guard stays silent
		self.assertEqual(_switch(), 1)

	def test_a_switch_only_change_queues_no_llm_sync(self):
		# A converged baseline (the test site may carry a failed prior sync, which the
		# classifier rightly re-applies for any save): the switch is the ONLY change.
		# The save itself is not run end to end: the site's models[] rows may not
		# validate, and the models-less leg is the one _classify_llm_change gates.
		saved = {
			f: frappe.db.get_single_value(SETTINGS, f) for f in ("last_sync_status", "last_sync_requested_at")
		}
		frappe.db.set_single_value(SETTINGS, "last_sync_status", "ok (pc handoff test)")
		frappe.db.commit()
		self.addCleanup(self._restore_sync_fields, saved)

		settings = frappe.get_single(SETTINGS)
		settings.load_doc_before_save()
		setattr(settings, FIELD, 1)
		self.assertTrue(settings.has_value_changed(FIELD))
		self.assertIsNone(settings._classify_llm_change())
		self.assertEqual(
			settings._pool_state_snapshot(settings.get_doc_before_save()),
			settings._pool_state_snapshot(settings),
			"the pool leg's diff gate must not see the switch either",
		)
		with (
			patch.object(jarvis_settings, "_write_settings_fields") as write_fields,
			patch.object(jarvis_settings, "_switch_or_enqueue") as switch_or_enqueue,
			patch("frappe.enqueue") as enqueue,
		):
			settings._on_update_single_model_legacy()
		write_fields.assert_not_called()
		switch_or_enqueue.assert_not_called()
		enqueue.assert_not_called()

	@staticmethod
	def _restore_sync_fields(saved):
		for field, value in saved.items():
			frappe.db.set_single_value(SETTINGS, field, value)
		frappe.db.commit()
