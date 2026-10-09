"""Row-level ownership scoping for the Jarvis chat doctypes (Jarvis
Conversation, Jarvis Chat Message, Jarvis Chat Turn, Jarvis Approval Request,
Jarvis Voice Note).

This is the data-layer twin of ``jarvis/chat/wiki_permissions.py``: list /
report / generic-REST queries are scoped via the ``permission_query_conditions``
hook, and per-doc read/write/create/delete via the ``has_permission`` hook -
both registered in ``hooks.py``. Together with putting the ``Jarvis User`` role
(not ``role: "All"``) on the doctype permission rows, this closes the
REST-insert bypass and the cross-user injection at the ORM, so *every* endpoint
(current and future) and stock ``/api/resource`` / ``frappe.client.*`` inherit
owner scoping automatically instead of relying on a hand-rolled owner check in
each whitelisted function.

Ownership axes (deliberately NOT uniform - each doctype's real owner differs):

  * Jarvis Conversation   -> the row's own ``owner`` (the chat user). Matches
    the ``if_owner`` perm rule that also stays on the doctype.
  * Jarvis Chat Message   -> the owner of the LINKED conversation, never the
    message row's own owner. Assistant / tool / system rows are inserted by the
    worker or tool dispatcher (as the impersonated owner or Administrator), so
    the row owner is not a reliable authority; ``api.get_conversation``
    deliberately treats the conversation owner as the single source of truth.
  * Jarvis Chat Turn      -> the owner of the LINKED conversation, like Message.
    A turn row carries the user's prompt (``dispatch_payload``) and the
    assistant's reply (``terminal_payload``), so it is exactly as private as the
    conversation: System Manager and Jarvis Admin see only their own turns, as
    with Jarvis Conversation. The turn pipeline (pump / turn_state / settlement)
    reads and writes turns through ``frappe.db`` in system context, so it never
    consults these hooks.
  * Jarvis Approval Request -> owner of the linked conversation OR a DocShare
    read grant on the approval itself (mirrors ``approvals_api``'s EXISTS
    probes: a tagged user may READ/view on the board, only the owner or a
    System Manager may ACT). System Manager sees all (oversight; existing SM
    perm row).
  * Jarvis Voice Note     -> the row's own ``owner``; System Manager gets
    org-wide READ (the Business-tab processing view; existing SM perm row).

Server context: assistant / tool / system rows and the API-layer inserts are
written with ``insert(ignore_permissions=True)`` (often under
``impersonate(owner)``), so the ``has_permission`` hook is never consulted for
them. The controller ``validate`` cross-link checks (message->conversation,
voice->conversation) additionally skip when ``self.flags.ignore_permissions``
is set. ``Administrator`` bypasses Frappe perms entirely (the hook is not even
called for it), but every function guards for it defensively.

NOTE (hooks can only DENY): on this Frappe version a falsy ``has_permission``
return (``None`` included) denies, so every allow path returns an explicit
``True`` to defer to the normal role-perm check.
"""

from __future__ import annotations

import frappe

CONVERSATION = "Jarvis Conversation"
MESSAGE = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"
VOICE_NOTE = "Jarvis Voice Note"

# ptypes that reveal a row's content; everything read-shaped maps to visibility.
_READ_PTYPES = ("read", "select", "print", "email", "export", "share", "report")


def _is_sm(user: str) -> bool:
	# Administrator's get_roles returns every Role; the explicit check is a
	# shortcut so both spellings of "full access" land here.
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _conversation_owner(conversation) -> str | None:
	if not conversation:
		return None
	return frappe.db.get_value(CONVERSATION, conversation, "owner")


# --------------------------------------------------------------------------- #
# Jarvis Conversation - the row's own owner is the axis (matches ``if_owner``).
# --------------------------------------------------------------------------- #
def conversation_query_conditions(user: str | None = None) -> str:
	"""Scope every list/report/REST query on Jarvis Conversation to the caller's
	own rows. Administrator is unrestricted; System Manager sees only its own
	conversations (unchanged from the pre-fix ``All`` + ``if_owner`` rule - we do
	NOT expand SM to org-wide private chats)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return f"`tabJarvis Conversation`.`owner` = {frappe.db.escape(user)}"


def has_conversation_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if ptype == "create":
		# ``owner`` is assigned by Frappe during insert; the role + ``if_owner``
		# rule governs create (creator == owner trivially). Enforcing owner here
		# would race the owner assignment. Reads/writes/deletes of EXISTING rows
		# are gated below.
		return True
	return doc.get("owner") == user


# --------------------------------------------------------------------------- #
# Jarvis Chat Message - the LINKED conversation's owner is the axis.
# --------------------------------------------------------------------------- #
def message_query_conditions(user: str | None = None) -> str:
	"""Scope Jarvis Chat Message queries to rows whose linked conversation the
	caller owns (a subquery on ``tabJarvis Conversation`` - the message row's own
	owner is deliberately NOT the authority). Administrator is unrestricted."""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	esc = frappe.db.escape(user)
	return (
		"exists (select 1 from `tabJarvis Conversation` c "
		"where c.name = `tabJarvis Chat Message`.`conversation` "
		f"and c.owner = {esc})"
	)


def has_message_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""Per-doc gate for Jarvis Chat Message: read/write/create/delete all require
	owning the LINKED conversation. This is the primary control that blocks the
	cross-user injection (inserting a message with ``conversation`` = another
	user's id) at the ORM, so it covers generic REST too."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return _conversation_owner(doc.get("conversation")) == user


# --------------------------------------------------------------------------- #
# Jarvis Chat Turn - the LINKED conversation's owner is the axis (like Message).
# --------------------------------------------------------------------------- #
def turn_query_conditions(user: str | None = None) -> str:
	"""Scope Jarvis Chat Turn queries to turns whose linked conversation the
	caller owns. Administrator is unrestricted; System Manager and Jarvis Admin
	see only turns of their own conversations, matching Jarvis Conversation (a
	turn's prompt and reply are the conversation's content)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	esc = frappe.db.escape(user)
	return (
		"exists (select 1 from `tabJarvis Conversation` c "
		"where c.name = `tabJarvis Chat Turn`.`conversation` "
		f"and c.owner = {esc})"
	)


def has_turn_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""Per-doc gate for Jarvis Chat Turn: every ptype requires owning the LINKED
	conversation, so another user's prompt and reply never leak through desk,
	``/api/resource`` or ``frappe.client.*``."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return _conversation_owner(doc.get("conversation")) == user


# --------------------------------------------------------------------------- #
# Jarvis Approval Request - linked-conversation owner OR DocShare read grant.
# --------------------------------------------------------------------------- #
def approval_query_conditions(user: str | None = None) -> str:
	"""Scope Jarvis Approval Request queries exactly like ``approvals_api``'s
	list: the caller owns the linked conversation, OR holds a DocShare read grant
	on the approval (tagging). System Manager (and Administrator) see all."""
	user = user or frappe.session.user
	if _is_sm(user):
		return ""
	esc = frappe.db.escape(user)
	return (
		"(exists (select 1 from `tabJarvis Conversation` c "
		"where c.name = `tabJarvis Approval Request`.`conversation` "
		f"and c.owner = {esc}) "
		"or exists (select 1 from `tabDocShare` ds "
		"where ds.share_doctype = 'Jarvis Approval Request' "
		"and ds.share_name = `tabJarvis Approval Request`.`name` "
		f"and ds.user = {esc} and ds.`read` = 1))"
	)


def has_approval_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	"""Per-doc gate for Jarvis Approval Request. System Manager: all. Owner of the
	linked conversation: full. A DocShare-read holder: READ only (view on the
	board, ``can_act=0`` - mirrors ``get_approval`` vs ``decide``). Create of a
	row that names another user's conversation is denied (cross-inject guard),
	but a conversation-less agent approval is allowed."""
	user = user or frappe.session.user
	if _is_sm(user):
		return True
	owner = _conversation_owner(doc.get("conversation"))
	if ptype == "create":
		# Block injecting an approval into another user's conversation; a row
		# with no conversation (agent-internal) is allowed, gated by the role.
		return owner is None or owner == user
	if owner == user:
		return True
	# Non-owner may only READ a row explicitly shared to them.
	if ptype in _READ_PTYPES:
		return bool(
			frappe.db.exists(
				"DocShare",
				{
					"share_doctype": APPROVAL,
					"share_name": doc.get("name"),
					"user": user,
					"read": 1,
				},
			)
		)
	return False


# --------------------------------------------------------------------------- #
# Jarvis Voice Note - the row's own owner is the axis; SM gets org-wide read.
# --------------------------------------------------------------------------- #
def voice_note_query_conditions(user: str | None = None) -> str:
	"""Scope Jarvis Voice Note queries to the caller's own notes. System Manager
	(and Administrator) get org-wide READ - the Business-tab processing view;
	the existing SM perm row is read-only, so this never widens SM write."""
	user = user or frappe.session.user
	if _is_sm(user):
		return ""
	return f"`tabJarvis Voice Note`.`owner` = {frappe.db.escape(user)}"


def has_voice_note_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	user = user or frappe.session.user
	if _is_sm(user):
		return True
	return doc.get("owner") == user
