import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from jarvis.jarvis.doctype.jarvis_activity_event.jarvis_activity_event import JarvisActivityEvent
from jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger import DENYLISTED_DOCTYPES

DT = "Jarvis Activity Event"

# Independent literal (never imported from the writer): the only columns this
# audit table may ever carry. Metadata only, so a content column (args, results,
# messages, error text) trips test_schema_is_metadata_only.
_META_ONLY = {
	"at",
	"actor",
	"actor_name",
	"event",
	"method",
	"label",
	"ref_doctype",
	"ref_name",
	"row_count",
	"outcome",
	"channel",
	"code",
}
_FORBIDDEN = {"tool_args", "tool_result", "content", "error", "message", "args", "result"}

_ADMIN_EMAIL = "activity-evt-jadmin@example.com"
_PLAIN_EMAIL = "activity-evt-plain@example.com"
_SM_EMAIL = "activity-evt-sm@example.com"


def _ensure_user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Evt", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)
	return email


def _row(**kw):
	d = {
		"doctype": DT,
		"at": frappe.utils.now(),
		"actor": "Administrator",
		"actor_name": "Administrator",
		"event": "read",
		"method": "get_list",
		"outcome": "ok",
		"channel": "api",
	}
	d.update(kw)
	# ignore_links: synthetic actors in other tests need not be real Users.
	return frappe.get_doc(d).insert(ignore_permissions=True, ignore_links=True).name


class TestActivityEventDoctype(FrappeTestCase):
	def test_schema_is_metadata_only(self):
		fields = {f.fieldname for f in frappe.get_meta(DT).fields}
		self.assertEqual(fields, _META_ONLY)
		self.assertFalse(_FORBIDDEN & fields)  # content can never be stored

	def test_enums_match_the_plan(self):
		meta = frappe.get_meta(DT)
		self.assertEqual(
			meta.get_field("event").options.split("\n"),
			["read", "export", "action", "denied_call", "auth_rejected", "foreign_session"],
		)
		self.assertEqual(meta.get_field("outcome").options.split("\n"), ["ok", "denied", "failed"])
		self.assertEqual(
			meta.get_field("channel").options.split("\n"),
			["desktop", "pwa", "chat", "agent_run", "macro", "trigger", "api"],
		)

	def test_field_lengths(self):
		meta = frappe.get_meta(DT)
		self.assertEqual(int(meta.get_field("ref_name").length), 140)
		self.assertEqual(int(meta.get_field("code").length), 64)

	def test_composite_actor_at_index(self):
		idx = frappe.db.sql(f"SHOW INDEX FROM `tab{DT}` WHERE Key_name = 'actor_at_index'", as_dict=True)
		cols = [r["Column_name"] for r in sorted(idx, key=lambda r: r["Seq_in_index"])]
		self.assertEqual(cols, ["actor", "at"])

	def test_permissions_are_read_only_for_every_role(self):
		perms = frappe.get_meta(DT).permissions
		self.assertEqual({p.role for p in perms}, {"System Manager", "Jarvis Admin"})
		for p in perms:
			self.assertTrue(p.read and p.report and p.export)
			for right in (
				"create",
				"write",
				"delete",
				"submit",
				"cancel",
				"amend",
				"import",
				"share",
				"email",
			):
				self.assertFalse(p.get(right), msg=f"{p.role} holds {right}")

	def test_jarvis_admin_cannot_insert_write_or_delete(self):
		name = _row()
		admin = _ensure_user(_ADMIN_EMAIL, "Jarvis Admin")
		frappe.set_user(admin)
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{"doctype": DT, "at": frappe.utils.now(), "event": "read", "outcome": "ok"}
				).insert(ignore_permissions=False)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(DT, name).save()
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc(DT, name, ignore_permissions=False)
		finally:
			frappe.set_user("Administrator")

	def test_read_access_by_role(self):
		name = _row()
		admin = _ensure_user(_ADMIN_EMAIL, "Jarvis Admin")
		plain = _ensure_user(_PLAIN_EMAIL)
		sm = _ensure_user(_SM_EMAIL, "System Manager")
		self.assertTrue(frappe.has_permission(DT, "read", user=admin))
		self.assertTrue(frappe.has_permission(DT, "read", doc=name, user=admin))
		self.assertTrue(frappe.has_permission(DT, "read", user=sm))
		self.assertFalse(frappe.has_permission(DT, "read", user=plain))
		self.assertFalse(frappe.has_permission(DT, "read", doc=name, user=plain))

	def test_deleting_a_user_keeps_their_events_with_actor_name(self):
		# Plan D12: a deleted user's events stay until the 90-day expiry; the Link
		# would otherwise block the delete (ignore_links_on_delete).
		email = "activity-evt-leaver@example.com"
		_ensure_user(email)
		name = _row(actor=email, actor_name="Evt Leaver")
		frappe.delete_doc("User", email, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("User", email))
		row = frappe.db.get_value(DT, name, ["actor", "actor_name"], as_dict=True)
		self.assertIsNotNone(row)
		self.assertEqual(row.actor_name, "Evt Leaver")

	def test_not_renamable(self):
		name = _row()
		with self.assertRaises(Exception):
			frappe.rename_doc(DT, name, "renamed-evt", force=True)

	def test_triggers_cannot_target_the_audit_doctypes(self):
		# A trigger on an audit row would amplify: each fire's reads insert more rows.
		for dt in (DT, "Jarvis Agent Write"):
			with self.subTest(doctype=dt):
				self.assertIn(dt, DENYLISTED_DOCTYPES)
				trig = frappe.get_doc(
					{
						"doctype": "Jarvis Trigger",
						"trigger_name": f"evt-{frappe.generate_hash(length=8)}",
						"target_doctype": dt,
						"doc_event": "after_insert",
						"action_type": "LLM",
						"llm_instruction": "Check this row.",
						"enabled": 1,
					}
				)
				with self.assertRaises(frappe.ValidationError) as cm:
					trig.insert(ignore_permissions=True)
				self.assertIn("not allowed", str(cm.exception))

	def test_clear_old_logs_prunes_only_rows_past_retention(self):
		old = _row(at=add_days(now_datetime(), -91))
		inside = _row(at=add_days(now_datetime(), -89))
		new = _row()
		JarvisActivityEvent.clear_old_logs(days=90)
		self.assertFalse(frappe.db.exists(DT, old))
		self.assertTrue(frappe.db.exists(DT, inside))
		self.assertTrue(frappe.db.exists(DT, new))

	def test_hook_registration(self):
		# default_log_clearing_doctypes is silently dropped without clear_old_logs ON
		# THE CLASS, so pin both halves: the hook value and the class method.
		self.assertEqual(frappe.get_hooks("default_log_clearing_doctypes").get(DT), [90])
		self.assertTrue(callable(JarvisActivityEvent.clear_old_logs))
		self.assertIn(DT, frappe.get_hooks("ignore_links_on_delete"))
