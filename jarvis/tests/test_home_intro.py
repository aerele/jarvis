"""Tests for the first-chat introduction (the static welcome bubble).

The bubble itself is SPA presentation - there is nothing server-side to render.
What the server owns is the cadence (a per-user, versioned, cross-device seen
flag) and, far more importantly, the promise that showing it changes NOTHING in
the chat data model.

That promise is the reason the introduction is not a persisted assistant
message, and it is what the ``TestLifecycleInvariants`` class below defends:

- ``list_conversations`` hides zero-message conversations, so a persisted
  welcome row would un-hide every abandoned empty chat in the sidebar;
- File Box derives its status from message rows;
- ``create_or_focus_empty`` reuse and the empty-conversation reaper both key on
  "this conversation has zero messages";
- the first real send must still be ``seq=1``, and the pump/turn state must not
  see text the model never produced.

``TestAckAtomicity`` covers the other half of the server side: the settings row
has concurrent single-field writers, so the ack must not be a full-doc save.
``TestVeteranBackfill`` covers patch v2_10, which is what keeps the
introduction pointed at genuinely new users.

Hermetic: disposable enabled System Users with the Jarvis User role (NOT admin
- the ack must work for the ordinary owner-scoped grant, which is ``if_owner``
with **no create** permission). ``mark_home_intro_seen`` writes a real row, and
``get_or_create_user_settings`` commits, so teardown deletes fixture rows
explicitly rather than trusting the transaction rollback.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jarvis.chat import usage, user_settings_api
from jarvis.chat.api import create_or_focus_empty, get_chat_ui_settings, list_conversations
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_user_role,
)

USETT = "Jarvis User Settings"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
SESSION = "Jarvis Chat Session"

USER_A = "jarvis-intro-a@example.test"
USER_B = "jarvis-intro-b@example.test"
_ALL_USERS = (USER_A, USER_B)


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "IntroTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(JARVIS_USER_ROLE)
	# The ack must hold for a plain Jarvis User: their permlevel-0 grant is
	# read+write ``if_owner`` with NO create, so a user meeting the introduction
	# before they have a settings row would 403 on a naive insert.
	present = {r.role for r in doc.get("roles", [])}
	drop = present & {"System Manager", JARVIS_ADMIN_ROLE}
	if drop:
		doc.remove_roles(*drop)


def _cleanup() -> None:
	for email in _ALL_USERS:
		for name in frappe.get_all(CONV, filters={"owner": email}, pluck="name"):
			for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
				frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
			frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(SESSION, filters={"user": email}, pluck="name"):
			frappe.delete_doc(SESSION, name, ignore_permissions=True, force=True)


class _IntroTestBase(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _seen(self, user: str) -> int:
		return frappe.utils.cint(
			frappe.db.get_value(USETT, {"user": user}, "home_intro_seen_version")
		)


# --------------------------------------------------------------------------- #
# 1. Cadence: the boot payload, the ack, and the version
# --------------------------------------------------------------------------- #
class TestCadence(_IntroTestBase):
	def test_boot_payload_offers_the_introduction_to_a_new_user(self):
		"""A user with no settings row at all reads seen=0 against version 1 -
		that is a real answer, not a failure, so the SPA shows the bubble."""
		frappe.set_user(USER_A)
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		ui = get_chat_ui_settings()
		self.assertEqual(ui["home_intro_version"], user_settings_api.HOME_INTRO_VERSION)
		self.assertEqual(ui["home_intro_seen_version"], 0)
		self.assertGreater(ui["home_intro_version"], ui["home_intro_seen_version"])
		# Reading the boot payload must not itself create the row (the ack does).
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))

	def test_ack_retires_the_introduction_across_devices(self):
		"""The seen flag is a DB row, not localStorage: the next boot on ANY
		device reads it back and the compact hero returns."""
		frappe.set_user(USER_A)
		out = user_settings_api.mark_home_intro_seen(version=1)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["home_intro_seen_version"], 1)
		ui = get_chat_ui_settings()
		self.assertEqual(ui["home_intro_seen_version"], ui["home_intro_version"])
		frappe.set_user("Administrator")
		self.assertEqual(self._seen(USER_A), 1)
		self.assertIsNotNone(frappe.db.get_value(USETT, {"user": USER_A}, "home_intro_seen_at"))

	def test_ack_works_for_a_plain_jarvis_user_with_no_row(self):
		"""The permlevel-0 grant is ``if_owner`` with NO create. The ack must go
		through the shared get-or-create path or a first-time user 403s."""
		frappe.set_user(USER_A)
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		user_settings_api.mark_home_intro_seen(version=1)
		frappe.set_user("Administrator")
		row = frappe.db.get_value(USETT, {"user": USER_A}, ["owner", "home_intro_seen_version"], as_dict=True)
		self.assertIsNotNone(row)
		# owner must be the settings user, or the if_owner grant stops holding.
		self.assertEqual(row.owner, USER_A)
		self.assertEqual(frappe.utils.cint(row.home_intro_seen_version), 1)

	def test_ack_is_idempotent(self):
		frappe.set_user(USER_A)
		user_settings_api.mark_home_intro_seen(version=1)
		modified = frappe.db.get_value(USETT, {"user": USER_A}, "modified")
		out = user_settings_api.mark_home_intro_seen(version=1)
		self.assertEqual(out["data"]["home_intro_seen_version"], 1)
		# A replay is a genuine no-op, not a rewrite of the same value.
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "modified"), modified)
		frappe.set_user("Administrator")
		self.assertEqual(len(frappe.get_all(USETT, filters={"user": USER_A})), 1)

	def test_ack_never_lowers_a_seen_version(self):
		"""A stale tab acking 0 (or an older version) must not re-show an
		introduction the user has already been given."""
		frappe.set_user(USER_A)
		user_settings_api.mark_home_intro_seen(version=1)
		user_settings_api.mark_home_intro_seen(version=0)
		self.assertEqual(get_chat_ui_settings()["home_intro_seen_version"], 1)
		frappe.set_user("Administrator")
		self.assertEqual(self._seen(USER_A), 1)

	def test_ack_clamps_to_the_server_version(self):
		"""The server owns the version. A client cannot mute a future
		introduction by acking a version that does not exist yet."""
		frappe.set_user(USER_A)
		user_settings_api.mark_home_intro_seen(version=99)
		frappe.set_user("Administrator")
		self.assertEqual(self._seen(USER_A), user_settings_api.HOME_INTRO_VERSION)

	def test_ack_coerces_a_hostile_version(self):
		"""``from __future__ import annotations`` turns this module's arg types
		into strings, so frappe's whitelist gate does not coerce them - the
		handler must survive raw junk rather than 500."""
		frappe.set_user(USER_A)
		for bad in ("1", "", None, "not-a-number", -5):
			out = user_settings_api.mark_home_intro_seen(version=bad)
			self.assertTrue(out["ok"])
		frappe.set_user("Administrator")
		# Only the "1" reached a real version; nothing negative was stored.
		self.assertEqual(self._seen(USER_A), 1)

	def test_ack_touches_only_the_callers_row(self):
		frappe.set_user(USER_B)
		user_settings_api.mark_home_intro_seen(version=1)
		frappe.set_user(USER_A)
		self.assertEqual(get_chat_ui_settings()["home_intro_seen_version"], 0)
		frappe.set_user("Administrator")
		self.assertEqual(self._seen(USER_A), 0)
		self.assertEqual(self._seen(USER_B), 1)

	def test_version_bump_reoffers_the_introduction_once(self):
		"""Materially new capability copy can be shown once more by bumping
		HOME_INTRO_VERSION; an already-acked user is offered it exactly once."""
		frappe.set_user(USER_A)
		user_settings_api.mark_home_intro_seen(version=1)
		self.assertEqual(get_chat_ui_settings()["home_intro_seen_version"], 1)
		# Simulate the next release without editing the constant under test.
		orig = user_settings_api.HOME_INTRO_VERSION
		try:
			user_settings_api.HOME_INTRO_VERSION = 2
			ui = get_chat_ui_settings()
			self.assertEqual(ui["home_intro_version"], 2)
			self.assertEqual(ui["home_intro_seen_version"], 1)  # due again
			user_settings_api.mark_home_intro_seen(version=2)
			ui = get_chat_ui_settings()
			self.assertEqual(ui["home_intro_seen_version"], 2)  # and only once
		finally:
			user_settings_api.HOME_INTRO_VERSION = orig
		frappe.set_user("Administrator")

	def test_seen_key_is_omitted_when_it_cannot_be_read(self):
		"""Same fail-quiet contract as preferred_persona: on a read failure the
		key is absent, and the SPA keeps the ordinary hero rather than
		re-lecturing a user who has already been introduced."""
		from jarvis.chat import api as chat_api

		frappe.set_user(USER_A)
		orig = chat_api._home_intro_seen_version
		try:
			chat_api._home_intro_seen_version = lambda: None
			ui = get_chat_ui_settings()
			self.assertNotIn("home_intro_seen_version", ui)
			self.assertIn("home_intro_version", ui)
		finally:
			chat_api._home_intro_seen_version = orig
		frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# 2. Ack atomicity - the row has other, concurrent writers
# --------------------------------------------------------------------------- #
class TestAckAtomicity(_IntroTestBase):
	"""``Jarvis User Settings`` is written by several server paths that touch a
	single field with raw ``update_modified=False`` SQL (admin_set_user_limit's
	spend cap, the usage counters, the greeting cadence counter). Because those
	writes leave ``modified`` alone, Frappe's timestamp check cannot catch a
	full-doc save built from a snapshot taken before them - it just silently
	writes the stale values back. The ack therefore touches ONLY its own two
	columns, via one conditional UPDATE."""

	def test_ack_does_not_clobber_a_concurrent_field_write(self):
		frappe.set_user("Administrator")
		usage.get_or_create_user_settings(USER_A)
		frappe.db.commit()

		# Deterministic interleaving: an admin sets this user's monthly spend cap
		# (exactly what admin_set_user_limit does - raw, update_modified=False)
		# in the window between the ack obtaining the row and the ack writing.
		real = usage.get_or_create_user_settings

		def racing_admin_write(user):
			doc = real(user)
			frappe.db.set_value(
				USETT, {"user": user}, "monthly_token_limit", 50000, update_modified=False
			)
			return doc

		frappe.set_user(USER_A)
		with patch.object(usage, "get_or_create_user_settings", side_effect=racing_admin_write):
			user_settings_api.mark_home_intro_seen(version=1)
		frappe.set_user("Administrator")

		# The cap the admin just set must survive...
		self.assertEqual(
			cint(frappe.db.get_value(USETT, {"user": USER_A}, "monthly_token_limit")),
			50000,
			"the ack wrote a stale snapshot back over a concurrent field write",
		)
		# ...and the ack must still have landed.
		self.assertEqual(self._seen(USER_A), 1)

	def test_ack_leaves_modified_untouched(self):
		"""Not bumping ``modified`` is deliberate, not an oversight: this row is
		app state, and a touched timestamp would make every ack look like a user
		preference change to anything watching the row."""
		frappe.set_user("Administrator")
		usage.get_or_create_user_settings(USER_A)
		frappe.db.commit()
		before = frappe.db.get_value(USETT, {"user": USER_A}, "modified")
		frappe.set_user(USER_A)
		user_settings_api.mark_home_intro_seen(version=1)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "modified"), before)
		self.assertEqual(self._seen(USER_A), 1)

	def test_ack_does_not_disturb_sibling_preferences(self):
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(notify_enabled=0, preferred_persona="Jara")
		user_settings_api.mark_home_intro_seen(version=1)
		out = user_settings_api.get_my_settings()["data"]
		self.assertEqual(out["notify_enabled"], 0)
		self.assertEqual(out["preferred_persona"], "Jara")
		frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# 3. Lifecycle invariants - the reason the bubble is not a persisted message
# --------------------------------------------------------------------------- #
class TestLifecycleInvariants(_IntroTestBase):
	"""Every assertion here fails against an implementation that persists the
	welcome as a Jarvis Chat Message (see the module docstring for why each one
	matters). They are the regression net for the design decision itself, not
	for the copy."""

	def _counts(self, user: str) -> tuple[int, int, int, int, int]:
		"""Everything showing the introduction must leave alone: conversations,
		messages, gateway sessions, and token spend. The last three exist so a
		variant that spins an openclaw session or calls a model to "generate" the
		greeting fails here too, not just one that persists a row."""
		convs = frappe.get_all(CONV, filters={"owner": user}, pluck="name")
		msgs = frappe.db.count(MSG, {"conversation": ["in", convs]}) if convs else 0
		sessions = frappe.db.count(SESSION, {"user": user})
		row = frappe.db.get_value(
			USETT, {"user": user}, ["month_tokens", "total_tokens"], as_dict=True
		)
		return (
			len(convs),
			msgs,
			sessions,
			cint(row.month_tokens) if row else 0,
			cint(row.total_tokens) if row else 0,
		)

	def test_showing_the_introduction_creates_no_rows(self):
		"""Boot + ack is the entire server side of showing the bubble. It must
		create no conversation and no message."""
		frappe.set_user(USER_A)
		before = self._counts(USER_A)
		get_chat_ui_settings()
		user_settings_api.mark_home_intro_seen(version=1)
		self.assertEqual(self._counts(USER_A), before)
		frappe.set_user("Administrator")
		self.assertEqual(self._counts(USER_A), (0, 0, 0, 0, 0))

	def test_the_empty_chat_stays_empty(self):
		"""The bubble renders over a genuinely empty conversation, and it must
		STAY empty: that zero-message property is what create_or_focus_empty's
		reuse and the empty-conversation reaper both key on."""
		frappe.set_user(USER_A)
		first = create_or_focus_empty()
		user_settings_api.mark_home_intro_seen(version=1)
		self.assertEqual(frappe.db.count(MSG, {"conversation": first}), 0)
		# Reuse still finds it: no new row, same name back.
		self.assertEqual(create_or_focus_empty(), first)
		self.assertEqual(len(frappe.get_all(CONV, filters={"owner": USER_A})), 1)
		frappe.set_user("Administrator")

	def test_sidebar_still_hides_the_untouched_new_chat(self):
		"""list_conversations filters on EXISTS(message). A persisted welcome
		would un-hide every abandoned empty chat - the loudest symptom of the
		design being violated."""
		frappe.set_user(USER_A)
		conv = create_or_focus_empty()
		user_settings_api.mark_home_intro_seen(version=1)
		self.assertNotIn(conv, [c["name"] for c in list_conversations()])
		frappe.set_user("Administrator")

	def test_first_real_message_is_still_seq_1(self):
		"""Nothing occupies sequence 1 ahead of the user's own first message,
		so the transcript still begins with what the user actually said."""
		frappe.set_user(USER_A)
		conv = create_or_focus_empty()
		user_settings_api.mark_home_intro_seen(version=1)
		frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": 1, "role": "user", "content": "hello"}
		).insert(ignore_permissions=True)
		rows = frappe.get_all(
			MSG, filters={"conversation": conv}, fields=["seq", "role"], order_by="seq asc"
		)
		self.assertEqual([(r.seq, r.role) for r in rows], [(1, "user")])
		frappe.set_user("Administrator")

	def test_a_conversation_with_messages_is_untouched_by_the_ack(self):
		"""A proactive conversation is a REAL persisted assistant message. The
		introduction never competes with it: the ack neither adds to nor
		reorders such a conversation (and the SPA gate cannot even reach it -
		showWelcome is false whenever a conversation has visible messages)."""
		frappe.set_user(USER_A)
		conv = create_or_focus_empty()
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 1,
				"role": "assistant",
				"content": "Your GST filing is due in 3 days.",
			}
		).insert(ignore_permissions=True)
		before = frappe.get_all(
			MSG, filters={"conversation": conv}, fields=["name", "seq", "role", "content"]
		)
		user_settings_api.mark_home_intro_seen(version=1)
		after = frappe.get_all(
			MSG, filters={"conversation": conv}, fields=["name", "seq", "role", "content"]
		)
		self.assertEqual(before, after)
		self.assertEqual(len(after), 1)
		frappe.set_user("Administrator")

	def test_business_greeting_cadence_is_not_consumed(self):
		"""The SPA suppresses the business-note banner while the introduction
		shows. That is a render-time gate only: the cadence counter is bumped by
		create_or_focus_empty and maybe_greet is a pure reader, so nothing about
		the introduction advances, resets or consumes a cadence tick."""
		from jarvis.chat.greeting import maybe_greet

		frappe.set_user(USER_A)
		create_or_focus_empty()
		count_before = frappe.utils.cint(
			frappe.db.get_value(USETT, {"user": USER_A}, "business_greeting_chat_count")
		)
		get_chat_ui_settings()
		user_settings_api.mark_home_intro_seen(version=1)
		maybe_greet()
		count_after = frappe.utils.cint(
			frappe.db.get_value(USETT, {"user": USER_A}, "business_greeting_chat_count")
		)
		self.assertEqual(count_after, count_before)
		self.assertEqual(count_before, 1)
		frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# 4. Existing-user backfill (patch v2_10)
# --------------------------------------------------------------------------- #
class TestVeteranBackfill(_IntroTestBase):
	"""The seen column arrives at 0 on every row, so without the backfill the
	release would introduce Jarvis to people who have used it for months. The
	signal is behavioural - a conversation that actually has messages - not a
	registration date."""

	def _run_patch(self) -> None:
		from jarvis.patches.v2_10_backfill_home_intro_seen import execute

		execute()

	def _seed_history(self, user: str) -> str:
		"""A conversation owned by ``user`` carrying one real message."""
		conv = frappe.get_doc({"doctype": CONV, "title": "history"}).insert(ignore_permissions=True)
		frappe.db.set_value(CONV, conv.name, "owner", user, update_modified=False)
		frappe.get_doc(
			{"doctype": MSG, "conversation": conv.name, "seq": 1, "role": "user", "content": "hi"}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return conv.name

	def test_veteran_with_history_is_marked_seen(self):
		usage.get_or_create_user_settings(USER_A)
		self._seed_history(USER_A)
		self.assertEqual(self._seen(USER_A), 0)
		self._run_patch()
		self.assertEqual(self._seen(USER_A), 1)
		self.assertIsNotNone(frappe.db.get_value(USETT, {"user": USER_A}, "home_intro_seen_at"))
		# ...and the boot payload agrees: no introduction for them.
		frappe.set_user(USER_A)
		ui = get_chat_ui_settings()
		self.assertEqual(ui["home_intro_seen_version"], ui["home_intro_version"])
		frappe.set_user("Administrator")

	def test_veteran_without_a_settings_row_gets_one(self):
		"""Rows are created lazily, so a veteran can legitimately have none - and
		a missing row reads as seen=0, exactly the case this patch prevents."""
		self._seed_history(USER_A)
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		self._run_patch()
		row = frappe.db.get_value(
			USETT,
			{"user": USER_A},
			["owner", "home_intro_seen_version", "notify_enabled", "preferred_persona"],
			as_dict=True,
		)
		self.assertIsNotNone(row)
		self.assertEqual(cint(row.home_intro_seen_version), 1)
		# owner must be the settings user or the if_owner permlevel-0 grant breaks.
		self.assertEqual(row.owner, USER_A)
		# Schema defaults, the same values get_or_create_user_settings produces.
		self.assertEqual(cint(row.notify_enabled), 1)
		self.assertEqual(row.preferred_persona, "Jarvis")

	def test_genuinely_new_user_is_left_alone(self):
		usage.get_or_create_user_settings(USER_B)
		frappe.db.commit()
		self._run_patch()
		self.assertEqual(self._seen(USER_B), 0)
		frappe.set_user(USER_B)
		ui = get_chat_ui_settings()
		self.assertGreater(ui["home_intro_version"], ui["home_intro_seen_version"])
		frappe.set_user("Administrator")

	def test_an_empty_conversation_is_not_history(self):
		"""An abandoned New Chat (or a File Box drop that never sent) proves the
		user has seen nothing, so it must not grandfather them."""
		usage.get_or_create_user_settings(USER_B)
		conv = frappe.get_doc({"doctype": CONV, "title": "abandoned"}).insert(ignore_permissions=True)
		frappe.db.set_value(CONV, conv.name, "owner", USER_B, update_modified=False)
		frappe.db.commit()
		self._run_patch()
		self.assertEqual(self._seen(USER_B), 0)

	def test_patch_is_idempotent_and_never_lowers(self):
		self._seed_history(USER_A)
		self._run_patch()
		stamp = frappe.db.get_value(USETT, {"user": USER_A}, "home_intro_seen_at")
		self._run_patch()
		self.assertEqual(self._seen(USER_A), 1)
		# Re-running neither re-stamps nor duplicates the row.
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "home_intro_seen_at"), stamp)
		self.assertEqual(len(frappe.get_all(USETT, filters={"user": USER_A})), 1)

		# And a user already ahead of the patch keeps their higher version: a
		# replay after a future HOME_INTRO_VERSION bump must not walk them back.
		frappe.db.set_value(USETT, {"user": USER_A}, "home_intro_seen_version", 5)
		frappe.db.commit()
		self._run_patch()
		self.assertEqual(self._seen(USER_A), 5)
