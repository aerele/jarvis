"""Security review PART 2 — exploit reproductions + fix proofs.

Covers the Custom Skill scope ladder + guards + ORM hook (TASK 10, 13), the
role-restricted-body push exclusion (TASK 11), the reviewer/admin apply gate
(TASK 12), the wiki-promotion-request generic-REST leak (TASK 14), the
personalise-origin scrub provenance (TASK 16), the personalise-question
`user`-keyed ORM hook (TASK 17) and four-eyes on promotion (TASK 19).

Every test runs inside the FrappeTestCase transaction (rolled back), and each
exploit is exercised through the SAME door a real attacker would use — a
perm-checked ``doc.insert()`` / ``doc.save()`` (the generic-REST path) or the
whitelisted endpoint — NOT ``ignore_permissions`` (which would mask the gate).
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.custom_skills import build_push_payload, prefixed_slug

SKILL = "Jarvis Custom Skill"
RESV = "Jarvis Shared Skill Slug"
WIKI = "Jarvis Wiki Page"
WIKI_PROMO = "Jarvis Wiki Promotion Request"
SKILL_PROMO = "Jarvis Skill Promotion Request"
QUESTION = "Jarvis Personalise Question"

REVIEWER = "p2-reviewer@example.com"
USER_A = "p2-usera@example.com"
USER_B = "p2-userb@example.com"
PFX = "p2sec"


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


def _ensure_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User")
	# Reload AFTER the db.set_value so add/remove_roles don't trip the timestamp
	# concurrency check; each roles mutation saves, so reload between them.
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
	add = [r for r in roles if r not in set(frappe.get_roles(email))]
	if add:
		frappe.get_doc("User", email).add_roles(*add)
	return email


def _sweep():
	"""Committing cleanup: several endpoints under test call frappe.db.commit()
	(approve/promote), so their rows survive the per-test rollback and would
	otherwise pollute later runs (and trip the unique pattern_key). Roll back
	uncommitted work first, then hard-delete + commit the committed residue."""
	frappe.db.rollback()
	# Reservation rows (SR4-2) are named after the bare slug, so a PFX-slug reservation
	# is a p2sec-... row; drop them first so the skill delete below never trips over a
	# lingering reservation and so a committed migration reservation is cleaned.
	if frappe.db.table_exists(RESV):
		for n in frappe.get_all(RESV, filters={"slug": ["like", f"{PFX}-%"]}, pluck="name"):
			frappe.delete_doc(RESV, n, force=True, ignore_permissions=True)
	for dt, filt in (
		(SKILL, {"skill_name": ["like", f"{PFX}-%"]}),
		(WIKI, {"slug": ["like", f"{PFX}-%"]}),
		("Jarvis Learned Pattern", {"pattern_key": ["like", f"{PFX}-%"]}),
	):
		for n in frappe.get_all(dt, filters=filt, pluck="name"):
			frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
	for dt in (SKILL_PROMO, WIKI_PROMO, QUESTION):
		frappe.db.delete(dt, {"owner": ["in", [REVIEWER, USER_A, USER_B]]})
	frappe.db.commit()


def _mk_skill(
	owner, skill_name, *, scope="User", allowed_roles=None, target_role=None, shared_with=None, enabled=1
):
	"""Mint a skill owned by ``owner`` at any scope (engine flag bypasses the
	creation guard — that guard is proven separately)."""
	with _as(owner), _engine_flag():
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": skill_name,
				"description": f"{skill_name} desc",
				"instructions": "body",
				"scope": scope,
				"enabled": enabled,
				"user_invocable": 1,
				"target_role": target_role,
				"shared_with": [{"user": u} for u in (shared_with or [])],
				"allowed_roles": [{"role": r} for r in (allowed_roles or [])],
			}
		)
		doc.insert(ignore_permissions=True)
	return doc


class Part2Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_sweep()
		_ensure_user(REVIEWER, ["Jarvis User", "Jarvis Skill Reviewer"])
		_ensure_user(USER_A, ["Jarvis User", "Sales User"])
		_ensure_user(USER_B, ["Jarvis User"])

	def tearDown(self):
		frappe.set_user("Administrator")
		_sweep()
		super().tearDown()


class TestScopeCreationGuard(Part2Base):
	"""TASK 10: new skills default User; a non-reviewer cannot mint or widen a
	Role/Org skill by any door."""

	def test_default_scope_is_user(self):
		with _as(USER_A):
			doc = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": f"{PFX}-default",
					"description": "d",
					"instructions": "i",
				}
			)
			doc.insert()
		self.assertEqual(frappe.db.get_value(SKILL, doc.name, "scope"), "User")

	def test_non_reviewer_cannot_create_org_skill(self):
		with _as(USER_A):
			doc = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": f"{PFX}-orgcreate",
					"description": "d",
					"instructions": "i",
					"scope": "Org",
				}
			)
			with self.assertRaises(frappe.PermissionError):
				doc.insert()

	def test_non_reviewer_cannot_flip_scope_via_save(self):
		# The frappe.client.set_value / REST-PUT path both go through doc.save().
		doc = _mk_skill(USER_A, f"{PFX}-flip", scope="User")
		with _as(USER_A):
			reloaded = frappe.get_doc(SKILL, doc.name)
			reloaded.scope = "Org"
			with self.assertRaises(frappe.PermissionError):
				reloaded.save()
		self.assertEqual(frappe.db.get_value(SKILL, doc.name, "scope"), "User")

	def test_reviewer_can_create_org_skill(self):
		with _as(REVIEWER):
			doc = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": f"{PFX}-revorg",
					"description": "d",
					"instructions": "i",
					"scope": "Org",
				}
			)
			doc.insert()
		self.assertEqual(frappe.db.get_value(SKILL, doc.name, "scope"), "Org")

	def test_tool_caps_scope_at_user(self):
		from jarvis.tools.create_custom_skill import create_custom_skill

		with _as(USER_A):
			out = create_custom_skill(f"{PFX}-toolorg", "desc", "instr", scope="Org")
		self.assertEqual(out["scope"], "User")
		self.assertEqual(frappe.db.get_value(SKILL, out["name"], "scope"), "User")

	def test_owner_can_narrow_own_scope(self):
		# TASK E2: narrowing (Org->User) reduces visibility, so the owner may
		# self-demote without a reviewer; only widening stays reviewer-gated.
		skill = _mk_skill(USER_A, f"{PFX}-narrow", scope="Org")
		with _as(USER_A):
			doc = frappe.get_doc(SKILL, skill.name)
			doc.scope = "User"
			doc.save()
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")

	def test_non_owner_update_gets_clear_message(self):
		# TASK E1: a non-owner write is denied with a CLEAR message (not an
		# empty-string PermissionError).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_B, f"{PFX}-e1", scope="Org")
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError) as ctx:
				custom_skills_api.update_custom_skill(name=skill.name, description="hax")
		self.assertIn("your own skills", str(ctx.exception))


class TestSkillOrmHook(Part2Base):
	"""TASK 13: generic-REST (get_list) returns the SAME visibility set as the
	tools/SPA — owner OR scope=Org OR scope=Role role-match OR shared."""

	def test_get_list_scopes_to_visible_set(self):
		own = _mk_skill(USER_A, f"{PFX}-own", scope="User")
		org = _mk_skill(USER_B, f"{PFX}-org", scope="Org")
		role = _mk_skill(USER_B, f"{PFX}-role", scope="Role", target_role="Sales User")
		other_private = _mk_skill(USER_B, f"{PFX}-bpriv", scope="User")
		other_role_nomatch = _mk_skill(USER_B, f"{PFX}-rolex", scope="Role", target_role="Stock User")
		shared = _mk_skill(USER_B, f"{PFX}-shared", scope="User", shared_with=[USER_A])

		with _as(USER_A):
			names = set(
				frappe.get_list(
					SKILL, filters={"skill_name": ["like", f"{PFX}-%"]}, pluck="name", limit_page_length=0
				)
			)
		self.assertIn(own.name, names)  # own
		self.assertIn(org.name, names)  # org = everyone
		self.assertIn(role.name, names)  # role-match (USER_A is Sales User)
		self.assertIn(shared.name, names)  # shared with me
		self.assertNotIn(other_private.name, names)  # another user's private
		self.assertNotIn(other_role_nomatch.name, names)  # role I don't hold

	def test_has_permission_denies_foreign_private_read(self):
		bpriv = _mk_skill(USER_B, f"{PFX}-bpriv2", scope="User")
		with _as(USER_A):
			self.assertFalse(frappe.has_permission(SKILL, "read", bpriv.name))
			# write on a foreign row is denied too (owner-only).
			org = _mk_skill(USER_B, f"{PFX}-borg", scope="Org")
			self.assertFalse(frappe.has_permission(SKILL, "write", org.name))


class TestRoleRestrictedPushExclusion(Part2Base):
	"""TASK 11: a role-restricted Org body is never written to the shared,
	role-blind container push."""

	def test_role_restricted_org_skill_excluded_from_push(self):
		_mk_skill(REVIEWER, f"{PFX}-pushplain", scope="Org")
		_mk_skill(REVIEWER, f"{PFX}-pushrestricted", scope="Org", allowed_roles=["Sales User"])
		slugs = {p["slug"] for p in build_push_payload()}
		self.assertIn(prefixed_slug(f"{PFX}-pushplain"), slugs)
		self.assertNotIn(prefixed_slug(f"{PFX}-pushrestricted"), slugs)


class TestApplyGate(Part2Base):
	"""TASK 12: a plain Jarvis User cannot trigger a bench-wide apply."""

	def test_plain_user_apply_denied(self):
		from jarvis.chat import custom_skills_api

		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.apply_custom_skills()

	def test_reviewer_apply_passes_gate(self):
		from jarvis.chat import custom_skills_api

		with patch.object(custom_skills_api, "_apply_custom_skills_impl", return_value={"ok": True}) as impl:
			with _as(REVIEWER):
				out = custom_skills_api.apply_custom_skills()
		self.assertTrue(out["ok"])
		impl.assert_called_once()


class TestSkillPromotionWorkflow(Part2Base):
	"""TASK 10 promotion workflow + TASK 19 four-eyes."""

	def test_request_then_reviewer_publishes_shared_copy(self):
		# CDX-SP-1 materialize model: approval PUBLISHES a new system-owned Org copy
		# from the reviewed snapshot; the requester's OWN skill stays a private,
		# intact User row (it is NOT re-scoped in place).
		from jarvis.chat import custom_skills, custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-promote", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", "PROMOTE-BODY-v1")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		self.assertTrue(req["ok"])
		# Still private until a reviewer decides.
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		# The requester's OWN skill is untouched (private + still theirs).
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "owner"), USER_A)
		# A SEPARATE system-owned Org copy carries EXACTLY the reviewed snapshot and
		# is the one that reaches the shared container.
		shared = frappe.get_all(
			SKILL,
			filters={"skill_name": f"{PFX}-promote", "scope": "Org"},
			fields=["name", "owner", "instructions"],
		)
		self.assertEqual(len(shared), 1)
		self.assertEqual(shared[0].owner, "Administrator")
		self.assertEqual(shared[0].instructions, "PROMOTE-BODY-v1")
		self.assertEqual(out["skill"], f"{PFX}-promote")
		self.assertIn(
			prefixed_slug(f"{PFX}-promote"), {p["slug"] for p in custom_skills.build_push_payload()}
		)

	def test_requester_cannot_self_approve(self):
		from jarvis.chat import custom_skills_api

		# REVIEWER owns the skill AND requests promotion, then tries to decide.
		skill = _mk_skill(REVIEWER, f"{PFX}-selfpromote", scope="User")
		with _as(REVIEWER):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")

	def test_non_reviewer_cannot_decide(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-nodecide", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.decide_skill_promotion(req["request"], 1)

	def test_reviewer_discovers_pending_requests(self):
		# TASK D: a reviewer-only user must be able to SEE pending requests (the
		# doctype perms are owner-scoped, so a reviewer needs the gated list
		# endpoint to discover a JSPR they didn't create).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-discover", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			res = custom_skills_api.list_skill_promotion_requests(status="Pending")
		names = [r["name"] for r in res["rows"]]
		self.assertIn(req["request"], names)
		row = next(r for r in res["rows"] if r["name"] == req["request"])
		self.assertEqual(row["requested_by"], USER_A)
		self.assertEqual(row["to_scope"], "Org")
		# A plain (non-reviewer) user cannot list the queue.
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.list_skill_promotion_requests()


class TestWikiPromotionRequestLeak(Part2Base):
	"""TASK 14: a generic-REST insert of a promotion request pointing at another
	user's private wiki page must not snapshot that page's body."""

	def _mk_private_page(self, owner, slug, body):
		with _as(owner):
			doc = frappe.get_doc(
				{
					"doctype": WIKI,
					"slug": slug,
					"title": slug,
					"page_type": "Process",
					"scope": "User",
					"target_user": owner,
					"body_md": body,
					"status": "Active",
				}
			)
			doc.insert(ignore_permissions=True)
		return doc

	def _promo(self, page):
		return frappe.get_doc(
			{
				"doctype": WIKI_PROMO,
				"page": page,
				"from_scope": "User",
				"to_scope": "Org",
				"status": "Pending",
			}
		)

	def test_foreign_private_page_snapshot_blocked_via_rest(self):
		# Generic-REST insert (no ignore_permissions): the dropped `All: create`
		# grant denies it outright.
		victim_page = self._mk_private_page(USER_B, f"{PFX}-secret", "TOP SECRET personal note")
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				self._promo(victim_page.name).insert()

	def test_foreign_private_page_snapshot_blocked_even_under_ignore_perms(self):
		# Defense in depth: even a create-perm-bypassing server path is blocked by
		# the before_insert read guard, so the victim's body is never snapshotted.
		victim_page = self._mk_private_page(USER_B, f"{PFX}-secret2", "TOP SECRET personal note")
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				self._promo(victim_page.name).insert(ignore_permissions=True)

	def test_owner_can_still_snapshot_own_page(self):
		# The legit endpoint path inserts with ignore_permissions; the guard must
		# not over-block the owner reading their own page.
		mine = self._mk_private_page(USER_A, f"{PFX}-mine", "my own note body")
		with _as(USER_A):
			req = self._promo(mine.name)
			req.insert(ignore_permissions=True)
		self.assertEqual(req.body_snapshot, "my own note body")


class TestPersonaliseQuestionHook(Part2Base):
	"""TASK 17: generic-REST list of personalise questions is scoped on the
	`user` field, surviving owner/user drift."""

	def _mk_question(self, user, owner=None):
		doc = frappe.get_doc(
			{
				"doctype": QUESTION,
				"user": user,
				"question": f"{PFX} question for {user}",
				"origin": "From your organisation",
				"status": "Unanswered",
			}
		)
		doc.insert(ignore_permissions=True)
		if owner and owner != doc.owner:
			frappe.db.set_value(QUESTION, doc.name, "owner", owner, update_modified=False)
		return doc

	def test_list_scoped_on_user_not_owner(self):
		mine = self._mk_question(USER_A)
		theirs = self._mk_question(USER_B)
		# Drift: a row whose `user` is B but `owner` was flipped to A must NOT
		# leak to A (the hook keys on `user`).
		drift = self._mk_question(USER_B, owner=USER_A)
		with _as(USER_A):
			names = set(
				frappe.get_list(
					QUESTION, filters={"question": ["like", f"{PFX}%"]}, pluck="name", limit_page_length=0
				)
			)
		self.assertIn(mine.name, names)
		self.assertNotIn(theirs.name, names)
		self.assertNotIn(drift.name, names)


class TestPersonaliseOriginProvenance(Part2Base):
	"""TASK 16: a personalise-origin pattern carries a scrub warning to the
	reviewer on the board + the promote action."""

	def _mk_pattern(self, key, personalise_origin):
		with _engine_flag():
			doc = frappe.get_doc(
				{
					"doctype": "Jarvis Learned Pattern",
					"pattern_key": key,
					"detector_id": "voice_facts",
					"domain": "selling",
					"pattern_statement": f"{PFX} rule statement",
					"skill_draft": "- do the thing",
					"status": "Proposed",
					"strength_band": "High",
					"sensitivity": "A",
					"effective_sensitivity": "A",
					"surfaced": 1,
					"personalise_origin": 1 if personalise_origin else 0,
				}
			)
			doc.insert(ignore_permissions=True)
		return doc

	def test_board_row_carries_scrub_warning(self):
		from jarvis.chat import learned_api

		p = self._mk_pattern(f"{PFX}-po-key", personalise_origin=True)
		with _as(REVIEWER):
			res = learned_api.list_learned_patterns_page(status="Proposed", search=f"{PFX}")
		row = next(r for r in res["rows"] if r["name"] == p.name)
		self.assertEqual(row["personalise_origin"], 1)
		self.assertTrue(row["scrub_warning"])

	def test_approve_returns_scrub_warning_for_personalise_origin(self):
		# Run as Administrator: approving writes the SM-only JLP (the reviewer
		# save-perm mechanics are orthogonal to this provenance check).
		from jarvis.chat import learned_api

		p = self._mk_pattern(f"{PFX}-po-appr", personalise_origin=True)
		with _as("Administrator"):
			out = learned_api.approve_learned_pattern(p.name)
		self.assertIn("scrub_warning", out)

	def test_no_warning_for_non_personalise_pattern(self):
		from jarvis.chat import learned_api

		p = self._mk_pattern(f"{PFX}-nopo", personalise_origin=False)
		with _as("Administrator"):
			out = learned_api.approve_learned_pattern(p.name)
		self.assertNotIn("scrub_warning", out)


class TestSkillPromotionSurfacing(Part2Base):
	"""Skills-area promotion SURFACING (wiring the existing backend to the UI): the
	tab badge count, the reviewer content preview + push-budget context, and the
	requester-side status read + role picker the SPA needs. The request/decide/list
	endpoints already existed and are covered above; these are the smallest reads
	added to make them usable, plus the badge count."""

	def test_review_access_counts_pending_skill_promotions(self):
		from jarvis.chat import custom_skills_api, learned_api

		skill = _mk_skill(USER_A, f"{PFX}-badge", scope="User")
		with _as(REVIEWER):
			before = learned_api.get_review_access().get("pending_skill_promotions", 0)
		with _as(USER_A):
			custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			after = learned_api.get_review_access()
		self.assertEqual(after["pending_skill_promotions"], before + 1)

	def test_get_custom_skill_exposes_scope_fields(self):
		# The requester UI gates "Request promotion…" on scope=User + not-learned,
		# so get_custom_skill must expose them.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-scopefield", scope="User")
		with _as(USER_A):
			out = custom_skills_api.get_custom_skill(skill.name)
		self.assertEqual(out["scope"], "User")
		self.assertEqual(out["managed_by_learning"], 0)
		self.assertIn("target_role", out)

	def test_reviewer_list_carries_body_excerpt_and_budget(self):
		# A reviewer can read a User-scope skill's BODY via the gated list even
		# though get_custom_skill denies them (owner/shared only) — the content
		# preview the decide UI needs — plus the push-budget context for ruling 2.
		from jarvis.chat import custom_skills, custom_skills_api

		secret = "SECRET-INSTRUCTIONS-marker-9f3"
		skill = _mk_skill(USER_A, f"{PFX}-excerpt", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", secret)
		with _as(USER_A):
			custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			res = custom_skills_api.list_skill_promotion_requests(status="Pending")
		row = next(r for r in res["rows"] if r["skill"] == skill.name)
		self.assertIn(secret, row["body_excerpt"])
		self.assertEqual(res["push_budget"], custom_skills.MAX_SKILLS_PER_PUSH)
		self.assertEqual(res["push_count"], custom_skills.pushable_org_skill_count())

	def test_push_count_reflects_only_pushable_org_skills(self):
		from jarvis.chat import custom_skills

		base = custom_skills.pushable_org_skill_count()
		_mk_skill(USER_A, f"{PFX}-org1", scope="Org", enabled=1)
		_mk_skill(USER_A, f"{PFX}-org2", scope="Org", enabled=1)
		# role-restricted Org row is NOT pushable (scope-exfil guard) → excluded
		_mk_skill(USER_A, f"{PFX}-orgrestr", scope="Org", enabled=1, allowed_roles=["Sales User"])
		# disabled Org row is NOT pushable → excluded
		_mk_skill(USER_A, f"{PFX}-orgoff", scope="Org", enabled=0)
		self.assertEqual(custom_skills.pushable_org_skill_count(), base + 2)

	def test_my_skill_promotion_is_owner_scoped(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-mine", scope="User")
		with _as(USER_A):
			self.assertEqual(custom_skills_api.my_skill_promotion(skill.name), {})
			req = custom_skills_api.request_skill_promotion(skill.name, "Role", target_role="Sales User")
			mine = custom_skills_api.my_skill_promotion(skill.name)
		self.assertEqual(mine["name"], req["request"])
		self.assertEqual(mine["status"], "Pending")
		self.assertEqual(mine["to_scope"], "Role")
		self.assertEqual(mine["target_role"], "Sales User")
		# another user gets nothing for the same skill (owner-scoped read)
		with _as(USER_B):
			self.assertEqual(custom_skills_api.my_skill_promotion(skill.name), {})

	def test_my_skill_promotion_reflects_decision(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-decided", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req["request"], 1, note="looks good")
		with _as(USER_A):
			mine = custom_skills_api.my_skill_promotion(skill.name)
		self.assertEqual(mine["status"], "Approved")
		self.assertEqual(mine["reviewer"], REVIEWER)
		self.assertEqual(mine["decision_note"], "looks good")

	def test_promotable_target_roles_are_own_targetable_roles(self):
		from jarvis.chat import custom_skills_api

		with _as(USER_A):
			roles = custom_skills_api.promotable_target_roles()["roles"]
		self.assertIn("Sales User", roles)  # USER_A holds Sales User
		self.assertNotIn("All", roles)
		self.assertNotIn("System Manager", roles)
		self.assertNotIn("Administrator", roles)

	# ── negative gates: the reviewer reads refuse a non-reviewer ────────────────
	def test_non_reviewer_denied_on_reviewer_reads(self):
		# USER_B holds only "Jarvis User" — NOT a reviewer role. Both the reviewer
		# discovery list AND the Review-tab badge probe must refuse them (the
		# review surface is reviewer-gated end to end).
		from jarvis.chat import custom_skills_api, learned_api

		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.list_skill_promotion_requests(status="Pending")
			with self.assertRaises(frappe.PermissionError):
				learned_api.get_review_access()

	def test_get_custom_skill_denies_stranger_even_with_scope_fields(self):
		# The new scope/target_role fields on get_custom_skill must NOT open a read
		# path: a third user who is neither the owner nor a share target is still
		# refused (owner/shared-only), fields or no fields.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-stranger", scope="User")
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.get_custom_skill(skill.name)

	# ── SAR-1 server belt: a stored wiki title can never carry markup ───────────
	def test_wiki_title_is_html_neutralized_at_write_funnel(self):
		# The reviewer's promotion-approve confirm renders the wiki title via
		# v-html; if a requester could store `<img onerror=…>` in a title, it would
		# run in the reviewer's privileged session on Approve. The write-funnel
		# sanitizer strips it, so the stored title is HTML-free. This is the
		# authoritative server-side XSS-closed proof (not just a JS assertion).
		with _as(USER_A):
			doc = frappe.get_doc(
				{
					"doctype": WIKI,
					"slug": f"{PFX}-xss",
					"title": "Safe Title <img src=x onerror=alert(1)>",
					"page_type": "Process",
					"scope": "User",
					"target_user": USER_A,
					"body_md": "body",
					"status": "Active",
				}
			)
			doc.insert(ignore_permissions=True)
		stored = frappe.db.get_value(WIKI, doc.name, "title")
		self.assertNotIn("<img", stored)
		self.assertNotIn("onerror", stored)
		self.assertNotIn("<", stored)
		self.assertNotIn(">", stored)
		self.assertIn("Safe Title", stored)  # legitimate text is preserved

	# ── SAR-3: body_excerpt over-reads only Pending private bodies ──────────────
	def test_body_excerpt_only_for_pending_rows(self):
		# A Pending row carries the current instructions excerpt (the reviewer
		# needs it to decide); a decided (Rejected/Approved) row must NOT over-read
		# the requester's now-private body — its excerpt is empty.
		from jarvis.chat import custom_skills_api

		secret = "SECRET-body-marker-4b2"
		skill = _mk_skill(USER_A, f"{PFX}-decidedbody", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", secret)
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			pending = custom_skills_api.list_skill_promotion_requests(status="Pending")
		prow = next(r for r in pending["rows"] if r["skill"] == skill.name)
		self.assertIn(secret, prow["body_excerpt"])
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req["request"], 0, note="no")
			decided = custom_skills_api.list_skill_promotion_requests(status="Rejected")
		drow = next(r for r in decided["rows"] if r["skill"] == skill.name)
		self.assertEqual(drow["body_excerpt"], "")

	# ── decide idempotency: an already-decided request is a no-op ───────────────
	def test_decide_already_decided_is_noop(self):
		# TOCTOU-safe re-read: the second decide of the same request returns
		# {ok: False, reason: …} rather than re-applying — this is exactly the
		# stale-card signal the SPA now refreshes on (SPX-6).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-twice", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			first = custom_skills_api.decide_skill_promotion(req["request"], 1, note="ok")
			second = custom_skills_api.decide_skill_promotion(req["request"], 0, note="again")
		self.assertEqual(first.get("status"), "Approved")
		self.assertIs(second.get("ok"), False)
		self.assertIn("reason", second)


class TestSkillPromotionContentBinding(Part2Base):
	"""CDX-SP-1: the approval is bound to an IMMUTABLE snapshot of the full content,
	so the reviewer decides on exactly what the Role/Org audience ends up running —
	no truncated excerpt, no edit-between-request-and-approval swap, no post-approval
	rewrite by the requester. The four-eyes bypass Codex flagged is closed."""

	def test_reviewer_sees_full_snapshot_past_truncation(self):
		# Attack (a): content hidden past the old 300-char excerpt. The reviewer read
		# now returns the FULL bound snapshot, so a marker at char 400 is visible.
		from jarvis.chat import custom_skills_api

		marker = "HIDDEN-PAST-300-e7c1"
		body = ("x" * 400) + marker
		skill = _mk_skill(USER_A, f"{PFX}-longbody", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", body)
		with _as(USER_A):
			custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			res = custom_skills_api.list_skill_promotion_requests(status="Pending")
		row = next(r for r in res["rows"] if r["skill"] == skill.name)
		self.assertIn(marker, row["body_excerpt"])
		self.assertEqual(row["body_excerpt"], body)  # the FULL snapshot, not instr[:300]

	def test_edit_between_request_and_approval_promotes_snapshot_not_live(self):
		# Attack (b): the requester edits the skill AFTER filing the request. The
		# audience must get the REVIEWED snapshot (v1), never the post-request edit
		# (v2) — and the requester keeps their v2 privately (no data loss).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-edited", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", "REVIEWED-v1")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
			custom_skills_api.update_custom_skill(name=skill.name, instructions="SNEAKED-v2")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		shared = frappe.get_all(
			SKILL, filters={"skill_name": f"{PFX}-edited", "scope": "Org"}, fields=["instructions"]
		)
		self.assertEqual(len(shared), 1)
		self.assertEqual(shared[0].instructions, "REVIEWED-v1")  # audience = reviewed content
		# requester's private skill keeps their own edit, still User-scope + intact
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "instructions"), "SNEAKED-v2")
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")

	def test_requester_cannot_edit_published_shared_skill(self):
		# Attack (c): after approval, the requester tries to rewrite the shared copy.
		# They do NOT own it (it is system-owned) → the write endpoint refuses them.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-nopost", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", "REVIEWED")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req["request"], 1)
		shared_name = frappe.get_all(
			SKILL, filters={"skill_name": f"{PFX}-nopost", "scope": "Org"}, pluck="name"
		)[0]
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.update_custom_skill(name=shared_name, instructions="HACKED")
		self.assertEqual(frappe.db.get_value(SKILL, shared_name, "instructions"), "REVIEWED")

	def test_guard_blocks_nonreviewer_content_change_on_shared_skill(self):
		# _guard_content_change: even the OWNER of a Role/Org skill cannot change the
		# content the audience runs without a reviewer — they must narrow it back to
		# private first (fewer viewers is always safe), then edit + re-request.
		skill = _mk_skill(USER_A, f"{PFX}-locked", scope="Org")
		with _as(USER_A):
			doc = frappe.get_doc(SKILL, skill.name)
			doc.instructions = "REWRITTEN without review"
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		self.assertNotEqual(
			frappe.db.get_value(SKILL, skill.name, "instructions"), "REWRITTEN without review"
		)
		# narrow to private (safe), then the owner may edit their now-User skill
		with _as(USER_A):
			doc = frappe.get_doc(SKILL, skill.name)
			doc.scope = "User"
			doc.save()
			doc.instructions = "now editable as a private skill"
			doc.save()
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		self.assertEqual(
			frappe.db.get_value(SKILL, skill.name, "instructions"), "now editable as a private skill"
		)

	def test_reviewer_may_change_shared_skill_content(self):
		# The guard binds CONTENT to the four-eyes authority (a reviewer), mirroring
		# _guard_scope_change — a reviewer/admin may still maintain a shared skill.
		skill = _mk_skill(USER_A, f"{PFX}-revedit", scope="Org")
		with _as(REVIEWER):
			doc = frappe.get_doc(SKILL, skill.name)
			doc.instructions = "reviewer-maintained content"
			doc.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value(SKILL, skill.name, "instructions"), "reviewer-maintained content"
		)

	def test_preflight_endpoint_gated_and_scope_aware(self):
		# CDX-SP-2: a fresh, reviewer-gated projection recomputed at decision time.
		# Org → a projection; Role → None (never reaches the push); non-reviewer → 403.
		from jarvis.chat import custom_skills_api

		org_skill = _mk_skill(USER_A, f"{PFX}-preflight", scope="User")
		role_skill = _mk_skill(USER_A, f"{PFX}-preflightrole", scope="User")
		with _as(USER_A):
			req_org = custom_skills_api.request_skill_promotion(org_skill.name, "Org")
			req_role = custom_skills_api.request_skill_promotion(
				role_skill.name, "Role", target_role="Sales User"
			)
		with _as(REVIEWER):
			org_pre = custom_skills_api.preflight_skill_promotion(req_org["request"])
			role_pre = custom_skills_api.preflight_skill_promotion(req_role["request"])
		self.assertIsNotNone(org_pre["push_projection"])
		self.assertEqual(org_pre["to_scope"], "Org")
		self.assertIsNone(role_pre["push_projection"])
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.preflight_skill_promotion(req_org["request"])


class TestPromotionPushProjection(FrappeTestCase):
	"""CDX-SP-2: the server-side push projection tells the TRUTH per mode — the
	interactive Apply FAILS over the cap (nothing pushed), and the unattended sync
	drops the ``skill_name``-asc tail, which may be an EXISTING shared skill rather
	than the newly promoted one. Unit-tested against a controlled pushable set + a
	small cap so displacement vs omission is deterministic (no need to mint 25 rows)."""

	def _rows(self, *skill_names):
		return [frappe._dict(name=f"row-{s}", skill_name=s) for s in skill_names]

	def test_promoted_sorts_after_existing_is_the_dropped_one(self):
		from jarvis.chat import custom_skills

		with (
			patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2),
			patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("aaa", "bbb")),
		):
			proj = custom_skills.project_org_promotion_push("row-zzz", "zzz")
		self.assertTrue(proj["over_budget"])
		self.assertTrue(proj["strict_would_fail"])
		self.assertEqual(proj["dropped_slugs"], [prefixed_slug("zzz")])
		self.assertTrue(proj["promoted_dropped"])  # the NEW skill is the one omitted

	def test_promoted_sorts_before_drops_an_existing_skill(self):
		from jarvis.chat import custom_skills

		with (
			patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2),
			patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("bbb", "ccc")),
		):
			proj = custom_skills.project_org_promotion_push("row-aaa", "aaa")
		self.assertTrue(proj["over_budget"])
		# sorted [aaa, bbb, ccc], cap 2 → drops ccc (an EXISTING skill), NOT aaa
		self.assertEqual(proj["dropped_slugs"], [prefixed_slug("ccc")])
		self.assertFalse(proj["promoted_dropped"])

	def test_at_budget_is_not_over(self):
		from jarvis.chat import custom_skills

		with (
			patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2),
			patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("aaa")),
		):
			proj = custom_skills.project_org_promotion_push("row-bbb", "bbb")
		self.assertFalse(proj["over_budget"])
		self.assertTrue(proj["at_budget"])
		self.assertEqual(proj["dropped_slugs"], [])

	def test_recompute_catches_concurrent_change(self):
		# A value captured when the catalog had room is WRONG after a concurrent
		# promotion fills it; a fresh server recompute reflects the new state.
		from jarvis.chat import custom_skills

		with patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2):
			with patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("aaa")):
				before = custom_skills.project_org_promotion_push("row-zzz", "zzz")
			with patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("aaa", "bbb")):
				after = custom_skills.project_org_promotion_push("row-zzz", "zzz")
		self.assertFalse(before["over_budget"])  # a stale client would say "fine"
		self.assertTrue(after["over_budget"])  # the server recompute catches the truth

	def test_idempotent_reapprove_not_double_counted(self):
		# An already-pushable skill (its docname already in the set) is not added
		# again, so re-projecting an already-Org skill never inflates the count.
		from jarvis.chat import custom_skills

		with (
			patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2),
			patch.object(custom_skills, "_pushable_org_rows", return_value=self._rows("aaa", "bbb")),
		):
			proj = custom_skills.project_org_promotion_push("row-aaa", "aaa")
		self.assertEqual(proj["projected_count"], 2)
		self.assertFalse(proj["over_budget"])

	def test_projection_orders_by_explicit_key_not_input_order(self):
		# R2-SP-4: the projection ranks the pushable set by the ONE deterministic
		# comparator (`_pushable_sort_key`), never the order rows happened to arrive
		# in. Hyphen + digit boundary names where a raw DB collation and Python
		# codepoint order can disagree, handed in a NON-sorted order:
		from jarvis.chat import custom_skills

		unsorted_rows = self._rows("ab", "a-2", "a1", "a-10")
		with (
			patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 2),
			patch.object(custom_skills, "_pushable_org_rows", return_value=unsorted_rows),
		):
			proj = custom_skills.project_org_promotion_push("row-zz", "zz")
		# Python order of the lowercased slugs: a-10 < a-2 < a1 < ab < zz; cap 2 keeps
		# the first two and DROPS the ordered tail — proving the sort key, not input
		# order, decides which skills are dropped.
		self.assertEqual(proj["dropped_slugs"], [prefixed_slug(s) for s in ("a1", "ab", "zz")])
		self.assertTrue(proj["promoted_dropped"])  # zz sorts last → the promoted one drops


class TestSkillPromotionRound2(Part2Base):
	"""Codex round-2 follow-ons on the promotion machinery: null-snapshot approval
	is refused with NO live fallback (R2-SP-1), the reviewer read surfaces EVERY
	field materialization publishes (R2-SP-2), materialization is atomic + lineage
	aware — same-slug conflicts, same-lineage supersedes (R2-SP-3), the pushable
	ranking is one shared comparator (R2-SP-4), and an Org approval rebinds the
	budget projection to the decision (R2-SP-5)."""

	# ── R2-SP-1: null / partial snapshot approval is refused, never live-read ────
	def test_null_snapshot_approval_refused_no_live_fallback(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-legacy", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", "LIVE-BODY-must-not-publish")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		# Model a legacy pre-binding row by nulling the bound snapshot after the fact.
		frappe.db.set_value(SKILL_PROMO, req["request"], "instructions_snapshot", None)
		frappe.db.set_value(SKILL_PROMO, req["request"], "description_snapshot", None)
		with _as(REVIEWER):
			with self.assertRaises(frappe.ValidationError):
				custom_skills_api.decide_skill_promotion(req["request"], 1)
		# NOTHING is published — approval never falls back to the mutable live source.
		self.assertEqual(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-legacy", "scope": "Org"}), [])
		# The request is left Pending (not silently consumed), so a resubmit is possible.
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")

	def test_new_request_must_carry_content_snapshot(self):
		# The request-time guard (R2-SP-1): a fresh JSPR without content snapshots is
		# refused at the controller, so no null-snapshot request can be filed anew.
		skill = _mk_skill(USER_A, f"{PFX}-needsnap", scope="User")
		with _as(USER_A):
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc(
					{
						"doctype": SKILL_PROMO,
						"skill": skill.name,
						"skill_name": skill.skill_name,
						"from_scope": "User",
						"to_scope": "Org",
						"status": "Pending",
						# deliberately NO instructions_snapshot / description_snapshot
					}
				).insert(ignore_permissions=True)

	# ── R2-SP-2: reviewer read carries EVERY field the approval publishes ────────
	def test_reviewer_read_carries_all_published_fields(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-allfields", scope="User")
		frappe.db.set_value(
			SKILL,
			skill.name,
			{
				"instructions": "BODY-marker-11",
				"description": "DESC-marker-22",
				"user_invocable": 0,
			},
		)
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			res = custom_skills_api.list_skill_promotion_requests(status="Pending")
		row = next(r for r in res["rows"] if r["skill"] == skill.name)
		# EVERY field _materialize_promotion consumes is present in the reviewer read.
		self.assertIn("BODY-marker-11", row["body_excerpt"])  # instructions_snapshot
		self.assertEqual(row["description_snapshot"], "DESC-marker-22")
		self.assertEqual(row["user_invocable_snapshot"], 0)
		# Strict-need parity (SAR-3): a decided row does NOT re-surface any of them.
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req["request"], 0, note="no")
			decided = custom_skills_api.list_skill_promotion_requests(status="Rejected")
		drow = next(r for r in decided["rows"] if r["skill"] == skill.name)
		self.assertEqual(drow["body_excerpt"], "")
		self.assertEqual(drow["description_snapshot"], "")
		self.assertIsNone(drow["user_invocable_snapshot"])

	# ── R2-SP-3: atomic same-slug conflict + same-lineage supersede ─────────────
	def test_same_slug_different_lineage_conflicts_no_duplicate(self):
		# Two owners each hold a private skill with the SAME authored slug (allowed —
		# per-owner uniqueness). Promoting both to Org: the first publishes the one
		# shared copy, the second is a hard conflict (one container dir per slug), so
		# there is never a duplicate shared row.
		from jarvis.chat import custom_skills_api

		a = _mk_skill(USER_A, f"{PFX}-dup", scope="User")
		b = _mk_skill(USER_B, f"{PFX}-dup", scope="User")
		with _as(USER_A):
			req_a = custom_skills_api.request_skill_promotion(a.name, "Org")
		with _as(USER_B):
			req_b = custom_skills_api.request_skill_promotion(b.name, "Org")
		with _as(REVIEWER):
			out_a = custom_skills_api.decide_skill_promotion(req_a["request"], 1)
			self.assertTrue(out_a["ok"])
			with self.assertRaises(frappe.ValidationError):
				custom_skills_api.decide_skill_promotion(req_b["request"], 1)
		shared = frappe.get_all(
			SKILL, filters={"skill_name": f"{PFX}-dup", "scope": ["in", ["Role", "Org"]]}, fields=["name"]
		)
		self.assertEqual(len(shared), 1)  # exactly one wins; no duplicate slug
		# The losing request is untouched (still Pending) — a clear conflict, not a
		# double-publish and not a silent consume.
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req_b["request"], "status"), "Pending")

	def test_reviewer_owning_same_slug_private_skill_does_not_block_promotion(self):
		# R2-SP-3a: the shared copy's uniqueness is validated against the FINAL system
		# owner, so a reviewer who happens to own a private skill of the same slug
		# does NOT spuriously trip per-owner uniqueness on the insert.
		from jarvis.chat import custom_skills_api

		rev_own = _mk_skill(REVIEWER, f"{PFX}-collide", scope="User")  # reviewer's own private skill
		skill = _mk_skill(USER_A, f"{PFX}-collide", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		shared = frappe.get_all(
			SKILL,
			filters={"skill_name": f"{PFX}-collide", "scope": "Org", "owner": "Administrator"},
			fields=["name"],
		)
		self.assertEqual(len(shared), 1)
		# the reviewer's own private skill is untouched (still User, still theirs)
		self.assertEqual(frappe.db.get_value(SKILL, rev_own.name, "owner"), REVIEWER)
		self.assertEqual(frappe.db.get_value(SKILL, rev_own.name, "scope"), "User")

	def test_user_role_then_org_supersedes_in_place(self):
		# R2-SP-3b: User->Role then User->Org from the intact private source leaves
		# ONE shared copy at Org (superseded in place), no orphan Role copy, and no
		# dead-end for the requester.
		from jarvis.chat import custom_skills, custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-ladder", scope="User")
		frappe.db.set_value(SKILL, skill.name, "instructions", "LADDER-v1")
		with _as(USER_A):
			req_role = custom_skills_api.request_skill_promotion(skill.name, "Role", target_role="Sales User")
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req_role["request"], 1)
		role_copies = frappe.get_all(
			SKILL,
			filters={"skill_name": f"{PFX}-ladder", "owner": "Administrator"},
			fields=["name", "scope", "source_skill"],
		)
		self.assertEqual(len(role_copies), 1)
		self.assertEqual(role_copies[0].scope, "Role")
		self.assertEqual(role_copies[0].source_skill, skill.name)  # lineage persisted
		role_copy_name = role_copies[0].name

		# Now promote the SAME still-private source to Org.
		with _as(USER_A):
			req_org = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req_org["request"], 1)
		self.assertTrue(out["ok"])
		org_copies = frappe.get_all(
			SKILL,
			filters={"skill_name": f"{PFX}-ladder", "owner": "Administrator"},
			fields=["name", "scope"],
		)
		self.assertEqual(len(org_copies), 1)  # ONE shared copy — no orphan Role copy
		self.assertEqual(org_copies[0].scope, "Org")  # final scope Org
		self.assertEqual(org_copies[0].name, role_copy_name)  # superseded IN PLACE
		# The requester's private source is intact — still User, still theirs.
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "owner"), USER_A)
		# The superseded Org copy is now pushable to the shared container.
		self.assertIn(prefixed_slug(f"{PFX}-ladder"), {p["slug"] for p in custom_skills.build_push_payload()})

	# ── R2-SP-4: pushable rows + payload share one comparator (real rows) ────────
	def test_pushable_rows_and_payload_share_one_comparator(self):
		from jarvis.chat import custom_skills

		made = [f"{PFX}-a-2", f"{PFX}-a1", f"{PFX}-a-10", f"{PFX}-ab", f"{PFX}-a-b"]
		for sn in made:
			_mk_skill(USER_A, sn, scope="Org")
		mineset = set(made)
		expected = sorted(mineset, key=str.lower)
		# _pushable_org_rows is returned in the explicit-comparator order (not the DB
		# collation order), so build_push_payload inherits it.
		rows = [r.skill_name for r in custom_skills._pushable_org_rows() if r.skill_name in mineset]
		self.assertEqual(rows, expected)
		myslugs = {prefixed_slug(s) for s in made}
		payload = [p["slug"] for p in custom_skills.build_push_payload() if p["slug"] in myslugs]
		self.assertEqual(payload, [prefixed_slug(s) for s in expected])

	# ── R2-SP-5: an Org approval rebinds the budget projection to the decision ───
	def test_org_approve_requires_reconfirm_when_catalog_moved(self):
		from jarvis.chat import custom_skills, custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-reconf", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		# A stale ack claiming "room to spare" against an over-budget catalog (cap 0):
		# the server recomputes fresh, sees the warning-worthy mismatch, and REFUSES
		# to publish — returning needs_reconfirm + the fresh projection.
		stale_ack = {
			"over_budget": False,
			"at_budget": False,
			"projected_count": 0,
			"dropped_slugs": [],
			"promoted_dropped": False,
		}
		with patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 0):
			with _as(REVIEWER):
				r1 = custom_skills_api.decide_skill_promotion(req["request"], 1, ack_projection=stale_ack)
		self.assertFalse(r1["ok"])
		self.assertTrue(r1.get("needs_reconfirm"))
		self.assertIsNotNone(r1["push_projection"])
		self.assertEqual(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-reconf", "scope": "Org"}), [])
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")
		# Reconfirming against the FRESH projection publishes exactly once.
		fresh_ack = r1["push_projection"]
		with patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", 0):
			with _as(REVIEWER):
				r2 = custom_skills_api.decide_skill_promotion(req["request"], 1, ack_projection=fresh_ack)
		self.assertTrue(r2["ok"])
		self.assertEqual(
			len(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-reconf", "scope": "Org"})), 1
		)

	def test_under_budget_org_approve_publishes_without_ack(self):
		# The reconfirm gate is warning-scoped: a clear (room to spare) catalog needs
		# no ack, so a well-formed under-budget approval publishes in one call.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-roomy", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)  # no ack
		self.assertTrue(out["ok"])
		self.assertEqual(
			len(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-roomy", "scope": "Org"})), 1
		)


class TestSkillPromotionRound3(Part2Base):
	"""Codex round-3: the promotion invariants are GLOBAL, not promotion-path-local.
	Shared-slug uniqueness holds across EVERY shared-write path (R3-SP-1), lineage is
	resolved by the immutable source link independent of the slug — Role sources and
	renamed sources included (R3-SP-2), the push budget serializes under ONE
	catalog-wide lock so different-slug approvals cannot both overshoot (R3-SP-3), and
	an immutable presence marker refuses a partial snapshot at approval (R3-SP-4)."""

	# ── R3-SP-1: GLOBAL shared-slug uniqueness across paths ─────────────────────
	def test_two_shared_skills_cannot_share_a_slug_across_owners(self):
		# A shared (Org) skill owned by USER_A blocks a SECOND shared skill of the same
		# slug owned by USER_B — no matter which path mints it. The container writes ONE
		# custom-<slug> dir, so this is the true invariant (owner-scoped uniqueness is
		# not enough).
		_mk_skill(USER_A, f"{PFX}-guniq", scope="Org")
		with self.assertRaises(frappe.ValidationError):
			_mk_skill(USER_B, f"{PFX}-guniq", scope="Org")
		# A Role skill of the same slug is a shared skill too → also blocked.
		with self.assertRaises(frappe.ValidationError):
			_mk_skill(USER_B, f"{PFX}-guniq", scope="Role", target_role="Sales User")
		# But a PRIVATE (User) skill of the same slug is fine — per-owner uniqueness
		# still lets two people each keep a private "guniq".
		priv = _mk_skill(USER_B, f"{PFX}-guniq", scope="User")
		self.assertEqual(frappe.db.get_value(SKILL, priv.name, "scope"), "User")

	def test_shared_slug_uniqueness_permits_in_place_update(self):
		# The uniqueness excludes SELF, so a legitimate in-place UPDATE of the ONE shared
		# row (the insight-apply / supersede path) is never blocked as a duplicate.
		org = _mk_skill(USER_A, f"{PFX}-inplace", scope="Org")
		with _engine_flag():
			doc = frappe.get_doc(SKILL, org.name)
			doc.description = "edited in place"
			doc.instructions = "new body"
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(SKILL, org.name, "description"), "edited in place")

	def test_direct_org_create_blocks_conflicting_promotion(self):
		# The invariant holds across DIFFERENT paths: a directly-created shared Org skill
		# blocks a promotion of a different lineage's private skill with the same slug —
		# never a duplicate shared row.
		from jarvis.chat import custom_skills_api

		_mk_skill(REVIEWER, f"{PFX}-crosspath", scope="Org")  # direct shared create
		priv = _mk_skill(USER_A, f"{PFX}-crosspath", scope="User")  # different lineage
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(priv.name, "Org")
		with _as(REVIEWER):
			with self.assertRaises(frappe.ValidationError):
				custom_skills_api.decide_skill_promotion(req["request"], 1)
		shared = frappe.get_all(
			SKILL, filters={"skill_name": f"{PFX}-crosspath", "scope": ["in", ["Role", "Org"]]}
		)
		self.assertEqual(len(shared), 1)  # only the direct one; no duplicate minted
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")

	# ── R3-SP-2: lineage by SOURCE link, not slug ───────────────────────────────
	def test_role_source_to_org_widens_in_place_no_dup(self):
		# Promoting a Role SOURCE to Org widens THAT row in place (the source itself is
		# the existing shared copy) — never a second Org row leaving the Role row.
		from jarvis.chat import custom_skills_api

		role = _mk_skill(USER_A, f"{PFX}-r2org", scope="Role", target_role="Sales User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(role.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		shared = frappe.get_all(
			SKILL,
			filters={"skill_name": f"{PFX}-r2org", "scope": ["in", ["Role", "Org"]]},
			fields=["name", "scope"],
		)
		self.assertEqual(len(shared), 1)  # widened in place — no duplicate
		self.assertEqual(shared[0].scope, "Org")
		self.assertEqual(shared[0].name, role.name)  # the SAME row
		self.assertEqual(frappe.db.get_value(SKILL, role.name, "owner"), "Administrator")

	def test_renamed_source_supersedes_role_copy_not_stranded(self):
		# User->Role, then the private source's slug is RENAMED, then User->Org: the
		# prior Role copy is found by its immutable source link (not the stale slug) and
		# SUPERSEDED in place — no stranded Role copy, no duplicate Org row.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-ren1", scope="User")
		with _as(USER_A):
			req_role = custom_skills_api.request_skill_promotion(skill.name, "Role", target_role="Sales User")
		with _as(REVIEWER):
			custom_skills_api.decide_skill_promotion(req_role["request"], 1)
		role_copies = frappe.get_all(
			SKILL, filters={"source_skill": skill.name}, fields=["name", "scope", "skill_name"]
		)
		self.assertEqual(len(role_copies), 1)
		self.assertEqual(role_copies[0].scope, "Role")
		role_copy_name = role_copies[0].name

		# Rename the still-private source's slug BEFORE the Org promotion.
		with _as(USER_A):
			doc = frappe.get_doc(SKILL, skill.name)
			doc.skill_name = f"{PFX}-ren2"
			doc.save()
			req_org = custom_skills_api.request_skill_promotion(skill.name, "Org")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req_org["request"], 1)
		self.assertTrue(out["ok"])
		shared = frappe.get_all(
			SKILL, filters={"source_skill": skill.name}, fields=["name", "scope", "skill_name"]
		)
		self.assertEqual(len(shared), 1)  # ONE shared copy — the Role copy was not stranded
		self.assertEqual(shared[0].name, role_copy_name)  # superseded IN PLACE
		self.assertEqual(shared[0].scope, "Org")
		self.assertEqual(shared[0].skill_name, f"{PFX}-ren2")  # adopted the renamed slug
		# The private source is intact — still User, still theirs, at the new slug.
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "owner"), USER_A)

	# ── R3-SP-3: CATALOG-WIDE budget lock (different-slug serialization) ─────────
	def test_catalog_wide_budget_serializes_different_slug_approvals(self):
		# Two DIFFERENT-slug Org approvals at the budget boundary: the FIRST publishes on
		# its matching (at-budget) ack; the SECOND — acked against the SAME pre-publish
		# projection — must reconfirm, because the fresh recheck now runs UNDER the
		# catalog-wide lock and sees the first's committed publish (now over budget). A
		# slug-scoped lock would have let both commit.
		from jarvis.chat import custom_skills, custom_skills_api

		a = _mk_skill(USER_A, f"{PFX}-cw-a", scope="User")
		b = _mk_skill(USER_B, f"{PFX}-cw-b", scope="User")
		with _as(USER_A):
			req_a = custom_skills_api.request_skill_promotion(a.name, "Org")
		with _as(USER_B):
			req_b = custom_skills_api.request_skill_promotion(b.name, "Org")
		# Budget = current pushable + 1, so promoting EITHER one lands exactly at budget.
		base = custom_skills.pushable_org_skill_count()
		with patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", base + 1):
			# Each reviewer confirms an at-budget projection computed BEFORE either commits.
			ack_a = custom_skills.project_org_promotion_push(a.name, f"{PFX}-cw-a")
			ack_b = custom_skills.project_org_promotion_push(b.name, f"{PFX}-cw-b")
			self.assertTrue(ack_a["at_budget"])
			self.assertTrue(ack_b["at_budget"])
			with _as(REVIEWER):
				r_a = custom_skills_api.decide_skill_promotion(req_a["request"], 1, ack_projection=ack_a)
				r_b = custom_skills_api.decide_skill_promotion(req_b["request"], 1, ack_projection=ack_b)
		self.assertTrue(r_a["ok"])  # first publishes without reconfirm
		self.assertTrue(r_b.get("needs_reconfirm"))  # second must reconfirm the moved catalog
		self.assertTrue(r_b["push_projection"]["over_budget"])  # now genuinely over budget
		shared = frappe.get_all(
			SKILL,
			filters={"skill_name": ["in", [f"{PFX}-cw-a", f"{PFX}-cw-b"]], "scope": "Org"},
			fields=["name"],
		)
		self.assertEqual(len(shared), 1)  # EXACTLY one published; the second held back
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req_b["request"], "status"), "Pending")

	# ── R3-SP-4: the request path always mints a FULL snapshot + presence marker ─
	def test_missing_invocable_snapshot_refused_at_request(self):
		# user_invocable_snapshot is a Check (tinyint NOT NULL): it can never be "missing"
		# — an omitted value is stored as 0, indistinguishable from a genuine False — so a
		# field-level "missing" refusal is impossible. The real request-time protection is
		# that request_skill_promotion ALWAYS captures a FULL snapshot from the source and
		# stamps the presence marker, so a legit request is always marked (and the marker
		# check at approval only ever trips a genuine legacy / partial row).
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-noinv", scope="User")
		# A distinctive (non-default) source value proves the snapshot is copied from source.
		frappe.db.set_value(SKILL, skill.name, "user_invocable", 0)
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		row = frappe.db.get_value(
			SKILL_PROMO,
			req["request"],
			["snapshotted", "instructions_snapshot", "description_snapshot", "user_invocable_snapshot"],
			as_dict=True,
		)
		self.assertEqual(row.snapshotted, 1)  # a legit request is always marked
		self.assertEqual(row.instructions_snapshot, "body")
		self.assertEqual(row.description_snapshot, f"{PFX}-noinv desc")
		self.assertEqual(int(row.user_invocable_snapshot), 0)  # captured the source's False

	def test_absent_marker_refused_at_approval(self):
		# A request whose presence marker is absent (a legacy / pre-binding row) is refused
		# at approval like a missing-instructions one — publishing nothing, leaving it Pending.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-marker", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "snapshotted"), 1)
		# Model a legacy / pre-binding row by clearing the presence MARKER. The marker — not
		# the Check value — is what proves a full snapshot: user_invocable_snapshot is a Check
		# (tinyint NOT NULL) that can never be null, and an omitted value is stored as 0
		# indistinguishable from a genuine False. So the marker's absence is what approval refuses.
		frappe.db.set_value(SKILL_PROMO, req["request"], "snapshotted", 0)
		with _as(REVIEWER):
			with self.assertRaisesRegex(frappe.ValidationError, "resubmit"):
				custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertEqual(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-marker", "scope": "Org"}), [])
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")

	def test_explicit_false_invocable_snapshot_publishes_as_false(self):
		# With the marker present and user_invocable explicitly False, approval publishes
		# invocable=False FAITHFULLY (not conflated with "omitted"): the marker records a
		# reviewed choice, so a full snapshot with a genuine False is honoured.
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-markok", scope="User")
		frappe.db.set_value(SKILL, skill.name, "user_invocable", 0)
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "snapshotted"), 1)
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "user_invocable_snapshot"), 0)
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		mat = frappe.db.get_value(
			SKILL, {"skill_name": f"{PFX}-markok", "scope": "Org"}, ["name", "user_invocable"], as_dict=True
		)
		self.assertIsNotNone(mat)
		self.assertEqual(int(mat.user_invocable), 0)  # explicit False preserved end-to-end


class TestSkillPromotionRound4(Part2Base):
	"""Codex round-4: the shared-slug invariant is enforced at its TRUE scope - a
	DB-unique reservation (row name == slug) plus the catalog-wide lock on EVERY shared
	eligibility-changing write, not just the promotion path (SR4-2); and the v2_06
	backfill no longer blesses an ambiguous legacy snapshot from the Check column
	(SR4-1)."""

	# ── SR4-1: the migration does not backfill the marker from the Check column ──
	def test_migration_leaves_omitted_normalized_legacy_request_unmarked(self):
		# An omitted user-invocable normalized to 0 is indistinguishable from a genuine
		# False, so a non-NULL snapshot never proves capture. The v2_06 backfill must
		# leave such a legacy row UNMARKED, and approval must still refuse it.
		from jarvis.chat import custom_skills_api
		from jarvis.patches import v2_06_skill_promotion_marker_and_shared_slug as patch_mod

		skill = _mk_skill(USER_A, f"{PFX}-legacymark", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		# Model a legacy row: invocable snapshot 0 (as if omitted->0) + marker cleared.
		frappe.db.set_value(SKILL_PROMO, req["request"], "user_invocable_snapshot", 0)
		frappe.db.set_value(SKILL_PROMO, req["request"], "snapshotted", 0)
		frappe.db.commit()
		patch_mod.execute()  # run the migration - it must not bless the ambiguous row
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "snapshotted"), 0)
		with _as(REVIEWER):
			with self.assertRaises(frappe.ValidationError):
				custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")
		self.assertEqual(
			frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-legacymark", "scope": "Org"}), []
		)

	# ── SR4-2: DB-unique slug reservation, kept in step on every write path ──────
	def test_shared_create_reserves_slug_and_release_frees_it(self):
		# A shared (Org) create RESERVES its slug (reservation name == slug). Narrowing
		# back to User releases it; deleting a shared skill releases it on trash.
		org = _mk_skill(USER_A, f"{PFX}-reserved", scope="Org")
		self.assertTrue(frappe.db.exists(RESV, f"{PFX}-reserved"))
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-reserved", "skill"), org.name)
		with _as(USER_A):  # owner self-demote (narrowing) is allowed without a reviewer
			doc = frappe.get_doc(SKILL, org.name)
			doc.scope = "User"
			doc.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(RESV, f"{PFX}-reserved"))  # released on narrow
		other = _mk_skill(USER_B, f"{PFX}-reserved2", scope="Org")
		self.assertTrue(frappe.db.exists(RESV, f"{PFX}-reserved2"))
		frappe.delete_doc(SKILL, other.name, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(RESV, f"{PFX}-reserved2"))  # released on trash

	def test_reservation_blocks_duplicate_shared_slug_at_db(self):
		# The controller belt catches the SEQUENTIAL duplicate; the DB reservation is
		# the concurrent backstop for two shared writes that both pass the belt SELECT
		# before either commits. Neutralize the belt so the second create reaches the
		# reservation and is refused there.
		from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import JarvisCustomSkill

		first = _mk_skill(USER_A, f"{PFX}-dbuniq", scope="Org")
		with patch.object(JarvisCustomSkill, "_validate_shared_slug_unique", lambda self: None):
			with self.assertRaises(frappe.ValidationError):
				_mk_skill(USER_B, f"{PFX}-dbuniq", scope="Org")
		# The duplicate was BLOCKED at the DB reservation: the slug's reservation still points
		# at the FIRST skill — the meaningful invariant (one shared owner per slug). We assert
		# the reservation holder, not a post-failure row count, which would depend on Frappe's
		# insert/rollback timing for the refused second insert.
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-dbuniq", "skill"), first.name)

	def test_rename_shared_skill_moves_reservation(self):
		# Renaming a shared skill releases the old slug's reservation and reserves the
		# new one, so the freed slug is available to a different shared skill.
		org = _mk_skill(REVIEWER, f"{PFX}-oldname", scope="Org")
		self.assertTrue(frappe.db.exists(RESV, f"{PFX}-oldname"))
		with _as(REVIEWER):
			doc = frappe.get_doc(SKILL, org.name)
			doc.skill_name = f"{PFX}-newname"
			doc.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(RESV, f"{PFX}-oldname"))  # old slug freed
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-newname", "skill"), org.name)
		other = _mk_skill(USER_A, f"{PFX}-oldname", scope="Org")  # freed slug reusable
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-oldname", "skill"), other.name)

	def test_insight_apply_inplace_update_does_not_refail_reservation(self):
		# In-place shared UPDATE (insight-apply / a reviewer content re-save) re-reserves
		# its OWN slug - idempotent, never a spurious duplicate failure.
		org = _mk_skill(USER_A, f"{PFX}-idem", scope="Org")
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-idem", "skill"), org.name)
		with _engine_flag():
			doc = frappe.get_doc(SKILL, org.name)
			doc.instructions = "reviewed new body"
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(SKILL, org.name, "instructions"), "reviewed new body")
		self.assertEqual(len(frappe.get_all(RESV, filters={"slug": f"{PFX}-idem"})), 1)
		self.assertEqual(frappe.db.get_value(RESV, f"{PFX}-idem", "skill"), org.name)

	# ── SR4-2: the catalog-wide lock serializes non-promotion shared writes ──────
	def test_shared_create_blocked_when_catalog_lock_held(self):
		# A shared (Org) create takes the catalog-wide lock; when it is already held the
		# create refuses (serialized with approvals). A PRIVATE create takes no lock.
		import jarvis._redis_lock as rl
		from jarvis.chat import custom_skills_api

		@contextlib.contextmanager
		def _busy(name, **kw):
			yield False

		with patch.object(rl, "redis_lock", _busy):
			with _as(REVIEWER):
				with self.assertRaises(frappe.ValidationError):
					custom_skills_api._create_custom_skill_impl(
						skill_name=f"{PFX}-locked",
						description="d",
						instructions="i",
						scope="Org",
						ignore_permissions=True,
					)
			with _as(USER_A):  # private create is lock-free even while the lock is held
				out = custom_skills_api._create_custom_skill_impl(
					skill_name=f"{PFX}-lockfree", description="d", instructions="i"
				)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(SKILL, out["data"]["name"], "scope"), "User")
		self.assertEqual(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-locked"}), [])

	def test_direct_org_create_serializes_with_approval_budget(self):
		# The cross-path budget race: a direct Org create and an approval serialize on
		# the SAME catalog lock. Model the create-wins-the-lock ordering - a different-
		# slug direct Org create commits first, so the approval's fresh under-lock
		# projection SEES it and must reconfirm the now-over-budget catalog. Exactly one
		# lands at the budget boundary; the approval holds back.
		from jarvis.chat import custom_skills, custom_skills_api

		src = _mk_skill(USER_A, f"{PFX}-b4race", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(src.name, "Org")
		base = custom_skills.pushable_org_skill_count()
		with patch.object(custom_skills, "MAX_SKILLS_PER_PUSH", base + 1):
			ack = custom_skills.project_org_promotion_push(src.name, f"{PFX}-b4race")
			self.assertTrue(ack["at_budget"])  # acked BEFORE the direct create
			with _as(REVIEWER):  # a different-slug direct Org create commits first
				custom_skills_api._create_custom_skill_impl(
					skill_name=f"{PFX}-b4race-x",
					description="d",
					instructions="i",
					scope="Org",
					ignore_permissions=True,
				)
				out = custom_skills_api.decide_skill_promotion(req["request"], 1, ack_projection=ack)
		self.assertTrue(out.get("needs_reconfirm"))  # the committed create moved the budget
		self.assertTrue(out["push_projection"]["over_budget"])
		self.assertEqual(frappe.db.get_value(SKILL_PROMO, req["request"], "status"), "Pending")
		self.assertEqual(
			len(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-b4race", "scope": "Org"})), 0
		)
		self.assertEqual(
			len(frappe.get_all(SKILL, filters={"skill_name": f"{PFX}-b4race-x", "scope": "Org"})), 1
		)


class TestRoleScopedSkillInvocation(Part2Base):
	"""Issues #477 + #478: the two halves of role-scoped /slug invocation.

	#478: a Role promotion wrote ``target_role`` only, so
	``_role_scoped_invocable_names`` (which matches on the ``allowed_roles`` CHILD
	rows) never saw it and no role-holder's ``/slug`` ever fired.

	#477: every invoked skill was named as an installed ``custom-<slug>``
	directory, including the role-restricted ones the push DELIBERATELY drops,
	so the trusted [Context:] line asserted a container dir that does not exist.

	They ship together: fixing #478 alone routes every Role-promoted skill into
	the phantom-directory clause, which is why #477's split lands with it.
	"""

	ROLE_HOLDER = "p2-roleholder@example.com"
	AUD_ROLE = "Sales User"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A role-holder who does NOT own the source skill, the point of the whole
		# tier. (Owners already match via the owner branch of invoked_skill_clause,
		# so testing with the owner would pass even with the bug present.)
		_ensure_user(cls.ROLE_HOLDER, ["Jarvis User", cls.AUD_ROLE])

	def _promote_to_role(self, slug: str) -> str:
		"""Requester USER_A's private skill -> reviewer-approved Role copy. Returns
		the shared copy's docname."""
		from jarvis.chat import custom_skills_api

		src = _mk_skill(USER_A, slug, scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(src.name, "Role", target_role=self.AUD_ROLE)
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		return out["materialized"]

	# ── #478: the promotion must write allowed_roles, not just target_role ──────
	def test_role_promotion_writes_allowed_roles_alongside_target_role(self):
		shared = self._promote_to_role(f"{PFX}-roleaud")
		self.assertEqual(frappe.db.get_value(SKILL, shared, "scope"), "Role")
		self.assertEqual(frappe.db.get_value(SKILL, shared, "target_role"), self.AUD_ROLE)
		self.assertEqual(
			frappe.get_all(
				"Jarvis Custom Skill Allowed Role",
				filters={"parent": shared, "parenttype": SKILL},
				pluck="role",
			),
			[self.AUD_ROLE],
		)

	def test_role_promoted_skill_is_slug_invocable_by_a_role_holder(self):
		from jarvis.chat.custom_skills import _role_scoped_invocable_names, invoked_skill_clause

		self._promote_to_role(f"{PFX}-roleinv")
		with _as(self.ROLE_HOLDER):
			self.assertIn(f"{PFX}-roleinv", _role_scoped_invocable_names(self.ROLE_HOLDER))
			clause = invoked_skill_clause(f"/{PFX}-roleinv close the month")
		self.assertIn(prefixed_slug(f"{PFX}-roleinv"), clause)

	def test_role_promoted_skill_stays_invisible_to_a_non_role_holder(self):
		from jarvis.chat.custom_skills import invoked_skill_clause

		self._promote_to_role(f"{PFX}-rolepriv")
		with _as(USER_B):  # Jarvis User only, no Sales User
			self.assertEqual(invoked_skill_clause(f"/{PFX}-rolepriv go"), "")

	def test_role_to_org_widen_clears_allowed_roles_and_stays_pushable(self):
		# The Role copy's allowed_roles row must NOT survive a widen to Org: an Org
		# row carrying allowed_roles is "role-restricted" and _pushable_org_rows drops
		# it, which would silently un-push the skill the reviewer just widened.
		from jarvis.chat import custom_skills, custom_skills_api

		shared = self._promote_to_role(f"{PFX}-widen")
		src = frappe.db.get_value(SKILL, shared, "source_skill")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(src, "Org")
		with _as(REVIEWER):
			self.assertTrue(custom_skills_api.decide_skill_promotion(req["request"], 1)["ok"])
		self.assertEqual(frappe.db.get_value(SKILL, shared, "scope"), "Org")
		self.assertEqual(
			frappe.get_all(
				"Jarvis Custom Skill Allowed Role",
				filters={"parent": shared, "parenttype": SKILL},
				pluck="role",
			),
			[],
		)
		self.assertIn(prefixed_slug(f"{PFX}-widen"), {p["slug"] for p in custom_skills.build_push_payload()})

	# ── #477: two clause shapes, only one of which claims the file is on disk ───
	def test_pushed_org_skill_keeps_the_apply_them_clause(self):
		from jarvis.chat.custom_skills import invoked_skill_clause

		# Owned by the invoker: /slug matches on owner / shared_with / role, so an Org
		# skill is only ever invocable by someone one of those three admits.
		_mk_skill(USER_A, f"{PFX}-ondisk", scope="Org")
		self.assertIn(prefixed_slug(f"{PFX}-ondisk"), {p["slug"] for p in build_push_payload()})
		with _as(USER_A):
			clause = invoked_skill_clause(f"/{PFX}-ondisk run it")
		self.assertIn(f"apply them: {prefixed_slug(f'{PFX}-ondisk')}", clause)
		self.assertNotIn("jarvis__get_skill", clause)

	def test_role_restricted_org_skill_gets_the_fetch_clause_not_the_on_disk_one(self):
		# The exact failing scenario in #477: an Org skill narrowed by allowed_roles is
		# dropped from the push, so naming it as installed pointed the agent at a dir
		# that the next reconcile deleted.
		from jarvis.chat.custom_skills import invoked_skill_clause

		_mk_skill(REVIEWER, f"{PFX}-narrowed", scope="Org", allowed_roles=[self.AUD_ROLE])
		self.assertNotIn(prefixed_slug(f"{PFX}-narrowed"), {p["slug"] for p in build_push_payload()})
		with _as(self.ROLE_HOLDER):
			clause = invoked_skill_clause(f"/{PFX}-narrowed please")
		self.assertIn(prefixed_slug(f"{PFX}-narrowed"), clause)
		self.assertIn("jarvis__get_skill", clause)
		self.assertNotIn("apply them", clause)
		# and it must not imply container-side access to a body that is not there
		self.assertNotIn("workspace", clause)

	def test_role_promoted_skill_gets_the_fetch_clause(self):
		# The #478 <-> #477 join: Role scope is excluded from the push outright, so the
		# skill #478 just made invocable must NOT be announced as an on-disk directory.
		from jarvis.chat.custom_skills import invoked_skill_clause

		self._promote_to_role(f"{PFX}-rolefetch")
		self.assertNotIn(prefixed_slug(f"{PFX}-rolefetch"), {p["slug"] for p in build_push_payload()})
		with _as(self.ROLE_HOLDER):
			clause = invoked_skill_clause(f"/{PFX}-rolefetch now")
		self.assertIn("jarvis__get_skill", clause)
		self.assertNotIn("apply them", clause)

	def test_private_user_skill_invoked_by_its_owner_gets_the_fetch_clause(self):
		# User-scope rows are excluded from the push too, so the owner's own /slug hit
		# the same phantom-directory bug.
		from jarvis.chat.custom_skills import invoked_skill_clause

		_mk_skill(USER_A, f"{PFX}-mine", scope="User")
		with _as(USER_A):
			clause = invoked_skill_clause(f"/{PFX}-mine draft it")
		self.assertIn("jarvis__get_skill", clause)
		self.assertNotIn("apply them", clause)

	def test_one_message_can_carry_both_clause_shapes(self):
		from jarvis.chat.custom_skills import invoked_skill_clause

		_mk_skill(self.ROLE_HOLDER, f"{PFX}-bothdisk", scope="Org")  # owned -> pushed
		_mk_skill(REVIEWER, f"{PFX}-bothrole", scope="Org", allowed_roles=[self.AUD_ROLE])
		with _as(self.ROLE_HOLDER):
			clause = invoked_skill_clause(f"/{PFX}-bothdisk then /{PFX}-bothrole")
		self.assertIn(f"apply them: {prefixed_slug(f'{PFX}-bothdisk')}", clause)
		self.assertIn("jarvis__get_skill", clause)
		# each slug appears in exactly ONE shape, never both
		fetch_half = clause.split("not loaded in this session:", 1)[1]
		self.assertIn(prefixed_slug(f"{PFX}-bothrole"), fetch_half)
		self.assertNotIn(prefixed_slug(f"{PFX}-bothdisk"), fetch_half)
