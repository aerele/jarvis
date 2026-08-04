"""Unit tests for ``user_can_use_skill`` (pattern-learning plan section 6.6).

Pure-logic tests: the helper accepts any dict-like row, and passing the child
tables + ``user_roles`` explicitly keeps these independent of the
``allowed_roles`` schema being migrated (Wave C wires the helper into the
listing/turn paths and adds the DB-backed integration coverage).
"""

import contextlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.custom_skills import learned_skill_clause
from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import user_can_use_skill

OWNER = "skill-owner@example.com"
PEER = "skill-peer@example.com"
STRANGER = "skill-stranger@example.com"
NONSM = "jcs-sec-nonsm@example.com"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _engine_flag():
	prev = frappe.flags.jarvis_pattern_engine
	frappe.flags.jarvis_pattern_engine = True
	try:
		yield
	finally:
		frappe.flags.jarvis_pattern_engine = prev


def _ensure_non_sm(email: str) -> str:
	"""A logged-in Jarvis User with NO System Manager role (created inside the
	test transaction, so it is rolled back with everything else). The Jarvis User
	role is what now grants Custom Skill create at the doctype layer (security
	review PART 2 TASK 13 replaced the old `All` grant), so a realistic non-SM
	author holds it — the point of these tests is that even a role-holding author
	cannot forge the managed flag / learned slug."""
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "jcs-sec",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	udoc = frappe.get_doc("User", email)
	if "System Manager" in set(frappe.get_roles(email)):
		udoc.remove_roles("System Manager")
	if "Jarvis User" not in set(frappe.get_roles(email)):
		udoc.add_roles("Jarvis User")
	return email


def _skill(**overrides):
	row = frappe._dict(
		name="jcs-visibility-test",
		owner=OWNER,
		shared_with=[],
		allowed_roles=[],
	)
	row.update(overrides)
	return row


class TestUserCanUseSkill(FrappeTestCase):
	def test_owner_passes_despite_role_mismatch(self):
		skill = _skill(allowed_roles=[{"role": "Sales User"}])
		self.assertTrue(user_can_use_skill(skill, OWNER, ["All"]))

	def test_shared_with_passes_despite_role_mismatch(self):
		skill = _skill(
			shared_with=[{"user": PEER}],
			allowed_roles=[{"role": "Sales User"}],
		)
		self.assertTrue(user_can_use_skill(skill, PEER, ["All"]))

	def test_empty_allowed_roles_means_everyone(self):
		self.assertTrue(user_can_use_skill(_skill(), STRANGER, ["All"]))

	def test_role_intersection_passes(self):
		skill = _skill(allowed_roles=[{"role": "Sales User"}, {"role": "Stock User"}])
		self.assertTrue(user_can_use_skill(skill, STRANGER, ["All", "Stock User"]))

	def test_role_mismatch_fails(self):
		skill = _skill(allowed_roles=[{"role": "Sales User"}])
		self.assertFalse(user_can_use_skill(skill, STRANGER, ["All", "Accounts User"]))

	def test_system_manager_always_passes(self):
		skill = _skill(allowed_roles=[{"role": "Sales User"}])
		self.assertTrue(user_can_use_skill(skill, "sm@example.com", ["System Manager"]))

	def test_administrator_always_passes(self):
		skill = _skill(allowed_roles=[{"role": "Sales User"}])
		self.assertTrue(user_can_use_skill(skill, "Administrator"))

	def test_child_rows_as_objects_and_strings(self):
		# Document child rows (attribute access) and pre-plucked strings both work.
		class Row:
			def __init__(self, role):
				self.role = role

		skill = _skill(allowed_roles=[Row("Stock User")])
		self.assertTrue(user_can_use_skill(skill, STRANGER, ["Stock User"]))
		skill = _skill(allowed_roles=["Stock User"])
		self.assertTrue(user_can_use_skill(skill, STRANGER, ["Stock User"]))
		self.assertFalse(user_can_use_skill(skill, STRANGER, ["Sales User"]))


class TestManagedFlagSecurity(FrappeTestCase):
	"""``managed_by_learning`` self-escalation guards (plan section 6.6 security).

	The compiler owns ``managed_by_learning`` and the ``learned-`` slug namespace;
	a normal author must not be able to forge either and have their row auto-
	injected into every user's turn by ``learned_skill_clause``.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.non_sm = _ensure_non_sm(NONSM)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _new(self, **kw):
		doc = frappe.new_doc("Jarvis Custom Skill")
		doc.update(
			{
				"skill_name": kw.pop("skill_name", "jcs-sec-skill"),
				"description": "security fixture",
				"instructions": "body",
				"enabled": 1,
				"user_invocable": 0,
			}
		)
		doc.update(kw)
		return doc

	def test_non_sm_cannot_set_managed_flag(self):
		# A non-SM author flipping managed_by_learning=1 on their OWN row is the
		# rejected escalation - validate() must throw before it can be injected.
		with _as(self.non_sm):
			doc = self._new(skill_name="jcs-sec-managed", managed_by_learning=1)
			with self.assertRaises(frappe.PermissionError):
				doc.insert()

	def test_non_admin_cannot_author_learned_slug(self):
		with _as(self.non_sm):
			doc = self._new(skill_name="learned-selling")
			with self.assertRaises(frappe.ValidationError):
				doc.insert()

	def test_engine_and_admin_can_author_learned_slug(self):
		# Engine flag: a real insert of learned-<domain> goes through (a fresh
		# owner keeps us under the per-owner cap; the production Administrator
		# owner is ~0 so the real compiler apply is likewise clear).
		with _as(self.non_sm), _engine_flag():
			doc = self._new(skill_name="learned-e2esec", managed_by_learning=1)
			doc.insert(ignore_permissions=True)
			self.assertTrue(frappe.db.exists("Jarvis Custom Skill", doc.name))
		# Administrator: the slug reservation is lifted even without the engine
		# flag. Checked in isolation so this dev site's >25 Administrator-owned
		# rows (the per-owner cap) cannot mask the reservation behaviour; a bare
		# _validate_slug() call raises nothing on success.
		with _as("Administrator"):
			self._new(skill_name="learned-admincheck")._validate_slug()

	def test_learned_clause_ignores_non_admin_managed_row(self):
		# Even force-set with the engine flag, a non-Administrator-owned managed
		# row is never injected: the clause query is pinned to owner=Administrator.
		with _as(self.non_sm), _engine_flag():
			doc = self._new(skill_name="learned-rogue", managed_by_learning=1)
			doc.insert(ignore_permissions=True)
		self.assertEqual(doc.owner, self.non_sm)
		self.assertNotIn("learned-rogue", learned_skill_clause(self.non_sm))
		self.assertNotIn("learned-rogue", learned_skill_clause("Administrator"))

	def test_learned_clause_emits_learned_namespace_slug(self):
		# Phase-2 namespace: the clause names the wire slug ``learned-<domain>``
		# VERBATIM (the managed row's skill_name) - never the pre-cutover
		# ``custom-learned-<domain>``. The persona interplay clause names both
		# prefixes, so agent-side behaviour is stable across the cutover.
		with _as("Administrator"), _engine_flag():
			doc = self._new(skill_name="learned-clausecheck", managed_by_learning=1)
			doc.insert(ignore_permissions=True)
		clause = learned_skill_clause("Administrator")
		self.assertIn("learned-clausecheck", clause)
		self.assertNotIn("custom-learned-clausecheck", clause)


# --------------------------------------------------------------------------- #
# issue #479: retrieval-gated learned skills
# --------------------------------------------------------------------------- #
FETCH_LEAD = "; these learned skills apply to you but are not loaded in this session:"
INSTALLED_LEAD = "; apply these learned skills:"

HOLDER = "jcs-479-holder@example.com"
OUTSIDER = "jcs-479-outsider@example.com"
HOLDER_ROLE = "Sales User"


def _ensure_role_user(email: str, role: str | None) -> str:
	"""An enabled System User holding ``role`` (or no extra role) and explicitly
	NOT System Manager, which auto-passes every visibility check and would make
	each assertion below pass for the wrong reason."""
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "jcs-479",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	held = set(frappe.get_roles(email))
	if "System Manager" in held:
		doc.remove_roles("System Manager")
	if role and role not in held:
		doc.add_roles(role)
	return email


class TestLearnedRetrievalGating(FrappeTestCase):
	"""Issue #479: a role-restricted learned body is never written into the
	shared, role-BLIND container. Its EXISTENCE is announced by
	``learned_skill_clause`` (already role-matched per chat user) and its BODY is
	served by ``jarvis__get_skill``, which re-derives the caller's roles at fetch
	time. The two role checks are the same predicate on the same identity, which
	is the property the pushed-file model could not offer at all.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.holder = _ensure_role_user(HOLDER, HOLDER_ROLE)
		cls.outsider = _ensure_role_user(OUTSIDER, None)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Jarvis Custom Skill", {"skill_name": ["like", "learned-479%"]})
		super().tearDown()

	def _managed(self, slug, roles=(), enabled=1):
		doc = frappe.new_doc("Jarvis Custom Skill")
		doc.update(
			{
				"skill_name": slug,
				"description": f"desc for {slug}",
				"instructions": f"# body of {slug}\n- the restricted rule",
				"enabled": enabled,
				"user_invocable": 0,
				"scope": "Org",
				"managed_by_learning": 1,
				"allowed_roles": [{"role": r} for r in roles],
			}
		)
		with _engine_flag():
			doc.insert(ignore_permissions=True)
		# insert() forces owner=session.user; the clause query is pinned to
		# Administrator, exactly as the compiler pins its managed rows.
		frappe.db.set_value("Jarvis Custom Skill", doc.name, "owner", "Administrator", update_modified=False)
		return doc.name

	@staticmethod
	def _halves(clause):
		"""``(installed_half, fetch_half)`` of a rendered clause."""
		head, _, tail = clause.partition(FETCH_LEAD)
		return head, tail

	# --- discovery ----------------------------------------------------------- #
	def test_restricted_learned_skill_lands_in_the_fetch_half_for_a_role_holder(self):
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		clause = learned_skill_clause(self.holder)
		installed, fetch = self._halves(clause)
		self.assertIn("learned-479restricted", fetch)
		self.assertNotIn("learned-479restricted", installed)
		self.assertIn("jarvis__get_skill", clause)
		# It must not imply container-side access to a body that is not there.
		self.assertNotIn("workspace", clause)

	def test_unrestricted_learned_skill_keeps_the_installed_clause(self):
		self._managed("learned-479open", roles=())
		installed, fetch = self._halves(learned_skill_clause(self.holder))
		self.assertIn(f"{INSTALLED_LEAD} ", installed)
		self.assertIn("learned-479open", installed)
		self.assertNotIn("learned-479open", fetch)

	def test_non_holder_is_told_about_neither(self):
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		self.assertNotIn("learned-479restricted", learned_skill_clause(self.outsider))

	def test_system_manager_is_not_told_a_restricted_row_is_on_disk(self):
		# The privileged branch used to short-circuit the child-row lookup, so
		# Administrator / System Manager alone were told a role-restricted skill
		# was installed. They skip the role INTERSECTION, never the emptiness test.
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		installed, fetch = self._halves(learned_skill_clause("Administrator"))
		self.assertIn("learned-479restricted", fetch)
		self.assertNotIn("learned-479restricted", installed)

	def test_one_turn_can_carry_both_learned_clause_shapes(self):
		self._managed("learned-479open", roles=())
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		installed, fetch = self._halves(learned_skill_clause(self.holder))
		self.assertIn("learned-479open", installed)
		self.assertNotIn("learned-479open", fetch)
		self.assertIn("learned-479restricted", fetch)
		self.assertNotIn("learned-479restricted", installed)

	# --- retrieval ----------------------------------------------------------- #
	def test_role_holder_can_fetch_the_restricted_body(self):
		# The whole design rests on this: discovery names it, retrieval serves it,
		# and the same predicate gates both.
		from jarvis.tools.get_skill import get_skill

		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		with _as(self.holder):
			out = get_skill("learned-479restricted")
		self.assertEqual(out["skill_name"], "learned-479restricted")
		self.assertIn("the restricted rule", out["instructions"])

	def test_non_holder_is_refused_the_restricted_body(self):
		from jarvis.exceptions import PermissionDeniedError
		from jarvis.tools.get_skill import get_skill

		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		with _as(self.outsider):
			with self.assertRaises(PermissionDeniedError):
				get_skill("learned-479restricted")

	def test_disabled_learned_row_is_not_fetchable(self):
		# find_skills filters enabled=1 and get_skill did not, so a disabled row
		# still served its body. Harmless until this tool became the ONLY delivery
		# path for a role-restricted body.
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools.get_skill import get_skill

		self._managed("learned-479off", roles=(HOLDER_ROLE,), enabled=0)
		with _as(self.holder):
			with self.assertRaises(InvalidArgumentError):
				get_skill("learned-479off")

	# --- the push, from the clause's side ------------------------------------ #
	def test_the_clause_split_predicate_is_the_push_filter(self):
		# The property #479 says the old design lacked: discovery and delivery
		# cannot disagree about which bodies are on disk, because one helper
		# answers both.
		from jarvis.chat.custom_skills import role_restricted_names
		from jarvis.learning.compiler import build_learned_push_payload

		restricted = self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		open_row = self._managed("learned-479open", roles=())
		self.assertEqual(role_restricted_names([restricted, open_row]), {restricted})
		slugs = {i["slug"] for i in build_learned_push_payload()}
		self.assertIn("learned-479open", slugs)
		self.assertNotIn("learned-479restricted", slugs)

	# --- migration: evict bodies already sitting in live containers ---------- #
	def _run_patch(self, sync_status):
		from unittest import mock

		from jarvis.patches.v2_13_repush_learned_skills_role_gated import execute

		before = frappe.db.get_single_value("Jarvis Settings", "learned_skills_sync_status")
		frappe.db.set_single_value(
			"Jarvis Settings", "learned_skills_sync_status", sync_status, update_modified=False
		)
		try:
			with mock.patch("jarvis.chat.learned_skills_api.enqueue_learned_skills_push") as enqueue:
				execute()
			return enqueue
		finally:
			frappe.db.set_single_value(
				"Jarvis Settings", "learned_skills_sync_status", before, update_modified=False
			)

	def test_patch_repushes_when_a_restricted_body_is_already_in_the_container(self):
		# The code fix closes the tap; it cannot remove what is already on disk.
		# One full-reconcile push per affected tenant deletes the dir, drops the
		# slug from the agent allowlist and restarts the container (the restart is
		# what rebuilds workspace/skills/ from the now-smaller source dir).
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		self._run_patch("ok (1 installed via admin)").assert_called_once()

	def test_patch_skips_a_bench_that_never_pushed(self):
		# Empty sync status: nothing of ours is in that container, so a restart
		# would be pure cost.
		self._managed("learned-479restricted", roles=(HOLDER_ROLE,))
		self._run_patch("").assert_not_called()

	def test_patch_skips_a_bench_with_nothing_restricted(self):
		self._managed("learned-479open", roles=())
		self._run_patch("ok (1 installed via admin)").assert_not_called()
