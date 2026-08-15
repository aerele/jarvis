"""Tests for jarvis.chat.docmeta_api (DESIGN-V3 §8.1 as amended by §14).

Covers: allowlist rejection · the owner bundle (get/comment/like on all four
doctypes, assign on the three assignable ones) · non-owner PermissionError ·
System Manager allowed · Approval-Request conversation-owner allowed (approval
owner=Administrator case) · toggle_assignment add (ToDo + DocShare read, and
the assignee can then get_docmeta but not write) / remove (ToDo cancelled +
DocShare deleted) · DA-09 (no assignment on Jarvis Custom Skill) · F1
toggle_share add/remove + allowlist + refusing to remove an assignment-backed
share · comment update/delete restricted to author/SM · delete_attachment
attached_to verification · toggle_like round-trip · exact bundle shape keys.

F6 VERIFICATION (§14, recorded here as required): YES — inserting a Comment row
triggers frappe's native mention notifications. ``Comment.after_insert`` calls
``notify_mentions(self.reference_doctype, self.reference_name, self.content)``
(apps/frappe/frappe/core/doctype/comment/comment.py:59 on the installed
v16.25.0; the same call exists on branch version-15). ``docmeta_api.add_comment``
inserts the Comment doc directly, so ``span[data-type=mention]`` mentions in doc
comments produce native Notification Log entries with no extra jarvis code.
"""

from __future__ import annotations

import contextlib
import unittest

import frappe

from jarvis.chat import agent_catalog, docmeta_api

USER_A = "dm-user-a@example.com"
USER_B = "dm-user-b@example.com"

SKILL = "Jarvis Custom Skill"
MACRO = "Jarvis Macro"
APPROVAL = "Jarvis Approval Request"
INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
CONV = "Jarvis Conversation"


def _ensure_user(email: str) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				# System User so the chat-surface access gate (which now requires a
				# Desk user + the Jarvis User role) admits the fixture.
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	roles = set(frappe.get_roles(email))
	if "System Manager" in roles:
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	# The docmeta/approvals endpoints are chat-surface: they now require the
	# Jarvis User role (security review TASK 8). Grant it so these non-SM
	# fixtures still exercise the ownership/DocShare logic.
	if "Jarvis User" not in roles:
		frappe.get_doc("User", email).add_roles("Jarvis User")
		frappe.db.commit()
	return email


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _clear_meta(doctype: str, name: str) -> None:
	"""Reset comments/todos/shares/files/likes on a fixture doc between tests."""
	for c in frappe.get_all(
		"Comment", filters={"reference_doctype": doctype, "reference_name": name}, pluck="name"
	):
		frappe.delete_doc("Comment", c, force=True, ignore_permissions=True)
	frappe.db.delete("ToDo", {"reference_type": doctype, "reference_name": name})
	frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name})
	for f in frappe.get_all(
		"File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, pluck="name"
	):
		frappe.delete_doc("File", f, force=True, ignore_permissions=True)
	try:
		frappe.db.set_value(doctype, name, "_liked_by", "[]", update_modified=False)
	except Exception:
		pass  # _liked_by column may not exist yet (never liked)
	frappe.db.commit()


def _attach_file(doctype: str, name: str, file_name: str) -> str:
	f = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"is_private": 1,
			"content": "docmeta test content",
		}
	)
	f.flags.ignore_permissions = True
	f.insert(ignore_permissions=True)
	frappe.db.commit()
	return f.name


class TestDocmetaApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)

		# One fixture doc per allowlisted doctype, owned by USER_A.
		with _as(USER_A):
			skill = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": "dm-skill-a",
					"description": "docmeta fixture",
					"instructions": "do the thing",
					"enabled": 1,
					"user_invocable": 1,
				}
			)
			skill.flags.ignore_validate = True
			skill.insert(ignore_permissions=True)
			cls.skill = skill.name

			macro = frappe.get_doc(
				{
					"doctype": MACRO,
					"macro_name": "dm-macro-a",
					"description": "m",
					"enabled": 1,
					"stop_on_error": 1,
					"steps": [{"label": "s1", "prompt": "prompt 1"}],
				}
			)
			macro.flags.ignore_validate = True
			macro.insert(ignore_permissions=True)
			cls.macro = macro.name

			conv = frappe.get_doc({"doctype": CONV, "title": "dm-conv-a", "status": "Active"})
			conv.insert(ignore_permissions=True)
			cls.conv = conv.name

			appr = frappe.get_doc(
				{
					"doctype": APPROVAL,
					"title": "dm-appr-a",
					"status": "Pending",
					"document_type": "Purchase Invoice",
					"conversation": cls.conv,
					"question": "ok?",
					"context_md": "ctx",
					"options": '["Yes","No"]',
				}
			)
			appr.insert(ignore_permissions=True)
			cls.approval = appr.name

		# Approval owned by Administrator whose CONVERSATION is owned by USER_A
		# (the decide()-parity case: conv-owner may act on a foreign-owned row).
		admin_appr = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "dm-appr-admin",
				"status": "Pending",
				"document_type": "Sales Invoice",
				"conversation": cls.conv,
				"question": "confirm?",
				"context_md": "ctx",
				"options": '["Yes","No"]',
			}
		)
		admin_appr.insert(ignore_permissions=True)
		frappe.db.commit()
		cls.admin_approval = admin_appr.name

		# Agent installation owned by USER_A (direct insert: validate() only
		# enforces uniqueness + cap, and this avoids role-restriction state).
		agent_catalog.sync_agent_listings()
		slug = "close-auditor"
		if not frappe.db.exists(LISTING, slug):
			slug = frappe.get_all(LISTING, filters={"status": "Published"}, pluck="name", limit=1)[0]
		# close-auditor requires GL Entry / Account / Company read (install A12-gate);
		# grant it so the self-mapped install validates.
		if frappe.db.exists("Role", "Accounts User"):
			frappe.get_doc("User", USER_A).add_roles("Accounts User")
			frappe.clear_cache(user=USER_A)
		for n in frappe.get_all(INSTALLATION, filters={"owner": USER_A}, pluck="name"):
			frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
		with _as(USER_A):
			inst = frappe.get_doc(
				{
					"doctype": INSTALLATION,
					"agent": slug,
					"enabled": 0,
					"run_as_user": frappe.session.user,  # self-map (reqd since Phase 1 identity)
					"installed_version": frappe.db.get_value(LISTING, slug, "version"),
					"installed_at": frappe.utils.now(),
				}
			)
			inst.insert(ignore_permissions=True)
		frappe.db.commit()
		cls.installation = inst.name

		cls.all_docs = [
			(SKILL, cls.skill),
			(MACRO, cls.macro),
			(APPROVAL, cls.approval),
			(INSTALLATION, cls.installation),
		]
		cls.assignable_docs = [
			(MACRO, cls.macro),
			(APPROVAL, cls.approval),
			(INSTALLATION, cls.installation),
		]

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, name in cls.all_docs:
			_clear_meta(dt, name)
		frappe.delete_doc(APPROVAL, cls.admin_approval, force=True, ignore_permissions=True)
		frappe.delete_doc(APPROVAL, cls.approval, force=True, ignore_permissions=True)
		frappe.delete_doc(INSTALLATION, cls.installation, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, cls.macro, force=True, ignore_permissions=True)
		frappe.delete_doc(SKILL, cls.skill, force=True, ignore_permissions=True)
		frappe.delete_doc(CONV, cls.conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		for dt, name in self.all_docs:
			_clear_meta(dt, name)
		_clear_meta(APPROVAL, self.admin_approval)

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ #
	# allowlist + gates
	# ------------------------------------------------------------------ #
	def test_allowlist_rejection(self):
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.get_docmeta(CONV, self.conv)
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.add_comment(CONV, self.conv, "hello")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_like(CONV, self.conv, 1)

	def test_owner_bundle_on_each_doctype(self):
		for dt, name in self.all_docs:
			with _as(USER_A):
				meta = docmeta_api.get_docmeta(dt, name)
				self.assertEqual(meta["created"]["owner"], USER_A, dt)
				row = docmeta_api.add_comment(dt, name, f"note on {dt}")
				self.assertEqual(row["owner"], USER_A)
				liked = docmeta_api.toggle_like(dt, name, 1)
				self.assertIn(USER_A, liked)
				meta = docmeta_api.get_docmeta(dt, name)
				self.assertTrue(meta["liked"])
				self.assertEqual([c["name"] for c in meta["comments"]], [row["name"]])
		for dt, name in self.assignable_docs:
			with _as(USER_A):
				assignees = docmeta_api.toggle_assignment(dt, name, USER_B, "add")
				self.assertIn(USER_B, [a["user"] for a in assignees])

	def test_get_docmeta_shape_keys_exact(self):
		with _as(USER_A):
			docmeta_api.add_comment(MACRO, self.macro, "shape check")
			meta = docmeta_api.get_docmeta(MACRO, self.macro)
		self.assertEqual(
			set(meta.keys()),
			{"comments", "assignees", "liked_by", "liked", "attachments", "shares", "created", "modified"},
		)
		self.assertEqual(
			set(meta["comments"][0].keys()),
			{"name", "content", "owner", "owner_name", "owner_image", "creation", "modified"},
		)
		self.assertEqual(set(meta["created"].keys()), {"owner", "full_name", "creation"})
		self.assertEqual(set(meta["modified"].keys()), {"modified_by", "full_name", "modified"})

	def test_non_owner_denied(self):
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.get_docmeta(MACRO, self.macro)
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.add_comment(MACRO, self.macro, "nope")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_like(MACRO, self.macro, 1)
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_share(MACRO, self.macro, USER_B, "add")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.delete_attachment(MACRO, self.macro, "whatever")

	def test_system_manager_allowed(self):
		# Administrator is a System Manager and owns none of the fixtures.
		meta = docmeta_api.get_docmeta(MACRO, self.macro)
		self.assertEqual(meta["created"]["owner"], USER_A)
		row = docmeta_api.add_comment(MACRO, self.macro, "sm note")
		self.assertEqual(row["owner"], "Administrator")

	def test_approval_conversation_owner_allowed(self):
		# admin_approval.owner == Administrator; its conversation belongs to A.
		self.assertEqual(frappe.db.get_value(APPROVAL, self.admin_approval, "owner"), "Administrator")
		with _as(USER_A):
			meta = docmeta_api.get_docmeta(APPROVAL, self.admin_approval)
			self.assertEqual(meta["created"]["owner"], "Administrator")
			docmeta_api.add_comment(APPROVAL, self.admin_approval, "conv owner note")
			# conv-owner is write-allowed too (assign works)
			assignees = docmeta_api.toggle_assignment(APPROVAL, self.admin_approval, USER_B, "add")
			self.assertIn(USER_B, [a["user"] for a in assignees])
		# ... but an unrelated user is still denied.
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			docmeta_api.toggle_assignment(APPROVAL, self.admin_approval, USER_B, "add")

	# ------------------------------------------------------------------ #
	# assignment
	# ------------------------------------------------------------------ #
	def test_toggle_assignment_add_creates_todo_and_docshare(self):
		with _as(USER_A):
			assignees = docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
		self.assertEqual([a["user"] for a in assignees], [USER_B])
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": MACRO,
					"reference_name": self.macro,
					"allocated_to": USER_B,
					"status": "Open",
				},
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"DocShare",
				{
					"share_doctype": MACRO,
					"share_name": self.macro,
					"user": USER_B,
					"read": 1,
				},
			)
		)
		# The assignee can now READ the bundle ...
		with _as(USER_B):
			meta = docmeta_api.get_docmeta(MACRO, self.macro)
			self.assertIn(USER_B, [a["user"] for a in meta["assignees"]])
			self.assertIn(USER_B, [s["user"] for s in meta["shares"]])
			# ... but not write (assign someone else).
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.toggle_assignment(MACRO, self.macro, USER_A, "add")
		# Idempotent: re-adding an existing assignee is a no-op, not an error.
		with _as(USER_A):
			again = docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
		self.assertEqual([a["user"] for a in again], [USER_B])

	def test_toggle_assignment_remove_cancels_todo_and_deletes_docshare(self):
		with _as(USER_A):
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
			assignees = docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "remove")
		self.assertEqual(assignees, [])
		self.assertEqual(
			frappe.db.get_value(
				"ToDo",
				{
					"reference_type": MACRO,
					"reference_name": self.macro,
					"allocated_to": USER_B,
				},
				"status",
			),
			"Cancelled",
		)
		self.assertFalse(
			frappe.db.exists(
				"DocShare",
				{
					"share_doctype": MACRO,
					"share_name": self.macro,
					"user": USER_B,
				},
			)
		)
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			docmeta_api.get_docmeta(MACRO, self.macro)

	def test_toggle_assignment_excludes_custom_skill(self):
		# §14 DA-09: skills keep the child-table share model; no ToDo assignment.
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			docmeta_api.toggle_assignment(SKILL, self.skill, USER_B, "add")

	def test_toggle_assignment_validation(self):
		with _as(USER_A):
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "bogus")
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.toggle_assignment(MACRO, self.macro, "Guest", "add")
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.toggle_assignment(MACRO, self.macro, "dm-no-such@example.com", "add")

	# ------------------------------------------------------------------ #
	# shares (§14 F1)
	# ------------------------------------------------------------------ #
	def test_toggle_share_add_and_remove(self):
		with _as(USER_A):
			shares = docmeta_api.toggle_share(MACRO, self.macro, USER_B, "add")
		self.assertEqual([s["user"] for s in shares], [USER_B])
		self.assertEqual(shares[0]["read"], 1)
		# owner never appears in the shares block
		self.assertNotIn(USER_A, [s["user"] for s in shares])
		with _as(USER_B):
			meta = docmeta_api.get_docmeta(MACRO, self.macro)  # read via share
			self.assertEqual([s["user"] for s in meta["shares"]], [USER_B])
		with _as(USER_A):
			shares = docmeta_api.toggle_share(MACRO, self.macro, USER_B, "remove")
		self.assertEqual(shares, [])
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			docmeta_api.get_docmeta(MACRO, self.macro)

	def test_toggle_share_excludes_custom_skill(self):
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			docmeta_api.toggle_share(SKILL, self.skill, USER_B, "add")

	def test_toggle_share_refuses_assignment_backed_removal(self):
		with _as(USER_A):
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.toggle_share(MACRO, self.macro, USER_B, "remove")
		# the read grant survives the refused removal
		self.assertTrue(
			frappe.db.exists(
				"DocShare",
				{
					"share_doctype": MACRO,
					"share_name": self.macro,
					"user": USER_B,
				},
			)
		)

	# ------------------------------------------------------------------ #
	# share/assign marker convention (notify_by_email disambiguation)
	# ------------------------------------------------------------------ #
	def _share_row(self):
		return frappe.db.get_value(
			"DocShare",
			{"share_doctype": MACRO, "share_name": self.macro, "user": USER_B},
			["name", "notify_by_email"],
			as_dict=True,
		)

	def test_share_and_assignment_rows_carry_markers(self):
		# toggle_share creates the row with notify_by_email=1 (explicit share).
		with _as(USER_A):
			docmeta_api.toggle_share(MACRO, self.macro, USER_B, "add")
		self.assertEqual(int(self._share_row().notify_by_email), 1)
		_clear_meta(MACRO, self.macro)
		# toggle_assignment creates its plumbing row with notify_by_email=0.
		with _as(USER_A):
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
		self.assertEqual(int(self._share_row().notify_by_email), 0)

	def test_unassign_preserves_independent_explicit_share(self):
		# Explicit share FIRST, then assign, then unassign: the explicit share
		# was granted independently, so revoking the assignment must NOT revoke
		# it (the pre-marker behavior deleted the row unconditionally).
		with _as(USER_A):
			docmeta_api.toggle_share(MACRO, self.macro, USER_B, "add")
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
			assignees = docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "remove")
		self.assertEqual(assignees, [])
		row = self._share_row()
		self.assertIsNotNone(row)  # survived the unassign
		self.assertEqual(int(row.notify_by_email), 1)  # still an explicit share
		with _as(USER_B):
			docmeta_api.get_docmeta(MACRO, self.macro)  # B can still read

	def test_share_after_assignment_upgrades_marker_and_survives_unassign(self):
		# Assign first (plumbing row, marker 0), then explicitly share (upgrades
		# to 1), then unassign: the now-explicit share survives.
		with _as(USER_A):
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "add")
			self.assertEqual(int(self._share_row().notify_by_email), 0)
			docmeta_api.toggle_share(MACRO, self.macro, USER_B, "add")
			self.assertEqual(int(self._share_row().notify_by_email), 1)
			docmeta_api.toggle_assignment(MACRO, self.macro, USER_B, "remove")
		self.assertIsNotNone(self._share_row())
		with _as(USER_B):
			docmeta_api.get_docmeta(MACRO, self.macro)

	# ------------------------------------------------------------------ #
	# comments
	# ------------------------------------------------------------------ #
	def test_comment_update_delete_restricted_to_author_or_sm(self):
		with _as(USER_A):
			row = docmeta_api.add_comment(MACRO, self.macro, "original")
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.update_comment(row["name"], "hijack")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.delete_comment(row["name"])
		with _as(USER_A):
			updated = docmeta_api.update_comment(row["name"], "edited by author")
		self.assertIn("edited by author", updated["content"])
		# System Manager may edit + delete another user's comment.
		sm_updated = docmeta_api.update_comment(row["name"], "edited by sm")
		self.assertIn("edited by sm", sm_updated["content"])
		docmeta_api.delete_comment(row["name"])
		self.assertFalse(frappe.db.exists("Comment", row["name"]))

	def test_comment_on_disallowed_reference_rejected(self):
		# A Comment living on a NON-allowlisted doctype can't be touched here,
		# even by a System Manager.
		stray = frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": CONV,
				"reference_name": self.conv,
				"content": "stray",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.update_comment(stray.name, "nope")
			with self.assertRaises(frappe.PermissionError):
				docmeta_api.delete_comment(stray.name)
		finally:
			frappe.delete_doc("Comment", stray.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_empty_comment_rejected(self):
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			docmeta_api.add_comment(MACRO, self.macro, "   ")

	# ------------------------------------------------------------------ #
	# attachments
	# ------------------------------------------------------------------ #
	def test_delete_attachment_verifies_attached_to(self):
		on_macro = _attach_file(MACRO, self.macro, "dm-on-macro.txt")
		on_skill = _attach_file(SKILL, self.skill, "dm-on-skill.txt")
		with _as(USER_A):
			# a File attached to a DIFFERENT doc is refused
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.delete_attachment(MACRO, self.macro, on_skill)
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.delete_attachment(MACRO, self.macro, "dm-no-such-file")
			docmeta_api.delete_attachment(MACRO, self.macro, on_macro)
		self.assertFalse(frappe.db.exists("File", on_macro))
		self.assertTrue(frappe.db.exists("File", on_skill))

	def test_attachments_in_bundle(self):
		f = _attach_file(MACRO, self.macro, "dm-listed.txt")
		with _as(USER_A):
			meta = docmeta_api.get_docmeta(MACRO, self.macro)
		self.assertEqual([a["name"] for a in meta["attachments"]], [f])
		self.assertEqual(
			set(meta["attachments"][0].keys()),
			{"name", "file_name", "file_url", "file_size", "is_private", "creation", "owner"},
		)

	# ------------------------------------------------------------------ #
	# likes
	# ------------------------------------------------------------------ #
	def test_toggle_like_roundtrip(self):
		with _as(USER_A):
			liked = docmeta_api.toggle_like(MACRO, self.macro, 1)
			self.assertEqual(liked, [USER_A])
			self.assertTrue(docmeta_api.get_docmeta(MACRO, self.macro)["liked"])
		# desk parity: a "Like" trace Comment row exists (and is NOT in comments)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"comment_type": "Like",
					"reference_doctype": MACRO,
					"reference_name": self.macro,
					"owner": USER_A,
				},
			)
		)
		with _as(USER_A):
			self.assertEqual(docmeta_api.get_docmeta(MACRO, self.macro)["comments"], [])
			liked = docmeta_api.toggle_like(MACRO, self.macro, 0)
			self.assertEqual(liked, [])
			self.assertFalse(docmeta_api.get_docmeta(MACRO, self.macro)["liked"])
		self.assertFalse(
			frappe.db.exists(
				"Comment",
				{
					"comment_type": "Like",
					"reference_doctype": MACRO,
					"reference_name": self.macro,
					"owner": USER_A,
				},
			)
		)

	def test_toggle_like_idempotent(self):
		with _as(USER_A):
			docmeta_api.toggle_like(MACRO, self.macro, 1)
			liked = docmeta_api.toggle_like(MACRO, self.macro, 1)
		self.assertEqual(liked, [USER_A])  # no duplicate entry


if __name__ == "__main__":
	unittest.main()
