"""Tests for the "Approve & run" schema fields + their admin guards.

Skill "Approve & run the plan" (design doc D-CONTROL, §3.4): an admin arms a
Jarvis Custom Skill (``allow_approve_run``); an approved run then stamps
``Jarvis Conversation.skill_autorun`` (+ the sliding ``skill_autorun_at``
timestamp) on its own conversation, which the write-confirmation gate reads
to run the explicit ``_SKILL_AUTORUN_COVERED`` allowlist uncarded. This
module covers only the schema + the admin guards on both flags - the LOAD-
BEARING backstop that a plain owner cannot self-grant either bypass through a
generic ``doc.save()``. Mirrors ``test_macro_skip_confirmation.py`` exactly.
"""

import uuid
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import turn_message_binding
from jarvis.chat.custom_skills import invoked_skill_clause, invoked_skill_slugs
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_auto_apply import (
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)

CONV = "Jarvis Conversation"
SKILL = "Jarvis Custom Skill"

# A Jarvis Admin who is NOT a System Manager - proves the guard admits the
# Jarvis Admin tier specifically, not only System Manager (edge-case review).
ADMIN_USER = "jarvis-approve-run-admin@example.com"


def _ensure_admin_user() -> None:
	"""A Jarvis Admin (+ Jarvis User so it can own/write a skill), NOT System Manager."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", ADMIN_USER):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": ADMIN_USER,
				"first_name": "Approve",
				"last_name": "Admin",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		doc.insert(ignore_permissions=True)
	user = frappe.get_doc("User", ADMIN_USER)
	roles = set(frappe.get_roles(ADMIN_USER))
	if "System Manager" in roles:
		user.remove_roles("System Manager")
	for role in ("Jarvis Admin", "Jarvis User"):
		if role not in roles:
			user.add_roles(role)  # add_roles auto-vivifies a missing Role row
	frappe.db.commit()


def _make_skill(owner: str, *, armed: bool = False, name: str = "approve-run-test") -> str:
	"""Create a User-scope Jarvis Custom Skill owned by ``owner``; return its name.
	``armed`` stamps allow_approve_run via a raw db write (bypassing the guard),
	simulating an already admin-armed skill."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": "approve & run test skill",
				"instructions": "do the thing",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		if armed:
			frappe.db.set_value(SKILL, doc.name, "allow_approve_run", 1, update_modified=False)
			frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestConversationSkillAutorunGuard(FrappeTestCase):
	"""The LOAD-BEARING guard: the gate will read Jarvis Conversation.skill_autorun,
	which is owner-writable with no permlevel. A non-admin owner must not flip it
	0 -> 1 through a generic save; only a Jarvis Admin / System Manager may."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		# _make_conv commits, so FrappeTestCase's class-rollback won't undo it.
		for owner in (NON_ADMIN_USER, ADMIN_USER):
			for name in frappe.get_all(CONV, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_save_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_jarvis_admin_save_can_enable(self):
		conv = _make_conv(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1)

	def test_non_admin_owner_save_can_disable(self):
		conv = _make_conv(NON_ADMIN_USER)
		# Server stamped it on (the sanctioned approve_and_run raw path), then the
		# owner saves again with it off - always free.
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_raw_stamp_bypasses_the_guard(self):
		"""The sanctioned approve_and_run path (raw db.set_value) sets the flag on a
		non-admin owner's conversation without tripping the controller guard."""
		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1)


class TestCustomSkillAllowApproveRunGuard(FrappeTestCase):
	"""Arming a skill (Jarvis Custom Skill.allow_approve_run 0 -> 1) requires
	admin (design doc D-CONTROL); editing other fields while armed keeps it
	armed - same D6 trust-persists-across-edits idiom the macro uses."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (NON_ADMIN_USER, ADMIN_USER):
			for name in frappe.get_all(SKILL, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_cannot_enable(self):
		skill = _make_skill(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run") or 0), 0)

	def test_jarvis_admin_can_enable_own_skill(self):
		skill = _make_skill(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run")), 1)

	def test_owner_can_disable(self):
		skill = _make_skill(ADMIN_USER, armed=True)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.allow_approve_run = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run") or 0), 0)

	def test_editing_content_keeps_arm_for_non_admin_owner(self):
		"""D6 trust model: an admin armed the skill; its NON-admin owner can later
		edit its instructions (a no-op 1 -> 1 transition on allow_approve_run)
		without admin rights and without disarming - arming trusts the owner for
		future edits too."""
		skill = _make_skill(NON_ADMIN_USER, armed=True)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(SKILL, skill)
		doc.instructions = "do a different thing"
		doc.save()  # must not raise; arm persists
		self.assertEqual(int(frappe.db.get_value(SKILL, skill, "allow_approve_run")), 1)


class TestApproveAndRunFieldShapes(FrappeTestCase):
	"""Field-presence sanity: the three schema fields exist with the shape the
	design doc + downstream lifecycle code depend on (§3.4: hidden/no_copy,
	db_set-only fields on the conversation; a listable/filterable admin toggle
	on the skill)."""

	def test_conversation_skill_autorun_field(self):
		df = frappe.get_meta(CONV).get_field("skill_autorun")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Check")
		self.assertEqual(int(df.hidden or 0), 1)
		self.assertEqual(int(df.no_copy or 0), 1)
		self.assertEqual(df.default, "0")

	def test_conversation_skill_autorun_at_field(self):
		df = frappe.get_meta(CONV).get_field("skill_autorun_at")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Datetime")
		self.assertEqual(int(df.hidden or 0), 1)
		self.assertEqual(int(df.no_copy or 0), 1)

	def test_custom_skill_allow_approve_run_field(self):
		df = frappe.get_meta(SKILL).get_field("allow_approve_run")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Check")
		self.assertEqual(df.default, "0")
		self.assertEqual(int(df.in_list_view or 0), 1)
		self.assertEqual(int(df.in_standard_filter or 0), 1)


# ── invoked_skill_slugs: the pure, identity-parameterized slug-set helper ────
#
# Design doc §3.3: the offer-gate needs (a) the SET of invoked skills to
# require exactly one and (b) to resolve that set under the message's SENDER,
# not the ambient exec user a park can run under. These tests pin the helper's
# contract directly, plus a behavior-preservation check that refactoring
# invoked_skill_clause to call it did not change the clause it returns.

SLUGSET_USER_A = "jarvis-slugset-a@example.com"
SLUGSET_USER_B = "jarvis-slugset-b@example.com"


def _ensure_slugset_user(email: str) -> None:
	"""A plain Jarvis chat user (Jarvis User role, no admin) - distinct identities
	A and B for the invoked_skill_slugs identity-parameterization test."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Slugset",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		doc.insert(ignore_permissions=True)
	if "Jarvis User" not in frappe.get_roles(email):
		frappe.get_doc("User", email).add_roles("Jarvis User")
	frappe.db.commit()


def _mk_slug_skill(owner: str, slug: str) -> str:
	"""An enabled, User-scope skill named ``slug`` owned by ``owner`` (the
	doctype defaults - enabled=1, scope=User - are exactly the shape
	invoked_skill_slugs's owner branch resolves)."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": slug,
				"description": "slug-set test skill",
				"instructions": "do the thing",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestInvokedSkillSlugs(FrappeTestCase):
	"""invoked_skill_slugs(message, *, user) - the pure slug-set primitive that
	both invoked_skill_clause and the (later) offer-gate share."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_slugset_user(SLUGSET_USER_A)
		_ensure_slugset_user(SLUGSET_USER_B)

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (SLUGSET_USER_A, SLUGSET_USER_B):
			frappe.db.delete(SKILL, {"owner": owner})
		frappe.db.commit()

	def test_multi_slug_message_returns_the_set_of_both(self):
		_mk_slug_skill(SLUGSET_USER_A, "slug-a")
		_mk_slug_skill(SLUGSET_USER_A, "slug-b")
		self.assertEqual(
			invoked_skill_slugs("/slug-a and /slug-b please", user=SLUGSET_USER_A),
			{"slug-a", "slug-b"},
		)

	def test_zero_when_no_slug_in_the_message_is_invocable_by_user(self):
		_mk_slug_skill(SLUGSET_USER_A, "owned-by-a-only")
		# Owned by A, and a slug naming no skill at all - neither is invocable by B.
		self.assertEqual(
			invoked_skill_slugs("/owned-by-a-only /not-a-skill-at-all", user=SLUGSET_USER_B),
			set(),
		)

	def test_identity_parameterization_keys_on_passed_user_not_session(self):
		"""A skill enabled/owned by A but not B: invoked in A's message returns
		{slug} under user=A, empty under user=B - proving resolution keys on the
		PASSED user. The ambient session is deliberately left on B throughout, so
		a session-keyed (rather than user-keyed) implementation would fail the
		very first assertion below."""
		_mk_slug_skill(SLUGSET_USER_A, "onlya")
		frappe.set_user(SLUGSET_USER_B)
		self.assertEqual(invoked_skill_slugs("/onlya go", user=SLUGSET_USER_A), {"onlya"})
		self.assertEqual(invoked_skill_slugs("/onlya go", user=SLUGSET_USER_B), set())

	def test_incidental_slash_token_ignored_unless_an_enabled_skill_names_it(self):
		# "see /invoicing-notes" - a prose slash, not an invocation, until a skill
		# literally named invoicing-notes exists and is enabled for this user.
		self.assertEqual(invoked_skill_slugs("see /invoicing-notes for details", user=SLUGSET_USER_A), set())
		_mk_slug_skill(SLUGSET_USER_A, "invoicing-notes")
		self.assertEqual(
			invoked_skill_slugs("see /invoicing-notes for details", user=SLUGSET_USER_A),
			{"invoicing-notes"},
		)

	def test_invoked_skill_clause_unchanged_for_a_known_invoked_skill(self):
		"""Behavior-preservation: invoked_skill_clause now delegates its matched
		set to invoked_skill_slugs(message, user=frappe.session.user), but must
		still return the EXACT clause string it did before the refactor."""
		_mk_slug_skill(SLUGSET_USER_A, "clausecheck")
		frappe.set_user(SLUGSET_USER_A)
		clause = invoked_skill_clause("/clausecheck run it")
		self.assertEqual(
			clause,
			"; the user invoked these skills, which are not loaded in this session: "
			"custom-clausecheck - call the jarvis__get_skill tool with each of those "
			"names to read its instructions, then follow them",
		)


# --------------------------------------------------------------------------- #
# Turn -> triggering-message binding (design §3.3, security-C1)
# --------------------------------------------------------------------------- #
#
# A conversation-keyed Redis binding, written at TURN START, that lets a later
# park-time offer-gate identify the EXACT user message that triggered the
# running turn - defeating the "latest hidden=0 message" race (a second tab's
# committed-but-queued send). Correctness rests on admission's one-in-flight-
# turn-per-conversation guarantee: the last writer of the key IS the running
# turn. This block covers ONLY the two helpers + the handle_chat_send call site
# (the offer-gate / token stamp / terminal clears are tasks #38/#41).


def _uniq_conv() -> str:
	"""A synthetic conversation id - the binding is a bare Redis key, so the
	helper tests need no real Jarvis Conversation row. Unique per call so tests
	never collide through Redis or the per-request local cache."""
	return f"tmb-{uuid.uuid4()}"


class TestTurnMessageBindingHelpers(FrappeTestCase):
	"""bind_turn_message / current_turn_message_id against ``frappe.cache()``."""

	def setUp(self):
		self._conv = _uniq_conv()

	def tearDown(self):
		# Don't leak the Redis key (or its per-request local-cache mirror).
		frappe.cache().delete_value(turn_message_binding._key(self._conv))

	def test_bind_then_read_returns_the_id(self):
		turn_message_binding.bind_turn_message(self._conv, "msg-alpha")
		self.assertEqual(turn_message_binding.current_turn_message_id(self._conv), "msg-alpha")

	def test_second_bind_overwrites(self):
		"""A new turn on the same conversation replaces the prior binding - the
		key always reflects the turn currently running."""
		turn_message_binding.bind_turn_message(self._conv, "msg-first")
		turn_message_binding.bind_turn_message(self._conv, "msg-second")
		self.assertEqual(turn_message_binding.current_turn_message_id(self._conv), "msg-second")

	def test_read_returns_none_when_unset(self):
		self.assertIsNone(turn_message_binding.current_turn_message_id(self._conv))

	def test_read_returns_none_after_delete(self):
		turn_message_binding.bind_turn_message(self._conv, "msg-gone")
		frappe.cache().delete_value(turn_message_binding._key(self._conv))
		self.assertIsNone(turn_message_binding.current_turn_message_id(self._conv))

	def test_ttl_is_set_on_the_key(self):
		"""The key carries a positive expiry (staleness backstop), bounded by the
		configured TTL - a live binding never lingers forever if a clear is
		missed."""
		turn_message_binding.bind_turn_message(self._conv, "msg-ttl")
		cache = frappe.cache()
		raw = cache.make_key(turn_message_binding._key(self._conv))
		ttl = cache.ttl(raw)  # remaining seconds; -1 = no expiry, -2 = missing
		self.assertGreater(ttl, 0)
		self.assertLessEqual(ttl, turn_message_binding._TTL_S)

	def test_empty_args_are_noops(self):
		"""A missing conversation or message id binds nothing (and reads None)."""
		turn_message_binding.bind_turn_message(self._conv, "")
		self.assertIsNone(turn_message_binding.current_turn_message_id(self._conv))
		turn_message_binding.bind_turn_message("", "msg-x")
		self.assertIsNone(turn_message_binding.current_turn_message_id(""))


class TestHandleChatSendBindsTurnMessage(FrappeTestCase):
	"""The call site: handle_chat_send binds THIS turn's genuine triggering
	message under its conversation, BEFORE the agent is dispatched."""

	def setUp(self):
		from jarvis.chat import agent_session_pool
		from jarvis.tests.test_chat_api import (
			TEST_USER,
			_cleanup_user_conversations,
			_ensure_test_user,
		)
		from jarvis.tests.test_chat_worker import _make_conversation_with_user_message

		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hello")

	def tearDown(self):
		from jarvis.tests.test_chat_api import _cleanup_user_conversations

		frappe.cache().delete_value(turn_message_binding._key(self.conv))
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _fake_session(self):
		from jarvis.tests.test_chat_worker import _fake_event_stream

		sess = MagicMock()
		sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "ok", "delta": "ok"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		return sess

	def test_bind_invoked_with_conv_and_message_before_dispatch(self):
		"""bind_turn_message(conversation, message_id) fires with the payload's
		genuine (conversation_id, message_id) and strictly BEFORE the agent
		session connect (the dispatch)."""
		from jarvis.chat import turn_handler

		order: list[str] = []
		bind_mock = MagicMock(side_effect=lambda *a, **k: order.append("bind"))

		def _connect(*a, **k):
			order.append("connect")
			return self._fake_session()

		with patch("jarvis.chat.turn_message_binding.bind_turn_message", bind_mock):
			with patch(
				"jarvis.chat.agent_session_pool.AgentSession.connect",
				side_effect=_connect,
			):
				with patch("jarvis.chat.worker.publish_to_user"):
					turn_handler.handle_chat_send(
						{
							"conversation_id": self.conv,
							"message_id": self.user_msg,
							"run_id": "r-bind",
						}
					)

		bind_mock.assert_called_once_with(self.conv, self.user_msg)
		self.assertIn("bind", order)
		self.assertIn("connect", order)
		self.assertLess(order.index("bind"), order.index("connect"))

	def test_real_binding_reflects_the_turns_genuine_trigger(self):
		"""End-to-end (bind NOT patched): after a stubbed turn, the binding reads
		back the exact ``message_id`` the turn ran on - proof the bound id is the
		turn's genuine trigger, not a racy table lookup."""
		from jarvis.chat import turn_handler

		with patch(
			"jarvis.chat.agent_session_pool.AgentSession.connect",
			return_value=self._fake_session(),
		):
			with patch("jarvis.chat.worker.publish_to_user"):
				turn_handler.handle_chat_send(
					{
						"conversation_id": self.conv,
						"message_id": self.user_msg,
						"run_id": "r-bind-real",
					}
				)

		self.assertEqual(turn_message_binding.current_turn_message_id(self.conv), self.user_msg)
