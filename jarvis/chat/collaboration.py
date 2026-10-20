"""Default-off collaboration foundation. No shared writer ships in C1.

These guards are deliberately independent of the feature switch: switching off
collaboration must never revive an owner's legacy session or expose raw rows.
Only trusted server services can stamp bindings; no function here is whitelisted.
"""

from dataclasses import dataclass

import frappe
from frappe.utils import cint

CONVERSATION = "Jarvis Conversation"
SESSION = "Jarvis Chat Session"
BINDING = "Jarvis Chat Binding"
SHARE = "Jarvis Conversation Share"
READER_FLOOR = 1
# C2 must supply the complete authorized runtime before this can advance.
WRITER_CAPABILITY = 0
NAMESPACE = ":dashboard:collab:"
IDENTITY_FIELDS = (
	"conversation",
	"actor",
	"logical_run_id",
	"session_key",
	"share",
	"grant_revision",
	"conversation_epoch",
)


def effective_mode(value) -> str:
	return "Chat" if value == "Chat" else "View"


def schema_ready() -> bool:
	if not frappe.db.table_exists(BINDING):
		return False
	return all(
		frappe.db.has_column(doctype, field)
		for doctype, field in (
			(CONVERSATION, "collaboration_active"),
			(CONVERSATION, "collaboration_epoch"),
			(SESSION, "collaboration_binding"),
			(SHARE, "access_revision"),
			(SHARE, "access_mode"),
			(BINDING, "conversation_epoch"),
		)
	)


def writes_ready() -> bool:
	return bool(
		WRITER_CAPABILITY
		and cint(frappe.conf.get("chat_collaboration_enabled"))
		and cint(frappe.conf.get("chat_collaboration_reader_floor")) >= READER_FLOOR
		and schema_ready()
	)


def _deny():
	frappe.throw("Collaborative chat execution is unavailable on this server.", frappe.PermissionError)


def require_writes_ready():
	if not writes_ready():
		_deny()


def is_collaborative(conversation) -> bool:
	if not conversation:
		return False
	if not isinstance(conversation, str):
		conversation = conversation.get("name")
	# A reader deployed before schema expansion can continue genuinely legacy
	# chats; reserved collaborative session keys still fail closed below.
	if not frappe.db.has_column(CONVERSATION, "collaboration_active"):
		return False
	return bool(cint(frappe.db.get_value(CONVERSATION, conversation, "collaboration_active")))


def require_legacy_conversation(conversation):
	if is_collaborative(conversation):
		_deny()


def is_collaborative_session(key: str) -> bool:
	if NAMESPACE in (key or ""):
		return True
	if frappe.db.has_column(SESSION, "collaboration_binding"):
		bindings = frappe.db.get_values(
			SESSION, {"session_key": key}, "collaboration_binding", limit=1, order_by=None
		)
		if bindings and bindings[0][0]:
			return True
	# The immutable ledger remains authoritative if an auth row is lost/rebuilt.
	if frappe.db.table_exists(BINDING) and frappe.db.get_values(
		BINDING, {"session_key": key}, "name", limit=1, order_by=None
	):
		return True
	conversation = frappe.db.get_value(CONVERSATION, {"session_key": key}, "name") if key else None
	return is_collaborative(conversation)


def require_legacy_session(key: str):
	if is_collaborative_session(key):
		_deny()


def legacy_session_user(key: str):
	require_legacy_session(key)
	return frappe.db.get_value(SESSION, {"session_key": key}, "user")


@dataclass(frozen=True)
class ActorBinding:
	name: str
	conversation: str
	actor: str
	logical_run_id: str
	session_key: str
	share: str | None
	grant_revision: int
	conversation_epoch: int


def resolve_binding(key: str) -> ActorBinding:
	"""Resolve identity only; this grants NO dispatch capability in C1.

	C2 must add live authorization and turn-state fencing at every dispatch.
	Never fall back to a conversation owner on malformed or retired bindings.
	"""
	if not schema_ready():
		_deny()
	row = frappe.db.get_value(
		BINDING, {"session_key": key}, ["name", *IDENTITY_FIELDS, "state"], as_dict=True
	)
	auth = frappe.db.get_value(SESSION, {"session_key": key}, ["user", "collaboration_binding"], as_dict=True)
	if (
		not row
		or row.state != "Active"
		or not auth
		or auth.user != row.actor
		or auth.collaboration_binding != row.name
	):
		_deny()
	if (
		NAMESPACE not in key
		or not row.actor
		or not row.logical_run_id
		or not is_collaborative(row.conversation)
	):
		_deny()
	if cint(frappe.db.get_value(CONVERSATION, row.conversation, "collaboration_epoch")) != cint(
		row.conversation_epoch
	):
		_deny()
	if row.share:
		grant = frappe.db.get_value(
			SHARE, row.share, ["conversation", "user", "access_mode", "access_revision"], as_dict=True
		)
		if (
			not grant
			or grant.conversation != row.conversation
			or grant.user != row.actor
			or effective_mode(grant.access_mode) != "Chat"
			or cint(grant.access_revision) != cint(row.grant_revision)
		):
			_deny()
	elif frappe.db.get_value(CONVERSATION, row.conversation, "owner") != row.actor:
		_deny()
	return ActorBinding(**{field: row.get(field) for field in ("name", *IDENTITY_FIELDS)})


def guard_server_fields(doc, fields):
	"""Immutable on existing rows, trusted-only on insert (Admin included)."""
	before = None if doc.is_new() else doc.get_doc_before_save()
	for field in fields:
		old = before.get(field) if before else None
		new = doc.get(field)
		df = doc.meta.get_field(field)
		if df and df.fieldtype in ("Check", "Int"):
			unchanged = cint(new) == cint(old)
		else:
			# Data/Link identity stamps must not collapse 0/False into empty.
			unchanged = (None if new in (None, "") else new) == (None if old in (None, "") else old)
		if unchanged:
			continue
		if before or not doc.flags.jarvis_collaboration_write:
			frappe.throw(
				"Collaboration identity fields are server-managed and immutable.", frappe.PermissionError
			)


def guard_record_binding(doc):
	guard_server_fields(doc, ("actor", "collaboration_binding"))
	if not doc.is_new():
		return
	if doc.get("actor") not in (None, "") or doc.get("collaboration_binding") not in (None, ""):
		binding_key = (
			frappe.db.get_value(BINDING, doc.get("collaboration_binding"), "session_key")
			if doc.get("collaboration_binding")
			else None
		)
		if not binding_key:
			_deny()
		binding = resolve_binding(binding_key)
		if binding.actor != doc.get("actor") or binding.conversation != doc.get("conversation"):
			_deny()
	elif doc.is_new() and is_collaborative(doc.get("conversation")):
		_deny()


def has_binding_evidence() -> bool:
	return bool(
		(frappe.db.table_exists(BINDING) and frappe.db.exists(BINDING))
		or (
			frappe.db.has_column(CONVERSATION, "collaboration_active")
			and frappe.db.exists(CONVERSATION, {"collaboration_active": 1})
		)
	)


def assert_rollback_floor(target_floor: int):
	"""Operator preflight, not a mechanism that can stop a binary/git rollback.

	Conservatively refuse below C1 while any collaborative evidence remains.
	Retiring auth capability never deletes the immutable audit ledger.
	"""
	if target_floor <= READER_FLOOR and cint(frappe.conf.get("chat_collaboration_enabled")):
		frappe.throw("Turn collaboration off before rollback.", frappe.PermissionError)
	if target_floor == READER_FLOOR and has_undrained_work():
		frappe.throw("Drain and reconcile collaborative work before rollback to C1.", frappe.PermissionError)
	if target_floor < READER_FLOOR and (
		cint(frappe.conf.get("chat_collaboration_enabled")) or has_binding_evidence()
	):
		frappe.throw("Rollback below the collaboration reader floor is unsafe.", frappe.PermissionError)


def has_undrained_work() -> bool:
	if frappe.db.table_exists(BINDING) and frappe.db.exists(BINDING, {"state": "Uncertain"}):
		return True
	if not frappe.db.has_column(CONVERSATION, "collaboration_active"):
		return False
	return bool(
		frappe.db.sql("""SELECT t.name FROM `tabJarvis Chat Turn` t
		JOIN `tabJarvis Conversation` c ON c.name=t.conversation
		WHERE COALESCE(c.collaboration_active,0)<>0
		AND t.state NOT IN ('done','errored','cancelled') LIMIT 1""")
	)
