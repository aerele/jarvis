"""Wave B1 pipeline-reroute tests (DESIGN.md sections 3, 5b, 6, 6b):

  * ``jarvis.learning.questions`` — pattern -> Personalise-question
    materialization (cap, dedupe, Deleted-suppression, voice-owner vs admin-bank
    targeting, the daily backstop scan) and rule -> question fan-out
    (Org/Role/User scoping, dedupe, inactive-rule skip).
  * ``jarvis.chat.wiki.apply_extracted_page_updates`` — the scope extension
    (User-scope audience-suffixed upsert; Org path unchanged byte-for-byte).
  * ``jarvis.chat.wiki`` promotion flow — ``request_wiki_promotion`` /
    ``apply_promotion`` (own-page guard, approve merges into the target scope,
    reject leaves the target untouched, idempotence).
  * ``jarvis.learning.voice_facts.process_single_note`` — the immediate
    single-note ingest (context facts fork to the owner's User wiki, rule facts
    mint a learned pattern + question, the note flips Processed, the
    personalise:processed receipt fires, extracted_text feeds the prompt).

No live LLM: the single-note test mocks ``jarvis.chat.voice.openrouter_complete``.
The learning engine is exercised only through its public dedupe helper.
"""

from __future__ import annotations

import contextlib
import uuid
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from jarvis.chat import wiki
from jarvis.learning import questions, voice_facts

QUESTION = "Jarvis Personalise Question"
RULE = "Jarvis Personalise Question Rule"
JLP = "Jarvis Learned Pattern"
NOTE = "Jarvis Voice Note"
WIKI = "Jarvis Wiki Page"
PROMO = "Jarvis Wiki Promotion Request"
RUN = "Jarvis Pattern Run"
SETTINGS = "Jarvis Settings"

USER_A = "persqp-user-a@example.com"
USER_B = "persqp-user-b@example.com"
ADMIN_USER = "persqp-admin@example.com"
TEST_ROLE = "Persqp Test Role"

FIXTURE_USERS = (USER_A, USER_B, ADMIN_USER)


def _ensure_user(email: str, roles: tuple = ()) -> str:
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
		u.insert(ignore_permissions=True)
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)
	return email


def _ensure_role(name: str) -> str:
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1, "is_custom": 1}).insert(
			ignore_permissions=True
		)
	return name


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _cleanup():
	frappe.db.delete(QUESTION, {"user": ["in", list(FIXTURE_USERS)]})
	frappe.db.delete(JLP, {"pattern_key": ["like", "persqp-%"]})
	# The single-note rule-fact path mints a JLP whose pattern_key is a
	# deterministic voice hash (NOT persqp-prefixed), so also clear any pattern
	# whose evidence references a fixture user - else a leftover row is seen as a
	# duplicate on the next run (its question never re-mints) and lingers as a
	# Proposed row the daily-scan backfill re-materializes.
	frappe.db.delete(JLP, {"evidence": ["like", "%persqp-%"]})
	frappe.db.delete(RULE, {"question": ["like", "%persqp%"]})
	frappe.db.delete(PROMO, {"page": ["like", "persqp%"]})
	frappe.db.delete(NOTE, {"owner": ["in", list(FIXTURE_USERS)]})
	frappe.db.delete(WIKI, {"slug": ["like", "persqp%"]})
	frappe.db.delete(WIKI, {"slug": ["like", "%persqp%"]})
	# The context-fact routing tests write the base Org note pages (org-notes--
	# <domain>), which carry no fixture marker; clear them too so a shared Org
	# page never leaks between tests.
	frappe.db.delete(WIKI, {"slug": ["like", "org-notes--%"]})
	frappe.db.delete(RUN, {"scan_mode": "voice"})
	frappe.db.commit()


class _PipelineFixture(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_role(TEST_ROLE)
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_ensure_user(ADMIN_USER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_cleanup()
		for email in FIXTURE_USERS:
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, ignore_permissions=True, force=True)
		if frappe.db.exists("Role", TEST_ROLE):
			frappe.delete_doc("Role", TEST_ROLE, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		_cleanup()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		super().tearDown()

	# -- fixtures ----------------------------------------------------------- #
	def _jlp(self, statement, evidence, detector_id="voice-context", domain="selling", **kw):
		prev = frappe.flags.jarvis_pattern_engine
		frappe.flags.jarvis_pattern_engine = True
		try:
			doc = frappe.get_doc(
				{
					"doctype": JLP,
					"detector_id": detector_id,
					"pattern_key": kw.get("pattern_key") or f"persqp-{uuid.uuid4().hex[:24]}",
					"domain": domain,
					"pattern_statement": statement,
					"skill_draft": f"- {statement}",
					"status": kw.get("status", "Proposed"),
					"evidence": frappe.as_json(evidence),
					"support_n": kw.get("support_n", 3),
					"confidence_pct": kw.get("confidence_pct", 100.0),
				}
			)
			doc.insert(ignore_permissions=True)
		finally:
			frappe.flags.jarvis_pattern_engine = prev
		return doc

	def _user_questions(self, user, **filters):
		f = {"user": user}
		f.update(filters)
		return frappe.get_all(
			QUESTION,
			filters=f,
			fields=["name", "origin", "status", "source_pattern", "source_config", "context_md", "question"],
		)


# --------------------------------------------------------------------------- #
# pattern -> question materialization
# --------------------------------------------------------------------------- #
class TestPatternQuestionMaterialization(_PipelineFixture):
	def test_voice_pattern_targets_note_owner_with_chat_origin(self):
		jlp = self._jlp(
			"Acme always ships from Mumbai.",
			{"source": "voice", "users": [USER_A], "notes": ["n1"]},
		)
		out = questions.maybe_materialize_for_pattern(jlp.name)
		self.assertEqual(out["created"], 1)
		rows = self._user_questions(USER_A)
		self.assertEqual(len(rows), 1)
		q = rows[0]
		self.assertEqual(q["origin"], "From your chat patterns")
		self.assertEqual(q["status"], "Unanswered")
		self.assertEqual(q["source_pattern"], jlp.name)
		self.assertIn("Acme always ships from Mumbai", q["context_md"])
		# owner-visibility rides doc.owner (stamped from user)
		self.assertEqual(frappe.db.get_value(QUESTION, q["name"], "owner"), USER_A)

	def test_dedupe_never_mints_a_second_question(self):
		jlp = self._jlp("Rush orders ship same day.", {"source": "voice", "users": [USER_A]})
		questions.maybe_materialize_for_pattern(jlp.name)
		questions.maybe_materialize_for_pattern(jlp.name)
		self.assertEqual(len(self._user_questions(USER_A)), 1)

	def test_deleted_question_permanently_suppresses_remint(self):
		jlp = self._jlp("Weekly stock counts on Fridays.", {"source": "voice", "users": [USER_A]})
		questions.maybe_materialize_for_pattern(jlp.name)
		q = self._user_questions(USER_A)[0]
		frappe.db.set_value(QUESTION, q["name"], "status", "Deleted")
		questions.maybe_materialize_for_pattern(jlp.name)
		# Still exactly one row (the Deleted one) — no fresh Unanswered.
		all_rows = self._user_questions(USER_A)
		self.assertEqual(len(all_rows), 1)
		self.assertEqual(all_rows[0]["status"], "Deleted")

	def test_daily_cap_defers_overflow(self):
		orig_cap = frappe.db.get_single_value(SETTINGS, "personalise_daily_question_cap")
		frappe.db.set_single_value(SETTINGS, "personalise_daily_question_cap", 2, update_modified=False)
		try:
			for i in range(3):
				jlp = self._jlp(f"Fact number {i} about A.", {"source": "voice", "users": [USER_A]})
				questions.maybe_materialize_for_pattern(jlp.name)
			learning = frappe.get_all(
				QUESTION,
				filters={"user": USER_A, "origin": ["in", list(questions.LEARNING_ORIGINS)]},
				pluck="name",
			)
			self.assertEqual(len(learning), 2)  # third is overflow, waits for a later run
		finally:
			frappe.db.set_single_value(
				SETTINGS, "personalise_daily_question_cap", orig_cap, update_modified=False
			)

	def test_detector_row_without_user_goes_to_admin_bank(self):
		jlp = self._jlp(
			"Sales orders over 1L need approval.",
			{},  # no identifiable user -> org-aggregate finding
			detector_id="approval-threshold",
		)
		with mock.patch.object(
			questions,
			"_users_with_role",
			side_effect=lambda role: [ADMIN_USER] if role == "Jarvis Admin" else [],
		):
			questions.maybe_materialize_for_pattern(jlp.name)
		rows = self._user_questions(ADMIN_USER)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["origin"], "Behavioural Learning")
		self.assertEqual(self._user_questions(USER_A), [])

	def test_daily_scan_backfills_unlinked_patterns(self):
		a = self._jlp("Backstop fact one.", {"source": "voice", "users": [USER_A]})
		b = self._jlp("Backstop fact two.", {"source": "voice", "users": [USER_B]})
		out = questions.materialize_questions_daily()
		self.assertGreaterEqual(out["created"], 2)
		self.assertEqual(self._user_questions(USER_A)[0]["source_pattern"], a.name)
		self.assertEqual(self._user_questions(USER_B)[0]["source_pattern"], b.name)
		# Re-running mints nothing new (coarse "no linked question" filter).
		again = questions.materialize_questions_daily()
		self.assertEqual(again["created"], 0)

	def test_disabled_gate_short_circuits(self):
		jlp = self._jlp("Gate test fact.", {"source": "voice", "users": [USER_A]})
		with mock.patch.object(questions, "_enabled", return_value=False):
			out = questions.maybe_materialize_for_pattern(jlp.name)
		self.assertEqual(out["created"], 0)
		self.assertEqual(self._user_questions(USER_A), [])

	def test_multi_user_pattern_asks_each_user_once(self):
		# One pattern whose evidence names TWO identifiable users must fan out to
		# BOTH - each gets exactly one question from the same source_pattern (guards
		# the per-user `continue` loop against collapsing into an early return).
		jlp = self._jlp(
			"Team ships from the Mumbai warehouse.",
			{"source": "voice", "users": [USER_A, USER_B]},
		)
		out = questions.maybe_materialize_for_pattern(jlp.name)
		self.assertEqual(out["created"], 2)
		for user in (USER_A, USER_B):
			rows = self._user_questions(user, source_pattern=jlp.name)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0]["origin"], "From your chat patterns")

	def test_multi_user_pattern_caps_are_independent(self):
		# Per-user cap is independent: USER_A is already at cap, USER_B is not, so a
		# shared pattern asks only USER_B (A's overflow waits for a later run).
		orig_cap = frappe.db.get_single_value(SETTINGS, "personalise_daily_question_cap")
		frappe.db.set_single_value(SETTINGS, "personalise_daily_question_cap", 1, update_modified=False)
		try:
			filler = self._jlp("Filler fact for A.", {"source": "voice", "users": [USER_A]})
			questions.maybe_materialize_for_pattern(filler.name)  # USER_A spends their slot
			self.assertEqual(len(self._user_questions(USER_A)), 1)
			shared = self._jlp("Shared fact for A and B.", {"source": "voice", "users": [USER_A, USER_B]})
			out = questions.maybe_materialize_for_pattern(shared.name)
			self.assertEqual(out["created"], 1)  # only USER_B
			self.assertEqual(len(self._user_questions(USER_A, source_pattern=shared.name)), 0)
			self.assertEqual(len(self._user_questions(USER_B, source_pattern=shared.name)), 1)
		finally:
			frappe.db.set_single_value(
				SETTINGS, "personalise_daily_question_cap", orig_cap, update_modified=False
			)


# --------------------------------------------------------------------------- #
# rule -> question materialization
# --------------------------------------------------------------------------- #
class TestRuleQuestionMaterialization(_PipelineFixture):
	def _rule(self, **kwargs):
		fields = {
			"doctype": RULE,
			"question": "persqp: how do you usually handle rush orders?",
			"scope": "Org",
			"active": 1,
		}
		fields.update(kwargs)
		doc = frappe.get_doc(fields)
		doc.insert(ignore_permissions=True)
		return doc

	def test_org_scope_fans_out_to_jarvis_user_and_sm(self):
		rule = self._rule(context_md="some context")
		with mock.patch.object(
			questions,
			"_users_with_role",
			side_effect=lambda role: {"Jarvis User": [USER_A], "System Manager": [USER_B]}.get(role, []),
		):
			out = questions.materialize_rule_questions(rule.name)
		self.assertEqual(out["created"], 2)
		for user in (USER_A, USER_B):
			rows = self._user_questions(user, source_config=rule.name)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0]["origin"], "From your organisation")
			self.assertEqual(rows[0]["question"], rule.question)

	def test_org_scope_includes_jarvis_admin(self):
		# An admin who authors an org-wide question is part of the org and must be
		# asked it too — a Jarvis-Admin-only account was previously left out.
		rule = self._rule()
		with mock.patch.object(
			questions,
			"_users_with_role",
			side_effect=lambda role: {"Jarvis Admin": [ADMIN_USER]}.get(role, []),
		):
			out = questions.materialize_rule_questions(rule.name)
		self.assertEqual(out["created"], 1)
		self.assertEqual(len(self._user_questions(ADMIN_USER, source_config=rule.name)), 1)

	def test_role_scope_only_targets_role_holders(self):
		rule = self._rule(scope="Role", target_role=TEST_ROLE)
		with mock.patch.object(
			questions,
			"_users_with_role",
			side_effect=lambda role: [USER_A] if role == TEST_ROLE else [],
		):
			questions.materialize_rule_questions(rule.name)
		self.assertEqual(len(self._user_questions(USER_A, source_config=rule.name)), 1)
		self.assertEqual(self._user_questions(USER_B, source_config=rule.name), [])

	def test_user_scope_targets_single_user(self):
		rule = self._rule(scope="User", target_user=USER_B)
		questions.materialize_rule_questions(rule.name)
		self.assertEqual(len(self._user_questions(USER_B, source_config=rule.name)), 1)
		self.assertEqual(self._user_questions(USER_A, source_config=rule.name), [])

	def test_rule_dedupe_and_deleted_suppression(self):
		rule = self._rule(scope="User", target_user=USER_A)
		questions.materialize_rule_questions(rule.name)
		questions.materialize_rule_questions(rule.name)  # dedupe
		self.assertEqual(len(self._user_questions(USER_A, source_config=rule.name)), 1)
		q = self._user_questions(USER_A, source_config=rule.name)[0]
		frappe.db.set_value(QUESTION, q["name"], "status", "Deleted")
		questions.materialize_rule_questions(rule.name)  # suppressed
		rows = self._user_questions(USER_A, source_config=rule.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["status"], "Deleted")

	def test_inactive_rule_is_skipped(self):
		rule = self._rule(scope="User", target_user=USER_A, active=0)
		out = questions.materialize_rule_questions(rule.name)
		self.assertEqual(out["created"], 0)
		self.assertEqual(self._user_questions(USER_A, source_config=rule.name), [])

	def test_rule_questions_are_uncapped(self):
		orig_cap = frappe.db.get_single_value(SETTINGS, "personalise_daily_question_cap")
		frappe.db.set_single_value(SETTINGS, "personalise_daily_question_cap", 0, update_modified=False)
		try:
			rule = self._rule(scope="User", target_user=USER_A)
			out = questions.materialize_rule_questions(rule.name)
			# cap=0 kills learning questions, but org-rule questions ignore the cap.
			self.assertEqual(out["created"], 1)
		finally:
			frappe.db.set_single_value(
				SETTINGS, "personalise_daily_question_cap", orig_cap, update_modified=False
			)


# --------------------------------------------------------------------------- #
# scope-aware wiki upsert
# --------------------------------------------------------------------------- #
class TestScopeAwareWikiUpsert(_PipelineFixture):
	def _update(self, append):
		return [
			{
				"slug": "persqp-scope-test",
				"title": "Persqp Scope Test",
				"page_type": "Org",
				"append_md": append,
			}
		]

	def test_org_path_unchanged(self):
		applied, failed = wiki.apply_extracted_page_updates(self._update("- org line"), "voice", USER_A)
		self.assertEqual((applied, failed), (1, 0))
		doc = frappe.get_doc(WIKI, {"slug": "persqp-scope-test"})
		self.assertEqual(doc.get("scope") or "Org", "Org")
		self.assertIsNone(doc.get("target_user"))
		self.assertIn("- org line", doc.body_md)

	def test_user_scope_forks_to_suffixed_personal_page(self):
		applied, failed = wiki.apply_extracted_page_updates(
			self._update("- private line"),
			"voice",
			USER_A,
			default_scope="User",
			target_user=USER_A,
		)
		self.assertEqual((applied, failed), (1, 0))
		# No Org page was created.
		self.assertFalse(frappe.db.exists(WIKI, {"slug": "persqp-scope-test"}))
		expected = wiki.user_scope_slug("persqp-scope-test", USER_A)
		self.assertIn("--u-", expected)
		doc = frappe.get_doc(WIKI, {"slug": expected})
		self.assertEqual(doc.scope, "User")
		self.assertEqual(doc.target_user, USER_A)
		self.assertIn("- private line", doc.body_md)

	def test_user_scope_reingest_merges_same_page(self):
		wiki.apply_extracted_page_updates(
			self._update("- first"),
			"voice",
			USER_A,
			default_scope="User",
			target_user=USER_A,
		)
		wiki.apply_extracted_page_updates(
			self._update("- second"),
			"voice",
			USER_A,
			default_scope="User",
			target_user=USER_A,
		)
		slug = wiki.user_scope_slug("persqp-scope-test", USER_A)
		pages = frappe.get_all(WIKI, filters={"slug": ["like", "persqp-scope-test%"]}, pluck="name")
		self.assertEqual(len(pages), 1)  # merged, not duplicated
		body = frappe.db.get_value(WIKI, {"slug": slug}, "body_md")
		self.assertIn("- first", body)
		self.assertIn("- second", body)

	def test_two_users_get_distinct_pages(self):
		wiki.apply_extracted_page_updates(
			self._update("- A note"), "voice", USER_A, default_scope="User", target_user=USER_A
		)
		wiki.apply_extracted_page_updates(
			self._update("- B note"), "voice", USER_B, default_scope="User", target_user=USER_B
		)
		self.assertNotEqual(
			wiki.user_scope_slug("persqp-scope-test", USER_A),
			wiki.user_scope_slug("persqp-scope-test", USER_B),
		)
		self.assertEqual(len(frappe.get_all(WIKI, filters={"slug": ["like", "persqp-scope-test%"]})), 2)


# --------------------------------------------------------------------------- #
# wiki promotion (User -> Role/Org through the reviewer)
# --------------------------------------------------------------------------- #
class TestWikiPromotion(_PipelineFixture):
	def _user_page(self, owner=USER_A, body="Ships rush orders on Fridays."):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": "persqp-promo",
				"title": "Persqp Promo Note",
				"page_type": "People",
				"scope": "User",
				"target_user": owner,
				"status": "Active",
				"body_md": body,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_owner_requests_promotion_snapshots_body(self):
		page = self._user_page()
		with _as(USER_A):
			out = wiki.request_wiki_promotion(page.slug, "Org", note="please widen")
		self.assertTrue(out["ok"])
		req = frappe.get_doc(PROMO, out["request"])
		self.assertEqual(req.status, "Pending")
		self.assertEqual(req.from_scope, "User")
		self.assertEqual(req.to_scope, "Org")
		self.assertEqual(req.body_snapshot, "Ships rush orders on Fridays.")
		self.assertEqual(req.owner, USER_A)

	def test_non_owner_cannot_request(self):
		page = self._user_page(owner=USER_A)
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			wiki.request_wiki_promotion(page.slug, "Org")

	def test_bad_target_scope_rejected(self):
		page = self._user_page()
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			wiki.request_wiki_promotion(page.slug, "User")

	def test_approve_merges_body_into_org_page(self):
		page = self._user_page()
		with _as(USER_A):
			out = wiki.request_wiki_promotion(page.slug, "Org")
		res = wiki.apply_promotion(out["request"], approve=1, note="ok", reviewer=ADMIN_USER)
		self.assertTrue(res["ok"])
		self.assertEqual(res["status"], "Approved")
		# Target Org page uses the BASE slug (no audience suffix) and carries the body.
		self.assertEqual(res["slug"], "persqp-promo")
		org = frappe.get_doc(WIKI, {"slug": "persqp-promo"})
		self.assertEqual(org.get("scope") or "Org", "Org")
		self.assertIn("Ships rush orders on Fridays.", org.body_md)
		# Source User page is intact.
		self.assertTrue(frappe.db.exists(WIKI, {"slug": page.slug}))
		req = frappe.get_doc(PROMO, out["request"])
		self.assertEqual(req.status, "Approved")
		self.assertEqual(req.reviewer, ADMIN_USER)
		self.assertIsNotNone(req.decided_at)

	def test_approve_to_role_creates_role_suffixed_page(self):
		page = self._user_page()
		with _as(USER_A):
			out = wiki.request_wiki_promotion(page.slug, "Role", target_role=TEST_ROLE)
		res = wiki.apply_promotion(out["request"], approve=1, reviewer=ADMIN_USER)
		self.assertIn("--r-", res["slug"])
		role_page = frappe.get_doc(WIKI, {"slug": res["slug"]})
		self.assertEqual(role_page.scope, "Role")
		self.assertEqual(role_page.target_role, TEST_ROLE)
		self.assertIn("Ships rush orders on Fridays.", role_page.body_md)

	def test_reject_leaves_no_target_page(self):
		page = self._user_page()
		with _as(USER_A):
			out = wiki.request_wiki_promotion(page.slug, "Org")
		res = wiki.apply_promotion(out["request"], approve=0, note="not org-wide")
		self.assertEqual(res["status"], "Rejected")
		self.assertFalse(frappe.db.exists(WIKI, {"slug": "persqp-promo"}))
		self.assertEqual(frappe.db.get_value(PROMO, out["request"], "status"), "Rejected")

	def test_decide_is_idempotent(self):
		page = self._user_page()
		with _as(USER_A):
			out = wiki.request_wiki_promotion(page.slug, "Org")
		wiki.apply_promotion(out["request"], approve=1, reviewer=ADMIN_USER)
		second = wiki.apply_promotion(out["request"], approve=1, reviewer=ADMIN_USER)
		self.assertFalse(second["ok"])


# --------------------------------------------------------------------------- #
# immediate single-note ingest
# --------------------------------------------------------------------------- #
def _facts_json(*items) -> str:
	return frappe.as_json(list(items))


class TestSingleNoteIngest(_PipelineFixture):
	def _note(self, owner=USER_A, **kwargs):
		fields = {
			"doctype": NOTE,
			"transcript": "Acme prefers deliveries in the morning.",
			"context_type": "Business",
			"source": "Personalise",
			"status": "New",
		}
		fields.update(kwargs)
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc(fields)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.set_user(prev)
		return doc

	def test_context_fact_forks_to_owner_user_wiki_and_publishes_receipt(self):
		note = self._note()
		fact = {
			"statement": "Acme prefers morning deliveries.",
			"domain": "selling",
			"names_party": True,
			"kind": "context",
		}
		with (
			mock.patch("jarvis.chat.voice.openrouter_complete", return_value=_facts_json(fact)),
			mock.patch("jarvis.chat.wiki.publish_to_user") as pub,
		):
			out = voice_facts.process_single_note(note)

		slug = wiki.user_scope_slug("org-notes--selling", USER_A)
		page = frappe.get_doc(WIKI, {"slug": slug})
		self.assertEqual(page.scope, "User")
		self.assertEqual(page.target_user, USER_A)
		self.assertIn("Acme prefers morning deliveries.", page.body_md)
		# Note consumed.
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "Processed")
		self.assertEqual(out["applied"], 1)
		# personalise:processed receipt fired to the owner with the page listed.
		receipts = [
			c.args[1]
			for c in pub.call_args_list
			if len(c.args) >= 2 and c.args[1].get("kind") == "personalise:processed"
		]
		self.assertTrue(receipts)
		self.assertEqual(receipts[0]["note"], note.name)
		self.assertEqual(receipts[0]["pages"][0]["slug"], slug)
		# No Org page leaked.
		self.assertFalse(frappe.db.exists(WIKI, {"slug": "org-notes--selling"}))

	def test_rule_fact_mints_learned_pattern_and_question(self):
		note = self._note(transcript="We always ship Acme from the Mumbai warehouse.")
		fact = {
			"statement": "Acme always ships from Mumbai.",
			"domain": "stock",
			"names_party": True,
			"kind": "rule",
		}
		with mock.patch("jarvis.chat.voice.openrouter_complete", return_value=_facts_json(fact)):
			voice_facts.process_single_note(note)

		key = voice_facts._pattern_key("Acme always ships from Mumbai.")
		jlp = frappe.db.exists(JLP, {"pattern_key": key})
		self.assertTrue(jlp)
		# The lifecycle hook materialized a question for the note owner.
		q = frappe.get_all(
			QUESTION, filters={"user": USER_A, "source_pattern": jlp}, fields=["origin", "status"]
		)
		self.assertEqual(len(q), 1)
		self.assertEqual(q[0]["origin"], "From your chat patterns")
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "Processed")

	def test_extracted_text_feeds_the_prompt(self):
		note = self._note(
			kind="Attachment",
			transcript="",
			attachment="/private/files/persqp.pdf",
			extracted_text="Distinctive extracted marker phrase seven.",
		)
		with mock.patch("jarvis.chat.voice.openrouter_complete", return_value="[]") as m:
			voice_facts.process_single_note(note)
		# The extraction prompt (user message) carried the extracted_text.
		self.assertTrue(m.call_args, "extraction was not called")
		user_msg = m.call_args.args[0][1]["content"]
		self.assertIn("Distinctive extracted marker phrase seven.", user_msg)
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "Processed")

	def test_failed_extraction_leaves_note_new(self):
		note = self._note()
		with mock.patch("jarvis.chat.voice.openrouter_complete", side_effect=frappe.ValidationError("down")):
			voice_facts.process_single_note(note)
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "New")


# --------------------------------------------------------------------------- #
# immediate-ingest queue worker (questions._run_note_ingest)
# --------------------------------------------------------------------------- #
class TestNoteIngestWorker(_PipelineFixture):
	"""The real queue worker behind the answer/free-capture immediate-ingest
	latency contract. In tests ``frappe.enqueue`` genuinely queues to Redis, so
	this glue (existence check, the atomic for_update New-claim, the delegating
	call) is never exercised via the API path - drive it directly."""

	def _note(self, owner=USER_A, **kwargs):
		fields = {
			"doctype": NOTE,
			"transcript": "Acme prefers morning deliveries.",
			"context_type": "Business",
			"source": "Personalise",
			"status": "New",
		}
		fields.update(kwargs)
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc(fields)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.set_user(prev)
		return doc

	def test_new_note_is_processed(self):
		note = self._note()
		with mock.patch.object(voice_facts, "process_single_note") as m:
			questions._run_note_ingest(note.name)
		m.assert_called_once()
		self.assertEqual(m.call_args.args[0].name, note.name)

	def test_missing_note_is_a_noop(self):
		with mock.patch.object(voice_facts, "process_single_note") as m:
			questions._run_note_ingest("JVN-does-not-exist-xyz")
		m.assert_not_called()

	def test_non_new_note_is_a_noop_the_race_case(self):
		# The daily sweep already claimed/consumed the note (status advanced past
		# New under the row lock), so the immediate job must NOT re-extract and
		# double-append to the wiki.
		note = self._note()
		frappe.db.set_value(NOTE, note.name, "status", "Processed")
		frappe.db.commit()
		with mock.patch.object(voice_facts, "process_single_note") as m:
			questions._run_note_ingest(note.name)
		m.assert_not_called()


# --------------------------------------------------------------------------- #
# cross-source context-fact merge + scope routing (voice_facts._merge_fact /
# _apply_context_facts) - the User->Org privacy rail (DESIGN.md 1)
# --------------------------------------------------------------------------- #
class TestCrossSourceMerge(_PipelineFixture):
	DOMAIN = "selling"

	def _merge(self, *contribs):
		"""Merge ``(statement, owner, personalise)`` contributions into one facts
		list exactly as the daily sweep does across batches (batches are per-
		(owner, personalise), so each contribution is its own batch)."""
		facts: dict = {}
		for stmt, owner, personalise in contribs:
			row = frappe._dict(name=f"note-{owner}-{personalise}", creation="2026-07-01 00:00:00")
			batch = {"owner": owner, "personalise": personalise, "notes": [row]}
			fact = {
				"statement": stmt,
				"domain": self.DOMAIN,
				"names_party": False,
				"kind": "context",
			}
			voice_facts._merge_fact(facts, fact, batch)
		return list(facts.values())

	def _org_slug(self):
		return f"org-notes--{self.DOMAIN}"

	def _user_slug(self, user):
		return wiki.user_scope_slug(self._org_slug(), user)

	def test_two_personalise_users_fork_to_each_user_never_org(self):
		# Same statement from two Personalise owners: it must fan out to EACH
		# owner's private User page and NEVER to the shared Org page (finding [2]:
		# a personalise contribution never auto-crosses to Org).
		facts = self._merge(
			("Team ships from Mumbai.", USER_A, True),
			("Team ships from Mumbai.", USER_B, True),
		)
		self.assertEqual(len(facts), 1)  # merged on pattern key
		voice_facts._apply_context_facts(facts)
		self.assertTrue(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_A)}))
		self.assertTrue(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_B)}))
		self.assertFalse(frappe.db.exists(WIKI, {"slug": self._org_slug()}))

	def test_personalise_and_org_source_split_across_scopes(self):
		# A statement seen by both a Personalise owner (A) and an Org source
		# (Business Tab, owner B): A keeps a private copy, the Org-sourced half
		# lands on the shared Org page, and B gets no private page.
		facts = self._merge(
			("Rush orders ship same day.", USER_A, True),
			("Rush orders ship same day.", USER_B, False),
		)
		self.assertEqual(len(facts), 1)
		voice_facts._apply_context_facts(facts)
		self.assertTrue(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_A)}))
		org = frappe.db.get_value(WIKI, {"slug": self._org_slug()}, "name")
		self.assertTrue(org)
		self.assertIn("Rush orders ship same day.", frappe.db.get_value(WIKI, org, "body_md"))
		self.assertFalse(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_B)}))

	def test_single_org_source_stays_org(self):
		# Unchanged Org behavior: a lone non-Personalise contribution routes to the
		# shared Org page and never forks a private page.
		facts = self._merge(("Invoices go out on Mondays.", USER_A, False))
		voice_facts._apply_context_facts(facts)
		self.assertTrue(frappe.db.exists(WIKI, {"slug": self._org_slug()}))
		self.assertFalse(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_A)}))

	def test_two_org_users_stay_org(self):
		# Two Org-sourced owners on the same statement: ambiguous owner stays Org
		# (no private fork for either).
		facts = self._merge(
			("Stock counts happen on Fridays.", USER_A, False),
			("Stock counts happen on Fridays.", USER_B, False),
		)
		self.assertEqual(len(facts), 1)
		voice_facts._apply_context_facts(facts)
		self.assertTrue(frappe.db.exists(WIKI, {"slug": self._org_slug()}))
		self.assertFalse(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_A)}))
		self.assertFalse(frappe.db.exists(WIKI, {"slug": self._user_slug(USER_B)}))
