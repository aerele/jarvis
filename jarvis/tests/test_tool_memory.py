"""Per-user agent memory: remember/recall tools + cross-user isolation.

The load-bearing test is ``test_recall_never_crosses_users``: user A saves a
secret, user B recalls, B gets nothing. Memory is scoped to ``frappe.session.user``
in the tools (no user arg) and, for any other read path, by the doctype's
``get_permission_query_conditions``.
"""

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.jarvis.doctype.jarvis_user_memory.jarvis_user_memory import (
	get_permission_query_conditions,
)
from jarvis.tools import _agent_run_ctx, _user_memory
from jarvis.tools.recall import recall
from jarvis.tools.registry import dispatch
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


class TestConcurrentMemorySaves(FrappeTestCase):
	"""#715 F09: two chats that read the same memory and each learn a new fact.

	One test transaction cannot hold two connections without committing, so these
	run the interleavings in sequence (the compare-and-swap logic) and check that the
	compare reads the row with FOR UPDATE (the lock that serialises real overlap)."""

	def setUp(self):
		self.user = "mem_cas@example.com"
		_ensure_user(self.user)
		# The class shares one transaction (rolled back at the end), so each test
		# starts this user with no memory.
		frappe.db.delete("Jarvis User Memory", {"user": self.user})
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(_agent_run_ctx.clear_session_key)
		frappe.set_user(self.user)

	def _as_session(self, session_key: str) -> None:
		"""Act as one chat (plugin session) of the same user; forget its cached base after."""
		_agent_run_ctx.set_session_key(session_key)
		key = _user_memory._seen_key()
		self.addCleanup(frappe.cache.delete_value, key, user=self.user)

	def test_two_saves_from_one_read_never_lose_the_first_fact(self):
		remember(content="- M")
		base = recall().get("version")  # both chats read M
		first = dispatch("remember", {"content": "- M\n- A", "base_version": base})
		second = dispatch("remember", {"content": "- M\n- B", "base_version": base})
		self.assertTrue(first["saved"])
		self.assertFalse(second["saved"])
		self.assertTrue(second["conflict"])
		self.assertEqual(second["content"], "- M\n- A")
		self.assertEqual(recall()["content"], "- M\n- A")

	def test_conflict_hands_back_what_a_merged_retry_needs(self):
		remember(content="- M")
		base = recall()["version"]
		remember(content="- M\n- A", base_version=base)
		conflict = remember(content="- M\n- B", base_version=base)
		self.assertEqual(conflict["version"], recall()["version"])
		self.assertIn("ask the user", conflict["message"])
		merged = remember(content="- M\n- A\n- B", base_version=conflict["version"])
		self.assertTrue(merged["saved"])
		self.assertEqual(recall()["content"], "- M\n- A\n- B")

	def test_recall_version_tracks_the_blob(self):
		self.assertEqual(recall(), {"content": "", "version": _user_memory.EMPTY_VERSION})
		out = remember(content="- likes tabs")
		self.assertEqual(out["version"], recall()["version"])
		self.assertNotEqual(out["version"], _user_memory.EMPTY_VERSION)

	def test_a_chat_can_chain_saves_on_its_own_receipt(self):
		first = remember(content="- A", base_version=recall()["version"])
		second = remember(content="- A\n- C", base_version=first["version"])
		self.assertTrue(second["saved"])
		self.assertEqual(recall()["content"], "- A\n- C")

	def test_first_saves_from_two_chats_conflict_too(self):
		base = recall()["version"]  # both chats start with no memory at all
		self.assertTrue(remember(content="- A", base_version=base)["saved"])
		second = remember(content="- B", base_version=base)
		self.assertTrue(second["conflict"])
		self.assertEqual(second["content"], "- A")

	def test_a_stale_base_with_no_row_is_a_conflict_on_the_empty_blob(self):
		out = remember(content="- B", base_version=_user_memory.version_of("- gone"))
		self.assertEqual((out["saved"], out["content"]), (False, ""))
		self.assertEqual(out["version"], _user_memory.EMPTY_VERSION)
		self.assertFalse(frappe.db.exists("Jarvis User Memory", {"user": self.user}))

	def test_a_concurrent_first_insert_falls_back_to_the_locked_compare(self):
		remember(content="- A")  # the other chat's row, invisible to this one's first read
		real_get_value = frappe.db.get_value

		def first_read_misses(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "Jarvis User Memory" and fieldname == "name" and not kwargs.get("for_update"):
				return None
			return real_get_value(doctype, filters, fieldname, *args, **kwargs)

		with mock.patch.object(frappe.db, "get_value", first_read_misses):
			out = remember(content="- B", base_version=_user_memory.EMPTY_VERSION)
			self.assertTrue(out["conflict"])
			self.assertEqual(out["content"], "- A")
			# No base at all keeps the old last-write-wins, through the same path.
			self.assertTrue(remember(content="- B")["saved"])
		self.assertEqual(recall()["content"], "- B")
		self.assertEqual(frappe.db.count("Jarvis User Memory", {"user": self.user}), 1)

	def test_the_compare_reads_the_row_under_a_lock(self):
		remember(content="- M")
		base = recall()["version"]
		with mock.patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
			self.assertTrue(remember(content="- M\n- A", base_version=base)["saved"])
		statements = [str(c.args[0]).strip().lower() for c in spy.call_args_list if c.args]
		memory = [q for q in statements if "jarvis user memory" in q]
		blob_reads = [q for q in memory if q.startswith("select") and ("`content`" in q or "select *" in q)]
		# The read the compare runs on is the locking one, not a plain snapshot read.
		self.assertIn("for update", blob_reads[0])
		self.assertTrue(memory[-1].startswith("update"))

	def test_a_session_without_base_version_is_checked_against_what_it_last_saw(self):
		remember(content="- M")
		self._as_session("f09-chat-1")
		recall()  # the session-start memory injection
		self._as_session("f09-chat-2")
		recall()
		self.assertTrue(remember(content="- M\n- A")["saved"])
		self._as_session("f09-chat-1")  # an old plugin: no base_version
		stale = remember(content="- M\n- B")
		self.assertTrue(stale["conflict"])
		self.assertEqual(
			frappe.db.get_value("Jarvis User Memory", {"user": self.user}, "content"), "- M\n- A"
		)
		# The conflict showed chat 1 the blob, so its merged retry lands.
		self.assertTrue(remember(content="- M\n- A\n- B")["saved"])

	def test_an_explicit_base_version_outranks_the_session_base(self):
		remember(content="- M")
		self._as_session("f09-chat-1")
		recall()
		_agent_run_ctx.clear_session_key()
		fresh = remember(content="- M\n- A")["version"]  # saved from outside the session
		self._as_session("f09-chat-1")
		self.assertTrue(remember(content="- M\n- A\n- B", base_version=fresh)["saved"])

	def test_rejects_a_non_string_base_version(self):
		with self.assertRaises(InvalidArgumentError):
			remember(content="- A", base_version=123)
