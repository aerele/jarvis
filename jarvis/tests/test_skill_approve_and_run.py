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

from jarvis import api
from jarvis.chat import actions_api, finalize, pending_confirm, session_lifecycle, turn_message_binding
from jarvis.chat.custom_skills import invoked_skill_clause, invoked_skill_slugs
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_auto_apply import (
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)

CONV = "Jarvis Conversation"
SKILL = "Jarvis Custom Skill"
MSG = "Jarvis Chat Message"

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


# --------------------------------------------------------------------------- #
# The park-time "Approve & run" OFFER gate (design §3.3)
# --------------------------------------------------------------------------- #
#
# api._resolve_approve_run_offer(conversation) decides whether a parked card may
# offer Approve & run and, if so, returns the ARMED skill (docname, slug) the gate
# stamps onto the pending-confirm token. Forgery-proof + fail-safe: it offers ONLY
# when the turn's triggering message (via the turn->message binding) invoked EXACTLY
# ONE live-armed skill, resolved under the MESSAGE OWNER's identity. This is the
# OFFER side only - no skill_autorun flag is set and no run is opened here.

OFFER_OWNER = SLUGSET_USER_A
OFFER_OTHER = SLUGSET_USER_B


def _make_user_msg(conv: str, owner: str, content: str) -> str:
	"""A real ``role=user`` Jarvis Chat Message on ``conv`` owned by ``owner`` with
	``content`` - the row the offer gate reads back through the turn->message
	binding (owner + content drive the slug resolution)."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 1,
				"role": "user",
				"content": content,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestResolveApproveRunOffer(FrappeTestCase):
	"""api._resolve_approve_run_offer / _armed_skill_docname_for_slug - the pure
	park-time offer resolution."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_slugset_user(OFFER_OWNER)
		_ensure_slugset_user(OFFER_OTHER)

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (OFFER_OWNER, OFFER_OTHER):
			frappe.db.delete(SKILL, {"owner": owner})
			for conv in frappe.get_all(CONV, filters={"owner": owner}, pluck="name"):
				frappe.db.delete(MSG, {"conversation": conv})
				frappe.cache().delete_value(turn_message_binding._key(conv))
				frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _bound_conv(self, owner: str, content: str) -> str:
		"""A conversation owned by ``owner`` with a bound triggering user message."""
		conv = _make_conv(owner)
		msg = _make_user_msg(conv, owner, content)
		turn_message_binding.bind_turn_message(conv, msg)
		return conv

	def test_armed_single_skill_returns_docname_and_slug(self):
		docname = _make_skill(OFFER_OWNER, armed=True, name="armed-one")
		conv = self._bound_conv(OFFER_OWNER, "/armed-one do it")
		self.assertEqual(api._resolve_approve_run_offer(conv), (docname, "armed-one"))

	def test_not_armed_skill_no_offer(self):
		# The invoked skill exists + is invocable by its owner, but its
		# allow_approve_run is 0. Mutation-verify: deleting the `if not armed:
		# return None` guard offers here and flips this red.
		_make_skill(OFFER_OWNER, armed=False, name="notarmed")
		conv = self._bound_conv(OFFER_OWNER, "/notarmed do it")
		self.assertEqual(api._resolve_approve_run_offer(conv), (None, None))

	def test_two_invoked_skills_no_offer_even_when_both_armed(self):
		# 2+ invoked skills: the run flag is conversation-wide with no per-write
		# skill attribution, so a co-invoked skill's writes would ride the approval.
		# Mutation-verify: relaxing `len(slugs) != 1` offers here and flips this red.
		_make_skill(OFFER_OWNER, armed=True, name="armed-a")
		_make_skill(OFFER_OWNER, armed=True, name="armed-b")
		conv = self._bound_conv(OFFER_OWNER, "/armed-a and /armed-b please")
		self.assertEqual(api._resolve_approve_run_offer(conv), (None, None))

	def test_no_binding_no_offer(self):
		# An armed skill exists, but nothing bound this conversation's turn.
		_make_skill(OFFER_OWNER, armed=True, name="armed-nb")
		conv = _make_conv(OFFER_OWNER)  # deliberately NOT bound
		self.assertEqual(api._resolve_approve_run_offer(conv), (None, None))

	def test_armed_skill_owned_by_different_user_no_offer(self):
		# The armed skill is owned by OFFER_OTHER; the triggering message is owned
		# by OFFER_OWNER, who neither owns nor is shared it. Resolving under the
		# MESSAGE owner yields no invocable slug -> no offer. The ambient session is
		# parked on the armed owner, so a session-keyed (rather than message-owner-
		# keyed) resolution would wrongly offer and flip this red.
		_make_skill(OFFER_OTHER, armed=True, name="foreign")
		conv = self._bound_conv(OFFER_OWNER, "/foreign do it")
		frappe.set_user(OFFER_OTHER)
		self.assertEqual(api._resolve_approve_run_offer(conv), (None, None))


class TestApproveRunOfferThroughRunTool(FrappeTestCase):
	"""End-to-end through the write-confirmation gate (api._run_tool): a parked
	create_doc stamps the token + flags the card iff the turn invoked one armed
	skill. TEST_USER (System Manager) owns the conversation so the park's create_doc
	dry-run is unencumbered."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		frappe.db.delete(SKILL, {"owner": TEST_USER})
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.cache().delete_value(turn_message_binding._key(conv))
		_cleanup_user_conversations(TEST_USER)
		frappe.db.commit()

	def _bound_conv(self, content: str) -> str:
		conv = _make_conv(TEST_USER)
		msg = _make_user_msg(conv, TEST_USER, content)
		turn_message_binding.bind_turn_message(conv, msg)
		return conv

	def _park_create_todo(self, conv: str, desc: str) -> dict:
		frappe.set_user(TEST_USER)
		return api._run_tool(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": desc}},
			conversation=conv,
		)

	def _token_record(self, conv: str) -> dict:
		recs = [
			r
			for r in pending_confirm.list_for_owner(TEST_USER, conversation=conv)
			if r.get("conversation") == conv
		]
		self.assertEqual(len(recs), 1, "exactly one parked token expected")
		return recs[0]

	def test_armed_single_skill_stamps_token_and_flags_card(self):
		docname = _make_skill(TEST_USER, armed=True, name="runarmed")
		conv = self._bound_conv("/runarmed make a todo")
		r = self._park_create_todo(conv, "jarvis-approverun-armed-001")
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		rec = self._token_record(conv)
		self.assertEqual(rec.get("skill_docname"), docname)
		card = (rec.get("preview") or {}).get("card")
		self.assertIsInstance(card, dict)
		self.assertTrue(card.get("approve_run"))
		self.assertEqual(card.get("skill_slug"), "runarmed")

	def test_unarmed_skill_no_stamp_no_flag(self):
		_make_skill(TEST_USER, armed=False, name="rununarmed")
		conv = self._bound_conv("/rununarmed make a todo")
		r = self._park_create_todo(conv, "jarvis-approverun-unarmed-001")
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		rec = self._token_record(conv)
		self.assertIsNone(rec.get("skill_docname"))
		card = (rec.get("preview") or {}).get("card")
		self.assertIsInstance(card, dict)
		self.assertNotIn("approve_run", card)


# --------------------------------------------------------------------------- #
# The transport-independent run-cancel signal (design §3.4, the Halt cancel-gate)
# --------------------------------------------------------------------------- #
#
# stop_run's turn-level abort is best-effort and never reaches the bench tool path
# (every cancel reader lives in the turn-settlement layer). The auto-run branch is
# the ONE place a bench-guaranteed Halt can land, so it needs a cancel signal that
# works in BOTH pump and legacy mode: a bare Redis key, set by stop_run, read by
# the gate before each covered write. A SHORT TTL - a cancel only matters during an
# active run.


class TestRunCancelSignalHelpers(FrappeTestCase):
	"""request_run_cancel / is_run_cancel_requested / clear_run_cancel."""

	def setUp(self):
		self._conv = _uniq_conv()

	def tearDown(self):
		turn_message_binding.clear_run_cancel(self._conv)

	def test_request_then_is_requested_true(self):
		self.assertFalse(turn_message_binding.is_run_cancel_requested(self._conv))
		turn_message_binding.request_run_cancel(self._conv)
		self.assertTrue(turn_message_binding.is_run_cancel_requested(self._conv))

	def test_clear_resets_the_signal(self):
		turn_message_binding.request_run_cancel(self._conv)
		turn_message_binding.clear_run_cancel(self._conv)
		self.assertFalse(turn_message_binding.is_run_cancel_requested(self._conv))

	def test_unset_reads_false(self):
		self.assertFalse(turn_message_binding.is_run_cancel_requested(self._conv))

	def test_empty_conversation_is_a_noop(self):
		turn_message_binding.request_run_cancel("")
		self.assertFalse(turn_message_binding.is_run_cancel_requested(""))

	def test_short_ttl_is_set_on_the_key(self):
		"""The signal carries a positive, SHORT expiry (bounded by the run-cancel
		TTL) - it must not linger past the run it was meant to interrupt."""
		turn_message_binding.request_run_cancel(self._conv)
		cache = frappe.cache()
		raw = cache.make_key(turn_message_binding._run_cancel_key(self._conv))
		ttl = cache.ttl(raw)  # remaining seconds; -1 = no expiry, -2 = missing
		self.assertGreater(ttl, 0)
		self.assertLessEqual(ttl, turn_message_binding._RUN_CANCEL_TTL_S)


class TestStopRunRequestsRunCancel(FrappeTestCase):
	"""The wiring: stop_run sets the run-cancel signal (beside its token sweep) so
	the auto-run cancel-gate halts the chain, in pump and legacy mode alike."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			turn_message_binding.clear_run_cancel(conv)
		_cleanup_user_conversations(TEST_USER)
		frappe.db.commit()

	def test_stop_run_sets_the_run_cancel_signal(self):
		# A session_key-less conversation: stop_run returns right after its token
		# sweep (where request_run_cancel sits), so no gateway is needed.
		from jarvis.chat import api as chat_api

		conv = _make_conv(TEST_USER)
		self.assertFalse(turn_message_binding.is_run_cancel_requested(conv))
		res = chat_api.stop_run(conv)
		self.assertTrue(res.get("ok"))
		self.assertTrue(
			turn_message_binding.is_run_cancel_requested(conv),
			"stop_run must set the transport-independent run-cancel signal",
		)


# --------------------------------------------------------------------------- #
# The skill auto-run gate branch (design §3.4, task #39 - the READ side)
# --------------------------------------------------------------------------- #
#
# In _run_tool, AFTER the macro armed-skip branch (macro-first for deterministic
# provenance): a conversation in an approved run (skill_autorun=1) runs the explicit
# _SKILL_AUTORUN_COVERED allowlist uncarded, gated on a SLIDING TTL and a cancel-gate.
# The flag is db_set directly here - the sanctioned raw-set enable path (task #40
# mints it through approve_and_run; this task tests the READ side).


def _stamp_autorun(conv: str, *, at=None) -> None:
	"""Directly stamp skill_autorun=1 (+ its sliding timestamp) via db_set - the
	sanctioned raw enable path. ``at`` defaults to now (a fresh, in-TTL run)."""
	frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
	frappe.db.set_value(
		CONV, conv, "skill_autorun_at", at or frappe.utils.now_datetime(), update_modified=False
	)
	frappe.db.commit()


def _pending_for(conv: str, user: str) -> int:
	return len(
		[r for r in pending_confirm.list_for_owner(user, conversation=conv) if r.get("conversation") == conv]
	)


class TestSkillAutorunPartition(FrappeTestCase):
	"""Fail-closed allowlist: _SKILL_AUTORUN_COVERED and _SKILL_AUTORUN_NEVER
	partition _GATED_WRITES exactly. A future gated tool filed in NEITHER set makes
	this RED, forcing a conscious auto-run-vs-park classification - this path has no
	kill switch, so a new tool must never silently become auto-runnable."""

	def test_covered_and_never_partition_gated_writes(self):
		self.assertEqual(
			api._SKILL_AUTORUN_COVERED & api._SKILL_AUTORUN_NEVER,
			frozenset(),
			"covered and never must be disjoint",
		)
		self.assertEqual(
			api._SKILL_AUTORUN_COVERED | api._SKILL_AUTORUN_NEVER,
			api._GATED_WRITES,
			"every gated write must be classified covered (auto-runs in an approved "
			"run) or never (always parks) - a tool in neither is a fail-open gap",
		)

	def test_every_covered_member_is_a_gated_write(self):
		self.assertLessEqual(api._SKILL_AUTORUN_COVERED, api._GATED_WRITES)

	def test_covered_is_the_exact_expected_set(self):
		self.assertEqual(
			api._SKILL_AUTORUN_COVERED,
			frozenset(
				{
					"create_doc",
					"create_docs",
					"update_doc",
					"submit_doc",
					"run_import",
					"apply_workflow_action",
					"send_email",
					"share_doc",
					"assign_to",
					"update_wiki",
					"run_method",
				}
			),
		)

	def test_never_set_is_the_trio_plus_create_custom_skill(self):
		self.assertEqual(
			api._SKILL_AUTORUN_NEVER,
			frozenset({"delete_doc", "cancel_doc", "amend_doc", "create_custom_skill"}),
		)

	def test_create_custom_skill_is_not_covered_here_though_the_macro_covers_it(self):
		# The explicit divergence from the macro's _ARMED_SKIP_COVERED (D-COVERED).
		self.assertNotIn("create_custom_skill", api._SKILL_AUTORUN_COVERED)
		self.assertIn("create_custom_skill", api._ARMED_SKIP_COVERED)

	def test_run_method_is_covered(self):
		self.assertIn("run_method", api._SKILL_AUTORUN_COVERED)


class TestSkillAutorunGate(FrappeTestCase):
	"""The auto-run branch: an approved run (skill_autorun=1, fresh timestamp) runs
	the covered set uncarded and slides the timestamp; the irreversible trio +
	create_custom_skill still park; a plain skill_autorun=0 conversation parks
	exactly as today (no regression)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
		for dt in (CONV, "ToDo"):
			for name in frappe.get_all(dt, filters={"owner": TEST_USER}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_covered_write_runs_uncarded_and_slides_timestamp(self):
		stamped = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-100)
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=stamped)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		disp.assert_called_once()
		self.assertTrue(r["ok"])
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertEqual(_pending_for(conv, TEST_USER), 0, "no token is minted on the auto-run path")
		new_at = frappe.utils.get_datetime(frappe.db.get_value(CONV, conv, "skill_autorun_at"))
		self.assertGreater(
			new_at, frappe.utils.get_datetime(stamped), "a successful covered write slides the timestamp"
		)
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1, "flag stays armed after a success"
		)

	def test_plain_conversation_parks_no_regression(self):
		conv = _make_conv(TEST_USER)  # skill_autorun defaults 0
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(disp.called)

	def test_delete_doc_still_parks_when_autorun(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "autorun-del-x"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "delete always parks")
		self.assertFalse(disp.called)
		self.assertTrue(frappe.db.exists("ToDo", todo.name))

	def test_create_custom_skill_still_parks_when_autorun(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool(
				"create_custom_skill",
				{"skill_name": "autorun-never-skill", "instructions": "do the thing"},
				conversation=conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "create_custom_skill always parks")
		self.assertFalse(disp.called)


class TestSkillAutorunCancelGate(FrappeTestCase):
	"""The cancel-gate (Halt made bench-guaranteed): with the run-cancel signal set,
	a covered write is REFUSED (RunHaltedError) without executing, and skill_autorun
	is cleared - the chain stops within one write."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			turn_message_binding.clear_run_cancel(conv)
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_cancel_requested_refuses_covered_write_and_clears_flag(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		turn_message_binding.request_run_cancel(conv)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertFalse(r["ok"], "a halted covered write is refused")
		self.assertEqual(r["error"]["code"], "RunHaltedError")
		self.assertFalse(disp.called, "the write must NOT execute after Halt")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0, "Halt clears the flag"
		)
		self.assertFalse(turn_message_binding.is_run_cancel_requested(conv), "the consumed signal is cleared")


class TestSkillAutorunTTL(FrappeTestCase):
	"""The sliding TTL: an approved run whose last covered write is older than the
	TTL parks (does not auto-run); a run with no timestamp parks."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_ttl_expired_parks(self):
		stale = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(api._SKILL_AUTORUN_TTL_S + 60)
		)
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=stale)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a TTL-expired run parks")
		self.assertFalse(disp.called)

	def test_no_timestamp_parks(self):
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		# skill_autorun_at deliberately left NULL
		frappe.db.commit()
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "no timestamp -> park")
		self.assertFalse(disp.called)


class TestSkillAutorunHardStop(FrappeTestCase):
	"""Hard-stop-on-error: the first covered dispatch returning ok:False clears
	skill_autorun (subsequent writes re-card) and returns that failure."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_dispatch_error_clears_the_flag(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		fail = {"ok": False, "error": {"code": "InvalidArgumentError", "message": "boom"}}
		with patch("jarvis.api.dispatch_confirmed", return_value=fail) as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		disp.assert_called_once()
		self.assertFalse(r["ok"], "the failing result is returned to the model")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"a covered write that fails clears the flag so the next write re-cards",
		)


class TestConvFlagsSingleQuery(FrappeTestCase):
	"""_conv_flags stays ONE get_value even after skill_autorun + skill_autorun_at
	join it: the gate reads all FIVE conversation flags in a single query."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_five_flags_read_in_one_query(self):
		conv = _make_conv(TEST_USER)
		# Spy on the real DB instance (frappe.db is a LocalProxy over frappe.local.db);
		# wraps=... records every get_value call yet executes it normally.
		spy = MagicMock(wraps=frappe.local.db.get_value)
		with patch.object(frappe.local.db, "get_value", spy):
			api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		flag_reads = [
			c
			for c in spy.call_args_list
			if len(c.args) >= 3
			and c.args[0] == CONV
			and isinstance(c.args[2], (list, tuple))
			and "skill_autorun" in c.args[2]
		]
		self.assertEqual(len(flag_reads), 1, "the conversation flags must be a SINGLE get_value")
		self.assertEqual(
			list(flag_reads[0].args[2]),
			["auto_apply", "file_box", "skip_confirmation", "skill_autorun", "skill_autorun_at"],
		)
		self.assertTrue(flag_reads[0].kwargs.get("as_dict"), "flags read as_dict")


# --------------------------------------------------------------------------- #
# approve_and_run: the sibling endpoint that opens an approved skill run
# (design §3.3 approve_and_run bullet, §3.4 the "approve_and_run order", §3.4.1)
# --------------------------------------------------------------------------- #
#
# It mirrors _confirm_core (owner-bound single-use consume, execution under the
# stored exec_user, receipt + one continuation) but dispatches STEP 1 and sets
# skill_autorun ONLY on step-1 success, BEFORE enqueueing the continuation. It is
# valid ONLY for a token the offer gate stamped with a skill_docname, re-checks the
# skill's arming LIVE (TOCTOU), and refuses a skip_confirmation (macro) conversation
# so the both-flags state is unreachable. All these tests mock dispatch_confirmed /
# the receipt / continuation so they assert the ORDER + the flag decision, not the
# tool internals.


def _mint_approve_token(conv: str, owner: str, docname: str | None, *, tool="run_method", args=None) -> str:
	"""Mint a pending-confirm token as the offer gate would - stamped with
	``skill_docname`` when ``docname`` is given (a runnable offer), or None (a plain
	card). Owner + exec_user are ``owner``; the tool defaults to run_method (a
	covered write) so the mocked dispatch stands in for a real step 1."""
	return pending_confirm.mint(
		conversation=conv,
		owner=owner,
		exec_user=owner,
		tool=tool,
		args=args if args is not None else {"method": "frappe.ping"},
		run_id="",
		skill_docname=docname,
	)


class TestApproveAndRun(FrappeTestCase):
	"""The approve_and_run endpoint end-to-end (dispatch mocked)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (TEST_USER, NON_ADMIN_USER):
			frappe.db.delete(SKILL, {"owner": owner})
			for conv in frappe.get_all(CONV, filters={"owner": owner}, pluck="name"):
				pending_confirm.clear_for_conversation(owner, conv)
				frappe.db.delete(MSG, {"conversation": conv})
				frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_armed_token_dispatches_step1_opens_run_fires_one_continuation(self):
		"""The happy path: step 1 dispatches, skill_autorun + skill_autorun_at get set,
		and EXACTLY ONE (non-failed) continuation fires."""
		docname = _make_skill(TEST_USER, armed=True, name="ar-ok")
		conv = _make_conv(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, docname)
		with (
			patch(
				"jarvis.api.dispatch_confirmed",
				return_value={"ok": True, "data": {"doctype": "ToDo", "name": "T-1"}},
			) as disp,
			patch("jarvis.api.persist_tool_receipt"),
			patch("jarvis.chat.admission.publish_action_confirmed"),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}) as cont,
		):
			res = actions_api.approve_and_run(token, conv)
		disp.assert_called_once()
		self.assertTrue(res.get("ok"))
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun")), 1, "the run opens on ok")
		self.assertTrue(
			frappe.db.get_value(CONV, conv, "skill_autorun_at"), "the sliding timestamp is stamped"
		)
		cont.assert_called_once()
		self.assertFalse(
			cont.call_args.kwargs.get("failed"), "the success continuation is not the failed scaffold"
		)
		self.assertIsNone(pending_confirm.peek(token), "the token is single-use consumed")

	def test_step1_failure_does_not_open_run_fires_failed_continuation(self):
		"""Mutation target #1: step-1 ok:False must NOT set the flag; the FAILED
		continuation fires. Making the flag-set unconditional flips this red."""
		docname = _make_skill(TEST_USER, armed=True, name="ar-fail")
		conv = _make_conv(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, docname)
		fail = {"ok": False, "error": {"code": "InvalidArgumentError", "message": "boom"}}
		with (
			patch("jarvis.api.dispatch_confirmed", return_value=fail) as disp,
			patch("jarvis.api.persist_tool_receipt"),
			patch("jarvis.chat.admission.publish_action_confirmed"),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}) as cont,
		):
			res = actions_api.approve_and_run(token, conv)
		disp.assert_called_once()
		self.assertFalse(res.get("ok"), "the failing result is returned")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"a failed first step opens NO run (the flag is never set)",
		)
		cont.assert_called_once()
		self.assertTrue(cont.call_args.kwargs.get("failed"), "the failed continuation scaffold fires")
		self.assertIsNone(pending_confirm.peek(token), "the token is burned on failure (user re-invokes)")

	def test_no_skill_docname_refused_not_consumed_no_flag(self):
		"""A plain card (no skill_docname) is refused WITHOUT consuming and never
		dispatches - it must go through confirm_tool."""
		conv = _make_conv(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, None)  # plain card
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = actions_api.approve_and_run(token, conv)
		self.assertFalse(res.get("ok"))
		self.assertFalse(disp.called, "a non-runnable card must not dispatch")
		self.assertIsNotNone(pending_confirm.peek(token), "the token is NOT consumed")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_unarmed_between_stamp_and_click_refused(self):
		"""TOCTOU: the skill was armed at offer time but an admin un-armed it before
		the click - the live re-check refuses without consuming."""
		docname = _make_skill(TEST_USER, armed=True, name="ar-toctou")
		conv = _make_conv(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, docname)
		# Admin un-arms the skill AFTER the offer was stamped on the token.
		frappe.db.set_value(SKILL, docname, "allow_approve_run", 0, update_modified=False)
		frappe.db.commit()
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = actions_api.approve_and_run(token, conv)
		self.assertFalse(res.get("ok"))
		self.assertFalse(disp.called, "an un-armed skill must not dispatch")
		self.assertIsNotNone(pending_confirm.peek(token), "the un-armed refusal does not consume")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_skip_confirmation_conversation_refused(self):
		"""Mutation target #2: a skip_confirmation (armed-macro) conversation is
		refused without consuming - making both flags unreachable by construction.
		Removing the skip_confirmation refusal flips this red (dispatch fires / token
		consumed)."""
		docname = _make_skill(TEST_USER, armed=True, name="ar-macro")
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		token = _mint_approve_token(conv, TEST_USER, docname)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = actions_api.approve_and_run(token, conv)
		self.assertFalse(res.get("ok"))
		self.assertFalse(disp.called, "a macro-run conversation must not open a skill run")
		self.assertIsNotNone(pending_confirm.peek(token), "the skip_confirmation refusal does not consume")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_other_user_cannot_consume_owners_token(self):
		"""Owner-bound: a different logged-in Jarvis user cannot consume TEST_USER's
		token - consume rejects on owner mismatch without burning it, no run opens."""
		docname = _make_skill(TEST_USER, armed=True, name="ar-owner")
		conv = _make_conv(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, docname)
		frappe.set_user(NON_ADMIN_USER)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = actions_api.approve_and_run(token, conv)
		self.assertFalse(res.get("ok"), "a non-owner is refused")
		self.assertFalse(disp.called, "a non-owner must not dispatch the write")
		frappe.set_user(TEST_USER)
		self.assertIsNotNone(pending_confirm.peek(token), "a wrong-owner probe does NOT burn the token")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)


# --------------------------------------------------------------------------- #
# The REMAINING skill_autorun clears + the [Context:] agent signal
# (design §3.4 "Clear" + "Other close-triggers" + the [Context:] signal)
# --------------------------------------------------------------------------- #
#
# The auto-run branch (task #39) already owns the hard-stop-on-error and the Halt
# cancel-gate clears. THIS block covers the four clears the branch does NOT own:
#   * on_terminal_turn - the turn-terminal clear, installed at all THREE chokepoints.
#     The Relay Pump is the DEFAULT transport and settles turns through
#     finalize._effect_macro_advance, NOT turn_handler (correctness-C1), so a clear
#     living only in turn_handler would never fire on the default path;
#   * a new top-level message (send_message) - a genuine new instruction ends the run,
#     but a CONSUMED typed approval (a destructive-pause resume) must NOT;
#   * a dismiss of the paused card (dismiss_tool) - declining the step ends the run;
# plus the "approved skill run" [Context:] clause folded into assemble_prompt.


def _park_pending_card(conv: str, owner: str) -> str | None:
	"""Mint a parked destructive card strictly bound to ``conv`` (a legit PAUSE of an
	approved run) so on_terminal_turn sees a pending card and KEEPS the flag."""
	return pending_confirm.mint(
		conversation=conv,
		owner=owner,
		exec_user=owner,
		tool="delete_doc",
		args={"doctype": "ToDo", "name": "AUTORUN-PAUSE-X"},
		run_id="",
	)


class TestTerminalTurnClear(FrappeTestCase):
	"""turn_message_binding.on_terminal_turn, wired at all THREE terminal chokepoints:
	finalize._effect_macro_advance (the DEFAULT pump path), turn_handler._advance_macro
	(legacy) and turn_recovery._advance_macro (park-and-recover). An approved run with
	no pending card is ENDED at the terminal; a run PAUSED on a parked card is KEPT."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			turn_message_binding.clear_run_cancel(conv)
			frappe.cache().delete_value(turn_message_binding._key(conv))
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _ctx(self, conv: str, *, errored: bool = False):
		"""A finalize._Ctx just rich enough for _effect_macro_advance (it reads only
		.conversation + .errored; the macro/app-learning hooks no-op on a plain conv)."""
		return finalize._Ctx(
			run_id="r-fin", turn={}, conversation=conv, owner=TEST_USER, errored=errored, payload={}
		)

	def test_pump_finalize_clears_autorun_when_no_pending_card(self):
		"""THE correctness-C1 regression: the clear fires on the DEFAULT pump path
		(finalize), not just the legacy worker. Mutation-verify: installing the clear
		ONLY in turn_handler (not finalize) flips this RED."""
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		finalize._effect_macro_advance(self._ctx(conv))
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"the DEFAULT pump path (finalize) must end the approved run at the terminal",
		)

	def test_pump_finalize_keeps_autorun_when_a_card_is_pending(self):
		"""A run PAUSED on a parked card must survive the terminal (the resume
		auto-runs). Mutation-verify: making the predicate ignore pending cards flips
		this RED."""
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		_park_pending_card(conv, TEST_USER)
		finalize._effect_macro_advance(self._ctx(conv))
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			1,
			"a run paused on a parked card must NOT be ended at the terminal",
		)

	def test_legacy_advance_macro_clears_autorun(self):
		from jarvis.chat import turn_handler

		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		turn_handler._advance_macro(conv, errored=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_recovery_advance_macro_clears_autorun(self):
		from jarvis.chat import turn_recovery

		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		turn_recovery._advance_macro(conv, errored=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)

	def test_clear_nulls_timestamp_and_run_state(self):
		"""Ending the run drops the sliding timestamp and the transport-independent
		run-cancel signal too (so a stale cancel can't halt a later re-approved run)."""
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		turn_message_binding.request_run_cancel(conv)
		turn_message_binding.on_terminal_turn(conv)
		self.assertIsNone(frappe.db.get_value(CONV, conv, "skill_autorun_at"))
		self.assertFalse(turn_message_binding.is_run_cancel_requested(conv))

	def test_on_terminal_turn_noops_for_non_autorun_conv(self):
		"""A conversation not in an approved run is untouched (a read-only no-op)."""
		conv = _make_conv(TEST_USER)  # skill_autorun defaults 0
		turn_message_binding.on_terminal_turn(conv)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)


class TestNewMessageClearsAutorun(FrappeTestCase):
	"""A genuine new top-level message ends the approved run (send_message), but a
	CONSUMED typed approval - a destructive-pause RESUME - must NOT clear it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		_cleanup_user_conversations(TEST_USER)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_new_top_level_message_clears_autorun(self):
		from jarvis.chat.api import send_message
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		# An ordinary instruction (NOT an approval phrase) falls through the
		# typed-approval gate to an ordinary turn, which ends the prior run.
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				res = send_message(conv, "make three todos please")
		self.assertTrue(res["ok"])
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"a genuine new top-level message ends the approved run",
		)

	def test_consumed_typed_approval_does_not_clear_autorun(self):
		"""A typed approval that CONSUMES a parked card returns early from send_message,
		BEFORE the clear - so a destructive-pause resume keeps the flag and auto-runs
		the rest. Pins the clear's placement structurally after the typed-approval
		early-return."""
		from jarvis.chat import api as chat_api

		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		with patch("jarvis.chat.api._typed_confirmation", return_value={"ok": True, "confirmed": True}):
			res = chat_api.send_message(conv, "go ahead")
		self.assertEqual(res, {"ok": True, "confirmed": True}, "the typed approval returns early")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			1,
			"a consumed typed-approval RESUME must NOT clear the run flag",
		)


class TestDismissClearsAutorun(FrappeTestCase):
	"""Dismissing the paused card ends the approved run (the user declined the step)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_dismiss_of_paused_card_clears_autorun(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		token = _park_pending_card(conv, TEST_USER)
		res = actions_api.dismiss_tool(token, conv)
		self.assertEqual(res["data"]["status"], "discarded")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"declining the paused step ends the approved run",
		)

	def test_dismiss_on_plain_conversation_is_unaffected(self):
		"""A dismiss on a conversation NOT in an approved run leaves skill_autorun 0
		(no regression on the ordinary discard path)."""
		conv = _make_conv(TEST_USER)  # skill_autorun defaults 0
		token = _park_pending_card(conv, TEST_USER)
		res = actions_api.dismiss_tool(token, conv)
		self.assertEqual(res["data"]["status"], "discarded")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)


class TestApprovedRunContextClause(FrappeTestCase):
	"""The [Context:] agent signal: assemble_prompt folds an "approved skill run"
	clause into the trusted bracket iff the conversation's skill_autorun is set."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			frappe.db.delete(MSG, {"conversation": conv})
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _assembled(self, conv_doc) -> str:
		"""Run the shared, read-only assemble_prompt (the ONE place the [Context:]
		bracket is built) for a real user message on conv_doc; return the prompt."""
		from jarvis.chat.turn_handler import assemble_prompt

		msg = frappe.get_doc(
			{"doctype": MSG, "conversation": conv_doc.name, "seq": 1, "role": "user", "content": "hello"}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		ap = assemble_prompt(
			conv_doc,
			message_id=msg.name,
			conversation_id=conv_doc.name,
			context={},
			attachments=[],
			user=TEST_USER,
		)
		return ap.user_message

	def test_clause_present_when_autorun_set(self):
		conv = _make_conv(TEST_USER)
		doc = frappe.get_doc(CONV, conv)
		# assemble_prompt reads conv.skill_autorun off the passed doc (like armed_run
		# reads conv.skip_confirmation), so an in-memory set exercises the branch.
		doc.skill_autorun = 1
		prompt = self._assembled(doc)
		self.assertIn("approved skill run: apply the plan's covered writes directly", prompt)
		# One clause only - a duplicated fold would still satisfy assertIn.
		self.assertEqual(prompt.count("approved skill run:"), 1)

	def test_clause_absent_when_autorun_unset(self):
		conv = _make_conv(TEST_USER)
		doc = frappe.get_doc(CONV, conv)  # skill_autorun defaults 0
		prompt = self._assembled(doc)
		self.assertNotIn("approved skill run", prompt)
		self.assertIn("; chat user:", prompt)  # assembly sanity


# --------------------------------------------------------------------------- #
# The stranded-flag reaper (task #42, design §3.4 "Sliding TTL + reaper")
# --------------------------------------------------------------------------- #
#
# A dedicated hourly cron that clears a STRANDED skill_autorun flag - an approved run
# whose worker died mid-run, so no terminal clear (on_terminal_turn) ever fired and its
# sliding skill_autorun_at froze. THREE discriminators, ALL required (any one alone would
# reap something live): (1) no live turn (the exact free_idle_sessions NOT EXISTS
# streaming/recovering predicate), (2) a stale/NULL sliding timestamp past the reap
# window (>= the gate TTL), (3) no pending card strictly bound to the conversation (a
# paused, resumable run). The reaper's home is session_lifecycle, beside free_idle_sessions.


def _live_turn_msg(conv: str, *, recovering: bool = False) -> None:
	"""Insert an in-flight (streaming or recovering) assistant Chat Message for ``conv`` -
	the live-turn signal the reaper's discriminator #1 must see and refuse to reap."""
	frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": 1,
			"role": "assistant",
			"content": "",
			"streaming": 0 if recovering else 1,
			"recovering": 1 if recovering else 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


class TestStrandedSkillAutorunReaper(FrappeTestCase):
	"""The reaper quartet (+ a NULL-timestamp variant), mirroring test_macro_scheduler's
	stranded / progressing / live / paused shape. A stranded flag is CLEARED; a run that
	is still progressing, has a live turn, or is paused on a parked card is KEPT."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		# The reap window is a MULTIPLE of the gate TTL; "old" sits well past it so the
		# staleness discriminator is unambiguously satisfied (deterministic, no freeze_time).
		self._old = frappe.utils.add_to_date(
			frappe.utils.now_datetime(),
			seconds=-(session_lifecycle._REAP_AUTORUN_TTL_MULTIPLE * api._SKILL_AUTORUN_TTL_S + 600),
		)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.db.delete(MSG, {"conversation": conv})
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _autorun(self, conv: str) -> int:
		return int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0)

	# --- REAPED: a genuinely stranded flag ---------------------------------- #

	def test_stranded_flag_is_reaped(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=self._old)  # skill_autorun=1, timestamp frozen past the window
		self.assertGreaterEqual(session_lifecycle.reap_stranded_skill_autorun(), 1)
		self.assertEqual(self._autorun(conv), 0, "a stranded flag past the reap window must be cleared")
		# The clear also nulls the sliding timestamp (via clear_skill_autorun).
		self.assertIsNone(frappe.db.get_value(CONV, conv, "skill_autorun_at"))

	def test_null_timestamp_flag_is_reaped(self):
		# A flag with NO sliding timestamp is malformed - it can never auto-run (the gate
		# parks on a missing timestamp) - so it is stranded by definition and reapable.
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "skill_autorun", 1, update_modified=False)
		frappe.db.set_value(CONV, conv, "skill_autorun_at", None, update_modified=False)
		frappe.db.commit()
		self.assertGreaterEqual(session_lifecycle.reap_stranded_skill_autorun(), 1)
		self.assertEqual(self._autorun(conv), 0, "a NULL-timestamp flag must be reapable")

	# --- KEPT: each discriminator, one at a time ---------------------------- #

	def test_progressing_run_within_cutoff_is_kept(self):
		# Discriminator 2 (sliding timestamp): a fresh timestamp means covered writes are
		# still landing - a live, progressing run, never reaped.
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)  # defaults to now() - inside the window
		session_lifecycle.reap_stranded_skill_autorun()
		self.assertEqual(self._autorun(conv), 1, "a progressing run must not be reaped")

	def test_live_streaming_turn_is_kept(self):
		# Discriminator 1 (no live turn): stale timestamp, but a streaming turn is in flight.
		# Mutation-verify: dropping the live-turn predicate (SQL NOT EXISTS + the re-read
		# _has_live_turn) reaps this and turns the test RED.
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=self._old)
		_live_turn_msg(conv)
		session_lifecycle.reap_stranded_skill_autorun()
		self.assertEqual(self._autorun(conv), 1, "a run with a live streaming turn must not be reaped")

	def test_live_recovering_turn_is_kept(self):
		# The other half of the live-turn predicate: a recovering=1 turn (a worker resuming
		# the approved run and legitimately continuing to auto-execute) is CORRECT, not stranded.
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=self._old)
		_live_turn_msg(conv, recovering=True)
		session_lifecycle.reap_stranded_skill_autorun()
		self.assertEqual(self._autorun(conv), 1, "a run with a recovering turn must not be reaped")

	def test_paused_on_pending_card_is_kept(self):
		# Discriminator 3 (no pending card): stale timestamp, no live turn, but a destructive
		# card is parked - a legitimate PAUSE that resumes on confirm. Mutation-verify:
		# dropping the pending-card re-check reaps this and turns the test RED.
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=self._old)
		self.assertIsNotNone(_park_pending_card(conv, TEST_USER), "the paused card must mint")
		session_lifecycle.reap_stranded_skill_autorun()
		self.assertEqual(self._autorun(conv), 1, "a run paused on a parked card must not be reaped")

	# --- Registration -------------------------------------------------------- #

	def test_reaper_is_wired_into_hooks_scheduler_events(self):
		from jarvis import hooks

		self.assertIn(
			"jarvis.chat.session_lifecycle.reap_stranded_skill_autorun",
			hooks.scheduler_events["hourly"],
			"the stranded-flag reaper must be registered as an hourly cron",
		)


# --------------------------------------------------------------------------- #
# skip_confirmation (macro run) vs skill_autorun (approved skill run): two
# LOOK-ALIKE conversation flags a future editor may be tempted to "generalize"
# into one guard. They are NOT the same policy - a macro run is a watchable,
# human-inert RUN LOG (no interactive entry at all); an approved skill run is
# still an ordinary chat the human can talk into and pause/resume via a
# destructive-write card. Each test below pins ONE invariant that would break
# silently if the two flags were ever merged or a guard's key swapped.
# --------------------------------------------------------------------------- #


class TestMacroSkillFlagSeparation(FrappeTestCase):
	"""Dedicated invariant tests: skip_confirmation and skill_autorun gate the
	send/confirm/approve_and_run entry points in OPPOSITE ways. Mirrors the
	corresponding tests in test_macro_skip_confirmation.py where noted."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (TEST_USER, NON_ADMIN_USER):
			for name in frappe.get_all(SKILL, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
			for name in frappe.get_all("ToDo", filters={"owner": owner}, pluck="name"):
				frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
			for conv in frappe.get_all(CONV, filters={"owner": owner}, pluck="name"):
				try:
					pending_confirm.clear_for_conversation(owner, conv)
				except Exception:
					pass
				frappe.db.delete(MSG, {"conversation": conv})
				frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_send_block_refuses_skip_confirmation_conv(self):
		"""INVARIANT: skip_confirmation=1 (a macro run) refuses an interactive send -
		the conversation is a watchable run log, not a continuable chat. Mirrors
		test_macro_skip_confirmation.test_send_message_rejected_on_armed_conversation."""
		from jarvis.chat import api as chat_api

		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		with patch.object(chat_api, "validate_can_send", return_value=(True, None)):
			with self.assertRaises(frappe.ValidationError):
				chat_api.send_message(conversation=conv, message="let me in")

	def test_send_is_allowed_and_clears_skill_autorun_conv(self):
		"""INVARIANT (opposite of the test above): skill_autorun=1 (an approved skill
		run) never blocks an interactive send - a genuine new top-level message is
		close-trigger #2 (design §3.4 "Other close-triggers") and ENDS the run,
		leaving skill_autorun 0 rather than refusing the send."""
		from jarvis.chat.api import send_message
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		frappe.set_user(TEST_USER)
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				res = send_message(conv, "make three todos please")
		self.assertTrue(res["ok"], "a skill_autorun conversation must accept an interactive send")
		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0),
			0,
			"a new top-level message closes the approved run",
		)

	def test_confirm_refused_on_skip_confirmation_conv(self):
		"""INVARIANT: skip_confirmation=1 withdraws a parked card's confirm too, not
		just the send entry point - the D5-excluded write waits for the sweep, never
		the human. Mirrors
		test_macro_skip_confirmation.TestReviewHardening.test_confirm_refused_on_armed_conversation."""
		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "flag-sep-confirm-race"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		recs = [
			t
			for t in pending_confirm.list_for_owner(NON_ADMIN_USER, conversation=conv)
			if t.get("conversation") == conv
		]
		self.assertEqual(len(recs), 1)
		token = recs[0]["token"]
		res = actions_api._confirm_core(token, conv)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["type"], "InvalidConfirmation")
		self.assertTrue(frappe.db.exists("ToDo", todo.name), "the delete must not have run")

	def test_confirm_succeeds_on_skill_autorun_conv_d1_resume(self):
		"""INVARIANT (opposite of the test above): skill_autorun=1 alone must NOT block
		a confirm - only skip_confirmation does. A D1 destructive write (delete_doc) is
		excluded from uncarded auto-run and still parks a real card under an approved
		run (_park_pending_card); confirming that PAUSED card is the RESUME step and
		must reach dispatch, not be refused. dispatch_confirmed/receipt/continuation are
		mocked (mirrors TestApproveAndRun's happy path) so only the gate decision is
		pinned, not delete_doc's own permission plumbing."""
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		frappe.set_user(TEST_USER)
		token = _park_pending_card(conv, TEST_USER)
		with (
			patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp,
			patch("jarvis.api.persist_tool_receipt"),
			patch("jarvis.chat.admission.publish_action_confirmed"),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			res = actions_api._confirm_core(token, conv)
		disp.assert_called_once()
		self.assertTrue(res.get("ok"), "an approved-run D1 resume confirm must succeed, not be refused")
		self.assertIsNone(pending_confirm.peek(token), "the resumed card is consumed")

	def test_approve_and_run_refused_on_skip_confirmation_conv_both_flags_unreachable(self):
		"""INVARIANT: approve_and_run refuses a token whose conversation already carries
		skip_confirmation=1, so a conversation can never end up with BOTH flags set -
		defense-in-depth pinned independently of TestApproveAndRun's own coverage."""
		docname = _make_skill(TEST_USER, armed=True, name="flag-sep-ar-macro")
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		frappe.set_user(TEST_USER)
		token = _mint_approve_token(conv, TEST_USER, docname)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = actions_api.approve_and_run(token, conv)
		self.assertFalse(res.get("ok"))
		self.assertFalse(disp.called, "a skip_confirmation conversation must not open a skill run")
		self.assertIsNotNone(pending_confirm.peek(token), "the refusal does not consume the token")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skill_autorun") or 0), 0)
		self.assertTrue(callable(session_lifecycle.reap_stranded_skill_autorun))
