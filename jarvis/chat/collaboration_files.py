"""Common attachment access, retaining the authority of the disclosing actor."""

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_database import fresh_read
from jarvis.chat.collaboration_permissions import actor_permissions
from jarvis.chat.collaboration_read import authorize_read, rewrite_common_files
from jarvis.chat.share_projection import safe_value
from jarvis.chat.shared_files import _LOCAL_FILE, _canvas, _refuse, download, static_html


def _source_actor(row, owner):
	if not row.collaboration_binding:
		return owner if not row.actor and not row.collaboration_run_id else None
	# Disclosure remains after revoke. Use durable attribution, not retired auth.
	binding = frappe.db.get_value(
		c.BINDING, row.collaboration_binding, ["actor", "conversation"], as_dict=True
	)
	turn = frappe.db.get_value(
		"Jarvis Chat Turn",
		row.collaboration_run_id,
		["actor", "conversation", "collaboration_binding"],
		as_dict=True,
	)
	if (
		binding
		and turn
		and (binding.actor, binding.conversation) == (row.actor, row.conversation)
		and (turn.actor, turn.conversation, turn.collaboration_binding)
		== (row.actor, row.conversation, row.collaboration_binding)
	):
		return row.actor
	return None


@fresh_read
def read_file(conversation: str, url: str):
	match = _LOCAL_FILE.fullmatch(url) if isinstance(url, str) else None
	if not match or match[2] in {".", ".."}:
		_refuse()
	# One fresh read-only snapshot covers membership, disclosure and source
	# permissions. No transcript or User locks survive a file download.
	owner = frappe.db.get_value(c.CONVERSATION, conversation, "owner")
	if not owner:
		_refuse()
	rows = frappe.get_all(
		"Jarvis Chat Message",
		{"conversation": conversation, "hidden": 0},
		["conversation", "actor", "collaboration_binding", "collaboration_run_id", "canvas"],
	)
	sources = {
		actor
		for row in rows
		if any(isinstance(item, dict) and item.get("file_url") == url for item in _canvas(row.canvas))
		if (actor := _source_actor(row, owner))
	}
	authorize_read(conversation, extra_users=sources, lock=False)
	if not sources:
		_refuse()
	for actor in sorted(sources):
		try:
			with actor_permissions(actor, impersonate_user=False, lock=False):
				for name in frappe.get_all(
					"File",
					filters={"file_url": url, "is_folder": 0, "is_private": int(bool(match[1]))},
					pluck="name",
					order_by="name",
				):
					row = frappe.get_doc("File", name)
					if (
						row.file_url == url
						and not row.is_folder
						and bool(row.is_private) == bool(match[1])
						and frappe.has_permission("File", "read", doc=row, user=actor)
					):
						return row
		except frappe.PermissionError:
			continue
	_refuse()


def get_file(conversation: str, url: str):
	download(read_file(conversation, url))


@fresh_read
def get_canvas(message: str, name: str, *, dark=False):
	row = frappe.db.get_value(
		"Jarvis Chat Message", message, ["conversation", "hidden", "canvas"], as_dict=True
	)
	if not row or row.hidden:
		_refuse()
	for item in _canvas(row.canvas):
		if not isinstance(item, dict) or item.get("name") != name:
			continue
		file = read_file(row.conversation, item.get("file_url"))
		current = frappe.db.get_value(
			"Jarvis Chat Message",
			message,
			["conversation", "hidden", "canvas"],
			as_dict=True,
		)
		if (
			not current
			or current.hidden
			or current.conversation != row.conversation
			or not any(
				isinstance(candidate, dict)
				and candidate.get("name") == name
				and candidate.get("file_url") == item.get("file_url")
				for candidate in _canvas(current.canvas)
			)
		):
			_refuse()
		if not (file.file_name or "").lower().endswith((".html", ".htm")):
			frappe.throw("This attachment is available as a download.")
		return {
			"type": "html",
			"title": rewrite_common_files(safe_value(item.get("title", name), row.conversation)),
			"content": static_html(file.get_content(), row.conversation, dark=dark),
		}
	_refuse()


def upload_file(conversation: str):
	from frappe.utils.file_manager import get_max_file_size
	from werkzeug.utils import secure_filename

	from jarvis.chat.collaboration_access import authorize

	authorize(conversation)
	files = getattr(getattr(frappe.local, "request", None), "files", None)
	if not files or len(files.getlist("file")) != 1 or set(files) != {"file"}:
		frappe.throw("Choose one file to upload.")
	file = files["file"]
	name = secure_filename(file.filename or "")
	if not name:
		frappe.throw("Choose a file with a name.")
	content = file.stream.read(int(get_max_file_size()) + 1)
	if not content or len(content) > get_max_file_size():
		frappe.throw("The file is empty or exceeds the upload size limit.")
	# No client owner, destination, privacy or remote URL. Normal File lifecycle
	# checks permission, extension, path and rollback cleanup. Send discloses it.
	with actor_permissions(frappe.session.user):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": name, "is_private": 1, "content": content}
		).insert()
	return {"name": doc.name, "file_name": doc.file_name, "file_url": doc.file_url}
