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

	def test_request_then_reviewer_widens_scope(self):
		from jarvis.chat import custom_skills_api

		skill = _mk_skill(USER_A, f"{PFX}-promote", scope="User")
		with _as(USER_A):
			req = custom_skills_api.request_skill_promotion(skill.name, "Org")
		self.assertTrue(req["ok"])
		# Still private until a reviewer decides.
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "User")
		with _as(REVIEWER):
			out = custom_skills_api.decide_skill_promotion(req["request"], 1)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(SKILL, skill.name, "scope"), "Org")

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
			req = custom_skills_api.request_skill_promotion(
				skill.name, "Role", target_role="Sales User"
			)
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
