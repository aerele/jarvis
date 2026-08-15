"""Tests for jarvis.chat.api.search_conversations (DESIGN-V3 §8.2 / D40).

Covers: owner scoping (another user's rows invisible) · Archived excluded ·
LIKE-wildcard escaping (``%`` and ``_`` are matched literally) · envelope keys
+ has_more math across pages · page_length clamp (1..50) / negative start ·
starred-first then last_active_at-desc ordering.

Uses two fresh non-SM users so totals are exact (they own nothing else).
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from jarvis.chat.api import search_conversations
from jarvis.permissions import ensure_jarvis_user_role

USER_A = "cs-user-a@example.com"
USER_B = "cs-user-b@example.com"

CONV = "Jarvis Conversation"


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	roles = set(frappe.get_roles(email))
	if "System Manager" in roles:
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	# A real chat user holds the "Jarvis User" role (granted at onboarding /
	# migration); it gates the chat APIs. Keep this fixture a *plain* Jarvis user
	# (no System Manager) so the owner-scoping assertions still exercise the
	# non-admin path, but with app access so search_conversations isn't 403'd.
	ensure_jarvis_user_role()  # the test site may not have run after_migrate
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


def _wipe() -> None:
	for name in frappe.get_all(CONV, filters={"title": ["like", "cs-%"]}, pluck="name"):
		frappe.db.delete("Jarvis Chat Message", {"conversation": name})
		frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_conv(
	owner: str, title: str, status: str = "Active", starred: int = 0, active_days_ago: int = 0
) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": CONV,
				"title": title,
				"status": status,
				"starred": starred,
			}
		)
		doc.insert(ignore_permissions=True)
	# search_conversations hides message-less drafts, so give each fixture a
	# message to keep it searchable (a real chat always has at least one).
	frappe.get_doc(
		{
			"doctype": "Jarvis Chat Message",
			"conversation": doc.name,
			"seq": 1,
			"role": "user",
			"content": "hi",
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value(
		CONV,
		doc.name,
		"last_active_at",
		now_datetime() - timedelta(days=active_days_ago),
		update_modified=False,
	)
	frappe.db.commit()
	return doc.name


def _names(rows):
	return [r["name"] for r in rows]


class TestConversationSearch(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_wipe()
		# USER_A actives (6): a starred old one must still sort first.
		cls.starred_old = _mk_conv(USER_A, "cs-starred old", starred=1, active_days_ago=30)
		cls.newest = _mk_conv(USER_A, "cs-alpha report", active_days_ago=0)
		cls.mid = _mk_conv(USER_A, "cs-beta plan", active_days_ago=1)
		cls.pct = _mk_conv(USER_A, "cs-100% done", active_days_ago=2)
		cls.underscore = _mk_conv(USER_A, "cs-a_b tokens", active_days_ago=3)
		cls.underscore_bait = _mk_conv(USER_A, "cs-axb tokens", active_days_ago=4)
		# excluded: archived + another owner's row
		cls.archived = _mk_conv(USER_A, "cs-archived thing", status="Archived")
		cls.b_conv = _mk_conv(USER_B, "cs-b private", active_days_ago=0)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe()

	def _search(self, user=USER_A, **kw):
		with _as(user):
			return search_conversations(**kw)

	def test_envelope_keys(self):
		res = self._search()
		self.assertEqual(set(res.keys()), {"rows", "total", "has_more", "start", "page_length"})
		self.assertEqual(set(res["rows"][0].keys()), {"name", "title", "starred", "last_active_at"})

	def test_owner_scoping(self):
		names = set(_names(self._search()["rows"]))
		self.assertNotIn(self.b_conv, names)
		b_res = self._search(user=USER_B)
		self.assertEqual(_names(b_res["rows"]), [self.b_conv])
		self.assertEqual(b_res["total"], 1)

	def test_archived_excluded_and_empty_search_returns_all(self):
		res = self._search()
		self.assertEqual(res["total"], 6)  # all of A's actives, not the archived one
		self.assertNotIn(self.archived, _names(res["rows"]))

	def test_like_escaping_percent(self):
		res = self._search(search="100%")
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.pct)
		# an unescaped '%' would wildcard-match everything here:
		self.assertEqual(self._search(search="cs-1%done")["total"], 0)

	def test_like_escaping_underscore(self):
		# unescaped '_' would match "a_b" AND "axb"
		res = self._search(search="a_b")
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.underscore)

	def test_starred_first_then_recent(self):
		rows = self._search()["rows"]
		self.assertEqual(rows[0]["name"], self.starred_old)  # starred beats recency
		rest = [r["name"] for r in rows[1:]]
		self.assertEqual(
			rest,
			[self.newest, self.mid, self.pct, self.underscore, self.underscore_bait],
		)

	def test_has_more_math_and_pagination(self):
		seen, start = [], 0
		while True:
			res = self._search(start=start, page_length=2)
			self.assertEqual(res["total"], 6)
			self.assertEqual(res["start"], start)
			self.assertLessEqual(len(res["rows"]), 2)
			self.assertEqual(res["has_more"], start + len(res["rows"]) < 6)
			seen.extend(_names(res["rows"]))
			if not res["has_more"]:
				break
			start += 2
		self.assertEqual(len(seen), 6)
		self.assertEqual(len(set(seen)), 6)  # no dup/overlap across pages

	def test_page_length_clamp_and_start_floor(self):
		self.assertEqual(self._search(page_length=500)["page_length"], 50)
		self.assertEqual(self._search(page_length=-3)["page_length"], 1)
		self.assertEqual(self._search(page_length=0)["page_length"], 20)  # falsy → default
		res = self._search(start=-10)
		self.assertEqual(res["start"], 0)
		res = self._search(start="oops", page_length="oops")
		self.assertEqual((res["start"], res["page_length"]), (0, 20))

	def test_search_matches_title_substring(self):
		res = self._search(search="alpha")
		self.assertEqual(_names(res["rows"]), [self.newest])


if __name__ == "__main__":
	unittest.main()
