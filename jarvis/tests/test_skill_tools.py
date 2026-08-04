"""Skill tools (find_skills / get_skill / create_custom_skill) + scope plumbing.

DB-backed: rows are created through the real doctype as non-SM System Users
(``frappe.set_user``) so controller validation, the ``user_can_use_skill``
visibility rules and the Personal-scope owner-only rule are exercised for
real. Registry/classification tests import ``jarvis.api`` /
``jarvis.tools.registry`` lazily (inside the test) because the registry
imports every tool module — including the wiki pair built alongside this
feature — at load time.
"""

import contextlib
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.custom_skills import (
	build_push_payload,
	personal_skill_clause,
	personal_skills_cache_key,
	prefixed_slug,
)
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role
from jarvis.tools.create_custom_skill import create_custom_skill
from jarvis.tools.find_skills import find_skills
from jarvis.tools.get_skill import (
	_LEARNED_PREFIX,
	_MISS_AUDIT_PREFIX,
	_MISS_AUDIT_TTL_S,
	_audit_learned_miss,
	get_skill,
)

SKILL = "Jarvis Custom Skill"
# All fixture slugs share this prefix so leftovers from committed runs are
# swept in setUpClass.
PFX = "sttool"

OWNER = "sttool-owner@example.com"
PEER = "sttool-peer@example.com"
THIRD = "sttool-third@example.com"
WEB = "sttool-web@example.com"

ERROR_LOG = "Error Log"
AUDIT_TITLE = "Jarvis: learned skill fetch missed"
# Two stand-ins for "two tenant benches sharing one redis". Never real sites:
# the point is only that ``frappe.local.site`` differs between the two calls.
AUDIT_SITE_A = "sttool-tenant-a.test"
AUDIT_SITE_B = "sttool-tenant-b.test"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _site(name: str):
	"""Run a block as if this process were serving another tenant's site.

	Safe to do mid-test: Frappe's own cache namespacing keys off
	``frappe.local.conf.db_name`` (RedisWrapper.make_key), not off
	``frappe.local.site``, so swapping the site name moves nothing except the
	hand-built key under test.
	"""
	orig = frappe.local.site
	frappe.local.site = name
	try:
		yield
	finally:
		frappe.local.site = orig


@contextlib.contextmanager
def _engine_flag():
	"""Bypass the scope-creation guard (security review PART 2 TASK 10) for a
	fixture that needs a pre-existing Role/Org skill owned by a NON-reviewer —
	the creation guard itself is covered by TestCreateCustomSkill; these tests
	exercise the VISIBILITY of already-scoped rows."""
	prev = frappe.flags.jarvis_pattern_engine
	frappe.flags.jarvis_pattern_engine = True
	try:
		yield
	finally:
		frappe.flags.jarvis_pattern_engine = prev


def _ensure_system_user(email: str) -> str:
	"""A non-SM System User (realistic tool caller).

	Carries the Jarvis User role: since the chat permission hardening (#284) the
	skill tools gate on System Manager or Jarvis User, and these fixtures are
	deliberately NOT System Managers — that is the whole point of the
	non-owner/peer scoping assertions — so without the role they are refused
	before reaching the logic under test.
	"""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "sttool",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
				"roles": [{"role": "Sales User"}, {"role": JARVIS_USER_ROLE}],
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	elif frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User")
	if JARVIS_USER_ROLE not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
		frappe.clear_cache(user=email)
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
	return email


def _ensure_website_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "sttool-web",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "Website User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	return email


def _sweep_fixture_skills():
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", f"{PFX}-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)


def _make_skill(
	owner: str,
	skill_name: str,
	description: str,
	*,
	scope: str = "Org",
	shared_with=None,
	allowed_roles=None,
	target_role=None,
	enabled: int = 1,
):
	"""Insert a row through the real doctype as ``owner`` (child tables can't
	be passed through the tool). The engine flag lets the fixture mint a
	Role/Org row directly (the reviewer-gated creation path is tested elsewhere)."""
	with _as(owner), _engine_flag():
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": skill_name,
				"description": description,
				"instructions": f"instructions for {skill_name}",
				"scope": scope,
				"target_role": target_role,
				"enabled": enabled,
				"user_invocable": 1,
				"shared_with": [{"user": u} for u in (shared_with or [])],
				"allowed_roles": [{"role": r} for r in (allowed_roles or [])],
			}
		)
		doc.insert(ignore_permissions=True)
	return doc


class SkillToolsTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_sweep_fixture_skills()
		_ensure_system_user(OWNER)
		_ensure_system_user(PEER)
		_ensure_system_user(THIRD)
		_ensure_website_user(WEB)

	def setUp(self):
		frappe.set_user("Administrator")
		for user in (OWNER, PEER, THIRD):
			frappe.cache().delete_value(personal_skills_cache_key(user))

	def tearDown(self):
		frappe.set_user("Administrator")
		_sweep_fixture_skills()


class TestFindSkills(SkillToolsTestCase):
	def test_short_or_empty_query_rejected(self):
		with _as(OWNER):
			for bad in ("", "  ", "a"):
				with self.assertRaises(InvalidArgumentError):
					find_skills(bad)

	def test_personal_row_invisible_to_other_user(self):
		with _as(OWNER):
			create_custom_skill(
				f"{PFX}-priv-alpha",
				"sttool marker alpha steps",
				"do the alpha thing",
				scope="Personal",
			)
		with _as(OWNER):
			mine = find_skills("marker alpha")
			self.assertEqual([s["skill_name"] for s in mine["skills"]], [f"{PFX}-priv-alpha"])
			# The tool now creates private (User-scope) skills — the ladder's
			# rename of "Personal" (security review PART 2 TASK 10).
			self.assertEqual(mine["skills"][0]["scope"], "User")
		with _as(PEER):
			theirs = find_skills("marker alpha")
			self.assertEqual(theirs["count"], 0)
			self.assertEqual(theirs["skills"], [])

	def test_org_row_visible_to_everyone(self):
		_make_skill(OWNER, f"{PFX}-org-beta", "sttool marker beta steps")
		with _as(PEER):
			res = find_skills("marker beta")
			names = [s["skill_name"] for s in res["skills"]]
			self.assertIn(f"{PFX}-org-beta", names)
			row = res["skills"][names.index(f"{PFX}-org-beta")]
			self.assertEqual(row["scope"], "Org")
			self.assertFalse(row["managed"])

	def test_shared_row_visible_despite_role_mismatch(self):
		# allowed_roles doesn't match PEER, but the explicit share wins;
		# THIRD (no share, no role) never sees it.
		_make_skill(
			OWNER,
			f"{PFX}-shared-gamma",
			"sttool marker gamma steps",
			shared_with=[PEER],
			allowed_roles=["Sales Manager"],
		)
		with _as(PEER):
			names = [s["skill_name"] for s in find_skills("marker gamma")["skills"]]
			self.assertIn(f"{PFX}-shared-gamma", names)
		with _as(THIRD):
			self.assertEqual(find_skills("marker gamma")["count"], 0)

	def test_disabled_rows_excluded(self):
		_make_skill(OWNER, f"{PFX}-off-delta", "sttool marker delta steps", enabled=0)
		with _as(OWNER):
			self.assertEqual(find_skills("marker delta")["count"], 0)

	def test_like_wildcards_are_escaped(self):
		# An unescaped "_" would make "a_b" match "axb" too.
		_make_skill(OWNER, f"{PFX}-lit-underscore", "sttool token a_b here")
		_make_skill(OWNER, f"{PFX}-lit-decoy", "sttool token axb here")
		with _as(OWNER):
			names = [s["skill_name"] for s in find_skills("token a_b")["skills"]]
			self.assertEqual(names, [f"{PFX}-lit-underscore"])

	def test_limit_caps_results(self):
		for i in range(3):
			_make_skill(OWNER, f"{PFX}-lim-{i}", "sttool marker limitcase steps")
		with _as(OWNER):
			res = find_skills("marker limitcase", limit=2)
			self.assertEqual(res["count"], 2)
			self.assertEqual(len(res["skills"]), 2)

	def test_gate_rejects_guest_and_website_user(self):
		with _as("Guest"):
			with self.assertRaises(PermissionDeniedError):
				find_skills("marker beta")
		with _as(WEB):
			with self.assertRaises(PermissionDeniedError):
				find_skills("marker beta")


class TestGetSkill(SkillToolsTestCase):
	def test_bare_and_custom_prefixed_names_resolve(self):
		with _as(OWNER):
			create_custom_skill(
				f"{PFX}-fetch-me",
				"sttool fetch desc",
				"fetch body",
				scope="Personal",
			)
		with _as(OWNER):
			for query_name in (f"{PFX}-fetch-me", f"custom-{PFX}-fetch-me"):
				out = get_skill(query_name)
				self.assertEqual(out["skill_name"], f"{PFX}-fetch-me")
				self.assertEqual(out["instructions"], "fetch body")
				self.assertEqual(out["scope"], "User")
				self.assertEqual(out["enabled"], 1)

	def test_unknown_skill_is_invalid_argument(self):
		with _as(OWNER):
			with self.assertRaises(InvalidArgumentError):
				get_skill(f"{PFX}-does-not-exist")

	def test_personal_row_denied_to_other_user(self):
		with _as(OWNER):
			create_custom_skill(
				f"{PFX}-priv-fetch",
				"sttool priv fetch",
				"secret body",
				scope="Personal",
			)
		with _as(PEER):
			with self.assertRaises(PermissionDeniedError):
				get_skill(f"{PFX}-priv-fetch")

	def test_role_scoped_org_row_denied_without_role_or_share(self):
		_make_skill(
			OWNER,
			f"{PFX}-role-only",
			"sttool role only",
			allowed_roles=["Sales Manager"],
		)
		with _as(PEER):
			with self.assertRaises(PermissionDeniedError):
				get_skill(f"{PFX}-role-only")

	def test_role_scope_visible_to_target_role_holder(self):
		# TASK 13 fix C: a scope=Role skill whose target_role the caller holds is
		# findable + fetchable through the tools (the tool SELECTs must carry
		# target_role, else the whole User->Role promotion tier is dead). PEER
		# holds "Sales User".
		_make_skill(
			OWNER,
			f"{PFX}-role-tier",
			"sttool role tier steps",
			scope="Role",
			target_role="Sales User",
		)
		with _as(PEER):
			names = [s["skill_name"] for s in find_skills("role tier")["skills"]]
			self.assertIn(f"{PFX}-role-tier", names)
			got = get_skill(f"{PFX}-role-tier")
			self.assertEqual(got["scope"], "Role")
			self.assertEqual(got["instructions"], f"instructions for {PFX}-role-tier")

	def test_role_scope_denied_to_non_target_role_holder(self):
		# A Role skill targeting a role the caller does NOT hold is invisible.
		_make_skill(
			OWNER,
			f"{PFX}-role-tier-x",
			"sttool role tier x steps",
			scope="Role",
			target_role="Accounts Manager",
		)
		with _as(PEER):  # PEER holds Sales User, not Accounts Manager
			self.assertEqual(find_skills("role tier x")["count"], 0)
			with self.assertRaises(PermissionDeniedError):
				get_skill(f"{PFX}-role-tier-x")

	def test_own_row_preferred_over_same_named_foreign_row(self):
		# skill_name is unique per owner, not globally. R3-SP-1 narrows that for
		# SHARED rows only (at most one Role/Org row may carry a given slug), so
		# the same-named pair is one shared row plus one private row rather than
		# two Org rows. That is also the sharper form of this test: OWNER's Org
		# row is genuinely VISIBLE to PEER, so the caller's own row has to
		# actively win the tie instead of being the only usable candidate.
		_make_skill(OWNER, f"{PFX}-dup-name", "sttool dup owner copy")
		_make_skill(PEER, f"{PFX}-dup-name", "sttool dup peer copy", scope="User")
		with _as(PEER):
			self.assertEqual(get_skill(f"{PFX}-dup-name")["description"], "sttool dup peer copy")


class TestLearnedMissAudit(SkillToolsTestCase):
	"""``_audit_learned_miss``: the throttled Error Log row for a missed
	``learned-`` fetch, and specifically its SITE SCOPING.

	The throttle key is built by hand as ``<prefix><site>:<slug>`` because the
	raw redis ``set`` it uses is NOT namespaced the way ``frappe.cache().
	set_value`` is (RedisWrapper.make_key prefixes with ``conf.db_name``;
	``redis.Redis.set`` prefixes with nothing). Every tenant bench on a shared
	redis carries the SAME handful of learned slugs, so without that qualifier
	the first tenant to miss ``learned-selling`` would silence every other
	tenant's audit row for ten minutes. That is a cross-tenant observability
	failure whose only symptom is a row that never appears, so nothing but a
	test can hold the qualifier in place.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# ``frappe.log_error`` calls the telemetry hook first, and that hook always
		# fails outside a request (``frappe.request`` is unbound), which sends it
		# down ``frappe.logger().error(...)`` -- a SITE-scoped rotating file
		# handler. Under a site name with no ``logs/`` directory that handler
		# cannot be constructed, the FileNotFoundError escapes log_error, and no
		# Error Log row is ever written. So give the two stand-in tenants the one
		# directory frappe insists on, and take it away again afterwards.
		for site in (AUDIT_SITE_A, AUDIT_SITE_B):
			os.makedirs(os.path.join(frappe.local.sites_path, site, "logs"), exist_ok=True)

	@classmethod
	def tearDownClass(cls):
		for site in (AUDIT_SITE_A, AUDIT_SITE_B):
			shutil.rmtree(os.path.join(frappe.local.sites_path, site), ignore_errors=True)
		super().tearDownClass()

	def _key(self, site: str, slug: str) -> str:
		return f"{_MISS_AUDIT_PREFIX}{site}:{slug}"

	def _forget(self, slug: str) -> None:
		"""Clear every key shape this slug could occupy.

		Includes the UNQUALIFIED shape a regression would write, so that running
		this suite against a reverted ``get_skill`` still leaves redis clean.
		"""
		cache = frappe.cache()
		keys = [f"{_MISS_AUDIT_PREFIX}{slug}"]
		keys += [self._key(s, slug) for s in (frappe.local.site, AUDIT_SITE_A, AUDIT_SITE_B)]
		for key in keys:
			cache.delete(key)

	def _audit_rows(self, slug: str) -> list[dict]:
		return frappe.get_all(
			ERROR_LOG,
			filters={"method": AUDIT_TITLE, "error": ["like", f"%{slug}%"]},
			fields=["name", "error"],
			ignore_permissions=True,
		)

	def _drop_audit_rows(self, slug: str) -> None:
		for row in self._audit_rows(slug):
			frappe.delete_doc(ERROR_LOG, row["name"], force=True, ignore_permissions=True)

	def _slug(self, suffix: str) -> str:
		"""A learned slug that starts every test with an empty throttle and an
		empty audit trail, and leaves both empty afterwards."""
		slug = f"{_LEARNED_PREFIX}{PFX}-{suffix}"
		self._forget(slug)
		self._drop_audit_rows(slug)
		self.addCleanup(self._drop_audit_rows, slug)
		self.addCleanup(self._forget, slug)
		return slug

	def test_two_sites_each_get_their_own_audit_row(self):
		"""THE regression: the same slug missed on two different sites must log
		TWICE. Drop ``frappe.local.site`` from the key and site B's row silently
		disappears into site A's throttle."""
		slug = self._slug("cross-site")

		with _site(AUDIT_SITE_A):
			_audit_learned_miss(slug, OWNER, "unknown")
		with _site(AUDIT_SITE_B):
			_audit_learned_miss(slug, OWNER, "denied")

		self.assertEqual(
			len(self._audit_rows(slug)),
			2,
			"one tenant's throttle swallowed another tenant's audit row",
		)
		# And the keys really are distinct, so neither site is reading the other's.
		# ``shared=True`` because RedisWrapper.exists() DOES run make_key while the
		# raw ``set`` under test does not: that asymmetry is the whole reason the
		# site qualifier has to be written by hand.
		cache = frappe.cache()
		self.assertTrue(cache.exists(self._key(AUDIT_SITE_A, slug), shared=True))
		self.assertTrue(cache.exists(self._key(AUDIT_SITE_B, slug), shared=True))

	def test_second_miss_on_the_same_site_is_throttled(self):
		"""Within one site the throttle still does its job: a tenant whose two role
		checks disagree on every turn gets one row, not one per turn."""
		slug = self._slug("throttled")

		with _site(AUDIT_SITE_A):
			_audit_learned_miss(slug, OWNER, "unknown")
			_audit_learned_miss(slug, OWNER, "denied")

		rows = self._audit_rows(slug)
		self.assertEqual(len(rows), 1)
		# The surviving row is the FIRST miss (nx=True never overwrites), and it
		# carries the reason and the caller, which is the whole diagnostic value.
		self.assertIn("unknown", rows[0]["error"])
		self.assertIn(OWNER, rows[0]["error"])
		# A key with no expiry would silence the audit forever, not for a window.
		ttl = frappe.cache().ttl(self._key(AUDIT_SITE_A, slug))
		self.assertGreater(ttl, 0)
		self.assertLessEqual(ttl, _MISS_AUDIT_TTL_S)

	def test_a_non_learned_slug_is_ignored_entirely(self):
		"""Narrow on purpose. A missed ``custom-`` slug is usually just the model
		guessing a name, so it must not reach the cache OR the log."""
		slug = f"custom-{PFX}-never-audited"
		self.addCleanup(self._drop_audit_rows, slug)

		with patch.object(frappe, "cache") as fake_cache:
			_audit_learned_miss(slug, OWNER, "unknown")
			fake_cache.assert_not_called()
		self.assertEqual(self._audit_rows(slug), [])

	def test_a_broken_cache_or_log_never_reaches_the_caller(self):
		"""The docstring's promise: an audit row must not fail a tool call. Both
		halves can fail independently on a real bench (redis down, Error Log
		table full), so break each one separately."""
		slug = self._slug("never-raises")
		# A separate slug for the caller-level check below: the broken-log call
		# above still WROTE the throttle key (the cache succeeded, the log did
		# not), so reusing that slug would take the early return and prove nothing.
		caller_slug = self._slug("broken-log")

		with patch.object(frappe, "cache", side_effect=RuntimeError("redis is down")):
			_audit_learned_miss(slug, OWNER, "unknown")
		with patch.object(frappe, "log_error", side_effect=RuntimeError("error log is full")):
			_audit_learned_miss(slug, OWNER, "unknown")

		# And through the real caller: the tool still fails the way it is supposed
		# to, with its own error, not with the audit's.
		with patch.object(frappe, "log_error", side_effect=RuntimeError("error log is full")):
			with _as(OWNER):
				with self.assertRaises(InvalidArgumentError):
					get_skill(caller_slug)

	def test_get_skill_audits_a_learned_miss_and_only_a_learned_miss(self):
		"""Wire check: the audit fires from the real tool path, on the branch that
		matters, and stays silent on the branch that does not."""
		learned = self._slug("wire-learned")
		custom = f"custom-{PFX}-wire-miss"
		self.addCleanup(self._drop_audit_rows, custom)

		with _as(OWNER):
			with self.assertRaises(InvalidArgumentError):
				get_skill(learned)
			with self.assertRaises(InvalidArgumentError):
				get_skill(custom)

		self.assertEqual(len(self._audit_rows(learned)), 1)
		self.assertEqual(self._audit_rows(custom), [])


class TestCreateCustomSkill(SkillToolsTestCase):
	def test_creates_private_by_default_with_note(self):
		with _as(OWNER):
			out = create_custom_skill(f"{PFX}-mk-default", "sttool mk desc", "mk body")
		self.assertEqual(out["scope"], "User")
		self.assertEqual(out["skill_name"], f"{PFX}-mk-default")
		self.assertIn("note", out)
		row = frappe.db.get_value(SKILL, out["name"], ["owner", "scope", "enabled"], as_dict=True)
		self.assertEqual(row.owner, OWNER)
		self.assertEqual(row.scope, "User")
		self.assertEqual(int(row.enabled), 1)

	def test_org_scope_request_is_capped_to_user(self):
		# Security review PART 2 TASK 10: the tool no longer mints a bench-wide
		# (Org) skill in one agent call. A scope="Org" request is honored as a
		# PRIVATE skill and the note explains promotion is reviewer-gated.
		with _as(OWNER):
			out = create_custom_skill(f"{PFX}-mk-org", "sttool mk org desc", "mk org body", scope="Org")
		self.assertEqual(out["scope"], "User")
		self.assertIn("reviewer", out["note"].lower())
		# The capped skill is private: a peer cannot see it via find_skills.
		with _as(PEER):
			found = find_skills("mk org desc")
			self.assertNotIn(f"{PFX}-mk-org", [s["skill_name"] for s in found["skills"]])

	def test_unknown_scope_is_capped_not_rejected(self):
		# The scope arg is advisory (always capped to User), so an unrecognized
		# value is not an error — it just yields a private skill.
		with _as(OWNER):
			out = create_custom_skill(f"{PFX}-mk-badscope", "d", "i", scope="Team")
		self.assertEqual(out["scope"], "User")

	def test_slug_and_cap_violations_surface_as_jarvis_errors(self):
		with _as(OWNER):
			# Bad slug grammar.
			with self.assertRaises(InvalidArgumentError):
				create_custom_skill("Bad Slug!", "sttool desc", "body")
			# Reserved wire prefix.
			with self.assertRaises(InvalidArgumentError):
				create_custom_skill("custom-sttool-x", "sttool desc", "body")
			# Length cap (description > 500).
			with self.assertRaises(InvalidArgumentError):
				create_custom_skill(f"{PFX}-mk-longdesc", "x" * 501, "body")
			# Duplicate per-owner name.
			create_custom_skill(f"{PFX}-mk-dup", "sttool desc", "body")
			with self.assertRaises(InvalidArgumentError):
				create_custom_skill(f"{PFX}-mk-dup", "sttool desc", "body")

	def test_gate_rejects_website_user(self):
		with _as(WEB):
			with self.assertRaises(PermissionDeniedError):
				create_custom_skill(f"{PFX}-mk-web", "d", "i")


class TestPushPayloadScope(SkillToolsTestCase):
	def test_personal_excluded_null_scope_pushed_as_org(self):
		_make_skill(OWNER, f"{PFX}-push-org", "sttool push org", scope="Org")
		_make_skill(OWNER, f"{PFX}-push-personal", "sttool push personal", scope="Personal")
		legacy = _make_skill(OWNER, f"{PFX}-push-legacy", "sttool push legacy", scope="Org")
		# Simulate a pre-migration row: scope NULL in the DB (validate() can't
		# normalize what never gets saved again).
		frappe.db.set_value(SKILL, legacy.name, "scope", None, update_modified=False)

		slugs = {p["slug"] for p in build_push_payload(owner=OWNER)}
		self.assertIn(prefixed_slug(f"{PFX}-push-org"), slugs)
		self.assertIn(prefixed_slug(f"{PFX}-push-legacy"), slugs)
		self.assertNotIn(prefixed_slug(f"{PFX}-push-personal"), slugs)


class TestPersonalSkillClause(SkillToolsTestCase):
	def test_zero_case_returns_empty(self):
		self.assertEqual(personal_skill_clause(THIRD), "")
		self.assertEqual(personal_skill_clause("Guest"), "")

	def test_clause_reads_cache(self):
		frappe.cache().set_value(personal_skills_cache_key(THIRD), 7, expires_in_sec=60)
		clause = personal_skill_clause(THIRD)
		self.assertIn("7 personal skill(s)", clause)
		self.assertIn("jarvis__find_skills", clause)

	def test_row_writes_invalidate_cache(self):
		# Prime the zero-case cache, then create a Personal row: the controller
		# on_update invalidation must make the next clause see it.
		self.assertEqual(personal_skill_clause(OWNER), "")
		with _as(OWNER):
			out = create_custom_skill(f"{PFX}-clause-one", "sttool clause desc", "clause body")
		self.assertIn("1 personal skill(s)", personal_skill_clause(OWNER))
		# Deleting the row invalidates again (on_trash).
		frappe.delete_doc(SKILL, out["name"], force=True, ignore_permissions=True)
		self.assertEqual(personal_skill_clause(OWNER), "")

	def test_org_rows_do_not_count(self):
		_make_skill(OWNER, f"{PFX}-clause-org", "sttool clause org", scope="Org")
		self.assertEqual(personal_skill_clause(OWNER), "")


class TestRegistryAndClassification(SkillToolsTestCase):
	def test_registry_lists_and_dispatches_new_tools(self):
		from jarvis.tools.registry import dispatch, list_tools

		tools = list_tools()
		for name in ("find_skills", "get_skill", "create_custom_skill", "read_wiki", "update_wiki"):
			self.assertIn(name, tools)

		with _as(OWNER):
			created = dispatch(
				"create_custom_skill",
				{
					"skill_name": f"{PFX}-via-registry",
					"description": "sttool registry marker",
					"instructions": "registry body",
				},
			)
			self.assertEqual(created["scope"], "User")

			found = dispatch("find_skills", {"query": "registry marker", "limit": 5})
			self.assertEqual([s["skill_name"] for s in found["skills"]], [f"{PFX}-via-registry"])

			got = dispatch("get_skill", {"skill_name": f"{PFX}-via-registry"})
			self.assertEqual(got["instructions"], "registry body")

	def test_classification_membership(self):
		from jarvis import api as jarvis_api

		for tool in ("create_custom_skill", "update_wiki"):
			self.assertIn(tool, jarvis_api._WRITE_TOOLS)
			self.assertIn(tool, jarvis_api._GATED_WRITES)
			self.assertNotIn(tool, jarvis_api._AUTO_APPLYABLE)
			self.assertNotIn(tool, jarvis_api._PREVIEWABLE)
		for tool in ("find_skills", "get_skill", "read_wiki"):
			self.assertNotIn(tool, jarvis_api._WRITE_TOOLS)
			self.assertNotIn(tool, jarvis_api._GATED_WRITES)


class TestReceiptRefStamping(SkillToolsTestCase):
	"""persist_tool_receipt stamps ref_doctype/ref_name via
	jarvis.chat.entities.refs_from_tool (best-effort, never breaks receipts)."""

	CONV_TITLE = "sttool receipt-ref test"

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Jarvis Conversation",
			filters={"title": self.CONV_TITLE},
			pluck="name",
		):
			frappe.delete_doc("Jarvis Conversation", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _conv(self) -> str:
		doc = frappe.get_doc({"doctype": "Jarvis Conversation", "title": self.CONV_TITLE})
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_receipt_carries_refs_from_args(self):
		from jarvis import api as jarvis_api

		conv = self._conv()
		jarvis_api.persist_tool_receipt(
			conv,
			"get_doc",
			{"doctype": "User", "name": "Administrator"},
			{"ok": True, "data": {"doctype": "User", "name": "Administrator"}},
		)
		row = frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": conv, "role": "tool"},
			fields=["ref_doctype", "ref_name", "tool_status"],
		)[0]
		self.assertEqual(row.ref_doctype, "User")
		self.assertEqual(row.ref_name, "Administrator")
		self.assertEqual(row.tool_status, "completed")

	def test_refless_call_still_persists_receipt(self):
		from jarvis import api as jarvis_api

		conv = self._conv()
		jarvis_api.persist_tool_receipt(
			conv,
			"run_report",
			{"report_name": "some-report"},
			{"ok": False, "error": {"code": "InvalidArgumentError", "message": "nope"}},
		)
		row = frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": conv, "role": "tool"},
			fields=["ref_doctype", "ref_name", "tool_status"],
		)[0]
		self.assertFalse(row.ref_doctype)
		self.assertFalse(row.ref_name)
		self.assertEqual(row.tool_status, "error")
