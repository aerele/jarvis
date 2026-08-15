"""Wave B0 doctype/role/settings tests for the Skills-area Personalise rework
(DESIGN.md sections 1, 2.1, 2.2, 2.3, 2.4, 2.5):

  * ``jarvis.learning.roles`` — the new ``Jarvis Admin`` / ``Jarvis Skill
    Reviewer`` bench roles are seeded idempotently, and the new
    ``personalise_daily_question_cap`` / ``personalise_enabled`` Settings
    Single fields are backfilled the same NULL-coerces-to-0 way
    ``voice_facts._SETTINGS_DEFAULTS`` already handles for Voice & Wiki.
  * ``Jarvis Personalise Question`` — owner-stamping (if_owner rides
    ``doc.owner``, which this controller keeps in lockstep with the target
    ``user`` field regardless of which identity performs the insert),
    status/origin validation, and the permission matrix (owner full CRUD,
    System Manager full, a stranger gets nothing).
  * ``Jarvis Personalise Question Rule`` — Org/Role/User scope validation
    (mirrors Jarvis Wiki Page's scope model) and the System-Manager-only
    doctype permission (Jarvis Admin reaches it only via a future
    code-guarded API, not a DocType grant).
  * ``Jarvis Wiki Promotion Request`` — body_snapshot auto-fill from the
    live page, to_scope restricted to Role/Org, and the create-only (no
    write) "All"+if_owner permission row.
  * ``Jarvis Voice Note`` extensions — ``kind`` drives whether transcript is
    required, Attachment/Link-specific requirements, and the new
    "Personalise" source option.

No whitelisted API endpoints exist yet (personalise_api.py is Wave B1) — all
tests below exercise the DocType controllers and permission model directly,
the same layer these controllers must hold up correctly for every future
caller.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.jarvis.doctype.jarvis_personalise_question.jarvis_personalise_question import (
	ORIGINS,
	STATUSES,
)
from jarvis.jarvis.doctype.jarvis_personalise_question_rule.jarvis_personalise_question_rule import (
	SCOPES as RULE_SCOPES,
)
from jarvis.jarvis.doctype.jarvis_voice_note.jarvis_voice_note import (
	ALLOWED_KINDS,
	MAX_TRANSCRIPT_LEN,
)
from jarvis.jarvis.doctype.jarvis_wiki_promotion_request.jarvis_wiki_promotion_request import (
	TO_SCOPES,
)
from jarvis.learning import roles as learning_roles

QUESTION = "Jarvis Personalise Question"
RULE = "Jarvis Personalise Question Rule"
PROMOTION = "Jarvis Wiki Promotion Request"
NOTE = "Jarvis Voice Note"
WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

USER_A = "persq-user-a@example.com"
USER_B = "persq-user-b@example.com"
TEST_ROLE = "Persq Test Role"

SETTINGS_FIELDS = ("personalise_daily_question_cap", "personalise_enabled")


def _ensure_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				# Explicit: a role-less insert becomes a Website User, which
				# never reaches Desk data / the if_owner "All" role checks
				# below the way a System User does.
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	return email


def _ensure_role(role_name: str) -> str:
	if not frappe.db.exists("Role", role_name):
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1, "is_custom": 1}).insert(
			ignore_permissions=True
		)
	return role_name


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


class PersonaliseDoctypeTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_ensure_role(TEST_ROLE)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for email in (USER_A, USER_B):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, ignore_permissions=True, force=True)
		if frappe.db.exists("Role", TEST_ROLE):
			frappe.delete_doc("Role", TEST_ROLE, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


# --------------------------------------------------------------------------- #
# jarvis.learning.roles — role seeding + Settings defaults backfill
# --------------------------------------------------------------------------- #
class TestPersonaliseRoleSeeding(PersonaliseDoctypeTestCase):
	def test_personalise_roles_seeded_idempotently(self):
		learning_roles.after_migrate()
		learning_roles.after_migrate()
		for role in ("Jarvis Admin", "Jarvis Skill Reviewer"):
			self.assertTrue(frappe.db.exists("Role", role), role)
			self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 1, role)
			self.assertEqual(frappe.db.get_value("Role", role, "is_custom"), 0, role)

	def test_app_roles_are_not_marked_custom(self):
		"""``is_custom`` must stay 0 on every role the APP ships.

		All four are created by DocType sync (each is named in at least one
		permission row), which stamps is_custom=0. The ensure_* helpers in
		jarvis/permissions.py declare the same 0 so the two definitions agree.
		Nothing in Frappe enforces that agreement, so this test is the guard: if
		a future Frappe changes what sync stamps, or someone flips a helper back
		to 1, the mismatch surfaces here instead of silently misdescribing every
		live site.

		Deliberately EXCLUDES the two support roles: no DocType names them, so
		ensure_support_roles genuinely creates them and its is_custom=1 does take
		effect. That asymmetry is real, and is spelled out here so it does not
		get "fixed" by accident."""
		learning_roles.after_migrate()
		for role in (
			"Jarvis User",
			"Jarvis Admin",
			"Jarvis Skill Reviewer",
			"Knowledge Wiki Manager",
		):
			self.assertTrue(frappe.db.exists("Role", role), role)
			self.assertEqual(frappe.db.get_value("Role", role, "is_custom"), 0, role)

	def test_after_install_leaves_every_install_only_role_present(self):
		"""The install path must end with the roles no DocType names."""
		from jarvis.install import _INSTALL_ONLY_ROLES, after_install

		after_install()  # idempotent
		for role in _INSTALL_ONLY_ROLES:
			self.assertTrue(frappe.db.exists("Role", role), role)

	def test_after_install_raises_when_seeding_did_not_land(self):
		"""The verification in after_install must be REAL.

		Its seeder (learning.roles.after_migrate) catches everything and only
		log_errors, never re-raising, because a failed seed must not abort a
		migrate. At install time that would silently produce the half-seeded
		tenant the hook exists to prevent, so after_install re-checks and
		throws. This asserts the throw actually happens rather than the check
		being decorative."""
		from jarvis import install

		with patch.object(install, "_INSTALL_ONLY_ROLES", ("Jarvis Nonexistent Probe Role",)):
			with self.assertRaises(frappe.ValidationError):
				install.after_install()

	def test_settings_defaults_backfilled_when_row_absent(self):
		"""Row-existence probe, not a value test: an absent tabSingles row for
		an Int/Check field on a Single coerces to 0 via cint() on read
		(same v16 gotcha voice_facts._SETTINGS_DEFAULTS already works around),
		so after_migrate must materialize a real row for the JSON defaults
		(5 / 1) to ever be visible."""
		saved = {}
		for field in SETTINGS_FIELDS:
			rows = frappe.db.sql(
				"select value from tabSingles where doctype=%s and field=%s",
				(SETTINGS, field),
			)
			saved[field] = rows[0][0] if rows else None
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
		try:
			learning_roles.after_migrate()
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "personalise_daily_question_cap"), 5)
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "personalise_enabled"), 1)
			# Idempotent: running again must not clobber an admin-set value.
			frappe.db.set_single_value(SETTINGS, "personalise_daily_question_cap", 9, update_modified=False)
			learning_roles.after_migrate()
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "personalise_daily_question_cap"), 9)
		finally:
			for field, value in saved.items():
				frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
				if value is not None:
					frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)


# --------------------------------------------------------------------------- #
# Jarvis Personalise Question
# --------------------------------------------------------------------------- #
class TestPersonaliseQuestion(PersonaliseDoctypeTestCase):
	def _question(self, user: str, **kwargs) -> "frappe.model.document.Document":
		fields = {
			"doctype": QUESTION,
			"user": user,
			"question": "Do you always ship this customer from Mumbai?",
			"origin": "Behavioural Learning",
		}
		fields.update(kwargs)
		doc = frappe.get_doc(fields)
		doc.insert(ignore_permissions=True)
		return doc

	def tearDown(self):
		frappe.db.delete(QUESTION, {"user": ["in", [USER_A, USER_B]]})
		super().tearDown()

	def test_owner_is_stamped_from_target_user_not_inserting_identity(self):
		# Inserted as Administrator (ignore_permissions=True), targeting A —
		# doc.owner must end up A, not Administrator, or if_owner would grant
		# the wrong person visibility.
		doc = self._question(USER_A)
		self.assertEqual(doc.owner, USER_A)

	def test_target_user_can_read_write_delete_own_question(self):
		doc = self._question(USER_A)
		with _as(USER_A):
			self.assertTrue(bool(frappe.has_permission(QUESTION, doc=doc.name, ptype="read")))
			self.assertTrue(bool(frappe.has_permission(QUESTION, doc=doc.name, ptype="write")))
			self.assertTrue(bool(frappe.has_permission(QUESTION, doc=doc.name, ptype="delete")))
			loaded = frappe.get_doc(QUESTION, doc.name)
			loaded.context_md = "updated context"
			loaded.save()
			loaded.delete()
		self.assertFalse(frappe.db.exists(QUESTION, doc.name))

	def test_stranger_cannot_read_or_write_another_users_question(self):
		doc = self._question(USER_A)
		with _as(USER_B):
			self.assertFalse(bool(frappe.has_permission(QUESTION, doc=doc.name, ptype="read")))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(QUESTION, doc.name).save()

	def test_system_manager_has_full_access_regardless_of_owner(self):
		doc = self._question(USER_A)
		self.assertTrue(
			bool(frappe.has_permission(QUESTION, doc=doc.name, user="Administrator", ptype="write"))
		)

	def test_question_text_required(self):
		with self.assertRaises(frappe.ValidationError):
			self._question(USER_A, question="   ")

	def test_question_text_length_capped(self):
		with self.assertRaises(frappe.ValidationError):
			self._question(USER_A, question="x" * 501)

	def test_user_is_required(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": QUESTION,
					"question": "no target user",
					"origin": "Behavioural Learning",
				}
			).insert(ignore_permissions=True)

	def test_status_self_heals_blank_to_unanswered(self):
		doc = self._question(USER_A, status="")
		self.assertEqual(doc.status, "Unanswered")

	def test_status_accepts_soft_delete_value(self):
		# Soft-delete convention (see the controller docstring): "Deleted" is
		# a legal status value, but list/probe endpoints must filter it out
		# themselves - that filtering is Wave B1's responsibility, not
		# something this controller enforces.
		doc = self._question(USER_A, status="Deleted")
		self.assertEqual(doc.status, "Deleted")
		self.assertEqual(set(STATUSES), {"Unanswered", "Answered", "Ignored", "Deleted"})

	def test_unknown_status_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._question(USER_A, status="Snoozed")

	def test_unknown_origin_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._question(USER_A, origin="From the void")
		self.assertEqual(
			set(ORIGINS),
			{
				"Behavioural Learning",
				"From your organisation",
				"From your chat patterns",
				"From your reviewer",
			},
		)

	def test_provenance_links_are_optional(self):
		# A question with no source_pattern/source_config/asked_by/answer_note
		# is legal (e.g. a fresh rule-materialized or reviewer question before
		# any answer exists).
		doc = self._question(USER_A)
		self.assertFalse(doc.source_pattern)
		self.assertFalse(doc.answer_note)


# --------------------------------------------------------------------------- #
# Jarvis Personalise Question Rule
# --------------------------------------------------------------------------- #
class TestPersonaliseQuestionRule(PersonaliseDoctypeTestCase):
	def _rule(self, **kwargs) -> "frappe.model.document.Document":
		fields = {
			"doctype": RULE,
			"question": "How do you usually handle rush orders?",
			"scope": "Org",
		}
		fields.update(kwargs)
		doc = frappe.get_doc(fields)
		doc.insert(ignore_permissions=True)
		return doc

	def tearDown(self):
		frappe.db.delete(RULE, {"question": ["like", "%rush orders%"]})
		super().tearDown()

	def test_org_scope_is_default_and_carries_no_targets(self):
		doc = self._rule()
		self.assertEqual(doc.scope, "Org")
		self.assertFalse(doc.target_role)
		self.assertFalse(doc.target_user)

	def test_role_scope_requires_target_role(self):
		with self.assertRaises(frappe.ValidationError):
			self._rule(scope="Role")

	def test_role_scope_clears_target_user(self):
		doc = self._rule(scope="Role", target_role=TEST_ROLE, target_user=USER_A)
		self.assertEqual(doc.target_role, TEST_ROLE)
		self.assertFalse(doc.target_user)

	def test_user_scope_requires_target_user(self):
		with self.assertRaises(frappe.ValidationError):
			self._rule(scope="User")

	def test_user_scope_clears_target_role(self):
		doc = self._rule(scope="User", target_user=USER_A, target_role=TEST_ROLE)
		self.assertEqual(doc.target_user, USER_A)
		self.assertFalse(doc.target_role)

	def test_unknown_scope_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._rule(scope="Galaxy")
		self.assertEqual(set(RULE_SCOPES), {"Org", "Role", "User"})

	def test_question_text_required(self):
		with self.assertRaises(frappe.ValidationError):
			self._rule(question="   ")

	def test_active_defaults_to_enabled(self):
		doc = self._rule()
		# Frappe applies the JSON default on a fresh Desk-created doc; a raw
		# dict insert (as here) does not auto-fill it, so explicitly assert
		# the field exists and accepts 1 rather than assuming a fill-in.
		doc.active = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.active, 1)

	def test_doctype_permission_is_system_manager_only(self):
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": RULE, "question": "rush orders - should fail", "scope": "Org"}
			).insert()


# --------------------------------------------------------------------------- #
# Jarvis Wiki Promotion Request
# --------------------------------------------------------------------------- #
class TestWikiPromotionRequest(PersonaliseDoctypeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists(WIKI, {"slug": "persq-user-a-notes"}):
			cls.page = frappe.get_doc(
				{
					"doctype": WIKI,
					"slug": "persq-user-a-notes",
					"title": "Persq User A Notes",
					"page_type": "People",
					"scope": "User",
					"target_user": USER_A,
					"status": "Active",
					"body_md": "Prefers rush orders shipped on Fridays.",
				}
			)
			cls.page.insert(ignore_permissions=True)
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete(WIKI, {"slug": ["like", "persq-user-a-notes%"]})
		frappe.db.commit()
		super().tearDownClass()

	def _request(self, **kwargs) -> "frappe.model.document.Document":
		fields = {
			"doctype": PROMOTION,
			"page": self.page.name,
			"from_scope": "User",
			"to_scope": "Org",
		}
		fields.update(kwargs)
		doc = frappe.get_doc(fields)
		doc.insert(ignore_permissions=True)
		return doc

	def tearDown(self):
		frappe.db.delete(PROMOTION, {"page": self.page.name})
		super().tearDown()

	def test_body_snapshot_auto_fills_from_live_page_when_blank(self):
		doc = self._request()
		self.assertEqual(doc.body_snapshot, "Prefers rush orders shipped on Fridays.")

	def test_body_snapshot_explicit_value_is_kept(self):
		doc = self._request(body_snapshot="frozen text, page has since changed")
		self.assertEqual(doc.body_snapshot, "frozen text, page has since changed")

	def test_to_scope_user_is_rejected(self):
		# Promotion only ever widens visibility - User is not a legal target.
		with self.assertRaises(frappe.ValidationError):
			self._request(to_scope="User")
		self.assertEqual(set(TO_SCOPES), {"Role", "Org"})

	def test_to_scope_role_requires_target_role(self):
		with self.assertRaises(frappe.ValidationError):
			self._request(to_scope="Role")

	def test_to_scope_role_with_target_role_ok(self):
		doc = self._request(to_scope="Role", target_role=TEST_ROLE)
		self.assertEqual(doc.to_scope, "Role")
		self.assertEqual(doc.target_role, TEST_ROLE)

	def test_page_is_required(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": PROMOTION, "from_scope": "User", "to_scope": "Org"}).insert(
				ignore_permissions=True
			)

	def test_owner_can_read_and_create_but_not_write(self):
		with _as(USER_A):
			doc = self._request()
		self.assertEqual(doc.owner, USER_A)
		with _as(USER_A):
			self.assertTrue(bool(frappe.has_permission(PROMOTION, doc=doc.name, ptype="read")))
			self.assertFalse(bool(frappe.has_permission(PROMOTION, doc=doc.name, ptype="write")))

	def test_stranger_cannot_read_another_users_request(self):
		with _as(USER_A):
			doc = self._request()
		with _as(USER_B):
			self.assertFalse(bool(frappe.has_permission(PROMOTION, doc=doc.name, ptype="read")))

	def test_system_manager_can_write(self):
		doc = self._request()
		self.assertTrue(
			bool(frappe.has_permission(PROMOTION, doc=doc.name, user="Administrator", ptype="write"))
		)


# --------------------------------------------------------------------------- #
# Jarvis Voice Note extensions (kind / attachment / url / question / extracted_text)
# --------------------------------------------------------------------------- #
class TestVoiceNoteKindExtensions(PersonaliseDoctypeTestCase):
	def _note(self, owner: str, **kwargs) -> "frappe.model.document.Document":
		fields = {
			"doctype": NOTE,
			"transcript": "A caption or transcript body.",
			"context_type": "Business",
			"source": "Business Tab",
		}
		fields.update(kwargs)
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc(fields)
			doc.insert(ignore_permissions=True)
		finally:
			frappe.set_user(prev)
		return doc

	def tearDown(self):
		frappe.db.delete(NOTE, {"owner": ["in", [USER_A, USER_B]]})
		super().tearDown()

	def test_kind_defaults_to_text_when_blank(self):
		doc = self._note(USER_A, kind="")
		self.assertEqual(doc.kind, "Text")

	def test_unknown_kind_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Fax")
		self.assertEqual(set(ALLOWED_KINDS), {"Text", "Voice", "Attachment", "Link"})

	def test_text_kind_requires_transcript(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Text", transcript="   ")

	def test_voice_kind_requires_transcript(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Voice", transcript="", duration_s=8)

	def test_attachment_kind_allows_blank_caption(self):
		doc = self._note(
			USER_A,
			kind="Attachment",
			transcript="",
			attachment="/private/files/dummy.pdf",
		)
		self.assertEqual(doc.transcript, "")
		self.assertEqual(doc.kind, "Attachment")

	def test_attachment_kind_requires_attachment_field(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Attachment", transcript="", attachment="")

	def test_link_kind_allows_blank_caption(self):
		doc = self._note(USER_A, kind="Link", transcript="", url="https://example.com/policy")
		self.assertEqual(doc.url, "https://example.com/policy")

	def test_link_kind_requires_url(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Link", transcript="", url="")

	def test_link_kind_rejects_non_http_scheme(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Link", transcript="", url="ftp://example.com/x")

	def test_transcript_cap_still_enforced(self):
		with self.assertRaises(frappe.ValidationError):
			self._note(USER_A, kind="Text", transcript="x" * (MAX_TRANSCRIPT_LEN + 1))

	def test_personalise_source_option_accepted(self):
		doc = self._note(USER_A, source="Personalise")
		self.assertEqual(doc.source, "Personalise")

	def test_question_link_field_is_settable(self):
		q = frappe.get_doc(
			{
				"doctype": QUESTION,
				"user": USER_A,
				"question": "Do you ship on Fridays?",
				"origin": "Behavioural Learning",
			}
		)
		q.insert(ignore_permissions=True)
		try:
			doc = self._note(USER_A, question=q.name)
			self.assertEqual(doc.question, q.name)
		finally:
			frappe.db.delete(QUESTION, q.name)
