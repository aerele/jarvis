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
``base_version`` a plugin session is checked against the version it was last shown,
and a session the cache holds nothing for gets a conflict whenever a blob is stored
(``_user_memory``). Only a caller with no session key (REST / API token) is
last-write-wins, as before F09. With no row at all there is nothing to overwrite, so
the blob is inserted whatever the base.

The compare reads the row with FOR UPDATE, so two overlapping saves are serialised
by the database: the second sees the first's blob, or its lock wait fails and it
gets a RetryLaterError saying nothing was saved, never a receipt for an overwrite.
Capped to keep it terse and to bound the context it later consumes.

Written only in a chat with the person present: a File Box run, a delegated agent run
and a scheduled macro run are refused (``UNATTENDED_MESSAGE``), since what they read
from a document or a tool result could otherwise be saved into every later chat.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError, RetryLaterError
from jarvis.tools import _agent_run_ctx, _delegate_capability, _user_memory
from jarvis.tools._user_memory import DOCTYPE, version_of

MAX_BYTES = 8192

CONFLICT_MESSAGE = (
	"Not saved: the stored memory is not the version this save was built on (another chat "
	"may have saved since you read it). `content` is the memory as it stands now. Merge your new facts into it, keeping every "
	"line already there unless your fact replaces it. If a new fact contradicts a stored one, "
	"ask the user which holds before saving. Then call remember again with the merged blob "
	"and base_version set to `version`."
)

UNATTENDED_MESSAGE = (
	"Not saved: remember is not available in a File Box, agent or scheduled run, where no one is "
	"present to check what is stored. Do not retry. If a fact is worth keeping, say so in your "
	"final reply so the person can ask Jarvis to remember it in a chat."
)

BUSY_MESSAGE = (
	"Another chat was saving this person's memory at the same moment, so nothing was saved. "
	"Call recall, merge your new facts into the content it returns, and call remember again "
	"with its version."
)


def remember(content: str, base_version: str | None = None) -> dict:
	_refuse_unattended()
	content = _checked_content(content)
	base = _checked_base(base_version) or _user_memory.session_base()
	try:
		return _save(frappe.session.user, content, base)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as e:
		# A lock wait that failed (deadlock, timeout, or MariaDB's snapshot check:
		# the row changed after this request began). The database has rolled the
		# statement back; retrying here would hit the same snapshot.
		raise RetryLaterError(BUSY_MESSAGE) from e


def _refuse_unattended() -> None:
	"""Refuse a run with no person present: File Box, a delegate's Jarvis Agent Run, or a
	scheduled Jarvis Macro Run. Only a call over a plugin session can be one."""
	session_key = _agent_run_ctx.get_session_key()
	if not session_key:
		return
	conv = frappe.db.get_value(
		"Jarvis Conversation", {"session_key": session_key}, ["name", "file_box"], as_dict=True
	)
	if (
		(conv and conv.file_box)
		or _delegate_capability.resolve(session_key)
		or (conv and _scheduled_macro_run(conv.name))
	):
		raise PermissionDeniedError(UNATTENDED_MESSAGE)


def _scheduled_macro_run(conversation: str) -> bool:
	return bool(
		frappe.db.exists(
			"Jarvis Macro Run",
			{
				"conversation": conversation,
				"trigger": "scheduled",
				"status": ["in", ["queued", "running", "waiting_capacity"]],
			},
		)
	)


def _save(user: str, content: str, base: str | None) -> dict:
	"""Compare and write under the row lock. A second pass only when another chat's
	first save took the user's row between this one's read and its insert."""
	for _attempt in range(2):
		row = frappe.db.get_value(DOCTYPE, {"user": user}, ["name", "content"], as_dict=True, for_update=True)
		if row:
			return _replace(row, content, base)
		if _insert(user, content):
			return _saved(content)
	raise RetryLaterError(BUSY_MESSAGE)


def _insert(user: str, content: str) -> bool:
	"""No row, so nothing to overwrite: insert whatever the base was. False when the
	unique ``user`` index caught a concurrent first save."""
	try:
		frappe.get_doc({"doctype": DOCTYPE, "user": user, "content": content}).insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.clear_messages()
		return False
	return True


def _replace(row: dict, content: str, base: str | None) -> dict:
	current = row.content or ""
	if base and base != version_of(current):
		return _conflict(current)
	doc = frappe.get_doc(DOCTYPE, row.name, for_update=True)
	doc.content = content
	doc.save(ignore_permissions=True)
	return _saved(content)


def _saved(content: str) -> dict:
	version = version_of(content)
	_user_memory.note_seen(version, after_commit=True)
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
