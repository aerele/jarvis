"""Jarvis Trigger Activity as the per-file Document Intake job record."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

ACTIVITY = "Jarvis Trigger Activity"
UPLOADER = "jarvis-intake-uploader@example.com"
OTHER = "jarvis-intake-other@example.com"


def _ensure_user(email: str) -> None:
	if frappe.db.exists("User", email):
		return
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Intake",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)


class TestIntakeActivity(FrappeTestCase):
	def setUp(self):
		_ensure_user(UPLOADER)
		_ensure_user(OTHER)
		self._orig_user = frappe.session.user
		self._names: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._names:
			frappe.db.delete(ACTIVITY, {"name": name})

	def _row(self, owner: str | None = None, **fields) -> str:
		doc = frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"trigger_label": "intake test",
				"action_type": "Intake",
				"status": "Queued",
				**fields,
			}
		)
		if owner:
			doc.owner = owner
		doc.insert(ignore_permissions=True)
		if owner:
			# insert() stamps the session user as owner; set it explicitly.
			frappe.db.set_value(ACTIVITY, doc.name, "owner", owner, update_modified=False)
		self._names.append(doc.name)
		return doc.name

	def test_new_statuses_accepted(self):
		for status in ("Queued", "Running", "Needs Review"):
			name = self._row(status=status, attempts=1, file_hash="abc123")
			self.assertEqual(frappe.db.get_value(ACTIVITY, name, "status"), status)
		first = frappe.get_doc(ACTIVITY, self._names[0])
		dup = self._row(duplicate_of=first.name, status="Skipped")
		self.assertEqual(frappe.db.get_value(ACTIVITY, dup, "duplicate_of"), first.name)

	def test_owner_can_read_own_row_only(self):
		mine = self._row(owner=UPLOADER)
		mine_llm = self._row(owner=UPLOADER, action_type="LLM", status="Success")
		theirs = self._row(owner=OTHER)

		frappe.set_user(UPLOADER)
		self.assertTrue(frappe.has_permission(ACTIVITY, "read", mine))
		# Script/LLM rows keep managers-only detail: never readable by the owner.
		self.assertFalse(frappe.has_permission(ACTIVITY, "read", mine_llm))
		self.assertFalse(frappe.has_permission(ACTIVITY, "read", theirs))
		self.assertFalse(frappe.has_permission(ACTIVITY, "write", mine))
		self.assertFalse(frappe.has_permission(ACTIVITY, "delete", mine))

		visible = frappe.get_list(ACTIVITY, filters={"name": ("in", [mine, mine_llm, theirs])}, pluck="name")
		self.assertEqual(visible, [mine])

		frappe.set_user("Administrator")
		visible = frappe.get_list(ACTIVITY, filters={"name": ("in", [mine, mine_llm, theirs])}, pluck="name")
		self.assertCountEqual(visible, [mine, mine_llm, theirs])

	def test_clear_old_logs_keeps_open_rows(self):
		old = add_days(now_datetime(), -200)
		names = {s: self._row(status=s) for s in ("Queued", "Running", "Needs Review", "Success", "Failed")}
		for name in names.values():
			frappe.db.set_value(ACTIVITY, name, "creation", old, update_modified=False)

		frappe.get_attr(
			"jarvis.jarvis.doctype.jarvis_trigger_activity.jarvis_trigger_activity.JarvisTriggerActivity"
		).clear_old_logs(90)

		for status in ("Queued", "Running", "Needs Review"):
			self.assertTrue(frappe.db.exists(ACTIVITY, names[status]), status)
		for status in ("Success", "Failed"):
			self.assertFalse(frappe.db.exists(ACTIVITY, names[status]), status)
