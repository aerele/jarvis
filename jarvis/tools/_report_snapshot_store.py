"""Redis storage of report snapshots: keys, meta records, writes, index and deletes.

A snapshot is two keys: a small meta record (24 h) and the gzip rows (60 min, fixed).
Redis rules for every key here (plan B.9.0): keys are built with `cache.make_key` and
touched only through plain redis-py methods or a pipeline, which raise on a Redis
error. That includes the per-owner index (one WATCH + MULTI on its `make_key`'d key): the
wrapper's `lpush`, `lrange` and `ltrim` add the site prefix themselves and are not used
here. `RedisWrapper.exists`, `get_value`, `set_value`, `delete_value` and `expire_key`
add a prefix or swallow connection errors or are absent on v15, so none of them is used
on a snapshot key.
"""

import hashlib
import json
import re
import secrets
import time

import frappe
import redis
from frappe.utils.response import json_handler

from jarvis.tools import _report_snapshot_payload as payload
from jarvis.tools._report_snapshot_digest import perm_digest
from jarvis.tools._report_snapshot_events import log

META_PREFIX = "jarvis:report_snapshot_meta:"
CONTENT_PREFIX = "jarvis:report_snapshot:"
INDEX_PREFIX = "jarvis:report_snapshot_index:"

ID_PATTERN = re.compile(r"^rs_[A-Za-z0-9_-]{32}$")
CONTENT_TTL_S = 3600
META_TTL_S = 86400
INDEX_LIMIT = 5
INDEX_ATTEMPTS = 5
WATCHED_CHANGED = "Watched variable changed."


class Created(tuple):
	"""`(id, reason)` of `create`: `(id, "ok")` or `(None, reason)`. `size` is the stored
	gzip size in bytes (None unless stored); `error` the exception class behind reason
	`error` (never a message)."""

	def __new__(cls, snapshot_id, reason, size=None, error=None):
		self = super().__new__(cls, (snapshot_id, reason))
		self.size, self.error = size, error
		return self


def enabled() -> bool:
	return bool(frappe.utils.cint(frappe.conf.get("jarvis_report_snapshots", 1)))


def reload_enabled() -> bool:
	"""The reload switch; forced off when snapshots are off."""
	return enabled() and bool(frappe.utils.cint(frappe.conf.get("jarvis_report_snapshot_reload", 1)))


def valid_id(snapshot_id) -> bool:
	return isinstance(snapshot_id, str) and bool(ID_PATTERN.fullmatch(snapshot_id))


def keys(snapshot_id):
	"""`(content_key, meta_key)`, built for the plain redis-py methods."""
	cache = frappe.cache()
	return cache.make_key(CONTENT_PREFIX + snapshot_id), cache.make_key(META_PREFIX + snapshot_id)


# --- meta --------------------------------------------------------------------------


def parse_meta(raw):
	"""The stored record, or None when it is absent or not the shape `new_meta` writes."""
	if raw is None:
		return None
	try:
		meta = json.loads(raw)
	except (ValueError, TypeError):
		return None
	if (
		not isinstance(meta, dict)
		or not all(isinstance(meta.get(k), str) and meta[k] for k in ("owner", "report_name", "sha256"))
		or not isinstance(meta.get("perm_digest"), str)
		or not isinstance(meta.get("filters"), dict)
		or type(meta.get("generation")) is not int
	):
		return None
	return meta


def new_meta(owner, report_name, filters, json_bytes, run_ms, generation, created=None):
	"""The one meta record of a snapshot, at create (generation 0) and at every reload.
	`created` is `(created_at, created_ts)` of the first run, given for a reload. Each time
	is kept twice: as site-time text for people and as an epoch float for age math, which
	must not move with a DST change."""
	now, ts = frappe.utils.now(), time.time()
	created_at, created_ts = created or (now, ts)
	return {
		"owner": owner,
		"report_name": report_name,
		"filters": filters,
		"perm_digest": perm_digest(owner, report_name),
		"sha256": hashlib.sha256(json_bytes).hexdigest(),
		"created_at": created_at,
		"created_ts": created_ts,
		"reloaded_at": now if created else None,
		"reloaded_ts": ts if created else None,
		"generation": generation,
		"run_ms": run_ms,
		"reload_started_at": None,
		"content_deleted_at": None,
	}


def _dump(meta):
	return json.dumps(meta, default=json_handler)


def load_meta(snapshot_id):
	"""The meta record or None. Redis errors propagate."""
	# nosemgrep: frappe-cache-breaks-multitenancy -- key is make_key site-scoped; plain get raises on Redis errors (B.9.0)
	return parse_meta(frappe.cache().get(keys(snapshot_id)[1]))


def load_content(snapshot_id):
	"""The gzip rows or None. Redis errors propagate."""
	# nosemgrep: frappe-cache-breaks-multitenancy -- key is make_key site-scoped; plain get raises on Redis errors (B.9.0)
	return frappe.cache().get(keys(snapshot_id)[0])


def read_state(snapshot_id):
	"""`(meta | None, content_pttl)` in one non-transactional pipeline (`PTTL` is -2 when
	the rows are absent). Redis errors propagate: they are never a miss."""
	content_key, meta_key = keys(snapshot_id)
	with frappe.cache().pipeline(transaction=False) as pipe:
		pipe.get(meta_key)
		pipe.pttl(content_key)
		raw, content_pttl = pipe.execute()
	return parse_meta(raw), content_pttl


# --- create and reload write -------------------------------------------------------


def create(
	data, *, report_name, filters, run_ms, snapshot_id=None, generation=0, created=None, measured=None
):
	"""Store a cut inline result. Returns `Created`: `(id, "ok")` or `(None, reason)`.
	Never raises. `measured` is the size `api` already took of the result: it lets the caps
	refuse an oversized result before anything is serialised. With `snapshot_id` it
	rewrites that snapshot as a reload (B.9.4), with `created` the `(created_at, created_ts)` of its
	first run."""
	try:
		return _create(data, report_name, filters, run_ms, snapshot_id, generation, created, measured)
	except Exception as exc:
		log("create_error", exc)
		return Created(None, "error", error=type(exc).__name__)


def _create(data, report_name, filters, run_ms, snapshot_id, generation, created, measured):
	if not enabled():
		return Created(None, "disabled")
	reason = payload.skip_reason(data, measured)
	if reason:
		return Created(None, reason)
	built = payload.build(data, filters)
	if isinstance(built, str):
		return Created(None, built)
	json_bytes, content = built
	if snapshot_id is not None:
		return _write_reload(
			snapshot_id, json_bytes, content, report_name, filters, run_ms, generation, created
		)
	owner = frappe.session.user
	snapshot_id = "rs_" + secrets.token_urlsafe(24)
	meta = new_meta(owner, report_name, filters, json_bytes, run_ms, generation)
	content_key, meta_key = keys(snapshot_id)
	try:
		with frappe.cache().pipeline() as pipe:
			pipe.set(content_key, content, ex=CONTENT_TTL_S)
			pipe.set(meta_key, _dump(meta), ex=META_TTL_S)
			replies = pipe.execute()
		if not (isinstance(replies, list) and len(replies) == 2 and all(replies)):
			raise RuntimeError("snapshot write refused")
		_index(owner, snapshot_id)
	except Exception:
		delete_keys(snapshot_id)
		raise
	return Created(snapshot_id, "ok", len(content))


def _between_check_and_exec():
	"""Tests only: a named seam that runs after the generation check, before EXEC."""


def _write_reload(snapshot_id, json_bytes, content, report_name, filters, run_ms, generation, created):
	"""Rewrite an existing snapshot under the same id (B.9.4): WATCH meta, check that the
	generation is the one before this one, then MULTI `SET content EX`, `SET meta XX EX`.
	Returns `(id, "ok")`, `(None, "stale")` (a newer writer won: nothing is deleted) or
	`(None, "gone")` (the id was trimmed meanwhile: the content is deleted)."""
	owner = frappe.session.user
	content_key, meta_key = keys(snapshot_id)
	meta = new_meta(owner, report_name, filters, json_bytes, run_ms, generation, created)
	try:
		with frappe.cache().pipeline() as pipe:
			pipe.watch(meta_key)
			current = parse_meta(pipe.get(meta_key))
			if current is None:
				delete_content_key(snapshot_id)
				return Created(None, "gone")
			if current["owner"] != owner or current["generation"] != generation - 1:
				return Created(None, "stale")
			_between_check_and_exec()
			pipe.multi()
			pipe.set(content_key, content, ex=CONTENT_TTL_S)
			pipe.set(meta_key, _dump(meta), xx=True, ex=META_TTL_S)
			replies = pipe.execute()
	except redis.exceptions.WatchError as exc:
		return Created(None, lost_fence(exc))
	if not (isinstance(replies, list) and len(replies) == 2 and replies[0]):
		raise RuntimeError("snapshot write refused")
	if not replies[1]:
		delete_content_key(snapshot_id)
		return Created(None, "gone")
	try:  # the rows are written: an index fault is logged, and the id keeps its place in it
		_index(owner, snapshot_id)
	except Exception as exc:
		log("reindex_error", exc)
	return Created(snapshot_id, "ok", len(content))


def lost_fence(exc, outcome="stale"):
	"""The outcome of a `WatchError`: a newer writer got in (`outcome`) only for redis-py's own
	"Watched variable changed." (same text in 4.5.5 and 7.x). redis-py also raises `WatchError`,
	with version-specific text, when the connection died while watching: a Redis error."""
	if str(exc) != WATCHED_CHANGED:
		raise redis.exceptions.ConnectionError(str(exc)) from exc
	return outcome


def _before_meta_exec():
	"""Tests only: a named seam in `update_meta`, after the check and before EXEC."""


def update_meta(snapshot_id, change, *, check=None, drop_content=False):
	"""Fenced read-modify-write of the meta record: WATCH, `check(current)` must hold, then
	MULTI `SET XX KEEPTTL change(current)` (and, with `drop_content`, `DEL content` in the
	same MULTI, so rows are never deleted on behalf of a stale view of the record). Returns
	`"ok"`, `"stale"` (check failed), `"gone"` (no meta) or `"watch"` (another writer got
	in). Redis errors propagate."""
	content_key, meta_key = keys(snapshot_id)
	try:
		with frappe.cache().pipeline() as pipe:
			pipe.watch(meta_key)
			current = parse_meta(pipe.get(meta_key))
			if current is None:
				return "gone"
			if check is not None and not check(current):
				return "stale"
			_before_meta_exec()
			pipe.multi()
			if drop_content:
				pipe.delete(content_key)
			pipe.set(meta_key, _dump(change(current)), xx=True, keepttl=True)
			replies = pipe.execute()
	except redis.exceptions.WatchError as exc:
		return lost_fence(exc, "watch")
	return "ok" if replies and replies[-1] else "gone"


# --- index -------------------------------------------------------------------------


def _before_index_exec():
	"""Tests only: a named seam in `_index`, after the list was read and before EXEC."""


def _index(owner, snapshot_id):
	"""Put `snapshot_id` at the front of the owner's index and keep the newest five ids, in ONE
	`WATCH index` + MULTI: the list is rebuilt, its expiry set and the keys of every id that
	falls out are deleted together, so two creates of one owner never leave more than five
	live snapshots and a fault leaves the list and the keys as they were. A lost WATCH
	retries; the final failure raises."""
	cache = frappe.cache()
	index_key = cache.make_key(INDEX_PREFIX + owner)
	for _ in range(INDEX_ATTEMPTS):
		try:
			with cache.pipeline() as pipe:
				pipe.watch(index_key)
				current = [
					i.decode() if isinstance(i, bytes) else str(i) for i in pipe.lrange(index_key, 0, -1)
				]
				ordered = [snapshot_id] + [i for i in current if i != snapshot_id]
				kept, dropped = ordered[:INDEX_LIMIT], ordered[INDEX_LIMIT:]
				_before_index_exec()
				pipe.multi()
				pipe.delete(index_key)
				pipe.rpush(index_key, *kept)
				pipe.expire(index_key, META_TTL_S)
				for stale_id in dropped:
					if ID_PATTERN.fullmatch(stale_id):
						pipe.delete(*keys(stale_id))
				pipe.execute()
			return
		except redis.exceptions.WatchError as exc:
			lost_fence(exc)  # raises for a dead connection; a lost WATCH retries
	raise RuntimeError("snapshot index busy")


# --- deletes -----------------------------------------------------------------------


def delete_keys(snapshot_id):
	try:
		frappe.cache().delete(*keys(snapshot_id))
	except Exception as exc:
		log("delete_error", exc)


def delete_content_key(snapshot_id):
	try:
		frappe.cache().delete(keys(snapshot_id)[0])
	except Exception as exc:
		log("delete_error", exc)


def delete_content(snapshot_id, meta):
	"""Never serve these rows again; remember that Jarvis, not Redis, removed them. The delete
	and the `content_deleted_at` stamp are ONE fenced step (`update_meta`, `drop_content`):
	when the record is no longer the generation the caller read, a newer writer owns it and
	its rows are left alone. A lost WATCH is retried once, then left (the rows are never
	served under a changed digest anyway: `read()` checks it)."""
	for _ in range(2):
		try:
			state = update_meta(
				snapshot_id,
				lambda current: {**current, "content_deleted_at": frappe.utils.now()},
				check=lambda current: current["generation"] == meta["generation"],
				drop_content=True,
			)
		except Exception as exc:
			log("stamp_error", exc)
			return
		if state == "gone":  # no record: the rows cannot be reached; drop them
			delete_content_key(snapshot_id)
		if state != "watch":
			return
	log("stamp_watch")


def was_evicted(meta):
	"""Content gone inside its hour, and not because Jarvis deleted it. Epoch seconds: the
	text times follow the site clock, which a DST change moves by an hour."""
	if meta.get("content_deleted_at"):
		return False
	since = meta.get("reloaded_ts") or meta.get("created_ts")
	return isinstance(since, int | float) and 0 <= time.time() - since < CONTENT_TTL_S
