"""Per-user agent memory: remember/recall tools + cross-user isolation.

The load-bearing test is ``test_recall_never_crosses_users``: user A saves a
secret, user B recalls, B gets nothing. Memory is scoped to ``frappe.session.user``
in the tools (no user arg) and, for any other read path, by the doctype's
``get_permission_query_conditions``.
"""

from unittest import mock

import frappe
from frappe.exceptions import TimestampMismatchError
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.jarvis.doctype.jarvis_user_memory.jarvis_user_memory import (
	get_permission_query_conditions,
)
from jarvis.tools.recall import recall
from jarvis.tools.remember import remember


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)


class TestUserMemoryTools(FrappeTestCase):
	def setUp(self):
		self.a = "mem_a@example.com"
		self.b = "mem_b@example.com"
		_ensure_user(self.a)
		_ensure_user(self.b)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_remember_creates_and_recalls_own(self):
		frappe.set_user(self.a)
		text = "- likes tabs"
		out = remember(content=text)
		self.assertEqual(out["saved"], True)
		self.assertEqual(out["bytes"], len(text.encode("utf-8")))
		self.assertEqual(recall()["content"], text)

	def test_remember_full_replace_single_doc(self):
		frappe.set_user(self.a)
		remember(content="- one")
		remember(content="- two")
		rows = frappe.get_all("Jarvis User Memory", filters={"user": self.a}, ignore_permissions=True)
		self.assertEqual(len(rows), 1)
		self.assertEqual(recall()["content"], "- two")

	def test_recall_empty_for_new_user(self):
		frappe.set_user(self.b)
		self.assertEqual(recall()["content"], "")

	def test_recall_never_crosses_users(self):
		frappe.set_user(self.a)
		remember(content="- secret-A")
		frappe.set_user(self.b)
		self.assertEqual(recall()["content"], "")  # B never sees A's memory

	def test_remember_rejects_oversized(self):
		frappe.set_user(self.a)
		with self.assertRaises(InvalidArgumentError):
			remember(content="x" * (8192 + 1))

	def test_remember_retries_on_timestamp_mismatch(self):
		frappe.set_user(self.a)
		remember(content="- base")
		real_save = frappe.model.document.Document.save
		calls = {"n": 0}

		def flaky(doc, *args, **kwargs):
			if doc.doctype == "Jarvis User Memory" and calls["n"] == 0:
				calls["n"] += 1
				raise TimestampMismatchError
			return real_save(doc, *args, **kwargs)

		with mock.patch.object(frappe.model.document.Document, "save", flaky):
			out = remember(content="- updated")
		self.assertTrue(out["saved"])
		self.assertEqual(calls["n"], 1)  # it really did hit the mismatch once
		self.assertEqual(recall()["content"], "- updated")

	def test_get_permission_query_conditions_scopes_to_user(self):
		cond = get_permission_query_conditions(user=self.a)
		self.assertIn("`user`", cond)
		self.assertIn(self.a, cond)
		# a different session user yields a different scope
		self.assertNotEqual(cond, get_permission_query_conditions(user=self.b))

	def test_list_read_does_not_cross_users(self):
		frappe.set_user(self.a)
		remember(content="- secret-A")
		frappe.set_user(self.b)
		# The permission-CHECKED list path (get_list applies DocPerm +
		# get_permission_query_conditions; get_all deliberately bypasses both). B is
		# either denied outright (no DocPerm) or scoped to its own rows - never A's.
		try:
			rows = frappe.get_list("Jarvis User Memory", fields=["user"])
		except frappe.PermissionError:
			rows = []
		self.assertNotIn(self.a, [r.user for r in rows])
