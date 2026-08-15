"""Scope model + visibility/write-matrix tests for Jarvis Wiki Page (wiki v2):
the ``jarvis.chat.wiki_permissions`` matrix helpers and SQL fragments, the
controller's scope normalization / slug audience-suffixing / SM-only
scope-change guard, and the ``jarvis.learning.roles`` seeder.

Matrix under test (design D1; wiki v2 role consolidation):
  * System Manager: full read/write on every scope.
  * Plain user (no Jarvis role): reads Org only; creates nothing.
  * Jarvis User: + own User-scope pages (Knowledge Wiki User retired).
  * Knowledge Wiki Manager: + Role-scope pages for roles they hold.
  * Jarvis Admin: inherits Knowledge Wiki Manager (curates roles they hold).
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import wiki_permissions
from jarvis.learning import roles as learning_roles
from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE

WIKI = "Jarvis Wiki Page"

USER_PLAIN = "wiki-perm-plain@example.com"
# USER_KW now stands for a plain Jarvis User (Knowledge Wiki User was retired);
# the email is kept so the audience-suffixed slug assertions stay stable.
USER_KW = "wiki-perm-kwuser@example.com"
USER_KW_MGR = "wiki-perm-kwmgr@example.com"
USER_ADMIN = "wiki-perm-admin@example.com"
USER_SM = "wiki-perm-sm@example.com"

TEST_ROLE = "Wiki Perm Test Role"
OTHER_ROLE = "Wiki Perm Other Role"

SLUG_MARK = "wpermtest"


def _ensure_user(email: str, roles: tuple = ()) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				# Explicit: a role-less insert becomes a Website User, which
				# never reaches Desk data (and gets no Desk User role).
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if user.user_type != "System User":
		# Repair a stale fixture user left behind by an earlier run.
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
		user = frappe.get_doc("User", email)
	if roles:
		user.add_roles(*roles)
	elif "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")
	frappe.db.commit()
	return email


def _ensure_role(role_name: str) -> str:
	if not frappe.db.exists("Role", role_name):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	return role_name


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _delete_test_pages():
	frappe.db.delete(WIKI, {"slug": ["like", f"%{SLUG_MARK}%"]})


def _make_page(slug, page_type, scope="Org", target_role=None, target_user=None, **kwargs):
	doc = frappe.get_doc(
		{
			"doctype": WIKI,
			"slug": slug,
			"title": kwargs.pop("title", slug),
			"page_type": page_type,
			"scope": scope,
			"target_role": target_role,
			"target_user": target_user,
			**kwargs,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


class WikiPermTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Roles must exist before add_roles below; this also exercises the
		# after_migrate seeder on a site where it may already have run.
		learning_roles.after_migrate()
		_ensure_role(TEST_ROLE)
		_ensure_role(OTHER_ROLE)
		_ensure_user(USER_PLAIN)
		# Plain Jarvis User: own-page editing rides this platform role now.
		_ensure_user(USER_KW, roles=(JARVIS_USER_ROLE,))
		_ensure_user(USER_KW_MGR, roles=(wiki_permissions.WIKI_MANAGER_ROLE, TEST_ROLE))
		# Jarvis Admin holding TEST_ROLE: inherits Manager, scoped to held roles.
		_ensure_user(USER_ADMIN, roles=(JARVIS_ADMIN_ROLE, TEST_ROLE))
		_ensure_user(USER_SM, roles=("System Manager",))
		# USER_PLAIN stands for a user WITHOUT Jarvis app access. Fixtures persist
		# across runs and an earlier case may have left "Jarvis User" on it, so
		# strip every Jarvis/curator role to exercise the no-access case (reads
		# Org, writes nothing).
		frappe.db.delete(
			"Has Role",
			{
				"parenttype": "User",
				"parent": USER_PLAIN,
				"role": ["in", (JARVIS_USER_ROLE, JARVIS_ADMIN_ROLE, wiki_permissions.WIKI_MANAGER_ROLE)],
			},
		)
		frappe.clear_cache(user=USER_PLAIN)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		_delete_test_pages()
		self.org_page = _make_page(f"org--{SLUG_MARK}-org", "Org")
		self.role_page = _make_page(
			f"process--{SLUG_MARK}-role", "Process", scope="Role", target_role=TEST_ROLE
		)
		self.other_role_page = _make_page(
			f"process--{SLUG_MARK}-other", "Process", scope="Role", target_role=OTHER_ROLE
		)
		self.my_page = _make_page(f"people--{SLUG_MARK}-mine", "People", scope="User", target_user=USER_KW)
		self.their_page = _make_page(
			f"people--{SLUG_MARK}-theirs", "People", scope="User", target_user=USER_KW_MGR
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		_delete_test_pages()
		frappe.db.commit()
		super().tearDown()


# --------------------------------------------------------------------------- #
# roles seeder + scope model (controller)
# --------------------------------------------------------------------------- #
class TestScopeModel(WikiPermTestCase):
	def test_wiki_roles_seeded_idempotently(self):
		learning_roles.after_migrate()
		learning_roles.after_migrate()
		# after_migrate seeds the wiki curator role AND the app-access /
		# tenant-admin roles (jarvis.permissions), all idempotent + desk-access.
		# "Knowledge Wiki User" is retired and no longer seeded.
		for role in (
			wiki_permissions.WIKI_MANAGER_ROLE,
			JARVIS_USER_ROLE,
			JARVIS_ADMIN_ROLE,
		):
			self.assertTrue(frappe.db.exists("Role", role))
			self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 1, role)

	def test_org_scope_carries_no_targets(self):
		self.assertEqual(self.org_page.scope, "Org")
		self.assertFalse(self.org_page.target_role)
		self.assertFalse(self.org_page.target_user)
		self.assertEqual(self.org_page.name, f"org--{SLUG_MARK}-org")

	def test_user_scope_slug_gets_audience_suffix(self):
		# localpart of wiki-perm-kwuser@example.com, scrubbed
		self.assertEqual(self.my_page.name, f"people--{SLUG_MARK}-mine--u-wiki-perm-kwuser")
		self.assertEqual(self.my_page.slug, self.my_page.name)

	def test_role_scope_slug_gets_audience_suffix(self):
		self.assertEqual(self.role_page.name, f"process--{SLUG_MARK}-role--r-wiki-perm-test-role")

	def test_suffix_not_doubled_when_caller_presuffixed(self):
		doc = _make_page(
			f"people--{SLUG_MARK}-presuf--u-wiki-perm-kwuser",
			"People",
			scope="User",
			target_user=USER_KW,
		)
		self.assertEqual(doc.name, f"people--{SLUG_MARK}-presuf--u-wiki-perm-kwuser")

	def test_suffix_trims_base_to_max_len(self):
		from jarvis.jarvis.doctype.jarvis_wiki_page.jarvis_wiki_page import (
			MAX_SLUG_LEN,
			SLUG_RE,
		)

		long_slug = f"people--{SLUG_MARK}-" + "a" * (MAX_SLUG_LEN - 20)
		doc = _make_page(long_slug, "People", scope="User", target_user=USER_KW)
		self.assertLessEqual(len(doc.name), MAX_SLUG_LEN)
		self.assertTrue(doc.name.endswith("--u-wiki-perm-kwuser"))
		self.assertTrue(SLUG_RE.match(doc.name))

	def test_target_user_defaults_to_creator(self):
		with _as(USER_KW):
			doc = frappe.get_doc(
				{
					"doctype": WIKI,
					"slug": f"people--{SLUG_MARK}-default",
					"title": "default target",
					"page_type": "People",
					"scope": "User",
				}
			)
			doc.insert(ignore_permissions=True)
		self.assertEqual(doc.target_user, USER_KW)
		self.assertTrue(doc.name.endswith("--u-wiki-perm-kwuser"))

	def test_role_scope_requires_target_role(self):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": f"process--{SLUG_MARK}-notarget",
				"title": "no target",
				"page_type": "Process",
				"scope": "Role",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_scope_change_guard_blocks_non_sm(self):
		with _as(USER_KW):
			doc = frappe.get_doc(WIKI, self.my_page.name)
			doc.scope = "Org"
			# ignore_permissions mirrors the SPA/tool save paths; the guard
			# must hold regardless.
			with self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)

	def test_scope_change_guard_blocks_retarget(self):
		with _as(USER_KW):
			doc = frappe.get_doc(WIKI, self.my_page.name)
			doc.target_user = USER_PLAIN
			with self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)

	def test_non_sm_content_edit_passes_guard(self):
		with _as(USER_KW):
			doc = frappe.get_doc(WIKI, self.my_page.name)
			doc.body_md = "- my private note"
			doc.save(ignore_permissions=True)
		self.assertIn("my private note", frappe.db.get_value(WIKI, self.my_page.name, "body_md"))

	def test_sm_can_rescope(self):
		with _as(USER_SM):
			doc = frappe.get_doc(WIKI, self.role_page.name)
			doc.target_role = OTHER_ROLE
			doc.save()
		self.assertEqual(frappe.db.get_value(WIKI, self.role_page.name, "target_role"), OTHER_ROLE)


# --------------------------------------------------------------------------- #
# visibility (read) matrix
# --------------------------------------------------------------------------- #
class TestVisibility(WikiPermTestCase):
	def _visible_names(self, user) -> set:
		cond = wiki_permissions.visible_scope_condition(user)
		rows = frappe.db.sql(
			f"select name from `tabJarvis Wiki Page` where slug like %s and {cond}",
			(f"%{SLUG_MARK}%",),
		)
		return {r[0] for r in rows}

	def test_sm_reads_everything(self):
		for page in (
			self.org_page,
			self.role_page,
			self.other_role_page,
			self.my_page,
			self.their_page,
		):
			self.assertTrue(wiki_permissions.can_read_page(page, USER_SM), page.name)

	def test_plain_user_reads_org_only(self):
		self.assertTrue(wiki_permissions.can_read_page(self.org_page, USER_PLAIN))
		for page in (self.role_page, self.other_role_page, self.my_page, self.their_page):
			self.assertFalse(wiki_permissions.can_read_page(page, USER_PLAIN), page.name)

	def test_role_page_visible_to_role_holder_only(self):
		self.assertTrue(wiki_permissions.can_read_page(self.role_page, USER_KW_MGR))
		self.assertFalse(wiki_permissions.can_read_page(self.other_role_page, USER_KW_MGR))
		self.assertFalse(wiki_permissions.can_read_page(self.role_page, USER_KW))

	def test_user_page_visible_to_target_only(self):
		self.assertTrue(wiki_permissions.can_read_page(self.my_page, USER_KW))
		self.assertFalse(wiki_permissions.can_read_page(self.my_page, USER_KW_MGR))
		self.assertFalse(wiki_permissions.can_read_page(self.their_page, USER_KW))

	def test_null_scope_reads_as_org(self):
		# Pre-v2 rows: scope NULL. Bypass the ORM so normalization can't fill it.
		frappe.db.set_value(WIKI, self.org_page.name, "scope", None, update_modified=False)
		page = frappe.db.get_value(
			WIKI,
			self.org_page.name,
			["name", "scope", "target_role", "target_user"],
			as_dict=True,
		)
		self.assertTrue(wiki_permissions.can_read_page(page, USER_PLAIN))
		self.assertIn(self.org_page.name, self._visible_names(USER_PLAIN))

	def test_visible_scope_condition_sql(self):
		all_names = {
			self.org_page.name,
			self.role_page.name,
			self.other_role_page.name,
			self.my_page.name,
			self.their_page.name,
		}
		self.assertEqual(self._visible_names(USER_PLAIN), {self.org_page.name})
		self.assertEqual(self._visible_names(USER_KW), {self.org_page.name, self.my_page.name})
		self.assertEqual(
			self._visible_names(USER_KW_MGR),
			{self.org_page.name, self.role_page.name, self.their_page.name},
		)
		self.assertEqual(self._visible_names(USER_SM), all_names)

	def test_query_conditions_unrestricted_for_sm(self):
		self.assertEqual(wiki_permissions.wiki_page_query_conditions(USER_SM), "")
		self.assertTrue(wiki_permissions.wiki_page_query_conditions(USER_PLAIN))

	def test_get_list_respects_scope(self):
		filters = {"slug": ["like", f"%{SLUG_MARK}%"]}
		with _as(USER_PLAIN):
			names = set(frappe.get_list(WIKI, filters=filters, pluck="name"))
		self.assertEqual(names, {self.org_page.name})
		with _as(USER_KW_MGR):
			names = set(frappe.get_list(WIKI, filters=filters, pluck="name"))
		self.assertEqual(names, {self.org_page.name, self.role_page.name, self.their_page.name})

	def test_has_permission_hook_enforces_visibility(self):
		my_doc = frappe.get_doc(WIKI, self.my_page.name)
		org_doc = frappe.get_doc(WIKI, self.org_page.name)
		self.assertFalse(bool(frappe.has_permission(WIKI, doc=my_doc, user=USER_PLAIN)))
		self.assertTrue(bool(frappe.has_permission(WIKI, doc=org_doc, user=USER_PLAIN)))


# --------------------------------------------------------------------------- #
# human write matrix
# --------------------------------------------------------------------------- #
class TestWriteMatrix(WikiPermTestCase):
	def test_org_pages_are_sm_only(self):
		self.assertTrue(wiki_permissions.can_edit_page(self.org_page, USER_SM))
		# Jarvis Admin inherits Manager, NOT SM — Org stays SM-only.
		for user in (USER_PLAIN, USER_KW, USER_KW_MGR, USER_ADMIN):
			self.assertFalse(wiki_permissions.can_edit_page(self.org_page, user), user)

	def test_role_pages_need_manager_holding_the_role(self):
		self.assertTrue(wiki_permissions.can_edit_page(self.role_page, USER_KW_MGR))
		# Jarvis Admin inherits Manager, scoped to the roles it actually holds.
		self.assertTrue(wiki_permissions.can_edit_page(self.role_page, USER_ADMIN))
		# manager/admin without the target role
		self.assertFalse(wiki_permissions.can_edit_page(self.other_role_page, USER_KW_MGR))
		self.assertFalse(wiki_permissions.can_edit_page(self.other_role_page, USER_ADMIN))
		# role/plain user without the manager or admin role
		self.assertFalse(wiki_permissions.can_edit_page(self.role_page, USER_KW))
		self.assertFalse(wiki_permissions.can_edit_page(self.role_page, USER_PLAIN))
		self.assertTrue(wiki_permissions.can_edit_page(self.other_role_page, USER_SM))

	def test_user_pages_editable_by_target_jarvis_user(self):
		# my_page.target_user == USER_KW (a plain Jarvis User): own-page editing
		# now rides Jarvis User (Knowledge Wiki User retired).
		self.assertTrue(wiki_permissions.can_edit_page(self.my_page, USER_KW))
		self.assertTrue(wiki_permissions.can_edit_page(self.their_page, USER_KW_MGR))
		# not the target, even with a manager role
		self.assertFalse(wiki_permissions.can_edit_page(self.my_page, USER_KW_MGR))
		self.assertTrue(wiki_permissions.can_edit_page(self.my_page, USER_SM))

	def test_target_user_without_jarvis_user_cannot_edit(self):
		# USER_PLAIN holds no Jarvis role: reads their own page (target
		# visibility) but cannot write it — own-page editing needs Jarvis User.
		page = _make_page(f"people--{SLUG_MARK}-plain", "People", scope="User", target_user=USER_PLAIN)
		self.assertTrue(wiki_permissions.can_read_page(page, USER_PLAIN))
		self.assertFalse(wiki_permissions.can_edit_page(page, USER_PLAIN))

	def test_archive_mirrors_edit(self):
		for page in (self.org_page, self.role_page, self.my_page):
			for user in (USER_PLAIN, USER_KW, USER_KW_MGR, USER_ADMIN, USER_SM):
				self.assertEqual(
					wiki_permissions.can_archive_page(page, user),
					wiki_permissions.can_edit_page(page, user),
					(page.name, user),
				)

	def test_creatable_scopes(self):
		self.assertEqual(wiki_permissions.creatable_scopes(USER_SM), ["Org", "Role", "User"])
		self.assertEqual(wiki_permissions.creatable_scopes(USER_KW_MGR), ["Role", "User"])
		# Jarvis Admin inherits Manager -> Role + User.
		self.assertEqual(wiki_permissions.creatable_scopes(USER_ADMIN), ["Role", "User"])
		# Plain Jarvis User -> own (User) pages only.
		self.assertEqual(wiki_permissions.creatable_scopes(USER_KW), ["User"])
		self.assertEqual(wiki_permissions.creatable_scopes(USER_PLAIN), [])

	def test_manageable_roles(self):
		# framework/blanket roles are never targetable: "Desk User" is
		# effectively everyone with a login (an org-wide side door), and the
		# platform "Jarvis User"/"Jarvis Admin" roles are excluded for the same
		# reason.
		blocked_roles = (
			"Administrator",
			"Guest",
			"All",
			"Desk User",
			"System Manager",
			"Website Manager",
			JARVIS_USER_ROLE,
			JARVIS_ADMIN_ROLE,
		)
		sm_roles = wiki_permissions.manageable_roles(USER_SM)
		self.assertIn(TEST_ROLE, sm_roles)
		self.assertIn(OTHER_ROLE, sm_roles)
		for blocked in blocked_roles:
			self.assertNotIn(blocked, sm_roles)

		mgr_roles = wiki_permissions.manageable_roles(USER_KW_MGR)
		self.assertIn(TEST_ROLE, mgr_roles)
		self.assertNotIn(OTHER_ROLE, mgr_roles)
		for blocked in blocked_roles:
			self.assertNotIn(blocked, mgr_roles)

		# Jarvis Admin inherits Manager: targets roles it holds (TEST_ROLE), and
		# never the blanket platform roles it also holds.
		admin_roles = wiki_permissions.manageable_roles(USER_ADMIN)
		self.assertIn(TEST_ROLE, admin_roles)
		self.assertNotIn(OTHER_ROLE, admin_roles)
		for blocked in blocked_roles:
			self.assertNotIn(blocked, admin_roles)

		self.assertEqual(wiki_permissions.manageable_roles(USER_KW), [])
		self.assertEqual(wiki_permissions.manageable_roles(USER_PLAIN), [])

	def test_desk_write_stays_sm_only(self):
		my_doc = frappe.get_doc(WIKI, self.my_page.name)
		# The matrix allows the target user...
		self.assertTrue(wiki_permissions.can_edit_page(my_doc, USER_KW))
		# ...but Desk/ORM write perms stay SM-only by design: human edits flow
		# through the SPA endpoints, which check the matrix then save with
		# ignore_permissions.
		self.assertFalse(bool(frappe.has_permission(WIKI, ptype="write", doc=my_doc, user=USER_KW)))
		self.assertTrue(bool(frappe.has_permission(WIKI, ptype="write", doc=my_doc, user=USER_SM)))
