"""Tests for jarvis.tools.get_comments.

The Comment DocType is System Manager / Website Manager only, so a regular user
was refused when the agent tried to read the comments on a record they can open
in Desk. get_comments applies Desk's rule instead: read permission on the record
the comments are on. Fixtures use ToDo, whose read permission is per record
(allocated_to / assigned_by), so one plain user can read one record and not the
other.
"""

from __future__ import annotations

import contextlib
import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import get_comments as gc_module
from jarvis.tools.get_comments import get_comments
from jarvis.tools.get_list import get_list
from jarvis.tools.query import query

USER_A = "gc-user-a@example.com"
USER_B = "gc-user-b@example.com"


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
	if "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")
	# Committed: each test rolls back its own fixtures, the users must survive that.
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


def _todo(allocated_to: str, description: str) -> str:
	doc = frappe.get_doc(
		{"doctype": "ToDo", "description": description, "allocated_to": allocated_to, "status": "Open"}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _comment(
	reference_name: str,
	content: str,
	minutes_ago: int,
	by: str = USER_A,
	comment_type: str = "Comment",
	edited: bool = False,
) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": comment_type,
			"reference_doctype": "ToDo",
			"reference_name": reference_name,
			"content": content,
			"comment_email": by,
			"comment_by": by.split("@")[0],
		}
	)
	doc.insert(ignore_permissions=True)
	at = now_datetime() - timedelta(minutes=minutes_ago)
	modified = at + timedelta(minutes=1) if edited else at
	frappe.db.set_value("Comment", doc.name, {"creation": at, "modified": modified}, update_modified=False)
	return doc.name


def _names(out: dict) -> list[str]:
	return [c["name"] for c in out["comments"]]


def _in(*names: str) -> dict:
	return {"name": ["in", list(names)]}


class TestGetComments(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A)
		_ensure_user(USER_B)

	def setUp(self):
		self.todo_a = _todo(USER_A, "Prepare appraisal for Ravi")
		self.todo_b = _todo(USER_B, "Confidential note for B")

	def tearDown(self):
		frappe.db.rollback()

	def test_single_record_returns_comments_newest_first_as_plain_text(self):
		_comment(self.todo_a, "<p>First look done</p>", minutes_ago=30)
		_comment(self.todo_a, "<p>Review this on <strong>Monday</strong></p>", minutes_ago=5, edited=True)
		_comment(self.todo_b, "<p>Not for A</p>", minutes_ago=1)
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a)
		self.assertEqual(out["count"], 2)
		self.assertFalse(out["has_more"])
		self.assertIsNone(out["next_before"])
		first, second = out["comments"]
		self.assertIn("Review this on", first["text"])
		self.assertIn("Monday", first["text"])
		self.assertNotIn("<p>", first["text"])
		self.assertTrue(first["edited"])
		self.assertFalse(second["edited"])
		self.assertEqual(first["by_email"], USER_A)
		self.assertEqual(first["reference_name"], self.todo_a)
		self.assertEqual(first["title"], "Prepare appraisal for Ravi")

	def test_single_record_without_read_permission_is_refused(self):
		_comment(self.todo_b, "<p>Private</p>", minutes_ago=1)
		with _as(USER_A), self.assertRaises(PermissionDeniedError):
			get_comments("ToDo", self.todo_b)

	def test_user_without_comment_doctype_access_can_still_read(self):
		# The exact gap: the plain user cannot read the Comment DocType itself.
		with _as(USER_A):
			self.assertFalse(frappe.has_permission("Comment", "read"))
		_comment(self.todo_a, "<p>Visible through the record</p>", minutes_ago=1)
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a)
		self.assertEqual(out["count"], 1)

	def test_doctype_wide_keeps_only_readable_records(self):
		_comment(self.todo_a, "<p>On A</p>", minutes_ago=10)
		_comment(self.todo_b, "<p>On B</p>", minutes_ago=5)
		with _as(USER_A):
			out = get_comments("ToDo")
		refs = {c["reference_name"] for c in out["comments"]}
		self.assertIn(self.todo_a, refs)
		self.assertNotIn(self.todo_b, refs)

	def test_doctype_wide_without_doctype_read_is_refused(self):
		with _as(USER_A), self.assertRaises(PermissionDeniedError):
			get_comments("Role")

	def test_search_and_since_narrow_the_scan(self):
		_comment(self.todo_a, "<p>Old review note</p>", minutes_ago=60 * 24 * 10)
		_comment(self.todo_a, "<p>Review next Monday</p>", minutes_ago=10)
		_comment(self.todo_a, "<p>Unrelated</p>", minutes_ago=5)
		since = (now_datetime() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a, search="review", since=since)
		self.assertEqual([c["text"] for c in out["comments"]], ["Review next Monday"])

	def test_only_comment_type_comment_is_returned(self):
		_comment(self.todo_a, "<p>Real comment</p>", minutes_ago=5)
		_comment(self.todo_a, "Liked by someone", minutes_ago=1, comment_type="Like")
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a)
		self.assertEqual([c["text"] for c in out["comments"]], ["Real comment"])

	def test_paging_with_before_cursor(self):
		for i, minutes in enumerate((30, 20, 10)):
			_comment(self.todo_a, f"<p>Note {i}</p>", minutes_ago=minutes)
		with _as(USER_A):
			page1 = get_comments("ToDo", self.todo_a, limit=2)
			self.assertEqual([c["text"] for c in page1["comments"]], ["Note 2", "Note 1"])
			self.assertTrue(page1["has_more"])
			page2 = get_comments("ToDo", self.todo_a, limit=2, before=page1["next_before"])
		self.assertEqual([c["text"] for c in page2["comments"]], ["Note 0"])
		self.assertFalse(page2["has_more"])

	def test_long_comment_is_clipped_and_flagged(self):
		_comment(self.todo_a, "<p>" + ("x" * 5000) + "</p>", minutes_ago=1)
		with _as(USER_A):
			(c,) = get_comments("ToDo", self.todo_a)["comments"]
		self.assertTrue(c["text_truncated"])
		self.assertLessEqual(len(c["text"]), gc_module._MAX_TEXT_CHARS + 4)

	def test_text_budget_ends_page_early(self):
		for minutes in (30, 20, 10):
			_comment(self.todo_a, "<p>" + ("y" * 1500) + "</p>", minutes_ago=minutes)
		with _as(USER_A), patch.object(gc_module, "_PAGE_TEXT_BUDGET", 2000):
			out = get_comments("ToDo", self.todo_a)
		self.assertEqual(out["count"], 1)
		self.assertTrue(out["has_more"])

	def test_scan_cap_returns_cursor_past_unreadable_comments(self):
		_comment(self.todo_a, "<p>Older readable</p>", minutes_ago=60)
		for minutes in (30, 20, 10):
			_comment(self.todo_b, "<p>Unreadable</p>", minutes_ago=minutes)
		with (
			_as(USER_A),
			patch.object(gc_module, "_SCAN_CHUNK", 2),
			patch.object(gc_module, "_MAX_SCANNED", 2),
		):
			first = get_comments("ToDo")
			self.assertEqual(first["comments"], [])
			self.assertTrue(first["has_more"])
			second = get_comments("ToDo", before=first["next_before"])
		self.assertIn("Older readable", [c["text"] for c in second["comments"]])

	def test_invalid_arguments(self):
		with _as(USER_A):
			with self.assertRaises(InvalidArgumentError):
				get_comments("")
			with self.assertRaises(InvalidArgumentError):
				get_comments("No Such DocType")
			with self.assertRaises(InvalidArgumentError):
				get_comments("Comment")
			with self.assertRaises(InvalidArgumentError):
				get_comments("ToDo", "missing-todo-name")
			with self.assertRaises(InvalidArgumentError):
				get_comments("ToDo", self.todo_a, limit=0)
			with self.assertRaises(InvalidArgumentError):
				get_comments("ToDo", self.todo_a, since="not a date")

	def test_limit_is_capped(self):
		for minutes in range(1, 56):
			_comment(self.todo_a, f"<p>n{minutes}</p>", minutes_ago=minutes)
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a, limit=500)
		self.assertEqual(out["count"], gc_module._MAX_LIMIT)
		self.assertTrue(out["has_more"])

	def test_get_list_and_query_on_comment_point_to_get_comments(self):
		with _as(USER_A):
			with self.assertRaises(PermissionDeniedError) as ctx:
				get_list("Comment", fields=["name"])
			self.assertIn("get_comments", str(ctx.exception))
			with self.assertRaises(PermissionDeniedError) as ctx:
				query({"from": "Comment", "select": ["name"]})
			self.assertIn("get_comments", str(ctx.exception))

	def test_a_scan_spanning_several_chunks_finds_every_comment(self):
		for minutes in (50, 40, 30, 20, 10):
			_comment(self.todo_a, f"<p>chunked {minutes}</p>", minutes_ago=minutes)
		with _as(USER_A), patch.object(gc_module, "_SCAN_CHUNK", 2):
			out = get_comments("ToDo", search="chunked")
		self.assertEqual(len(out["comments"]), 5)
		self.assertFalse(out["has_more"])

	def test_a_page_that_holds_every_comment_has_no_more(self):
		for minutes in (20, 10):
			_comment(self.todo_a, f"<p>Exact {minutes}</p>", minutes_ago=minutes)
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a, limit=2)
		self.assertEqual(out["count"], 2)
		self.assertFalse(out["has_more"])
		self.assertIsNone(out["next_before"])

	def test_comments_sharing_a_timestamp_are_neither_skipped_nor_repeated(self):
		names = [_comment(self.todo_a, f"<p>Same {i}</p>", minutes_ago=15) for i in range(3)]
		same = frappe.db.get_value("Comment", names[0], "creation")
		for n in names:
			frappe.db.set_value("Comment", n, {"creation": same, "modified": same}, update_modified=False)
		seen = []
		before = None
		with _as(USER_A):
			for _ in range(4):
				out = get_comments("ToDo", self.todo_a, limit=1, before=before)
				seen += [c["name"] for c in out["comments"]]
				if not out["has_more"]:
					break
				before = out["next_before"]
		self.assertEqual(sorted(seen), sorted(names))

	def test_a_comment_on_a_deleted_record_does_not_break_the_scan(self):
		_comment(self.todo_a, "<p>still here</p>", minutes_ago=20)
		# A record removed under its comment (a raw delete, a restore): the comment stays.
		gone = _todo(USER_A, "deleted under its comment")
		_comment(gone, "<p>orphan here</p>", minutes_ago=10)
		frappe.db.delete("ToDo", gone)
		with _as(USER_A):
			out = get_comments("ToDo", search="here")
		self.assertEqual([c["text"] for c in out["comments"]], ["still here"])

	def test_search_matches_percent_and_underscore_literally(self):
		_comment(self.todo_a, "<p>discount 100% applied</p>", minutes_ago=20)
		_comment(self.todo_a, "<p>discount 1000 pending</p>", minutes_ago=10)
		_comment(self.todo_a, "<p>file a_b.pdf</p>", minutes_ago=5)
		with _as(USER_A):
			pct = get_comments("ToDo", self.todo_a, search="100%")
			under = get_comments("ToDo", self.todo_a, search="a_b")
			loose = get_comments("ToDo", self.todo_a, search="axb")
		self.assertEqual([c["text"] for c in pct["comments"]], ["discount 100% applied"])
		self.assertEqual([c["text"] for c in under["comments"]], ["file a_b.pdf"])
		self.assertEqual(loose["comments"], [])

	def test_a_scan_out_of_time_says_so_and_where_to_continue(self):
		_comment(self.todo_a, "<p>Later</p>", minutes_ago=5)
		with _as(USER_A), patch.object(gc_module, "_TIME_BUDGET_S", -1):
			out = get_comments("ToDo")
		self.assertTrue(out["has_more"])
		self.assertIn("before=next_before", out["note"])
		self.assertTrue(out["next_before"])

	def test_since_with_an_offset_is_read_on_the_site_clock(self):
		_comment(self.todo_a, "<p>Recent</p>", minutes_ago=30)
		from zoneinfo import ZoneInfo

		site = ZoneInfo(frappe.utils.get_system_timezone())
		hour_ago = (now_datetime() - timedelta(hours=1)).replace(tzinfo=site).astimezone(ZoneInfo("UTC"))
		with _as(USER_A):
			out = get_comments("ToDo", self.todo_a, since=hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ"))
		self.assertEqual([c["text"] for c in out["comments"]], ["Recent"])

	def test_arguments_must_be_text(self):
		with _as(USER_A), self.assertRaisesRegex(InvalidArgumentError, "name must be text"):
			get_comments("ToDo", {"name": ["like", "%"]})


class TestGetCommentsForASetOfRecords(FrappeTestCase):
	"""``filters``: the comments on every readable record that matches a get_list filter."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A)
		_ensure_user(USER_B)

	def setUp(self):
		self.todo_a = _todo(USER_A, "Prepare appraisal for Ravi")
		self.todo_a2 = _todo(USER_A, "Second appraisal check")
		# Readable by USER_A, but never named by the filters below.
		self.todo_a3 = _todo(USER_A, "Unrelated chore")
		self.todo_b = _todo(USER_B, "Confidential note for B")

	def tearDown(self):
		frappe.db.rollback()

	# -- arguments ---------------------------------------------------------

	def test_filters_with_name_is_refused(self):
		with _as(USER_A), self.assertRaisesRegex(InvalidArgumentError, r"(?s)name.*filters"):
			get_comments("ToDo", self.todo_a, filters={"status": "Open"})

	def test_filters_type_confusion_is_refused(self):
		for bad in (5, True, "not json", '"just text"', "null", "7", "[" * 100000):
			with self.subTest(filters=bad), _as(USER_A):
				with self.assertRaisesRegex(InvalidArgumentError, "filters"):
					get_comments("ToDo", filters=bad)

	def test_empty_filters_mean_no_filters(self):
		_comment(self.todo_a, "<p>On A</p>", minutes_ago=5)
		_comment(self.todo_b, "<p>On B</p>", minutes_ago=4)
		with _as(USER_A):
			for empty in ({}, [], "", "{}", "[]", None):
				with self.subTest(filters=empty):
					one = get_comments("ToDo", self.todo_a, filters=empty)
					self.assertEqual(one["name"], self.todo_a)
					self.assertEqual([c["text"] for c in one["comments"]], ["On A"])
			wide = get_comments("ToDo")
			self.assertEqual(get_comments("ToDo", filters=[]), wide)
			self.assertEqual(get_comments("ToDo", filters={}), wide)
		self.assertNotIn(self.todo_b, {c["reference_name"] for c in wide["comments"]})

	# -- resolving the set --------------------------------------------------

	def test_filters_return_comments_on_matching_readable_records_only(self):
		on_a = _comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		on_a2 = _comment(self.todo_a2, "<p>On A2</p>", minutes_ago=30)
		_comment(self.todo_a3, "<p>On A3</p>", minutes_ago=20)
		_comment(self.todo_b, "<p>On B</p>", minutes_ago=10)
		with _as(USER_A):
			out = get_comments("ToDo", filters=_in(self.todo_a, self.todo_a2, self.todo_b))
		self.assertEqual(_names(out), [on_a2, on_a])
		self.assertEqual([c["reference_name"] for c in out["comments"]], [self.todo_a2, self.todo_a])
		self.assertIsNone(out["name"])
		self.assertEqual(out["count"], 2)
		self.assertFalse(out["has_more"])

	def test_filters_on_a_field_dict_list_and_json_forms(self):
		on_a = _comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		on_a2 = _comment(self.todo_a2, "<p>On A2</p>", minutes_ago=30)
		_comment(self.todo_a3, "<p>On A3</p>", minutes_ago=20)
		_comment(self.todo_b, "<p>On B</p>", minutes_ago=10)
		as_dict = {"description": ["like", "%appraisal%"]}
		forms = (
			as_dict,
			[["description", "like", "%appraisal%"]],
			[["ToDo", "description", "like", "%appraisal%"]],
			json.dumps(as_dict),
		)
		with _as(USER_A):
			for form in forms:
				with self.subTest(filters=form):
					self.assertEqual(_names(get_comments("ToDo", filters=form)), [on_a2, on_a])

	def test_filters_over_the_cap_are_refused(self):
		_comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		_comment(self.todo_a2, "<p>On A2</p>", minutes_ago=30)
		with (
			_as(USER_A),
			patch.object(gc_module, "_MAX_FILTER_RECORDS", 1),
			self.assertRaisesRegex(InvalidArgumentError, "narrow"),
		):
			get_comments("ToDo", filters=_in(self.todo_a, self.todo_a2))

	def test_filters_exactly_at_the_cap_are_served(self):
		on_a = _comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		on_a2 = _comment(self.todo_a2, "<p>On A2</p>", minutes_ago=30)
		with _as(USER_A), patch.object(gc_module, "_MAX_FILTER_RECORDS", 2):
			out = get_comments("ToDo", filters=_in(self.todo_a, self.todo_a2))
		self.assertEqual(_names(out), [on_a2, on_a])

	def test_filters_matching_nothing_return_an_empty_page(self):
		_comment(self.todo_a, "<p>On A</p>", minutes_ago=5)
		with _as(USER_A):
			out = get_comments("ToDo", filters={"name": "no-such-todo"})
		self.assertEqual(out["comments"], [])
		self.assertEqual(out["count"], 0)
		self.assertFalse(out["has_more"])
		self.assertIsNone(out["next_before"])
		self.assertIsNone(out["name"])
		self.assertIn("No ToDo record you can read", out["note"])

	def test_filters_matching_only_unreadable_records_return_an_empty_page(self):
		_comment(self.todo_b, "<p>On B</p>", minutes_ago=5)
		with _as(USER_A):
			out = get_comments("ToDo", filters=_in(self.todo_b))
		self.assertEqual(out["comments"], [])
		self.assertIn("note", out)

	def test_filters_without_doctype_read_are_refused(self):
		with _as(USER_A), self.assertRaises(PermissionDeniedError):
			get_comments("Role", filters={"name": "Guest"})

	# -- reading the comments of the set -----------------------------------

	def test_filters_resolve_once_as_the_caller(self):
		_comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		real = frappe.get_list
		seen = []

		def spy(doctype, *args, **kwargs):
			seen.append((doctype, kwargs, frappe.session.user))
			return real(doctype, *args, **kwargs)

		with (
			_as(USER_A),
			patch.object(gc_module.frappe, "get_list", side_effect=spy),
			patch.object(gc_module._Readable, "prefetch", side_effect=AssertionError("prefetch")),
		):
			out = get_comments("ToDo", filters=_in(self.todo_a, self.todo_b))
		self.assertEqual(out["count"], 1)
		# The title lookup also lands here (get_all calls get_list); only the
		# resolution call has pluck="name".
		resolving = [c for c in seen if c[0] == "ToDo" and c[1].get("pluck") == "name"]
		self.assertEqual(len(resolving), 1)
		(_, kwargs, user) = resolving[0]
		self.assertEqual(user, USER_A)
		self.assertEqual(kwargs["pluck"], "name")
		self.assertEqual(kwargs["limit"], gc_module._MAX_FILTER_RECORDS + 1)
		self.assertFalse(kwargs.get("ignore_permissions"))

	def test_filters_still_check_each_shown_record(self):
		on_a = _comment(self.todo_a, "<p>On A</p>", minutes_ago=40)
		_comment(self.todo_a2, "<p>On A2</p>", minutes_ago=30)
		real = frappe.has_permission

		def deny_a2(doctype, *args, **kwargs):
			if kwargs.get("doc") == self.todo_a2:
				return False
			return real(doctype, *args, **kwargs)

		with _as(USER_A), patch.object(frappe, "has_permission", side_effect=deny_a2):
			out = get_comments("ToDo", filters=_in(self.todo_a, self.todo_a2))
		self.assertEqual(_names(out), [on_a])

	def test_filters_paging_with_before_is_exact(self):
		todo_a4 = _todo(USER_A, "Fourth record")
		made = [
			_comment(self.todo_a, "<p>one</p>", minutes_ago=50),
			_comment(self.todo_a2, "<p>two</p>", minutes_ago=40),
			_comment(todo_a4, "<p>three</p>", minutes_ago=30),
			_comment(self.todo_a, "<p>four</p>", minutes_ago=20),
			_comment(self.todo_a2, "<p>five</p>", minutes_ago=20),
		]
		# Two comments on one instant: the name tiebreak of the cursor decides.
		same = frappe.db.get_value("Comment", made[3], "creation")
		frappe.db.set_value("Comment", made[4], {"creation": same, "modified": same}, update_modified=False)
		outsider = _comment(self.todo_a3, "<p>newest, not asked for</p>", minutes_ago=1)
		expected = sorted(
			made, key=lambda n: (frappe.db.get_value("Comment", n, "creation"), n), reverse=True
		)
		filters = _in(self.todo_a, self.todo_a2, todo_a4)
		seen: list[str] = []
		before = None
		with _as(USER_A):
			for _ in range(6):
				out = get_comments("ToDo", filters=filters, limit=2, before=before)
				seen += _names(out)
				if not out["has_more"]:
					break
				before = out["next_before"]
		self.assertEqual(seen, expected)
		self.assertNotIn(outsider, seen)

	def test_filters_combine_with_search_and_since(self):
		_comment(self.todo_a, "<p>Old review note</p>", minutes_ago=60 * 24 * 10)
		wanted = _comment(self.todo_a2, "<p>Review next Monday</p>", minutes_ago=10)
		_comment(self.todo_a, "<p>Unrelated</p>", minutes_ago=5)
		_comment(self.todo_a3, "<p>Review on the outsider</p>", minutes_ago=3)
		since = (now_datetime() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
		with _as(USER_A):
			out = get_comments("ToDo", filters=_in(self.todo_a, self.todo_a2), search="review", since=since)
		self.assertEqual(_names(out), [wanted])

	def test_filters_scan_cap_still_bounds_a_denied_set(self):
		_comment(self.todo_a2, "<p>Older readable</p>", minutes_ago=60)
		for minutes in (30, 20, 10):
			_comment(self.todo_a, "<p>Denied</p>", minutes_ago=minutes)
		real = frappe.has_permission

		def deny_a(doctype, *args, **kwargs):
			if kwargs.get("doc") == self.todo_a:
				return False
			return real(doctype, *args, **kwargs)

		filters = _in(self.todo_a, self.todo_a2)
		with (
			_as(USER_A),
			patch.object(frappe, "has_permission", side_effect=deny_a),
			patch.object(gc_module, "_SCAN_CHUNK", 2),
			patch.object(gc_module, "_MAX_SCANNED", 2),
		):
			first = get_comments("ToDo", filters=filters)
			self.assertEqual(first["comments"], [])
			self.assertTrue(first["has_more"])
			self.assertTrue(first["next_before"])
			self.assertIn("matching records", first["note"])
			second = get_comments("ToDo", filters=filters, before=first["next_before"])
		self.assertIn("Older readable", [c["text"] for c in second["comments"]])
