"""An owner's explicit replacement for a File Box document.

The original pointer remains the drop's provenance/duplicate identity. Selecting
an attachment changes no approval, proposal or execution state.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_user

CONV = "Jarvis Conversation"
FIELD = "filebox_active_source_file"
FILE_FIELDS = ("name", "file_url", "file_name", "owner", "attached_to_doctype", "attached_to_name")


class SourceLost(frappe.ValidationError):
	pass


def ready() -> bool:
	return frappe.get_meta(CONV).has_field(FIELD) and frappe.db.has_column(CONV, FIELD)


def _lost():
	raise SourceLost(_("The source file is unavailable. Select a source file again."))


def selected_file(conversation: str, fields: tuple = ("file_url", "file_name"), *, read_as=None):
	"""None means no selection; a lost/revoked selection never means fallback."""
	if not ready():
		return None
	meta = frappe.db.get_value(CONV, conversation, ["file_box", FIELD], as_dict=True)
	if not meta or not meta.file_box or not meta.get(FIELD):
		return None
	row = frappe.db.get_value("File", meta[FIELD], list(dict.fromkeys((*FILE_FIELDS, *fields))), as_dict=True)
	if (
		not row
		or row.attached_to_doctype != CONV
		or row.attached_to_name != conversation
		or not frappe.has_permission("File", "read", doc=row.name, user=read_as)
	):
		_lost()
	return frappe._dict({field: row.get(field) for field in fields})


def _owned(conversation):
	from jarvis.chat.collaboration import require_legacy_conversation

	doc = frappe.get_doc(CONV, conversation)
	if doc.owner != frappe.session.user:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	require_legacy_conversation(conversation)
	if not doc.file_box:
		frappe.throw(_("Not a File Box conversation"))
	return doc


def _state(doc):
	version = doc.get(FIELD) or "" if ready() else ""
	try:
		from jarvis.chat.filebox import _source_file

		source = _source_file(doc.name, ("name", "file_url", "file_name"))
		if source and not frappe.has_permission("File", "read", doc=source.name):
			source = None
		status = "ok" if source else "none"
	except SourceLost:
		source, status = None, "lost"
	return {"ok": True, "status": status, "supported": ready(), "version": version, "source": source}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def get_source_state(
	conversation: str, message: str | None = None, item: str | None = None, file_url: str | None = None
) -> dict:
	doc = _owned(conversation)
	state = _state(doc)
	if any(value is not None for value in (message, item, file_url)):
		if not all((message, item, file_url)):
			frappe.throw(_("An attachment reference is required."))
		messages_before = len(frappe.local.message_log or [])
		candidate = None
		referenced = False
		try:
			_check_reference(conversation, message, item, file_url)
			referenced = True
			candidate = _referenced_file(conversation, message, item, file_url, lock=False, meta=doc)
			state["candidate_available"] = True
		except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError):
			del frappe.local.message_log[messages_before:]
			state["candidate_available"] = False
		# Eligibility never grants File read. A valid reference can still be
		# readable through another File row without being safe to re-parent.
		if referenced:
			state["preview_available"] = bool(candidate) or _readable_reference(file_url)
	return state


def _readable_reference(file_url):
	rows = frappe.get_all(
		"File",
		filters={"file_url": file_url},
		fields=["name", "owner"],
		order_by="creation desc",
		limit_page_length=0,
	)
	rows.sort(key=lambda row: row.owner != frappe.session.user)
	for row in rows:
		messages_before = len(frappe.local.message_log or [])
		try:
			if frappe.has_permission("File", "read", doc=row.name):
				return True
		except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError):
			del frappe.local.message_log[messages_before:]
	return False


def _check_reference(conversation, message, item, file_url):
	msg = frappe.db.get_value(
		"Jarvis Chat Message", message, ["conversation", "role", "canvas"], as_dict=True
	)
	if not msg or msg.conversation != conversation or msg.role != "user":
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	try:
		canvas = json.loads(msg.canvas or "[]")
	except (TypeError, ValueError):
		canvas = []
	if not isinstance(canvas, list) or not any(
		isinstance(entry, dict) and entry.get("name") == item and entry.get("file_url") == file_url
		for entry in canvas
	):
		frappe.throw(_("This attachment is not in this message."), frappe.PermissionError)


def _referenced_file(conversation, message, item, file_url, *, lock=True, meta=None):
	_check_reference(conversation, message, item, file_url)
	if not isinstance(file_url, str) or not file_url.startswith(("/private/files/", "/files/")):
		frappe.throw(_("Select an uploaded file."))
	# A draft copy can share this URL and be newer. Prefer the exact selected
	# or original conversation File, then an eligible upload; never a draft.
	if meta is None:
		meta = frappe.db.get_value(CONV, conversation, [FIELD, "filebox_source_file"], as_dict=True)
	file = None
	for name in (meta.get(FIELD), meta.filebox_source_file):
		if name:
			file = frappe.db.get_value(
				"File",
				{
					"name": name,
					"file_url": file_url,
					"attached_to_doctype": CONV,
					"attached_to_name": conversation,
				},
				list(FILE_FIELDS),
				as_dict=True,
			)
			if file:
				break
	if not file:
		file = frappe.db.get_value(
			"File",
			{"file_url": file_url, "owner": frappe.session.user, "attached_to_name": ["is", "not set"]},
			list(FILE_FIELDS),
			as_dict=True,
			order_by="creation desc",
		)
	if not file:
		file = frappe.db.get_value(
			"File",
			{"file_url": file_url, "attached_to_doctype": CONV, "attached_to_name": conversation},
			list(FILE_FIELDS),
			as_dict=True,
			order_by="creation desc",
		)
	if file and lock:
		# Serialize two chats that both saw the same unattached upload. Re-read
		# its link with a current lock read, not the earlier snapshot.
		rows = frappe.db.sql(
			"SELECT " + ", ".join(FILE_FIELDS) + " FROM `tabFile` WHERE name=%s FOR UPDATE",
			(file.name,),
			as_dict=True,
		)
		file = rows[0] if rows else None
	if not file or not frappe.has_permission("File", "read", doc=file.name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if file.attached_to_name:
		if file.attached_to_doctype != CONV or file.attached_to_name != conversation:
			frappe.throw(
				_("Upload your own copy of this file to use it as a source."), frappe.PermissionError
			)
	elif file.owner != frappe.session.user:
		frappe.throw(_("Upload your own copy of this file to use it as a source."), frappe.PermissionError)
	return file


def _busy(conversation):
	from jarvis.chat import admission, api, filebox, turn_state
	from jarvis.chat.pending_actions._store import EXECUTING, PA, WAITER

	if turn_state.unfinished_turn(conversation, states=admission._CONV_BLOCKING_STATES):
		return True
	if api._conversation_busy(conversation):
		return True
	if filebox._rerun_claimed(conversation, filebox._ladder_params(frappe.session.user)["fresh"]):
		return True
	if frappe.db.exists(WAITER, {"conversation": conversation, "resume_state": "claimed"}):
		return True
	return bool(
		frappe.db.sql(
			f"SELECT p.name FROM `tab{PA}` p LEFT JOIN `tab{WAITER}` w ON w.parent=p.name "
			"WHERE p.status=%s AND (p.conversation=%s OR w.conversation=%s) LIMIT 1",
			(EXECUTING, conversation, conversation),
		)
	)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def set_source(conversation: str, message: str, item: str, file_url: str, expected: str) -> dict:
	"""Select one persisted attachment, never dispatch or decide anything."""
	refuse_in_tool_dispatch()
	_owned(conversation)
	if not ready():
		frappe.throw(_("Update this site before selecting a source file."))
	from jarvis.chat.pending_actions._store import lock_conversation

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- finish read snapshot before canonical row lock
	lock_conversation(conversation)
	doc = _owned(conversation)
	file = _referenced_file(conversation, message, item, file_url)
	target = "" if file.name == doc.filebox_source_file else file.name
	current = doc.get(FIELD) or ""
	if target == current:
		return _state(doc)
	if expected != current:
		return {**_state(doc), "ok": False, "reason": "source_changed"}
	if _busy(conversation):
		return {**_state(doc), "ok": False, "reason": "busy"}
	frappe.db.savepoint("filebox_source_selection")
	try:
		if not file.attached_to_name:
			frappe.db.set_value(
				"File",
				file.name,
				{"attached_to_doctype": CONV, "attached_to_name": conversation},
				update_modified=False,
			)
		frappe.db.set_value(CONV, conversation, FIELD, target or None, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point="filebox_source_selection")
		raise
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- source/link durable together, retry reads current state
	return _state(_owned(conversation))


def prepare_attachments(conv, attachments):
	"""Worker inputs only: selected source survives a plain typed answer."""
	if not conv.get("file_box"):
		return attachments
	source = selected_file(conv.name, ("name", "file_url", "file_name"))
	if source is None:
		return attachments
	original = frappe.db.get_value(CONV, conv.name, "filebox_source_file")
	original_url = frappe.db.get_value("File", original, "file_url") if original else None
	seen = {source.file_url, original_url}
	result = [{"file_url": source.file_url, "file_name": source.file_name, "file_id": source.name}]
	for att in attachments or []:
		if isinstance(att, dict) and att.get("file_url") not in seen:
			result.append(att)
			seen.add(att.get("file_url"))
	return result


def selection_previews(conversations):
	"""Metadata for already-authorized lists; File byte reads keep their own gate.

	A selected name mapped to None is lost, not permission to show the original.
	"""
	if not conversations or not ready():
		return {}
	selected = {
		r.name: r.get(FIELD)
		for r in frappe.get_all(
			CONV, filters={"name": ["in", list(set(conversations))]}, fields=["name", FIELD]
		)
		if r.get(FIELD)
	}
	if not selected:
		return {}
	files = {
		r.name: r
		for r in frappe.get_all(
			"File", filters={"name": ["in", list(set(selected.values()))]}, fields=list(FILE_FIELDS)
		)
	}
	result = {}
	for conv, name in selected.items():
		file = files.get(name)
		result[conv] = (
			file if file and file.attached_to_doctype == CONV and file.attached_to_name == conv else None
		)
	return result


def display_file(conversation):
	"""Authorized review metadata; loss or viewer byte rights cannot break a card."""
	selected = selection_previews([conversation])
	if conversation in selected:
		return selected[conversation]
	from jarvis.chat.filebox import _source_file

	return _source_file(conversation)
