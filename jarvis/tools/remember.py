"""Remember durable facts about the CURRENT user.

Persists the agent's consolidated memory for whoever it is helping now, as one
terse markdown blob (one line per fact) - the bench-owned, per-user replacement
for the shared-workspace ``MEMORY.md``. Scope is implicit: the current user
(``frappe.session.user``, resolved from the chat session by ``jarvis.api.call_tool``
and set via ``impersonate(user)``). There is NO user argument, so the model cannot
address another user's memory.

Full replace with compare-and-swap (#715 F09): pass the whole updated blob and the
``version`` from the ``recall`` (or the last successful save) it was built on, as
``base_version``. When the stored blob has moved on, nothing is written: the reply
is ``saved: false, conflict: true`` with the current blob and its version, so the
agent merges and retries, asking the user when two facts contradict. Without a
``base_version`` the session's last-seen version is used (``_user_memory``), and with
neither the save is last-write-wins, as before F09.

The compare runs under a row lock, so two overlapping saves are serialised by the
database: the second sees the first's blob, or fails with a lock error, and never
gets a receipt for an overwrite. Capped to keep it terse and to bound the context
it later consumes.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _user_memory
from jarvis.tools._user_memory import DOCTYPE, EMPTY_VERSION, version_of

MAX_BYTES = 8192

CONFLICT_MESSAGE = (
	"Not saved: this person's memory changed since you read it (another chat saved first). "
	"`content` is the memory as it stands now. Merge your new facts into it, keeping every "
	"line already there unless your fact replaces it. If a new fact contradicts a stored one, "
	"ask the user which holds before saving. Then call remember again with the merged blob "
	"and base_version set to `version`."
)


def remember(content: str, base_version: str | None = None) -> dict:
	content = _checked_content(content)
	base = _checked_base(base_version) or _user_memory.last_seen()
	user = frappe.session.user
	if not frappe.db.get_value(DOCTYPE, {"user": user}, "name"):
		result = _insert_first(user, content, base)
		if result:
			return result
	return _replace_locked(user, content, base)


def _insert_first(user: str, content: str, base: str | None) -> dict | None:
	"""First save for this user. None when another chat created the row meanwhile."""
	if base and base != EMPTY_VERSION:
		return _conflict("")
	try:
		frappe.get_doc({"doctype": DOCTYPE, "user": user, "content": content}).insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		# The unique ``user`` index caught a concurrent first save: compare against
		# that row under the lock instead.
		frappe.clear_messages()
		return None
	return _saved(content)


def _replace_locked(user: str, content: str, base: str | None) -> dict:
	"""Replace the row, refusing when it no longer matches ``base``. The read takes
	the row lock, so no other save can land between the compare and the write."""
	row = frappe.db.get_value(DOCTYPE, {"user": user}, ["name", "content"], as_dict=True, for_update=True)
	if not row:
		raise InvalidArgumentError("memory changed while saving; call recall, then retry")
	current = row.content or ""
	if base and base != version_of(current):
		return _conflict(current)
	doc = frappe.get_doc(DOCTYPE, row.name, for_update=True)
	doc.content = content
	doc.save(ignore_permissions=True)
	return _saved(content)


def _saved(content: str) -> dict:
	version = version_of(content)
	_user_memory.note_seen(version)
	return {"saved": True, "bytes": len(content.encode("utf-8")), "version": version}


def _conflict(current: str) -> dict:
	version = version_of(current)
	# The reply shows the agent this blob, so it becomes the session's base.
	_user_memory.note_seen(version)
	return {
		"saved": False,
		"conflict": True,
		"content": current,
		"version": version,
		"message": CONFLICT_MESSAGE,
	}


def _checked_content(content) -> str:
	if content is None:
		content = ""
	if not isinstance(content, str):
		raise InvalidArgumentError("content must be a string")
	size = len(content.encode("utf-8"))
	if size > MAX_BYTES:
		raise InvalidArgumentError(f"memory too large ({size} bytes; max {MAX_BYTES})")
	return content


def _checked_base(base_version) -> str | None:
	if base_version is None:
		return None
	if not isinstance(base_version, str):
		raise InvalidArgumentError("base_version must be a string (the version recall returned)")
	return base_version.strip() or None
