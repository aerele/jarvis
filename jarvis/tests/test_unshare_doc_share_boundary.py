"""Regression tests for F18 - unshare_doc's broken permission boundary.

``frappe.share.remove()`` without ``ignore_permissions`` deletes the
DocShare row via ``frappe.delete_doc``, which checks DELETE permission on
the DocShare doctype ITSELF - a System-Manager-only role table with no
``if_owner`` - instead of "share" permission on the TARGET document. That
denied essentially every ordinary (non-System-Manager) user, even one who
legitimately shared the document themselves and should be able to revoke
their own grant.

The fix (jarvis/tools/unshare_doc.py) explicitly checks
``frappe.has_permission(doctype, "share", doc=name, throw=True)`` on the
target doc - mirroring the boundary ``frappe.share.add`` enforces via
``check_share_permission`` - then calls ``frappe.share.remove`` with
``flags={"ignore_permissions": True}`` to bypass the DocShare ACL, the same
pattern ``frappe.share.set_docshare_permission`` uses.

Fixture: ``Note`` is a core doctype whose "Desk User" DocPerm grants
``share=1`` only ``if_owner=1`` (frappe/core/doctype/note/note.json) - so a
plain Desk User who creates+owns a Note has "share" permission on THAT
Note, while a second, unrelated Desk User does not.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools.unshare_doc import unshare_doc

OWNER = "jv-unshare-owner@example.com"
RECIPIENT = "jv-unshare-recipient@example.com"
OUTSIDER = "jv-unshare-outsider@example.com"


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	# Fresh test-bench users may inherit System Manager - strip it so these
	# are genuinely "normal" (non-System-Manager) users, matching the
	# finding's empirical repro.
	if "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")
	if "Desk User" not in frappe.get_roles(email):
		user.add_roles("Desk User")
	return email


class TestUnshareDocPermissionBoundary(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OWNER)
		_ensure_user(RECIPIENT)
		_ensure_user(OUTSIDER)

	def setUp(self):
		self._orig_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig_user)

	def _make_shared_note(self) -> str:
		"""As OWNER (a plain Desk User): create a Note - if_owner gives OWNER
		"share" on it - and share it with RECIPIENT. Returns the note name."""
		frappe.set_user(OWNER)
		note = frappe.get_doc(
			{
				"doctype": "Note",
				"title": "jv-unshare-test-note",
				"content": "hello",
			}
		).insert()
		frappe.share.add(doctype="Note", name=note.name, user=RECIPIENT, read=1)
		self.assertTrue(
			frappe.db.exists(
				"DocShare",
				{"share_doctype": "Note", "share_name": note.name, "user": RECIPIENT},
			)
		)
		return note.name

	def test_owner_who_shared_can_unshare_without_permission_error(self):
		"""RED before the fix: unshare_doc raised frappe.PermissionError for
		OWNER (a normal Desk User, not System Manager) even though OWNER
		legitimately has "share" permission on their own Note - because the
		old code checked delete-permission on the System-Manager-only
		DocShare doctype instead."""
		note_name = self._make_shared_note()
		# Still acting as OWNER.
		out = unshare_doc("Note", note_name, RECIPIENT)
		self.assertEqual(out, {"doctype": "Note", "name": note_name, "user": RECIPIENT})
		self.assertFalse(
			frappe.db.exists(
				"DocShare",
				{"share_doctype": "Note", "share_name": note_name, "user": RECIPIENT},
			)
		)

	def test_user_without_share_permission_on_target_is_denied(self):
		"""A Desk User with no relationship to the Note (not owner, not
		granted share=True) is denied - the fix must not open unshare_doc up
		to every authenticated user, only those with "share" on the doc."""
		note_name = self._make_shared_note()
		frappe.set_user(OUTSIDER)
		with self.assertRaises(frappe.PermissionError):
			unshare_doc("Note", note_name, RECIPIENT)
		# Nothing was removed by the denied attempt.
		frappe.set_user(OWNER)
		self.assertTrue(
			frappe.db.exists(
				"DocShare",
				{"share_doctype": "Note", "share_name": note_name, "user": RECIPIENT},
			)
		)
