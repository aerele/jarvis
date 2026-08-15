"""Tests for the paginated feature-page endpoints + FB-1 cascade delete
(chat-features-page-migration-design §2.2-2.5 + §2.4/Q4).

Covers, per endpoint: pagination (start/page_length/total/has_more), search
hits, each filter facet, sort asc/desc + default, owner-scoping (a second user's
rows are NEVER returned; System Manager sees all approvals), and filter-injection
(unknown key throws). File Box additionally covers FB-1: cascade delete
(approvals + messages + File + conversation), refuse-while-streaming, bulk skip
of streaming/foreign rows, and clear-processed leaving processing/needs_approval.

Uses ``unittest.TestCase`` (like test_agents_marketplace) with explicit
commits + prefix-based cleanup, since these endpoints run raw owner-scoped SQL
and need two REAL users to prove scoping. Every row this module creates carries
an ``fp-`` marker so ``_wipe_all`` can remove it regardless of owner.
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime, today

from jarvis.chat.approvals_api import list_approvals_page, pending_count
from jarvis.chat.custom_skills_api import list_custom_skills_page
from jarvis.chat.filebox import (
	clear_processed_inbound,
	delete_inbound,
	delete_inbound_bulk,
	list_inbound_page,
)
from jarvis.chat.macros_api import list_macros_page

USER_A = "fp-user-a@example.com"
USER_B = "fp-user-b@example.com"

SKILL = "Jarvis Custom Skill"
MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"


# --------------------------------------------------------------------------- #
# module fixtures / helpers
# --------------------------------------------------------------------------- #
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
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	# Guarantee these test users are NOT System Managers (non-SM scoping path)
	# but DO hold the Jarvis User role (the approvals endpoints are chat-surface
	# and now require it — security review TASK 8).
	roles = set(frappe.get_roles(email))
	if "System Manager" in roles:
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	if "Jarvis User" not in roles:
		frappe.get_doc("User", email).add_roles("Jarvis User")
		frappe.db.commit()
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_A)
	_ensure_user(USER_B)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe_all() -> None:
	"""Delete every fp- marked row (any owner). get_all ignores permissions, so
	this runs correctly whatever the session user is."""
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", "fp-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(MACRO, filters={"macro_name": ["like", "fp-%"]}, pluck="name"):
		for run in frappe.get_all(RUN, filters={"macro": name}, pluck="name"):
			frappe.delete_doc(RUN, run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
	convs = set(
		frappe.get_all(CONV, filters={"title": ["like", "fp-%"]}, pluck="name")
		+ frappe.get_all(CONV, filters={"title": ["like", "File: fp-%"]}, pluck="name")
	)
	for conv in convs:
		for ap in frappe.get_all(APPROVAL, filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc(APPROVAL, ap, force=True, ignore_permissions=True)
		frappe.db.delete(MSG, {"conversation": conv})
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": CONV, "attached_to_name": conv},
			pluck="name",
		):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	for name in frappe.get_all(APPROVAL, filters={"title": ["like", "fp-%"]}, pluck="name"):
		frappe.delete_doc(APPROVAL, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_skill(owner, name, description=None, enabled=1, user_invocable=1, shared_with=None) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": description or f"{name} generic description",
				"instructions": "do the thing",
				"user_invocable": user_invocable,
				"enabled": enabled,
				"shared_with": [{"user": u} for u in (shared_with or [])],
			}
		)
		doc.flags.ignore_validate = True  # bypass the 25/owner cap for fixtures
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_macro(
	owner,
	name,
	description="m",
	enabled=1,
	schedule_enabled=0,
	schedule_frequency="daily",
	nsteps=1,
	merged_prompt="",
	merge_status="",
	last_run_days=None,
) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"description": description,
				"enabled": enabled,
				"stop_on_error": 1,
				"schedule_enabled": schedule_enabled,
				"schedule_frequency": schedule_frequency,
				"schedule_time": "09:00:00" if schedule_enabled else None,
				"steps": [{"label": f"s{k}", "prompt": f"prompt {k}"} for k in range(nsteps)],
				"merged_prompt": merged_prompt,
				"merge_status": merge_status,
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	if last_run_days is not None:
		frappe.db.set_value(
			MACRO,
			doc.name,
			"last_run_at",
			now_datetime() - timedelta(days=last_run_days),
			update_modified=False,
		)
	frappe.db.commit()
	return doc.name


def _mk_conv(owner, title, status="Active") -> str:
	with _as(owner):
		doc = frappe.get_doc({"doctype": CONV, "title": title, "status": status})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _add_msg(conv, seq, role, content, streaming=0, recovering=0, error="") -> None:
	frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": seq,
			"role": role,
			"content": content,
			"streaming": streaming,
			"recovering": recovering,
			"error": error,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _mk_approval(
	owner,
	title,
	status="Pending",
	document_type="",
	conversation=None,
	question="q?",
	ref_name=None,
	decision=None,
) -> str:
	with _as(owner):
		d = {
			"doctype": APPROVAL,
			"title": title,
			"status": status,
			"document_type": document_type,
			"conversation": conversation,
			"question": question,
			"context_md": "ctx",
			"options": '["Approve","Reject"]',
		}
		if ref_name is not None:
			d["ref_name"] = ref_name
		if decision is not None:
			d["decision"] = decision
		doc = frappe.get_doc(d)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _set_creation(dt, name, days_ago) -> None:
	frappe.db.set_value(
		dt, name, "creation", now_datetime() - timedelta(days=days_ago), update_modified=False
	)


def _names(rows, key="name"):
	return [r[key] for r in rows]


# =========================================================================== #
# Skills — list_custom_skills_page
# =========================================================================== #
class TestSkillsPage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		# USER_A: 24 own skills (i=0,1,2 drafts; even i invocable; i=5,6 "zebra";
		# i=10,11 shared with B).
		for i in range(24):
			_mk_skill(
				USER_A,
				f"fp-skill-a-{i:03d}",
				description="zebra alpha token" if i in (5, 6) else f"skill {i} generic",
				enabled=0 if i < 3 else 1,
				user_invocable=1 if i % 2 == 0 else 0,
				shared_with=[USER_B] if i in (10, 11) else None,
			)
		# USER_B: 3 private + 2 shared-with-A (enabled) + 1 shared-with-A (draft).
		for i in range(3):
			_mk_skill(USER_B, f"fp-skill-b-{i:03d}")
		_mk_skill(USER_B, "fp-skill-b-shared-000", enabled=1, user_invocable=1, shared_with=[USER_A])
		_mk_skill(USER_B, "fp-skill-b-shared-001", enabled=1, user_invocable=1, shared_with=[USER_A])
		_mk_skill(USER_B, "fp-skill-b-hidden-000", enabled=0, shared_with=[USER_A])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_custom_skills_page(**kw)

	def test_default_visibility_and_envelope(self):
		res = self._page()
		self.assertEqual(res["total"], 26)  # 24 own + 2 shared-enabled
		self.assertEqual(set(res.keys()), {"rows", "total", "has_more", "start", "page_length"})

	def test_pagination(self):
		seen, start, total = [], 0, None
		while True:
			res = self._page(start=start, page_length=10)
			total = res["total"]
			seen.extend(_names(res["rows"]))
			self.assertLessEqual(len(res["rows"]), 10)
			if not res["has_more"]:
				break
			start += 10
		self.assertEqual(total, 26)
		self.assertEqual(len(seen), 26)
		self.assertEqual(len(set(seen)), 26)  # no dup / overlap across pages

	def test_search(self):
		res = self._page(search="zebra")
		self.assertEqual(res["total"], 2)
		for r in res["rows"]:
			self.assertIn("zebra", r["description"])

	def test_filter_enabled(self):
		self.assertEqual(self._page(filters={"enabled": 1})["total"], 23)  # 21 own + 2 shared
		self.assertEqual(self._page(filters={"enabled": 0})["total"], 3)  # own drafts only

	def test_filter_user_invocable(self):
		res = self._page(filters={"user_invocable": 1})
		self.assertTrue(all(r["user_invocable"] == 1 for r in res["rows"]))
		self.assertEqual(res["total"], 14)  # 12 own even + 2 shared

	def test_filter_scope(self):
		mine = self._page(filters={"scope": "mine"})
		self.assertEqual(mine["total"], 24)
		self.assertTrue(all(r["mine"] == 1 for r in mine["rows"]))
		shared = self._page(filters={"scope": "shared"}, page_length=100)
		self.assertEqual(shared["total"], 2)
		self.assertTrue(all(r["mine"] == 0 for r in shared["rows"]))
		self.assertEqual(
			set(_names(shared["rows"], "skill_name")), {"fp-skill-b-shared-000", "fp-skill-b-shared-001"}
		)

	def test_sort(self):
		asc = self._page(sort_field="skill_name", sort_dir="asc", page_length=100)["rows"]
		desc = self._page(sort_field="skill_name", sort_dir="desc", page_length=100)["rows"]
		asc_names = _names(asc, "skill_name")
		self.assertEqual(asc_names, sorted(asc_names))
		self.assertEqual(asc_names[0], "fp-skill-a-000")
		self.assertEqual(_names(desc, "skill_name"), list(reversed(asc_names)))
		# default sort == skill_name asc
		default = self._page(page_length=100)["rows"]
		self.assertEqual(_names(default, "skill_name"), asc_names)

	def test_shared_count_and_shared_by(self):
		rows = {r["skill_name"]: r for r in self._page(page_length=100)["rows"]}
		self.assertEqual(rows["fp-skill-a-010"]["shared_count"], 1)
		self.assertEqual(rows["fp-skill-a-010"]["mine"], 1)
		self.assertEqual(rows["fp-skill-a-010"]["shared_by"], "")
		self.assertEqual(rows["fp-skill-a-000"]["shared_count"], 0)
		shared_row = rows["fp-skill-b-shared-000"]
		self.assertEqual(shared_row["mine"], 0)
		self.assertEqual(shared_row["shared_count"], 0)
		self.assertTrue(shared_row["shared_by"])  # owner's display name

	def test_owner_scoping(self):
		names = set(_names(self._page(page_length=100)["rows"], "skill_name"))
		# B's private + B's disabled-shared skill are never visible to A.
		self.assertNotIn("fp-skill-b-000", names)
		self.assertNotIn("fp-skill-b-hidden-000", names)
		# B sees its own private skills; not A's.
		b_names = set(_names(self._page(user=USER_B, page_length=100)["rows"], "skill_name"))
		self.assertIn("fp-skill-b-000", b_names)
		self.assertNotIn("fp-skill-a-000", b_names)

	def test_unknown_filter_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"bogus": 1})

	def test_bad_bool_value_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"enabled": 5})


# =========================================================================== #
# Macros — list_macros_page
# =========================================================================== #
class TestMacrosPage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		# USER_A: 24 macros. i<3 drafts; i<9 scheduled (freq cycles); i in {5,6}
		# search token; i%5==0 summarized; step_count = 1 + (i%4); a couple runs.
		for i in range(24):
			sched = i < 9
			_mk_macro(
				USER_A,
				f"fp-macro-a-{i:03d}",
				description="walrus token macro" if i in (5, 6) else f"macro {i} generic",
				enabled=0 if i < 3 else 1,
				schedule_enabled=1 if sched else 0,
				schedule_frequency=["daily", "weekly", "monthly"][i % 3] if sched else "daily",
				nsteps=1 + (i % 4),
				merged_prompt="merged body" if i % 5 == 0 else "",
				merge_status="ready" if i % 5 == 0 else "",
				last_run_days=(i if i in (3, 6) else None),
			)
		for i in range(3):
			_mk_macro(USER_B, f"fp-macro-b-{i:03d}")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_macros_page(**kw)

	def test_pagination_and_total(self):
		seen, start = [], 0
		while True:
			res = self._page(start=start, page_length=10)
			seen.extend(_names(res["rows"]))
			self.assertEqual(res["total"], 24)
			if not res["has_more"]:
				break
			start += 10
		self.assertEqual(len(set(seen)), 24)

	def test_search(self):
		self.assertEqual(self._page(search="walrus")["total"], 2)

	def test_filter_enabled(self):
		self.assertEqual(self._page(filters={"enabled": 1})["total"], 21)
		self.assertEqual(self._page(filters={"enabled": 0})["total"], 3)

	def test_filter_schedule_enabled(self):
		self.assertEqual(self._page(filters={"schedule_enabled": 1})["total"], 9)
		self.assertEqual(self._page(filters={"schedule_enabled": 0})["total"], 15)

	def test_filter_schedule_frequency(self):
		# scheduled i in 0..8; weekly at i=1,4,7.
		res = self._page(filters={"schedule_frequency": "weekly"})
		self.assertEqual(res["total"], 3)
		for r in res["rows"]:
			self.assertEqual(r["schedule_frequency"], "weekly")

	def test_bad_frequency_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"schedule_frequency": "hourly"})

	def test_has_summary_and_step_count(self):
		rows = {r["macro_name"]: r for r in self._page(page_length=100)["rows"]}
		self.assertEqual(rows["fp-macro-a-000"]["has_summary"], 1)  # i%5==0
		self.assertEqual(rows["fp-macro-a-001"]["has_summary"], 0)
		self.assertEqual(rows["fp-macro-a-007"]["step_count"], 1 + (7 % 4))  # =4

	def test_sort_name_and_default(self):
		asc = _names(
			self._page(sort_field="macro_name", sort_dir="asc", page_length=100)["rows"], "macro_name"
		)
		desc = _names(
			self._page(sort_field="macro_name", sort_dir="desc", page_length=100)["rows"], "macro_name"
		)
		self.assertEqual(asc, sorted(asc))
		self.assertEqual(desc, list(reversed(asc)))
		self.assertEqual(_names(self._page(page_length=100)["rows"], "macro_name"), asc)  # default asc

	def test_sort_last_run_at(self):
		rows = self._page(sort_field="last_run_at", sort_dir="desc", page_length=100)["rows"]
		vals = [r["last_run_at"] for r in rows if r["last_run_at"]]
		self.assertEqual(vals, sorted(vals, reverse=True))

	def test_owner_scoping(self):
		names = set(_names(self._page(page_length=100)["rows"], "macro_name"))
		self.assertNotIn("fp-macro-b-000", names)
		b_names = set(_names(self._page(user=USER_B, page_length=100)["rows"], "macro_name"))
		self.assertIn("fp-macro-b-000", b_names)
		self.assertNotIn("fp-macro-a-000", b_names)

	def test_unknown_filter_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"nope": 1})


# =========================================================================== #
# File Box — list_inbound_page + FB-1
# =========================================================================== #
class TestFileBoxPage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.ids = {}
		# 3 done, 1 error, 1 processing (no assistant msg), 1 processing (streaming),
		# 1 needs_approval. Plus a non-File-Box conv + an Archived File-Box conv.
		self.ids["done0"] = c = _mk_conv(USER_A, "File: fp-a-uniquetoken-0001.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "drafted")
		_set_creation(CONV, c, 100)
		self.ids["done1"] = c = _mk_conv(USER_A, "File: fp-a-0002.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "drafted")
		_set_creation(CONV, c, 50)
		self.ids["done2"] = c = _mk_conv(USER_A, "File: fp-a-0003.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "drafted")
		_set_creation(CONV, c, 1)
		self.ids["error0"] = c = _mk_conv(USER_A, "File: fp-a-0004.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "", error="boom")
		_set_creation(CONV, c, 1)
		self.ids["proc0"] = c = _mk_conv(USER_A, "File: fp-a-0005.pdf")  # no assistant msg
		_add_msg(c, 1, "user", "process")
		_set_creation(CONV, c, 1)
		self.ids["stream0"] = c = _mk_conv(USER_A, "File: fp-a-0006.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "...", streaming=1)
		_set_creation(CONV, c, 1)
		self.ids["na0"] = c = _mk_conv(USER_A, "File: fp-a-0007.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "queued")
		_mk_approval(USER_A, "fp-appr-na-0", "Pending", "Purchase Invoice", c, "confirm?")
		_set_creation(CONV, c, 1)
		# excluded rows
		self.ids["plain"] = _mk_conv(USER_A, "fp-a-plainconv")  # not a File: row
		self.ids["arch"] = _mk_conv(USER_A, "File: fp-a-archived.pdf", status="Archived")
		# second user's File Box row (scoping)
		self.ids["b0"] = c = _mk_conv(USER_B, "File: fp-b-0001.pdf")
		_add_msg(c, 1, "user", "process")
		_add_msg(c, 2, "assistant", "drafted")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_inbound_page(**kw)

	def test_derived_status(self):
		rows = {r["name"]: r for r in self._page(page_length=100)["rows"]}
		self.assertEqual(len(rows), 7)  # plain + archived excluded
		self.assertEqual(rows[self.ids["done2"]]["status"], "done")
		self.assertEqual(rows[self.ids["error0"]]["status"], "error")
		self.assertEqual(rows[self.ids["proc0"]]["status"], "processing")
		self.assertEqual(rows[self.ids["stream0"]]["status"], "processing")
		self.assertEqual(rows[self.ids["na0"]]["status"], "needs_approval")
		self.assertEqual(rows[self.ids["na0"]]["pending_approvals"], 1)

	def test_status_filter(self):
		self.assertEqual(self._page(filters={"status": "done"})["total"], 3)
		self.assertEqual(self._page(filters={"status": "processing"})["total"], 2)
		self.assertEqual(self._page(filters={"status": "error"})["total"], 1)
		self.assertEqual(self._page(filters={"status": "needs_approval"})["total"], 1)

	def test_search_title(self):
		res = self._page(search="uniquetoken")
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.ids["done0"])

	def test_date_filters(self):
		cutoff_from = today() and (now_datetime() - timedelta(days=60)).date().isoformat()
		res = self._page(filters={"from_date": cutoff_from})
		names = set(_names(res["rows"]))
		self.assertNotIn(self.ids["done0"], names)  # 100 days ago, excluded
		self.assertIn(self.ids["done1"], names)  # 50 days ago, included
		self.assertEqual(res["total"], 6)
		cutoff_to = (now_datetime() - timedelta(days=70)).date().isoformat()
		res2 = self._page(filters={"to_date": cutoff_to})
		self.assertEqual(res2["total"], 1)  # only the 100-day-old row
		self.assertEqual(res2["rows"][0]["name"], self.ids["done0"])

	def test_sort_creation_default_desc(self):
		rows = self._page(page_length=100)["rows"]
		self.assertEqual(rows[-1]["name"], self.ids["done0"])  # oldest last (default desc)
		asc = self._page(sort_field="creation", sort_dir="asc", page_length=100)["rows"]
		self.assertEqual(asc[0]["name"], self.ids["done0"])  # oldest first

	def test_sort_title(self):
		titles = _names(self._page(sort_field="title", sort_dir="asc", page_length=100)["rows"], "title")
		self.assertEqual(titles, sorted(titles))

	def test_owner_scoping_and_exclusions(self):
		names = set(_names(self._page(page_length=100)["rows"]))
		self.assertNotIn(self.ids["b0"], names)  # other user's row
		self.assertNotIn(self.ids["plain"], names)  # not a File: row
		self.assertNotIn(self.ids["arch"], names)  # archived
		b_names = set(_names(self._page(user=USER_B, page_length=100)["rows"]))
		self.assertIn(self.ids["b0"], b_names)
		self.assertNotIn(self.ids["done0"], b_names)

	def test_unknown_filter_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"bogus": 1})

	# --- FB-1 --------------------------------------------------------------- #
	def test_delete_cascade(self):
		conv = _mk_conv(USER_A, "File: fp-a-del.pdf")
		_add_msg(conv, 1, "user", "process")
		_add_msg(conv, 2, "assistant", "drafted")
		ap = _mk_approval(USER_A, "fp-appr-del", "Pending", "Purchase Invoice", conv, "confirm?")
		with _as(USER_A):
			f = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "fp-attach.txt",
					"attached_to_doctype": CONV,
					"attached_to_name": conv,
					"is_private": 1,
					"content": "hello world",
				}
			)
			f.flags.ignore_permissions = True
			f.insert(ignore_permissions=True)
		frappe.db.commit()
		file_name = f.name

		with _as(USER_A):
			res = delete_inbound(conv)
		self.assertTrue(res["ok"])
		self.assertFalse(frappe.db.exists(CONV, conv))
		self.assertEqual(frappe.db.count(MSG, {"conversation": conv}), 0)
		self.assertFalse(frappe.db.exists(APPROVAL, ap))
		self.assertFalse(frappe.db.exists("File", file_name))

	def test_delete_refuses_while_streaming(self):
		conv = self.ids["stream0"]
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			delete_inbound(conv)
		self.assertTrue(frappe.db.exists(CONV, conv))  # untouched

	def test_delete_owner_gate(self):
		conv = self.ids["done0"]
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			delete_inbound(conv)
		self.assertTrue(frappe.db.exists(CONV, conv))

	def test_delete_rejects_non_filebox(self):
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			delete_inbound(self.ids["plain"])
		self.assertTrue(frappe.db.exists(CONV, self.ids["plain"]))

	def test_bulk_skips_streaming_and_foreign(self):
		done = self.ids["done1"]
		stream = self.ids["stream0"]
		foreign = self.ids["b0"]
		with _as(USER_A):
			res = delete_inbound_bulk([done, stream, foreign])
		self.assertEqual(res["deleted"], 1)
		skipped = {s["conversation"]: s["reason"] for s in res["skipped"]}
		self.assertIn(stream, skipped)
		self.assertIn(foreign, skipped)
		self.assertEqual(skipped[foreign], "not permitted")
		self.assertFalse(frappe.db.exists(CONV, done))  # the done row went
		self.assertTrue(frappe.db.exists(CONV, stream))  # streaming row survived
		self.assertTrue(frappe.db.exists(CONV, foreign))  # foreign row survived

	def test_clear_processed_leaves_active(self):
		with _as(USER_A):
			res = clear_processed_inbound()
		self.assertTrue(res["ok"])
		self.assertEqual(res["deleted"], 4)  # 3 done + 1 error
		remaining = self._page(page_length=100)["rows"]
		self.assertEqual(len(remaining), 3)  # 2 processing + 1 needs_approval
		self.assertTrue(all(r["status"] in ("processing", "needs_approval") for r in remaining))


# =========================================================================== #
# Approvals — list_approvals_page (+ facets, SM vs non-SM scoping)
# =========================================================================== #
class TestApprovalsPage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv_a = _mk_conv(USER_A, "fp-a-appr-conv")
		self.conv_b = _mk_conv(USER_B, "fp-b-appr-conv")
		# conv_a pending: PIx2, SIx2, blankx1
		_mk_approval(
			USER_A,
			"fp-appr-a-p0",
			"Pending",
			"Purchase Invoice",
			self.conv_a,
			question="check kangaroo value",
		)
		_mk_approval(USER_A, "fp-appr-a-p1", "Pending", "Purchase Invoice", self.conv_a)
		_mk_approval(USER_A, "fp-appr-a-p2", "Pending", "Sales Invoice", self.conv_a, ref_name="REF-XYZ")
		_mk_approval(USER_A, "fp-appr-a-p3", "Pending", "Sales Invoice", self.conv_a)
		_mk_approval(USER_A, "fp-appr-a-p4", "Pending", "", self.conv_a)  # Unclassified
		# conv_a decided: 2 Approved + 1 Rejected (all PI)
		_mk_approval(USER_A, "fp-appr-a-d0", "Approved", "Purchase Invoice", self.conv_a, decision="ok")
		_mk_approval(USER_A, "fp-appr-a-d1", "Approved", "Purchase Invoice", self.conv_a, decision="ok")
		_mk_approval(USER_A, "fp-appr-a-d2", "Rejected", "Purchase Invoice", self.conv_a, decision="no")
		# conv_b pending (owned by B): invisible to A
		self.b_p0 = _mk_approval(USER_B, "fp-appr-b-p0", "Pending", "Purchase Invoice", self.conv_b)
		_mk_approval(USER_B, "fp-appr-b-p1", "Pending", "Sales Invoice", self.conv_b)
		# NULL-conversation pending: invisible to non-SM, visible to SM
		self.null_ap = _mk_approval(USER_A, "fp-appr-null", "Pending", "Purchase Invoice", None)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw)

	def test_default_pending_scoped(self):
		res = self._page()  # default status Pending
		self.assertEqual(res["total"], 5)  # only conv_a pending
		names = set(_names(res["rows"]))
		self.assertNotIn(self.b_p0, names)
		self.assertNotIn(self.null_ap, names)

	def test_status_filters(self):
		self.assertEqual(self._page(filters={"status": "Decided"})["total"], 3)
		self.assertEqual(self._page(filters={"status": "All"})["total"], 8)
		self.assertEqual(self._page(filters={"status": "Approved"})["total"], 2)
		self.assertEqual(self._page(filters={"status": "Rejected"})["total"], 1)

	def test_document_type_filter(self):
		self.assertEqual(self._page(filters={"document_type": "Purchase Invoice"})["total"], 2)
		un = self._page(filters={"document_type": "Unclassified"})
		self.assertEqual(un["total"], 1)
		self.assertEqual(un["rows"][0]["title"], "fp-appr-a-p4")

	def test_conversation_filter(self):
		self.assertEqual(self._page(filters={"conversation": self.conv_a})["total"], 5)
		# A can never see conv_b rows even by filtering on it.
		self.assertEqual(self._page(filters={"conversation": self.conv_b})["total"], 0)

	def test_search(self):
		self.assertEqual(self._page(search="kangaroo")["total"], 1)  # question
		self.assertEqual(self._page(search="REF-XYZ")["total"], 1)  # ref_name
		self.assertEqual(self._page(search="fp-appr-a-p0")["total"], 1)  # title

	def test_facets(self):
		res = self._page()  # Pending
		facets = {f["value"]: f["count"] for f in res["facets"]["document_type"]}
		self.assertEqual(facets, {"Purchase Invoice": 2, "Sales Invoice": 2, "Unclassified": 1})
		# facets are computed under the WHERE MINUS the document_type filter:
		# applying a document_type filter must NOT change them.
		res2 = self._page(filters={"document_type": "Purchase Invoice"})
		self.assertEqual(res2["total"], 2)
		facets2 = {f["value"]: f["count"] for f in res2["facets"]["document_type"]}
		self.assertEqual(facets2, facets)
		# sorted count desc
		counts = [f["count"] for f in res["facets"]["document_type"]]
		self.assertEqual(counts, sorted(counts, reverse=True))

	def test_sort(self):
		desc = self._page(filters={"status": "All"}, sort_field="creation", sort_dir="desc", page_length=100)[
			"rows"
		]
		asc = self._page(filters={"status": "All"}, sort_field="creation", sort_dir="asc", page_length=100)[
			"rows"
		]
		self.assertEqual(_names(desc), list(reversed(_names(asc))))
		# document_type sort is whitelisted (no throw, returns rows)
		self.assertTrue(
			self._page(filters={"status": "All"}, sort_field="document_type", sort_dir="asc")["rows"]
		)

	def test_pagination(self):
		seen, start = [], 0
		while True:
			res = self._page(filters={"status": "All"}, start=start, page_length=3)
			seen.extend(_names(res["rows"]))
			self.assertEqual(res["total"], 8)
			if not res["has_more"]:
				break
			start += 3
		self.assertEqual(len(set(seen)), 8)

	def test_owner_scoping_non_sm(self):
		names = set(_names(self._page(filters={"status": "All"}, page_length=100)["rows"]))
		self.assertNotIn(self.b_p0, names)
		self.assertNotIn(self.null_ap, names)

	def test_system_manager_sees_all(self):
		# Administrator is a System Manager: sees B's rows + the NULL-conversation
		# row. Exact totals are unsafe (real approvals may exist), so assert
		# presence + that non-SM cannot see them.
		with _as("Administrator"):
			res = list_approvals_page(filters={"status": "Pending"}, page_length=500)
		names = set(_names(res["rows"]))
		self.assertIn(self.b_p0, names)
		self.assertIn(self.null_ap, names)
		self.assertGreaterEqual(res["total"], 8)  # >= 5(a)+2(b)+1(null)

	def test_unknown_filter_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"bogus": 1})

	def test_bad_status_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._page(filters={"status": "Nonsense"})

	def test_pending_count_scoped(self):
		# Non-SM: a scoped COUNT over rows whose conversation the caller owns
		# (NULL-conversation rows excluded — same JOIN semantics as the list).
		with _as(USER_A):
			self.assertEqual(pending_count(), 5)
		with _as(USER_B):
			self.assertEqual(pending_count(), 2)
		# SM sees everything pending (>= our fixtures; real rows may exist).
		with _as("Administrator"):
			self.assertGreaterEqual(pending_count(), 8)


if __name__ == "__main__":
	unittest.main()
