"""Cross-user isolation for Jarvis Conversation / Jarvis Chat Message.

WHY THIS EXISTS: the DocType rows are deliberately broad. "Jarvis User" holds
read/write/create/delete on Jarvis Chat Message with if_owner=0, i.e. the role
layer alone would let any Jarvis user touch ANY message. The ONLY thing scoping
a user to their own chats is the pair of hooks in jarvis/chat/chat_permissions.py
(permission_query_conditions for lists, has_permission for single docs).

That makes those hooks load-bearing rather than defence in depth: if one is
unregistered, renamed, or returns "" for a non-Administrator, every user's
private chat history becomes readable and deletable by every other user, and no
DocType-level rule would catch it. These tests assert the observable outcome
(user B cannot see or touch user A's rows) rather than the hook internals, so
they fail on ANY regression that reopens the hole.

Hermetic: both fixture users and every row created here are removed in
tearDownClass.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, ensure_jarvis_user_role

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"

USER_A = "jarvis-iso-a@example.com"
USER_B = "jarvis-iso-b@example.com"
# Chat Turn fixtures: two admin-tier non-owners, plus an admin who owns a chat.
TURN_JADMIN = "jarvis-iso-turn-jadmin@example.com"  # Jarvis Admin, not System Manager
TURN_SM = "jarvis-iso-turn-sm@example.com"  # System Manager
TURN_OWNER = "jarvis-iso-turn-owner@example.com"  # Jarvis Admin who owns a chat


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Iso",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)


class TestChatCrossUserIsolation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_jarvis_user_role()
		_ensure_user(USER_A)
		_ensure_user(USER_B)

		# A conversation + message owned by A, created AS A so `owner` is A.
		frappe.set_user(USER_A)
		cls.conv_a = frappe.get_doc({"doctype": CONV, "title": "A private"}).insert().name
		cls.msg_a = (
			frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": cls.conv_a,
					"seq": 1,
					"role": "user",
					"content": "A's secret",
					"streaming": 0,
				}
			)
			.insert()
			.name
		)
		frappe.set_user("Administrator")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, name in ((MSG, cls.msg_a), (CONV, cls.conv_a)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		for email in (USER_A, USER_B):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	# -- lists (permission_query_conditions) --------------------------------- #
	#
	# frappe.get_list, NEVER frappe.get_all: get_all is documented as "will not
	# check for permissions" (it is get_list with ignore_permissions), so it
	# skips permission_query_conditions entirely and would pass this file
	# vacuously. get_list is also what the generic REST surface
	# (/api/resource/<doctype>) actually runs, which is the exposure being
	# guarded here.

	def test_b_cannot_list_as_conversation(self):
		frappe.set_user(USER_B)
		names = frappe.get_list(CONV, pluck="name", limit=0)
		self.assertNotIn(self.conv_a, names, "user B can list user A's conversation")

	def test_b_cannot_list_as_message(self):
		frappe.set_user(USER_B)
		names = frappe.get_list(MSG, pluck="name", limit=0)
		self.assertNotIn(self.msg_a, names, "user B can list user A's chat message")

	def test_b_cannot_reach_as_message_by_explicit_filter(self):
		"""Filtering straight at the row must not bypass the scoping."""
		frappe.set_user(USER_B)
		rows = frappe.get_list(MSG, filters={"name": self.msg_a}, pluck="name")
		self.assertEqual(rows, [], "explicit filter leaked user A's message")

	# -- single docs (has_permission) ---------------------------------------- #

	def test_b_cannot_read_as_conversation_doc(self):
		frappe.set_user(USER_B)
		self.assertFalse(
			frappe.has_permission(CONV, "read", doc=self.conv_a, user=USER_B),
			"user B has read permission on user A's conversation",
		)

	def test_b_cannot_read_as_message_doc(self):
		frappe.set_user(USER_B)
		self.assertFalse(
			frappe.has_permission(MSG, "read", doc=self.msg_a, user=USER_B),
			"user B has read permission on user A's message",
		)

	def test_b_cannot_delete_as_message_doc(self):
		frappe.set_user(USER_B)
		self.assertFalse(
			frappe.has_permission(MSG, "delete", doc=self.msg_a, user=USER_B),
			"user B has delete permission on user A's message",
		)

	# -- the owner still works (guards against an over-tight fix) ------------ #

	def test_a_can_still_read_own_rows(self):
		frappe.set_user(USER_A)
		self.assertIn(self.conv_a, frappe.get_list(CONV, pluck="name", limit=0))
		self.assertIn(self.msg_a, frappe.get_list(MSG, pluck="name", limit=0))
		self.assertTrue(frappe.has_permission(CONV, "read", doc=self.conv_a, user=USER_A))
		self.assertTrue(frappe.has_permission(MSG, "read", doc=self.msg_a, user=USER_A))


def _ensure_user_with_roles(email: str, roles: list[str]) -> None:
	_ensure_user(email)
	frappe.get_doc("User", email).add_roles(*roles)


def _make_turn(owner: str, tag: str) -> tuple[str, str, str]:
	"""A conversation + seed message owned by ``owner`` (created AS them so
	``owner`` is real), and a Chat Turn linked to it. The turn is inserted in
	system context, exactly as the turn pipeline creates them."""
	frappe.set_user(owner)
	conv = frappe.get_doc({"doctype": CONV, "title": f"turn iso {tag}"}).insert().name
	msg = (
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 1,
				"role": "user",
				"content": f"{tag}'s private prompt",
				"streaming": 0,
			}
		)
		.insert()
		.name
	)
	frappe.set_user("Administrator")
	turn = (
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": f"iso-turn-{tag}-{frappe.generate_hash(length=8)}",
				"conversation": conv,
				"seed_message": msg,
				"relay_target_id": "iso",
				"turn_class": "interactive",
				"state": "done",
				"dispatch_payload": frappe.as_json({"message": f"{tag}'s private prompt"}),
			}
		)
		.insert(ignore_permissions=True)
		.name
	)
	return conv, msg, turn


class TestChatTurnIsolation(FrappeTestCase):
	"""Jarvis Chat Turn holds the user's prompt (``dispatch_payload``) and the
	reply (``terminal_payload``), yet its DocType grants read to System Manager
	and Jarvis Admin. Only the turn_query_conditions / has_turn_permission hooks
	keep those admins to turns of their OWN conversations; without them any
	admin-tier user could read every user's private chats. Asserted on the
	observable outcome, like the class above. Hermetic."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_jarvis_user_role()
		_ensure_user(USER_A)
		_ensure_user_with_roles(TURN_JADMIN, [JARVIS_ADMIN_ROLE])
		_ensure_user_with_roles(TURN_SM, ["System Manager"])
		_ensure_user_with_roles(TURN_OWNER, [JARVIS_ADMIN_ROLE])
		cls.conv_a, cls.msg_a, cls.turn_a = _make_turn(USER_A, "a")
		cls.conv_o, cls.msg_o, cls.turn_o = _make_turn(TURN_OWNER, "owner")
		frappe.set_user("Administrator")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, name in (
			(TURN, cls.turn_a),
			(TURN, cls.turn_o),
			(MSG, cls.msg_a),
			(MSG, cls.msg_o),
			(CONV, cls.conv_a),
			(CONV, cls.conv_o),
		):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		for email in (TURN_JADMIN, TURN_SM, TURN_OWNER):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_admins_cannot_list_another_users_turn(self):
		for admin in (TURN_JADMIN, TURN_SM):
			frappe.set_user(admin)
			self.assertNotIn(self.turn_a, frappe.get_list(TURN, pluck="name", limit=0), admin)

	def test_admins_cannot_fetch_another_users_turn_by_name(self):
		# A direct name filter is the /api/resource/Jarvis Chat Turn/<run_id> shape.
		for admin in (TURN_JADMIN, TURN_SM):
			frappe.set_user(admin)
			rows = frappe.get_list(TURN, filters={"name": self.turn_a}, pluck="name")
			self.assertEqual(rows, [], admin)

	def test_admins_have_no_per_doc_read_on_another_users_turn(self):
		for admin in (TURN_JADMIN, TURN_SM):
			self.assertFalse(frappe.has_permission(TURN, "read", doc=self.turn_a, user=admin), admin)

	def test_admin_still_reads_own_turn(self):
		# Guards against an over-tight fix: an admin keeps access to turns of
		# conversations they own, and still cannot see anyone else's.
		frappe.set_user(TURN_OWNER)
		names = frappe.get_list(TURN, pluck="name", limit=0)
		self.assertIn(self.turn_o, names)
		self.assertNotIn(self.turn_a, names)
		self.assertTrue(frappe.has_permission(TURN, "read", doc=self.turn_o, user=TURN_OWNER))
