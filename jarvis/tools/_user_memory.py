"""Version tokens for the per-user memory blob (#715 F09).

``recall`` hands out a version with the memory and ``remember`` takes it back as
``base_version``: a save whose base no longer matches the stored blob is refused
with the current blob instead of overwriting it, so two chats that read the same
memory and each learned a fact can never silently drop one.

The version is a short hash of the blob's text, not the row's ``modified``. Equal
text means the save loses nothing, so a blob that changed and changed back is not
a conflict, and an absent row is simply the empty blob.

A plugin session also gets an implicit base: the last version the bench showed it
(its session-start recall, a later recall, a conflict, or its own save), held in
the cache for a week. A caller that sends no ``base_version`` (a plugin or persona
from before F09, or a model that left it out) is still checked against what its
session last saw. No session key, or a cache miss (TTL, a cache clear), falls back
to the old last-write-wins.
"""

import hashlib

import frappe

from jarvis.tools import _agent_run_ctx

DOCTYPE = "Jarvis User Memory"
_SEEN_TTL_S = 7 * 24 * 3600


def version_of(content: str | None) -> str:
	return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


EMPTY_VERSION = version_of("")


def note_seen(version: str) -> None:
	"""Record that this plugin session has now been shown ``version``."""
	key = _seen_key()
	if key:
		frappe.cache.set_value(key, version, user=frappe.session.user, expires_in_sec=_SEEN_TTL_S)


def last_seen() -> str | None:
	"""The version this plugin session was last shown, if the cache still has it."""
	key = _seen_key()
	if not key:
		return None
	return frappe.cache.get_value(key, user=frappe.session.user, expires=True)


def _seen_key() -> str | None:
	# The session key is a bearer: only its hash goes into a cache key.
	session_key = _agent_run_ctx.get_session_key()
	if not session_key:
		return None
	return "jarvis:memory_seen:" + hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
