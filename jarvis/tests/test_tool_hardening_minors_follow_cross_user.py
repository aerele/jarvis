"""Following or unfollowing for ANOTHER user needs System Manager."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools.follow_document import follow_document
from jarvis.tools.unfollow_document import unfollow_document

SESSION = "plain.user@example.com"
OTHER = "someone.else@example.com"


def _as(roles):
	return (
		patch("frappe.get_roles", return_value=roles),
		patch("frappe.db.exists", return_value=True),
		patch("frappe.has_permission", return_value=True),
	)


def _enter(patches, stack):
	for p in patches:
		stack.enter_context(p)


class TestFollowCrossUser(FrappeTestCase):
	def _run(self, session_user, roles, fn, **kwargs):
		from contextlib import ExitStack

		previous = frappe.session.user
		frappe.set_user(session_user)
		self.addCleanup(frappe.set_user, previous)
		with ExitStack() as stack:
			_enter(_as(roles), stack)
			stack.enter_context(
				patch("frappe.desk.form.document_follow._follow_document", create=True, return_value=True)
			)
			stack.enter_context(
				patch("frappe.desk.form.document_follow._unfollow_document", create=True, return_value=True)
			)
			stack.enter_context(patch("frappe.desk.form.document_follow.follow_document", return_value=True))
			stack.enter_context(
				patch("frappe.desk.form.document_follow.unfollow_document", return_value=True)
			)
			return fn(**kwargs)

	def test_non_sm_cross_user_follow_refused(self):
		with self.assertRaises(PermissionDeniedError):
			self._run(
				SESSION, ["Desk User"], follow_document, doctype="User", name="Administrator", user=OTHER
			)

	def test_non_sm_cross_user_follow_batch_refused(self):
		with self.assertRaises(PermissionDeniedError):
			self._run(SESSION, ["Desk User"], follow_document, doctype="User", names=["a", "b"], user=OTHER)

	def test_non_sm_cross_user_unfollow_refused(self):
		with self.assertRaises(PermissionDeniedError):
			self._run(
				SESSION, ["Desk User"], unfollow_document, doctype="User", name="Administrator", user=OTHER
			)

	def test_non_sm_cross_user_unfollow_batch_refused(self):
		with self.assertRaises(PermissionDeniedError):
			self._run(SESSION, ["Desk User"], unfollow_document, doctype="User", names=["a"], user=OTHER)

	def test_system_manager_cross_user_allowed(self):
		out = self._run(
			SESSION, ["System Manager"], follow_document, doctype="User", name="Administrator", user=OTHER
		)
		self.assertEqual(out["user"], OTHER)
		out = self._run(
			SESSION, ["System Manager"], unfollow_document, doctype="User", name="Administrator", user=OTHER
		)
		self.assertEqual(out["user"], OTHER)

	def test_same_user_unchanged_for_non_sm(self):
		out = self._run(SESSION, ["Desk User"], follow_document, doctype="User", name="Administrator")
		self.assertEqual(out["user"], SESSION)
		out = self._run(
			SESSION, ["Desk User"], unfollow_document, doctype="User", name="Administrator", user=SESSION
		)
		self.assertEqual(out["user"], SESSION)
