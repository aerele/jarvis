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
			# Frappe downgrades a role-less System User to Website User, which has no Desk User.
			"roles": [{"role": "Jarvis User"}],
		}
	).insert(ignore_permissions=True)
	# A previous run cached this address as a non-user (no Desk User role); drop it.
	frappe.clear_cache(user=email)


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

	def test_detail_is_manager_only_at_field_level(self):
		mine = self._row(owner=UPLOADER, detail="ValueError: internal text", summary="safe copy")
		frappe.set_user(UPLOADER)
		doc = frappe.get_doc(ACTIVITY, mine)
		doc.apply_fieldlevel_read_permissions()
		got = doc.as_dict()
		self.assertFalse(got.get("detail"))
		self.assertEqual(got.get("summary"), "safe copy")
		frappe.set_user("Administrator")
		manager = frappe.get_doc(ACTIVITY, mine)
		manager.apply_fieldlevel_read_permissions()
		self.assertEqual(manager.as_dict().get("detail"), "ValueError: internal text")
		self.assertEqual(frappe.get_meta(ACTIVITY).get_field("detail").permlevel, 1)

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

	def test_linked_activity_never_blocks_deleting_what_it_points_at(self):
		"""The 90-day log row must not hold a draft, a source file or an approval
		request hostage: a plain delete (no force) works while the row links them."""
		todo = frappe.get_doc({"doctype": "ToDo", "description": "intake link test"}).insert(
			ignore_permissions=True
		)
		fdoc = frappe.get_doc(
			{"doctype": "File", "file_name": "link-test.txt", "content": b"x", "is_private": 1}
		).insert(ignore_permissions=True)
		request = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": "link test",
				"status": "Pending",
				"source": "Intake",
				"question": "q",
			}
		)
		request.flags.jarvis_server_write = True
		request.insert(ignore_permissions=True)
		self._row(
			status="Needs Review",
			draft_doctype="ToDo",
			draft_name=todo.name,
			source_file=fdoc.name,
			approval_request=request.name,
		)
		frappe.delete_doc("ToDo", todo.name, ignore_permissions=True)
		frappe.delete_doc("File", fdoc.name, ignore_permissions=True)
		frappe.delete_doc("Jarvis Approval Request", request.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("ToDo", todo.name))
		self.assertFalse(frappe.db.exists("File", fdoc.name))
		self.assertFalse(frappe.db.exists("Jarvis Approval Request", request.name))

	def _can_retry_onload(self, name: str) -> bool:
		doc = frappe.get_doc(ACTIVITY, name)
		doc.run_method("onload")
		return bool((doc.get("__onload") or {}).get("can_retry_intake"))

	def test_desk_form_offers_retry_only_when_it_can_work(self):
		from jarvis.intake.capture import FILE_REMOVED_SUMMARY, UNREADABLE_TYPE_SUMMARY

		failed = self._row(owner=UPLOADER, status="Failed", summary="Reading the file failed.")
		dup = self._row(owner=UPLOADER, status="Skipped", duplicate_of=failed)
		perm = self._row(owner=UPLOADER, status="Skipped", summary=UNREADABLE_TYPE_SUMMARY)
		removed = self._row(owner=UPLOADER, status="Failed", summary=FILE_REMOVED_SUMMARY)
		success = self._row(owner=UPLOADER, status="Success")
		drafted = self._row(owner=UPLOADER, status="Failed", draft_doctype="User", draft_name=UPLOADER)
		llm = self._row(owner=UPLOADER, status="Failed", action_type="LLM")
		theirs = self._row(owner=OTHER, status="Failed")
		frappe.set_user(UPLOADER)
		self.assertTrue(self._can_retry_onload(failed))
		for hidden in (dup, perm, removed, success, drafted, llm):
			self.assertFalse(self._can_retry_onload(hidden), hidden)
		frappe.set_user("Administrator")
		self.assertTrue(self._can_retry_onload(theirs))  # a manager may retry anyone's


class TestIntakeActivityJarvisAdmin(FrappeTestCase):
	"""A Jarvis Admin who is not a System Manager manages triggers, so the ORM
	read rule matches the SPA feed: every Intake row, not only their own."""

	def setUp(self):
		from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, ensure_jarvis_user_role
		from jarvis.tests.test_triggers_api import JADMIN_USER, PLAIN_USER
		from jarvis.tests.test_triggers_api import _ensure_user as ensure_with_roles

		ensure_jarvis_user_role()
		ensure_with_roles(JADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE])
		ensure_with_roles(PLAIN_USER, [JARVIS_USER_ROLE])
		self.admin, self.plain = JADMIN_USER, PLAIN_USER
		self._orig_user = frappe.session.user
		self._names: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._names:
			frappe.db.delete(ACTIVITY, {"name": name})

	def test_jarvis_admin_reads_another_users_intake_row(self):
		doc = frappe.get_doc(
			{"doctype": ACTIVITY, "trigger_label": "t", "action_type": "Intake", "status": "Queued"}
		).insert(ignore_permissions=True)
		self._names.append(doc.name)
		frappe.db.set_value(ACTIVITY, doc.name, "owner", OTHER, update_modified=False)
		frappe.set_user(self.admin)
		self.assertTrue(frappe.has_permission(ACTIVITY, "read", doc.name))
		self.assertEqual(frappe.get_list(ACTIVITY, filters={"name": doc.name}, pluck="name"), [doc.name])
		frappe.set_user(self.plain)
		self.assertFalse(frappe.has_permission(ACTIVITY, "read", doc.name))
