"""Wiki v2 scope tests: SPA endpoint visibility + write matrix
(jarvis.chat.wiki list/get/create/save/archive/caps/language) and tool-side
scope handling (jarvis.tools.read_wiki visibility, jarvis.tools.update_wiki
scope param).

No LLM anywhere in these paths (ingest is exercised in test_wiki.py); the
mirror/lint `_now` endpoints are exercised in their own module tests. Pages
use ``wkscope`` slugs/titles so cleanup can never touch real data; non-Org
fixtures capture ``doc.slug`` because the controller suffixes Role/User slugs
(``--r-…`` / ``--u-…``) on create.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import wiki
from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE
from jarvis.tools.read_wiki import read_wiki
from jarvis.tools.update_wiki import update_wiki

WIKI_DT = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

# "Knowledge Wiki User" was retired; personal-page editing rides Jarvis User.
KW_MANAGER_ROLE = "Knowledge Wiki Manager"
TEST_ROLE = "Wkscope Test Role"
OTHER_ROLE = "Wkscope Other Role"

PLAIN_USER = "wkscope-plain@test.invalid"
KW_USER = "wkscope-kwu@test.invalid"
KW_MANAGER = "wkscope-kwm@test.invalid"


def _delete_scope_pages():
	frappe.db.delete(WIKI_DT, {"slug": ["like", "%wkscope%"]})
	frappe.db.commit()


def _ensure_role(name):
	if not frappe.db.exists("Role", name):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": name,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)


def _ensure_user(email, first_name, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				# Role-less users flip to Website User; every fixture carries at
				# least one desk role so user_type sticks.
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)


def _make_page(slug, title, page_type="Org", **kwargs):
	fields = {
		"doctype": WIKI_DT,
		"slug": slug,
		"title": title,
		"page_type": page_type,
		"status": "Active",
		# Fresh by default so the attention filter only matches pages a test
		# deliberately made stale / flagged / unconfirmed.
		"last_confirmed_at": now_datetime(),
	}
	fields.update(kwargs)
	doc = frappe.get_doc(fields)
	doc.insert(ignore_permissions=True)
	return doc


class _WikiScopeFixture(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# KW roles are normally seeded by after_migrate; ensure idempotently
		# so this module doesn't depend on hook ordering.
		for role in (KW_MANAGER_ROLE, TEST_ROLE, OTHER_ROLE):
			_ensure_role(role)
		_ensure_user(PLAIN_USER, "Wkscope Plain", ["Desk User"])
		# KW_USER now stands for a plain Jarvis User (own-page editor).
		_ensure_user(KW_USER, "Wkscope KWU", ["Desk User", JARVIS_USER_ROLE])
		_ensure_user(KW_MANAGER, "Wkscope KWM", ["Desk User", KW_MANAGER_ROLE, TEST_ROLE])
		# PLAIN_USER stands for a user WITHOUT Jarvis app access. Fixtures persist
		# across runs and an earlier case may have left "Jarvis User" on it, so
		# strip it to exercise the no-access cases (creates nothing; personal
		# writes refused).
		frappe.db.delete(
			"Has Role",
			{
				"parenttype": "User",
				"parent": PLAIN_USER,
				"role": ["in", (JARVIS_USER_ROLE, KW_MANAGER_ROLE)],
			},
		)
		frappe.clear_cache(user=PLAIN_USER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_delete_scope_pages()
		for email in (PLAIN_USER, KW_USER, KW_MANAGER):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, ignore_permissions=True, force=True)
		# The KW roles are site fixtures (after_migrate reseeds them) — only
		# the module-local test roles are removed.
		for role in (TEST_ROLE, OTHER_ROLE):
			if frappe.db.exists("Role", role):
				frappe.delete_doc("Role", role, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_delete_scope_pages()

	def tearDown(self):
		frappe.set_user("Administrator")
		_delete_scope_pages()

	# -- shared fixtures ---------------------------------------------------- #
	def _plant_matrix_pages(self):
		"""One page per scope cell; returns {label: slug} (post-suffix)."""
		org = _make_page("org--wkscope-handbook", "Wkscope Handbook")
		role = _make_page(
			"process--wkscope-role-note",
			"Wkscope Role Note",
			page_type="Process",
			scope="Role",
			target_role=TEST_ROLE,
		)
		mine = _make_page(
			"people--wkscope-plain-note",
			"Wkscope Plain Note",
			page_type="People",
			scope="User",
			target_user=PLAIN_USER,
		)
		other = _make_page(
			"people--wkscope-kwu-note",
			"Wkscope KWU Note",
			page_type="People",
			scope="User",
			target_user=KW_USER,
		)
		return {
			"org": org.slug,
			"role": role.slug,
			"plain_user": mine.slug,
			"kwu_user": other.slug,
		}


class TestWikiCapsAndLanguage(_WikiScopeFixture):
	def test_caps_matrix(self):
		frappe.set_user(PLAIN_USER)
		caps = wiki.get_wiki_caps()
		self.assertFalse(caps["is_sm"])
		self.assertEqual(set(caps["creatable_scopes"]), set())

		frappe.set_user(KW_USER)
		caps = wiki.get_wiki_caps()
		self.assertFalse(caps["is_sm"])
		self.assertEqual(set(caps["creatable_scopes"]), {"User"})

		frappe.set_user(KW_MANAGER)
		caps = wiki.get_wiki_caps()
		self.assertFalse(caps["is_sm"])
		self.assertEqual(set(caps["creatable_scopes"]), {"Role", "User"})
		self.assertIn(TEST_ROLE, caps["manageable_roles"])
		self.assertNotIn(OTHER_ROLE, caps["manageable_roles"])

		frappe.set_user("Administrator")
		caps = wiki.get_wiki_caps()
		self.assertTrue(caps["is_sm"])
		self.assertEqual(set(caps["creatable_scopes"]), {"Org", "Role", "User"})
		self.assertIn(TEST_ROLE, caps["manageable_roles"])

	def test_manager_with_no_targetable_roles_hides_role_scope(self):
		# A wiki manager (here a Jarvis Admin) who holds only blanket roles has an
		# empty manageable_roles: JARVIS_ADMIN_ROLE / JARVIS_USER_ROLE / "Desk User"
		# are all in _NON_TARGETABLE_ROLES. creatable_scopes must NOT offer "Role"
		# then, or the New-page dialog strands on scope=Role with an empty,
		# unsubmittable picker. "User" is still offered (an admin is a wiki user).
		email = "wkscope-admin@test.invalid"
		_ensure_user(email, "Wkscope Admin", ["Desk User", JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE])
		frappe.clear_cache(user=email)
		frappe.db.commit()
		try:
			frappe.set_user(email)
			caps = wiki.get_wiki_caps()
			self.assertFalse(caps["is_sm"])
			self.assertEqual(caps["manageable_roles"], [])
			self.assertNotIn("Role", caps["creatable_scopes"])
			self.assertIn("User", caps["creatable_scopes"])
		finally:
			frappe.set_user("Administrator")
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, ignore_permissions=True, force=True)
			frappe.db.commit()

	def test_caps_guest_rejected(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			wiki.get_wiki_caps()

	def test_knowledge_language_default_and_setter(self):
		rows = frappe.db.sql(
			"select value from `tabSingles` where doctype='Jarvis Settings' and field='knowledge_language'"
		)
		original = rows[0][0] if rows else None
		try:
			frappe.db.delete("Singles", {"doctype": SETTINGS, "field": "knowledge_language"})
			self.assertEqual(wiki.get_wiki_caps()["knowledge_language"], "English")

			# SM-only setter.
			frappe.set_user(PLAIN_USER)
			with self.assertRaises(frappe.PermissionError):
				wiki.set_knowledge_language("Original")

			frappe.set_user("Administrator")
			with self.assertRaises(frappe.ValidationError):
				wiki.set_knowledge_language("Klingon")
			out = wiki.set_knowledge_language("Original")
			self.assertEqual(out, {"ok": True, "knowledge_language": "Original"})
			self.assertEqual(wiki.get_wiki_caps()["knowledge_language"], "Original")
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("Singles", {"doctype": SETTINGS, "field": "knowledge_language"})
			if original is not None:
				frappe.db.set_single_value(SETTINGS, "knowledge_language", original, update_modified=False)


class TestWikiListScopes(_WikiScopeFixture):
	def _visible_slugs(self, **kwargs):
		out = wiki.list_wiki_pages_page(search="wkscope", page_length=100, **kwargs)
		return {r["slug"] for r in out["rows"]}, out

	def test_guest_rejected(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			wiki.list_wiki_pages_page()

	def test_plain_user_sees_org_and_own_only(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		visible, out = self._visible_slugs()
		self.assertEqual(visible, {slugs["org"], slugs["plain_user"]})
		# The total honors the same visibility filter (real COUNT, not a
		# row materialization).
		self.assertEqual(out["total"], 2)
		self.assertFalse(out["has_more"])

	def test_role_holder_sees_role_page(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(KW_MANAGER)
		visible, _ = self._visible_slugs()
		self.assertEqual(visible, {slugs["org"], slugs["role"]})

	def test_sm_sees_all(self):
		slugs = self._plant_matrix_pages()
		visible, out = self._visible_slugs()
		self.assertEqual(visible, set(slugs.values()))
		self.assertEqual(out["total"], 4)

	def test_scope_filter(self):
		slugs = self._plant_matrix_pages()
		visible, _ = self._visible_slugs(scope_filter="org")
		self.assertEqual(visible, {slugs["org"]})
		visible, _ = self._visible_slugs(scope_filter="role")
		self.assertEqual(visible, {slugs["role"]})
		# "mine" is the CALLER's User pages: none for Administrator, exactly
		# one for the plain user.
		visible, _ = self._visible_slugs(scope_filter="mine")
		self.assertEqual(visible, set())
		frappe.set_user(PLAIN_USER)
		visible, _ = self._visible_slugs(scope_filter="mine")
		self.assertEqual(visible, {slugs["plain_user"]})
		with self.assertRaises(frappe.ValidationError):
			wiki.list_wiki_pages_page(scope_filter="bogus")

	def test_attention_filter(self):
		_make_page("org--wkscope-fresh", "Wkscope Fresh")
		stale = _make_page(
			"org--wkscope-stale",
			"Wkscope Stale",
			last_confirmed_at=add_to_date(now_datetime(), days=-100),
		)
		contra = _make_page("org--wkscope-contra", "Wkscope Contra", contradiction_flag=1)
		never = _make_page("org--wkscope-never", "Wkscope Never", last_confirmed_at=None)
		visible, out = self._visible_slugs(attention=1)
		self.assertEqual(visible, {stale.slug, contra.slug, never.slug})
		row = next(r for r in out["rows"] if r["slug"] == stale.slug)
		self.assertTrue(row["stale"])
		self.assertEqual(row["scope"], "Org")

	def test_page_type_and_search_filters(self):
		self._plant_matrix_pages()
		out = wiki.list_wiki_pages_page(search="wkscope", page_type="Process", page_length=100)
		self.assertEqual(out["total"], 1)
		self.assertEqual(len(out["rows"]), 1)
		self.assertEqual(out["rows"][0]["page_type"], "Process")
		self.assertTrue(out["rows"][0]["slug"].startswith("process--wkscope-role-note"))
		out = wiki.list_wiki_pages_page(search="wkscope handbook", page_length=100)
		self.assertEqual(out["total"], 1)
		self.assertEqual(out["rows"][0]["slug"], "org--wkscope-handbook")
		with self.assertRaises(frappe.ValidationError):
			wiki.list_wiki_pages_page(page_type="Blog")

	def test_pagination_and_total(self):
		for i in range(5):
			_make_page(f"org--wkscope-pg-{i}", f"Wkscope Pg {i}")
		seen = set()
		for page in (1, 2, 3):
			out = wiki.list_wiki_pages_page(search="wkscope-pg", page=page, page_length=2)
			self.assertEqual(out["total"], 5)
			self.assertEqual(out["has_more"], page < 3)
			seen |= {r["slug"] for r in out["rows"]}
		self.assertEqual(len(seen), 5)


class TestWikiGetPage(_WikiScopeFixture):
	def test_org_page_flags_by_role(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		page = wiki.get_wiki_page(slugs["org"])
		self.assertEqual(page["scope"], "Org")
		self.assertFalse(page["can_edit"])
		self.assertFalse(page["can_archive"])
		frappe.set_user("Administrator")
		page = wiki.get_wiki_page(slugs["org"])
		self.assertTrue(page["can_edit"])
		self.assertTrue(page["can_archive"])

	def test_own_user_page_editable_with_kw_role(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(KW_USER)
		page = wiki.get_wiki_page(slugs["kwu_user"])
		self.assertEqual(page["scope"], "User")
		self.assertEqual(page["target_user"], KW_USER)
		self.assertTrue(page["can_edit"])

	def test_foreign_user_page_hidden(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.ValidationError):
			wiki.get_wiki_page(slugs["kwu_user"])

	def test_role_page_visible_to_holder_only(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(KW_MANAGER)
		page = wiki.get_wiki_page(slugs["role"])
		self.assertEqual(page["scope"], "Role")
		self.assertEqual(page["target_role"], TEST_ROLE)
		self.assertTrue(page["can_edit"])
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.ValidationError):
			wiki.get_wiki_page(slugs["role"])


class TestWikiCreateMatrix(_WikiScopeFixture):
	def test_sm_creates_org_page_with_derived_slug(self):
		out = wiki.create_wiki_page(
			title="Wkscope Returns Policy",
			page_type="Org",
			summary="How wkscope returns work.",
			body_md="- 30 days",
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["slug"], "org--wkscope-returns-policy")
		doc = frappe.get_doc(WIKI_DT, out["slug"])
		self.assertEqual(doc.get("scope"), "Org")
		self.assertEqual(doc.status, "Active")
		self.assertIsNotNone(doc.last_confirmed_at)

	def test_duplicate_slug_reports_reason(self):
		wiki.create_wiki_page(title="Wkscope Dup", page_type="Org")
		out = wiki.create_wiki_page(title="Wkscope Dup", page_type="Org")
		self.assertFalse(out["ok"])
		self.assertIn("already exists", out["reason"])

	def test_plain_user_cannot_create(self):
		frappe.set_user(PLAIN_USER)
		for scope in ("Org", "User"):
			out = wiki.create_wiki_page(title="Wkscope Nope", page_type="Org", scope=scope)
			self.assertFalse(out["ok"])
			self.assertTrue(out["reason"])

	def test_kw_user_creates_user_scope_only(self):
		frappe.set_user(KW_USER)
		out = wiki.create_wiki_page(title="Wkscope My Notes", page_type="People", scope="User")
		self.assertTrue(out["ok"])
		# Controller suffixes User-scope slugs so users can never collide.
		self.assertIn("--u-", out["slug"])
		self.assertTrue(out["slug"].startswith("people--wkscope-my-notes"))
		doc = frappe.get_doc(WIKI_DT, out["slug"])
		self.assertEqual(doc.get("scope"), "User")
		self.assertEqual(doc.get("target_user"), KW_USER)

		out = wiki.create_wiki_page(title="Wkscope Org Try", page_type="Org")
		self.assertFalse(out["ok"])

	def test_user_scope_slugs_do_not_collide_across_users(self):
		frappe.set_user(KW_USER)
		a = wiki.create_wiki_page(title="Wkscope Same Title", page_type="People", scope="User")
		frappe.set_user(KW_MANAGER)
		b = wiki.create_wiki_page(title="Wkscope Same Title", page_type="People", scope="User")
		self.assertTrue(a["ok"] and b["ok"])
		self.assertNotEqual(a["slug"], b["slug"])

	def test_kw_manager_role_scope_matrix(self):
		frappe.set_user(KW_MANAGER)
		out = wiki.create_wiki_page(
			title="Wkscope Team Runbook",
			page_type="Process",
			scope="Role",
			target_role=TEST_ROLE,
		)
		self.assertTrue(out["ok"])
		self.assertIn("--r-", out["slug"])
		doc = frappe.get_doc(WIKI_DT, out["slug"])
		self.assertEqual(doc.get("scope"), "Role")
		self.assertEqual(doc.get("target_role"), TEST_ROLE)

		# A role the manager does not hold is not manageable.
		out = wiki.create_wiki_page(
			title="Wkscope Foreign Runbook",
			page_type="Process",
			scope="Role",
			target_role=OTHER_ROLE,
		)
		self.assertFalse(out["ok"])
		# Role scope without a target role is malformed.
		with self.assertRaises(frappe.ValidationError):
			wiki.create_wiki_page(title="Wkscope No Role", page_type="Process", scope="Role")

	def test_malformed_input_throws(self):
		with self.assertRaises(frappe.ValidationError):
			wiki.create_wiki_page(title="", page_type="Org")
		with self.assertRaises(frappe.ValidationError):
			wiki.create_wiki_page(title="Wkscope X", page_type="Blog")
		with self.assertRaises(frappe.ValidationError):
			wiki.create_wiki_page(title="Wkscope X", page_type="Org", scope="Galaxy")


class TestWikiSaveArchiveMatrix(_WikiScopeFixture):
	def test_plain_user_cannot_save_org_page(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.PermissionError):
			wiki.save_wiki_page(slug=slugs["org"], body_md="hijack")

	def test_sm_saves_org_page(self):
		slugs = self._plant_matrix_pages()
		out = wiki.save_wiki_page(slug=slugs["org"], body_md="- reviewed")
		self.assertTrue(out["ok"])
		self.assertIn("reviewed", frappe.get_doc(WIKI_DT, slugs["org"]).body_md)

	def test_kw_user_saves_own_page_not_others(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(KW_USER)
		out = wiki.save_wiki_page(slug=slugs["kwu_user"], body_md="- mine")
		self.assertTrue(out["ok"])
		with self.assertRaises(frappe.PermissionError):
			wiki.save_wiki_page(slug=slugs["plain_user"], body_md="- not mine")

	def test_own_user_page_without_kw_role_read_only(self):
		# The write matrix needs a KW role even on your own page; the plain
		# user can read it but not save it.
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		self.assertEqual(wiki.get_wiki_page(slugs["plain_user"])["can_edit"], False)
		with self.assertRaises(frappe.PermissionError):
			wiki.save_wiki_page(slug=slugs["plain_user"], body_md="- edit")

	def test_kw_manager_saves_and_archives_held_role_page(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(KW_MANAGER)
		out = wiki.save_wiki_page(slug=slugs["role"], body_md="- team update")
		self.assertTrue(out["ok"])
		out = wiki.archive_wiki_page(slug=slugs["role"])
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(WIKI_DT, slugs["role"], "status"), "Archived")

	def test_archive_org_page_sm_only(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.PermissionError):
			wiki.archive_wiki_page(slug=slugs["org"])
		frappe.set_user("Administrator")
		out = wiki.archive_wiki_page(slug=slugs["org"])
		self.assertTrue(out["ok"])

	def test_delete_follows_archive_authority(self):
		slugs = self._plant_matrix_pages()
		# plain user: no delete on org pages
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.PermissionError):
			wiki.delete_wiki_page(slug=slugs["org"])
		# KW manager may delete pages of roles they hold (same right as archive)
		frappe.set_user(KW_MANAGER)
		out = wiki.delete_wiki_page(slug=slugs["role"])
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.exists(WIKI_DT, {"slug": slugs["role"]}))
		# SM deletes org pages
		frappe.set_user("Administrator")
		out = wiki.delete_wiki_page(slug=slugs["org"])
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.exists(WIKI_DT, {"slug": slugs["org"]}))

	def test_restore_after_archive(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user("Administrator")
		wiki.archive_wiki_page(slug=slugs["org"])
		# archived listing shows the page; active listing doesn't
		archived = wiki.list_wiki_pages_page(archived=1, page_length=50)
		self.assertIn(slugs["org"], [r["slug"] for r in archived["rows"]])
		active = wiki.list_wiki_pages_page(page_length=50)
		self.assertNotIn(slugs["org"], [r["slug"] for r in active["rows"]])
		# restore obeys the same matrix as archive
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.PermissionError):
			wiki.restore_wiki_page(slug=slugs["org"])
		frappe.set_user("Administrator")
		out = wiki.restore_wiki_page(slug=slugs["org"])
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(WIKI_DT, slugs["org"], "status"), "Active")


class TestWikiToolVisibility(_WikiScopeFixture):
	def test_read_by_slug_honors_visibility(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		page = read_wiki(slug=slugs["org"])
		self.assertEqual(page["slug"], slugs["org"])
		# #733: a foreign private page and a missing page raise the
		# byte-identical error, so the exception can't be used to learn
		# a private page exists.
		with self.assertRaises(InvalidArgumentError) as forbidden:
			read_wiki(slug=slugs["kwu_user"])
		self.assertEqual(str(forbidden.exception), f"unknown wiki page: {slugs['kwu_user']}")
		with self.assertRaises(InvalidArgumentError) as missing:
			read_wiki(slug="wkscope-missing-slug")
		self.assertEqual(str(missing.exception), "unknown wiki page: wkscope-missing-slug")
		# The owner still reads their own page.
		frappe.set_user(KW_USER)
		page = read_wiki(slug=slugs["kwu_user"])
		self.assertEqual(page["slug"], slugs["kwu_user"])

	def test_query_search_honors_visibility(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		found = {r["slug"] for r in read_wiki(query="wkscope", limit=10)}
		self.assertEqual(found, {slugs["org"], slugs["plain_user"]})
		frappe.set_user(KW_MANAGER)
		found = {r["slug"] for r in read_wiki(query="wkscope", limit=10)}
		self.assertEqual(found, {slugs["org"], slugs["role"]})
		frappe.set_user("Administrator")
		found = {r["slug"] for r in read_wiki(query="wkscope", limit=10)}
		self.assertEqual(found, set(slugs.values()))


class TestUpdateWikiScope(_WikiScopeFixture):
	def test_plain_user_org_channel_preserved(self):
		# v1 LLM-channel behavior: any desk user maintains ORG pages through
		# the confirm-gated tool, even with doctype write moved off Desk User.
		frappe.set_user(PLAIN_USER)
		out = update_wiki(
			slug="org--wkscope-tool-org",
			title="Wkscope Tool Org",
			page_type="Org",
			replace_body_md="First.",
		)
		self.assertTrue(out["ok"] and out["created"])
		self.assertEqual(out["scope"], "Org")
		out = update_wiki(slug="org--wkscope-tool-org", append_md="Second.")
		self.assertFalse(out["created"])
		body = frappe.db.get_value(WIKI_DT, "org--wkscope-tool-org", "body_md")
		self.assertIn("First.", body)
		self.assertIn("Second.", body)

	def test_user_scope_requires_jarvis_user(self):
		# PLAIN_USER holds only Desk User (no Jarvis User), so personal-page
		# creation is refused now that own-page editing rides Jarvis User.
		frappe.set_user(PLAIN_USER)
		out = update_wiki(
			slug="wkscope-personal",
			title="Wkscope Personal",
			page_type="People",
			scope="User",
			replace_body_md="x",
		)
		self.assertFalse(out["ok"])
		self.assertIn("Jarvis User", out["reason"])
		self.assertFalse(frappe.db.exists(WIKI_DT, {"slug": ["like", "wkscope-personal%"]}))

	def test_user_scope_happy_path_and_base_slug_reuse(self):
		frappe.set_user(KW_USER)
		out = update_wiki(
			slug="wkscope-personal",
			title="Wkscope Personal",
			page_type="People",
			scope="User",
			replace_body_md="First.",
		)
		self.assertTrue(out["ok"] and out["created"])
		self.assertEqual(out["scope"], "User")
		self.assertIn("--u-", out["slug"])
		created_slug = out["slug"]
		doc = frappe.get_doc(WIKI_DT, created_slug)
		self.assertEqual(doc.get("target_user"), KW_USER)

		# A follow-up call with the ORIGINAL base slug lands on the same
		# personal page (suffix probe), not a duplicate.
		out = update_wiki(slug="wkscope-personal", scope="User", append_md="Second.")
		self.assertTrue(out["ok"])
		self.assertFalse(out["created"])
		self.assertEqual(out["slug"], created_slug)
		body = frappe.get_doc(WIKI_DT, created_slug).body_md
		self.assertIn("First.", body)
		self.assertIn("Second.", body)

	def test_user_scope_on_org_slug_forks_a_personal_page(self):
		org = _make_page(
			"process--wkscope-shared",
			"Wkscope Shared",
			page_type="Process",
			body_md="Org body.",
		)
		frappe.set_user(KW_USER)
		out = update_wiki(
			slug="process--wkscope-shared",
			title="Wkscope Shared Fork",
			page_type="Process",
			scope="User",
			append_md="- personal view",
		)
		self.assertTrue(out["ok"] and out["created"])
		self.assertNotEqual(out["slug"], org.slug)
		self.assertEqual(out["scope"], "User")
		# The org page is untouched.
		self.assertEqual(frappe.db.get_value(WIKI_DT, org.slug, "body_md"), "Org body.")

	def test_foreign_user_page_not_writable_via_tool(self):
		slugs = self._plant_matrix_pages()
		frappe.set_user(PLAIN_USER)
		# #733: masked as the same "missing title/page_type" error a create
		# attempt against a genuinely unknown slug raises — a forbidden
		# existing page can't be distinguished from one that doesn't exist.
		with self.assertRaises(InvalidArgumentError) as forbidden:
			update_wiki(slug=slugs["kwu_user"], append_md="- vandalism")
		with self.assertRaises(InvalidArgumentError) as missing:
			update_wiki(slug="wkscope-missing-write-slug", append_md="- vandalism")
		self.assertEqual(str(forbidden.exception), str(missing.exception))
		self.assertEqual(str(forbidden.exception), "creating a new wiki page requires title and page_type")

	def test_invalid_scope_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			update_wiki(slug="org--wkscope-x", title="X", page_type="Org", scope="Team")
