"""Version tokens for the per-user memory blob (#715 F09).

``recall`` hands out a version with the memory and ``remember`` takes it back as
``base_version``: a save whose base no longer matches the stored blob is refused
with the current blob instead of overwriting it, so two chats that read the same
memory and each learned a fact can never silently drop one.

The version is a short hash of the blob's text, not the row's ``modified``. Equal
text means the save loses nothing, so a blob that changed and changed back is not
a conflict, and an absent row is simply the empty blob.

A plugin session also gets an implicit base: the last version the bench showed it
(its session-start recall, a later recall, a conflict, or its own committed save),
held in the cache for a week under ``SEEN_PREFIX``, which ``hooks.persistent_cache_keys``
keeps through the watchdog's routine ``clear_cache``. A caller that sends no
``base_version`` (a plugin or persona from before F09, or a model that left it out)
is checked against what its session last saw. When the cache has nothing for the
session (TTL, a Redis restart), the base is ``UNSEEN``: any stored blob is then a
conflict, so the agent pays one round trip instead of overwriting blind. Only a
caller with no session key at all (the REST / API-token path) keeps the old
last-write-wins.
"""

import hashlib

import frappe

from jarvis.tools import _agent_run_ctx

DOCTYPE = "Jarvis User Memory"
SEEN_PREFIX = "jarvis:memory_seen:"
_SEEN_TTL_S = 7 * 24 * 3600


def version_of(content: str | None) -> str:
	return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


EMPTY_VERSION = version_of("")
# Never equal to a version: the base of a session the cache holds nothing for.
UNSEEN = "unseen"


def note_seen(version: str, *, after_commit: bool = False) -> None:
	"""Record that this plugin session has now been shown ``version``. A saved
	version waits for the commit, so a save that rolls back leaves no base behind."""
	key = _seen_key()
	if not key:
		return
	if after_commit:
		frappe.db.after_commit.add(lambda: _store(key, version))
	else:
		_store(key, version)


def session_base() -> str | None:
	"""The base for a call without ``base_version``: the version this plugin session
	was last shown, ``UNSEEN`` when the cache has none, None with no session key."""
	key = _seen_key()
	if not key:
		return None
	return frappe.cache.get_value(key, expires=True) or UNSEEN


def _store(key: str, version: str) -> None:
	frappe.cache.set_value(key, version, expires_in_sec=_SEEN_TTL_S)


def _seen_key() -> str | None:
	# Resolved now, not at commit: the dispatcher drops the session key when the call
	# ends. No ``user=`` cache prefix, so the key keeps SEEN_PREFIX; the user goes into
	# the hash with the session key, which is a bearer and never stored in clear.
	session_key = _agent_run_ctx.get_session_key()
	if not session_key:
		return None
	scope = f"{frappe.session.user}\n{session_key}".encode()
	return SEEN_PREFIX + hashlib.sha256(scope).hexdigest()[:32]
