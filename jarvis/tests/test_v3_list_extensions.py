"""Tests for the v3 additive list/detail extensions (DESIGN-V3 §8.3, D38/D39,
§14 F5).

Covers: ``delete_custom_skills_bulk`` (skips shared/not-owned rows with
reasons + exactly one deduped apply enqueue, none when nothing deleted) ·
``delete_macros_bulk`` (removes run history, skips foreign rows) ·
``list_macro_runs.total`` under owner/status/macro filters · D38 row-field
extensions (``enabled``/``user_invocable`` on skills rows, ``merge_status`` on
macros rows) · ``get_approval`` gate + ``can_act`` (conversation-owner / SM /
DocShare-read assignee / stranger) · ``get_agent`` (installation only for the
owner, ``all_roles`` only for System Managers, ``install_count``) ·
``list_agents`` rows gaining ``install_count``.
"""

from __future__ import annotations

import contextlib
import unittest

import frappe

from jarvis.chat import agent_catalog, agents_api, custom_skills_api, macros_api
from jarvis.chat.approvals_api import get_approval
from jarvis.chat.custom_skills_api import delete_custom_skills_bulk, list_custom_skills_page
from jarvis.chat.macros_api import delete_macros_bulk, list_macro_runs, list_macros_page

USER_A = "v3-user-a@example.com"
USER_B = "v3-user-b@example.com"

SKILL = "Jarvis Custom Skill"
MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
CONV = "Jarvis Conversation"
APPROVAL = "Jarvis Approval Request"
LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"


def _ensure_user(email: str) -> str:
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
		frappe.db.set_value("User", email, "user_type", "System User")
		frappe.db.commit()
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	# The Jarvis User role is what the @require_jarvis_user endpoint decorators
	# require (security review PART 1 TASK 8); a realistic non-SM Jarvis caller
	# holds it.
	if "Jarvis User" not in set(frappe.get_roles(email)):
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
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", "v3-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(MACRO, filters={"macro_name": ["like", "v3-%"]}, pluck="name"):
		for run in frappe.get_all(RUN, filters={"macro": name}, pluck="name"):
			frappe.delete_doc(RUN, run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(APPROVAL, filters={"title": ["like", "v3-%"]}, pluck="name"):
		frappe.db.delete("DocShare", {"share_doctype": APPROVAL, "share_name": name})
		frappe.delete_doc(APPROVAL, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(CONV, filters={"title": ["like", "v3-%"]}, pluck="name"):
		frappe.db.delete("Jarvis Chat Message", {"conversation": name})
		frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_skill(owner: str, name: str, shared_with=None) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": "d",
				"instructions": "do it",
				"enabled": 1,
				"user_invocable": 1,
				"shared_with": [{"user": u} for u in (shared_with or [])],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_macro(owner: str, name: str) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"description": "m",
				"enabled": 1,
				"stop_on_error": 1,
				"steps": [{"label": "s1", "prompt": "prompt 1"}],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_run(owner: str, macro: str, status: str = "completed") -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro,
				"status": status,
				"trigger": "manual",
			}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


# =========================================================================== #
# Skills — delete_custom_skills_bulk + D38 row fields
# =========================================================================== #
class TestSkillsBulkDelete(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		# USER_A owns + deletes the fixture skills; the bulk-delete reconcile is
		# now reviewer-gated (security review PART 2 TASK 12), so give USER_A the
		# reviewer role to exercise the (deduped) apply path.
		if "Jarvis Skill Reviewer" not in set(frappe.get_roles(USER_A)):
			frappe.get_doc("User", USER_A).add_roles("Jarvis Skill Reviewer")

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		self.a0 = _mk_skill(USER_A, "v3-skill-a-0")
		self.a1 = _mk_skill(USER_A, "v3-skill-a-1")
		self.b_shared = _mk_skill(USER_B, "v3-skill-b-shared", shared_with=[USER_A])
		self.b_priv = _mk_skill(USER_B, "v3-skill-b-priv")
		# Stub the apply pipeline (it talks to the admin service) and count calls.
		# delete_custom_skills_bulk calls the undecorated _apply_custom_skills_impl.
		self.apply_calls = []
		self._orig_apply = custom_skills_api._apply_custom_skills_impl
		custom_skills_api._apply_custom_skills_impl = lambda: self.apply_calls.append(1)

	def tearDown(self):
		custom_skills_api._apply_custom_skills_impl = self._orig_apply
		frappe.set_user("Administrator")
		_wipe()

	def test_bulk_delete_skips_with_reasons_and_enqueues_apply(self):
		with _as(USER_A):
			res = delete_custom_skills_bulk(
				[self.a0, self.a1, self.b_shared, self.b_priv, "v3-no-such-skill"]
			)
		self.assertEqual(res["deleted"], 2)
		skipped = {s["name"]: s["reason"] for s in res["skipped"]}
		self.assertEqual(skipped[self.b_shared], "not owner")  # shared-with-me row
		self.assertEqual(skipped[self.b_priv], "not owner")
		self.assertEqual(skipped["v3-no-such-skill"], "not found")
		self.assertFalse(frappe.db.exists(SKILL, self.a0))
		self.assertFalse(frappe.db.exists(SKILL, self.a1))
		self.assertTrue(frappe.db.exists(SKILL, self.b_shared))  # B's rows survive
		self.assertTrue(frappe.db.exists(SKILL, self.b_priv))
		self.assertEqual(len(self.apply_calls), 1)  # ONE apply at the end

	def test_bulk_delete_no_apply_when_nothing_deleted(self):
		with _as(USER_A):
			res = delete_custom_skills_bulk([self.b_priv])
		self.assertEqual(res["deleted"], 0)
		self.assertEqual(self.apply_calls, [])

	def test_bulk_delete_accepts_json_string(self):
		import json

		with _as(USER_A):
			res = delete_custom_skills_bulk(json.dumps([self.a0]))
		self.assertEqual(res["deleted"], 1)
		self.assertEqual(len(self.apply_calls), 1)

	def test_skills_page_rows_contain_enabled_and_user_invocable(self):
		with _as(USER_A):
			rows = list_custom_skills_page(page_length=100)["rows"]
		self.assertTrue(rows)
		for r in rows:
			self.assertIn("enabled", r)
			self.assertIn("user_invocable", r)


# =========================================================================== #
# Macros — delete_macros_bulk + list_macro_runs.total + D38 merge_status
# =========================================================================== #
class TestMacrosBulkAndRunsTotal(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		self.a0 = _mk_macro(USER_A, "v3-macro-a-0")
		self.a1 = _mk_macro(USER_A, "v3-macro-a-1")
		self.b0 = _mk_macro(USER_B, "v3-macro-b-0")
		# runs: a0 has 2; a1 has 3 completed + 2 failed; b0 has 1.
		self.a0_runs = [_mk_run(USER_A, self.a0) for _ in range(2)]
		for _ in range(3):
			_mk_run(USER_A, self.a1, "completed")
		for _ in range(2):
			_mk_run(USER_A, self.a1, "failed")
		_mk_run(USER_B, self.b0)

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	def test_bulk_delete_removes_run_history_and_skips_foreign(self):
		with _as(USER_A):
			res = delete_macros_bulk([self.a0, self.b0, "v3-no-such-macro"])
		self.assertEqual(res["deleted"], 1)
		skipped = {s["name"]: s["reason"] for s in res["skipped"]}
		self.assertEqual(skipped[self.b0], "not owner")
		self.assertEqual(skipped["v3-no-such-macro"], "not found")
		self.assertFalse(frappe.db.exists(MACRO, self.a0))
		self.assertEqual(frappe.db.count(RUN, {"macro": self.a0}), 0)  # history gone
		self.assertTrue(frappe.db.exists(MACRO, self.b0))  # B untouched
		self.assertEqual(frappe.db.count(RUN, {"macro": self.b0}), 1)

	def test_list_macro_runs_total_owner_scoped(self):
		with _as(USER_A):
			res = list_macro_runs()
		self.assertEqual(res["total"], 7)  # 2 + 5, never B's run
		with _as(USER_B):
			self.assertEqual(list_macro_runs()["total"], 1)

	def test_list_macro_runs_total_under_filters(self):
		with _as(USER_A):
			self.assertEqual(list_macro_runs(status="completed")["total"], 5)  # 2 + 3
			self.assertEqual(list_macro_runs(status="failed")["total"], 2)
			self.assertEqual(list_macro_runs(macro=self.a1)["total"], 5)
			self.assertEqual(list_macro_runs(status="failed", macro=self.a1)["total"], 2)

	def test_list_macro_runs_total_independent_of_limit(self):
		with _as(USER_A):
			res = list_macro_runs(limit=2)
		self.assertEqual(res["total"], 7)
		self.assertEqual(len(res["runs"]), 2)
		self.assertTrue(res["has_more"])

	def test_macros_page_rows_contain_merge_status(self):
		with _as(USER_A):
			rows = list_macros_page(page_length=100)["rows"]
		self.assertTrue(rows)
		for r in rows:
			self.assertIn("merge_status", r)


# =========================================================================== #
# Approvals — get_approval gate + can_act
# =========================================================================== #
class TestGetApproval(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		with _as(USER_A):
			conv = frappe.get_doc({"doctype": CONV, "title": "v3-appr-conv", "status": "Active"})
			conv.insert(ignore_permissions=True)
		self.conv = conv.name
		# Pending approval owned by Administrator, conversation owned by A.
		appr = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "v3-appr-pending",
				"status": "Pending",
				"document_type": "Purchase Invoice",
				"conversation": self.conv,
				"question": "post it?",
				"context_md": "ctx",
				"options": '["Post","Hold"]',
				"ref_doctype": "Purchase Invoice",
				"ref_name": "PI-0001",
			}
		)
		appr.insert(ignore_permissions=True)
		self.pending = appr.name
		decided = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": "v3-appr-decided",
				"status": "Approved",
				"document_type": "Sales Invoice",
				"conversation": self.conv,
				"question": "send it?",
				"options": '["Yes"]',
				"decision": "Yes",
				"decided_by": "Administrator",
				"decided_at": frappe.utils.now_datetime(),
			}
		)
		decided.insert(ignore_permissions=True)
		frappe.db.commit()
		self.decided = decided.name

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	def test_conversation_owner_reads_and_can_act(self):
		with _as(USER_A):
			res = get_approval(self.pending)
		self.assertEqual(res["name"], self.pending)
		self.assertEqual(res["can_act"], 1)
		self.assertEqual(res["status"], "Pending")
		self.assertEqual(res["options"], ["Post", "Hold"])  # parsed, not raw JSON
		self.assertEqual(res["ref_name"], "PI-0001")
		self.assertEqual(res["conversation"], self.conv)

	def test_stranger_denied(self):
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			get_approval(self.pending)

	def test_system_manager_can_act(self):
		res = get_approval(self.pending)  # Administrator
		self.assertEqual(res["can_act"], 1)

	def test_assignee_docshare_reads_but_cannot_act(self):
		# a DocShare read row (what toggle_assignment creates) grants READ only
		frappe.get_doc(
			{
				"doctype": "DocShare",
				"share_doctype": APPROVAL,
				"share_name": self.pending,
				"user": USER_B,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		with _as(USER_B):
			res = get_approval(self.pending)
		self.assertEqual(res["can_act"], 0)

	def test_decided_fields(self):
		with _as(USER_A):
			res = get_approval(self.decided)
		self.assertEqual(res["status"], "Approved")
		self.assertEqual(res["decision"], "Yes")
		self.assertEqual(res["decided_by"], "Administrator")
		self.assertTrue(res["decided_by_name"])
		self.assertTrue(res["decided_at"])


# =========================================================================== #
# Agents — get_agent + list_agents install_count (§14 F5)
# =========================================================================== #
class TestGetAgent(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		agent_catalog.sync_agent_listings()
		cls.slug = "close-auditor"
		if not frappe.db.exists(LISTING, cls.slug):
			cls.slug = frappe.get_all(LISTING, filters={"status": "Published"}, pluck="name", limit=1)[0]
		# close-auditor requires GL Entry / Account / Company read (install A12-gate);
		# grant it to the installers so the self-mapped installs validate.
		if frappe.db.exists("Role", "Accounts User"):
			for u in (USER_A, USER_B):
				frappe.get_doc("User", u).add_roles("Accounts User")
				frappe.clear_cache(user=u)

	def setUp(self):
		frappe.set_user("Administrator")
		for owner in (USER_A, USER_B):
			for n in frappe.get_all(INSTALLATION, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
		frappe.db.commit()
		with _as(USER_A):
			inst = frappe.get_doc(
				{
					"doctype": INSTALLATION,
					"agent": self.slug,
					"enabled": 0,
					"run_as_user": frappe.session.user,  # self-map (reqd since Phase 1 identity)
					"installed_version": frappe.db.get_value(LISTING, self.slug, "version"),
					"installed_at": frappe.utils.now(),
				}
			)
			inst.insert(ignore_permissions=True)
		frappe.db.commit()
		self.installation = inst.name

	def tearDown(self):
		frappe.set_user("Administrator")
		for owner in (USER_A, USER_B):
			for n in frappe.get_all(INSTALLATION, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_owner_sees_own_installation(self):
		with _as(USER_A):
			res = agents_api.get_agent(self.slug)
		self.assertEqual(res["agent_slug"], self.slug)
		self.assertIsNotNone(res["installation"])
		self.assertEqual(res["installation"]["name"], self.installation)
		self.assertNotIn("all_roles", res)  # SM-only payload
		self.assertEqual(res["install_count"], frappe.db.count(INSTALLATION, {"agent": self.slug}))
		self.assertGreaterEqual(res["install_count"], 1)

	def test_non_installer_gets_no_installation(self):
		with _as(USER_B):
			res = agents_api.get_agent(self.slug)
		self.assertIsNone(res["installation"])
		self.assertNotIn("all_roles", res)
		self.assertIn("allowed", res)
		self.assertIn("allowed_roles", res)

	def test_sm_gets_all_roles(self):
		res = agents_api.get_agent(self.slug)  # Administrator
		self.assertIn("all_roles", res)
		self.assertTrue(isinstance(res["all_roles"], list) and res["all_roles"])
		for banned in ("Administrator", "Guest", "All"):
			self.assertNotIn(banned, res["all_roles"])

	def test_unknown_agent_throws(self):
		with self.assertRaises(frappe.DoesNotExistError):
			agents_api.get_agent("v3-no-such-agent")

	def test_list_agents_rows_gain_install_count(self):
		with _as(USER_A):
			rows = agents_api.list_agents()
		self.assertTrue(rows)
		by_slug = {}
		for r in rows:
			self.assertIn("install_count", r)
			by_slug[r["agent_slug"]] = r
		self.assertEqual(
			by_slug[self.slug]["install_count"],
			frappe.db.count(INSTALLATION, {"agent": self.slug}),
		)
		self.assertGreaterEqual(by_slug[self.slug]["install_count"], 1)


if __name__ == "__main__":
	unittest.main()
