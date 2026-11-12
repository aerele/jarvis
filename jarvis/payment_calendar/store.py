"""Ephemeral, site/actor-bound snapshots with atomic admission and publication."""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from secrets import token_hex

import frappe

from jarvis.payment_calendar import detail
from jarvis.payment_calendar.errors import SAFE_CODES, ScopeChanged, SnapshotExpired

RESULT_TTL = 300
QUEUED_TTL = 900
RUNNING_TTL = 360
INDEX_TTL = 1800
# A refusal loop is counted over this many seconds, from the first discard.
REFUSAL_WINDOW = 300
RESULT_SCHEMA = 2
SIDES = ("Payable", "Receivable")
# A snapshot is stored as these parts, one key each: `jarvis:pcal:{owner}:part-{name}:{sid}`.
PARTS = ("status", "payable", "receivable")
SIDE_PARTS = {"Payable": "payable", "Receivable": "receivable"}
STATUS_RESULT_KEYS = ("projection", "currency", "floor", "generated_at", "bank_code", "unavailable")
# What the status part copies from a result (the keys the status reply reads, plus the stamp).
STATUS_PART_KEYS = ("schema", *STATUS_RESULT_KEYS, "horizon", "bank")
# Per-side Redis hashes next to the parts: `proofs-{side}` (field = row id, value = proof JSON) and
# `index-{side}` (field = `day:<iso>` / `bucket:<name>`, value = positions, party summary, total).
HASHES = tuple(f"{kind}-{part}" for kind in ("proofs", "index") for part in SIDE_PARTS.values())
# The identity a stored proof must share with the row it belongs to.
PROOF_IDENTITY = ("voucher_type", "voucher_no", "party")
# Keys of a side source that stay out of its part: the status part or no stored part holds them.
PRIVATE_SOURCE_KEYS = ("excluded_rows", "permission_columns")
# Active (queued or running) jobs allowed per actor, counted separately for each job kind.
ADMISSION_LIMITS = {"snapshot": 2, "balances": 1}

# Every key a result is stored under. Lua scripts that delete a result call this function.
_STORED = ("result", *(f"part-{part}" for part in PARTS), *HASHES)
_LUA_DELETE_STORED = (
	"local function delete_stored(prefix, sid)\n"
	"    for _, kind in ipairs({" + ", ".join(f"'{kind}'" for kind in _STORED) + "}) do\n"
	"        redis.call('UNLINK', prefix .. kind .. ':' .. sid)\n"
	"    end\n"
	"end\n"
)

_ADMIT = (
	_LUA_DELETE_STORED
	+ """
local count = 0
for _, entry in ipairs(redis.call('SMEMBERS', KEYS[3])) do
    local old_sid, old_digest = string.match(entry, '^(%x+):(%x+)$')
    if old_sid then
        local encoded = redis.call('GET', ARGV[6] .. 'status:' .. old_sid)
        if not encoded then
            redis.call('SREM', KEYS[3], entry)
            delete_stored(ARGV[6], old_sid)
            local active = ARGV[7] .. old_digest
            if redis.call('GET', active) == old_sid then redis.call('DEL', active) end
        else
            local row = cjson.decode(encoded)
            if (row.state == 'queued' or row.state == 'running') and (row.kind or 'snapshot') == ARGV[8] then
                count = count + 1
            end
        end
    end
end
local old = redis.call('GET', KEYS[1])
if old then return {0, old} end
if count >= tonumber(ARGV[9]) then return {-1, ''} end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
local row = cjson.decode(ARGV[2])
redis.call('SADD', KEYS[3], ARGV[1] .. ':' .. row.scope_hash)
redis.call('EXPIRE', KEYS[3], ARGV[5])
redis.call('SADD', KEYS[4], ARGV[4])
redis.call('EXPIRE', KEYS[4], ARGV[5])
return {1, ARGV[1]}
"""
)

_RUN = """
local encoded = redis.call('GET', KEYS[1])
if not encoded or redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local row = cjson.decode(encoded)
if row.state ~= 'queued' then return 0 end
row.state = 'running'; row.started_at = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(row), 'EX', ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[1] .. ':' .. row.scope_hash); redis.call('EXPIRE', KEYS[3], ARGV[5])
redis.call('SADD', KEYS[4], ARGV[4]); redis.call('EXPIRE', KEYS[4], ARGV[5])
return 1
"""

_PUBLISH = """
local encoded = redis.call('GET', KEYS[1])
if not encoded or redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local row = cjson.decode(encoded)
if row.state ~= 'running' then return 0 end
row.state = 'complete'; row.completed_at = ARGV[3]; row.expires_at = ARGV[4]
redis.call('SET', KEYS[3], ARGV[2], 'EX', ARGV[5])
redis.call('SET', KEYS[1], cjson.encode(row), 'EX', ARGV[5])
redis.call('DEL', KEYS[2])
redis.call('SADD', KEYS[4], ARGV[1] .. ':' .. row.scope_hash); redis.call('EXPIRE', KEYS[4], ARGV[7])
redis.call('SADD', KEYS[5], ARGV[6]); redis.call('EXPIRE', KEYS[5], ARGV[7])
return 1
"""

# Parts arrive verbatim in ARGV and are never decoded here: cjson would turn an empty array into an object.
# KEYS: status, active lock, index, actors, one key per part, then one key per hash.
# ARGV: sid, completed, expires, ttl, owner, index ttl, part count, one payload per part, then per hash its
# pair count followed by field, value pairs. Each hash is deleted first, so no stale field survives.
_PUBLISH_PARTS = """
local encoded = redis.call('GET', KEYS[1])
if not encoded or redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local row = cjson.decode(encoded)
if row.state ~= 'running' then return 0 end
row.state = 'complete'; row.completed_at = ARGV[2]; row.expires_at = ARGV[3]
local parts = tonumber(ARGV[7])
local at = 8
for i = 1, parts do
    redis.call('SET', KEYS[4 + i], ARGV[at], 'EX', ARGV[4])
    at = at + 1
end
for i = 5 + parts, #KEYS do
    redis.call('DEL', KEYS[i])
    local pairs_count = tonumber(ARGV[at])
    at = at + 1
    for n = 1, pairs_count do
        redis.call('HSET', KEYS[i], ARGV[at], ARGV[at + 1])
        -- Right after the first field exists: a hash never lives without an expiry, whatever happens next.
        if n == 1 then redis.call('EXPIRE', KEYS[i], ARGV[4]) end
        at = at + 2
    end
end
redis.call('SET', KEYS[1], cjson.encode(row), 'EX', ARGV[4])
redis.call('DEL', KEYS[2])
redis.call('SADD', KEYS[3], ARGV[1] .. ':' .. row.scope_hash); redis.call('EXPIRE', KEYS[3], ARGV[6])
redis.call('SADD', KEYS[4], ARGV[5]); redis.call('EXPIRE', KEYS[4], ARGV[6])
return 1
"""

# One hash read: {0} when the hash is gone, else {1, values}; a missing field is nil in `values`.
# EXISTS and the read run in one script, so "hash gone" and "field missing" cannot be confused.
_HMGET = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {0} end
return {1, redis.call('HMGET', KEYS[1], unpack(ARGV))}
"""
_HGET = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {0} end
return {1, redis.call('HGET', KEYS[1], ARGV[1])}
"""

# KEYS: status, active lock, index, tombstone, then every stored result key.
# A balances job leaves no tombstone.
_DISCARD = """
local encoded = redis.call('GET', KEYS[1])
if encoded then
    local ok, row = pcall(cjson.decode, encoded)
    if ok and (row.kind or 'snapshot') == 'snapshot' then
        redis.call('SET', KEYS[4], '1', 'EX', ARGV[3])
    end
end
redis.call('DEL', KEYS[1])
for i = 5, #KEYS do redis.call('UNLINK', KEYS[i]) end
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
redis.call('SREM', KEYS[3], ARGV[1] .. ':' .. ARGV[2])
return 1
"""

# One refusal per discarded snapshot: the window starts when the set is created. Returns {added, size}.
_REFUSAL = """
local existed = redis.call('EXISTS', KEYS[1])
local added = redis.call('SADD', KEYS[1], ARGV[1])
if existed == 0 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return {added, redis.call('SCARD', KEYS[1])}
"""

_FAIL = """
local encoded = redis.call('GET', KEYS[1])
if not encoded then return 0 end
local row = cjson.decode(encoded)
if row.state ~= 'queued' and row.state ~= 'running' then return 0 end
row.state = 'failed'; row.code = ARGV[2]; row.completed_at = ARGV[3]
redis.call('SET', KEYS[1], cjson.encode(row), 'EX', ARGV[4])
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
redis.call('SADD', KEYS[3], ARGV[1] .. ':' .. row.scope_hash); redis.call('EXPIRE', KEYS[3], ARGV[6])
redis.call('SADD', KEYS[4], ARGV[5]); redis.call('EXPIRE', KEYS[4], ARGV[6])
return 1
"""

_PURGE = (
	_LUA_DELETE_STORED
	+ """
for _, entry in ipairs(redis.call('SMEMBERS', KEYS[1])) do
    local sid, digest = string.match(entry, '^(%x+):(%x+)$')
    if not sid then
        sid = entry
        local encoded = redis.call('GET', ARGV[1] .. 'status:' .. sid)
        if encoded then
            local ok, row = pcall(cjson.decode, encoded)
            if ok then digest = row.scope_hash end
        end
    end
    if digest then
        local active = ARGV[2] .. digest
        if redis.call('GET', active) == sid then redis.call('DEL', active) end
    end
    redis.call('DEL', ARGV[1] .. 'status:' .. sid)
    delete_stored(ARGV[1], sid)
end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[3])
return 1
"""
)


def _owner(actor):
	if not isinstance(actor, str) or not actor:
		raise ValueError("An actor is required.")
	return sha256(actor.encode()).hexdigest()[:32]


def _key(actor, sid, kind):
	return frappe.cache.make_key(f"jarvis:pcal:{_owner(actor)}:{kind}:{sid}")


def _part_key(actor, sid, part):
	return _key(actor, sid, f"part-{part}")


def _refusals(owner):
	return frappe.cache.make_key(f"jarvis:pcal:{owner}:refusals")


def _active(digest):
	return frappe.cache.make_key(f"jarvis:pcal:active:{digest}")


def _index(owner):
	return frappe.cache.make_key(f"jarvis:pcal:index:{owner}")


def _actors():
	return frappe.cache.make_key("jarvis:pcal:actors")


def _json(value):
	return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False, default=str)


def _now():
	return datetime.now(timezone.utc)


def _eval(script, keys, args):
	return frappe.cache.eval(script, len(keys), *keys, *args)


class ActorBusy(Exception):
	pass


def admit(actor: str, digest: str, scope: dict, fingerprint: str, kind: str = "snapshot") -> tuple[str, bool]:
	if (
		kind not in ADMISSION_LIMITS
		or not isinstance(scope, dict)
		or scope.get("actor") != actor
		or not isinstance(digest, str)
		or not re.fullmatch(r"[a-f0-9]{64}", digest)
	):
		raise ValueError("Invalid snapshot scope.")
	sid, owner = token_hex(16), _owner(actor)
	status = {
		"snapshot_id": sid,
		"site": str(frappe.local.site),
		"kind": kind,
		# Keep nested arrays opaque to Redis Lua cjson, which turns [] into {}
		# when re-encoding status during state transitions.
		"scope_json": _json(scope),
		"scope_hash": digest,
		"fingerprint": fingerprint,
		"state": "queued",
		"queued_at": _now().isoformat(),
		"started_at": None,
		"completed_at": None,
		"expires_at": None,
		"code": None,
	}
	created, active = _eval(
		_ADMIT,
		[_active(digest), _key(actor, sid, "status"), _index(owner), _actors()],
		[
			sid,
			_json(status),
			QUEUED_TTL,
			owner,
			INDEX_TTL,
			frappe.cache.make_key(f"jarvis:pcal:{owner}:").decode(),
			frappe.cache.make_key("jarvis:pcal:active:").decode(),
			kind,
			ADMISSION_LIMITS[kind],
		],
	)
	if created == -1:
		raise ActorBusy("Payment Calendar work is already active.")
	return active.decode(), bool(created)


def _read(key):
	# _key already adds the database prefix. get_value would prefix it again
	# and unpickle the JSON written by the Lua state transitions.
	value = frappe.cache.execute_command("GET", key)
	if value is None:
		return None
	try:
		result = json.loads(value)
		return result if isinstance(result, dict) else None
	except (ValueError, TypeError):
		return None


def _valid_sid(sid):
	return isinstance(sid, str) and bool(re.fullmatch(r"[a-f0-9]{32}", sid))


def load_status(actor: str, sid: str) -> dict | None:
	if not _valid_sid(sid):
		return None
	row = _read(_key(actor, sid, "status"))
	if row and isinstance(row.get("scope_json"), str):
		try:
			row["scope"] = json.loads(row["scope_json"])
		except (TypeError, ValueError):
			return None
	if (
		not row
		or row.get("site") != str(frappe.local.site)
		or row.get("snapshot_id") != sid
		or not isinstance(row.get("scope"), dict)
		or row["scope"].get("actor") != actor
		or row.get("state") not in ("queued", "running", "complete", "failed")
	):
		return None
	return row


PUBLIC_STATUS_KEYS = ("state", "code", "queued_at", "started_at", "completed_at", "expires_at")


def public_status(status: dict, id_key: str = "snapshot_id") -> dict:
	"""The client view of a status row: its id under `id_key` plus the lifecycle keys."""
	return {id_key: status.get("snapshot_id"), **{key: status.get(key) for key in PUBLIC_STATUS_KEYS}}


def split_result(result: dict, scope: dict) -> dict:
	"""The stored pieces of a schema-2 result.

	`status` plus one part per available side (rows in default order, no proofs), and per side a
	`proofs-{side}` hash (row id -> proof, absent without rows) and an `index-{side}` hash
	(`detail.index_side`, built for `scope`'s anchor and window).

	Strict: a result that lacks a key the status reply reads is a bug, refused here at publish
	(a KeyError that names the keys only) rather than failing on every read.
	"""
	_require_complete(result)
	unavailable = result["unavailable"]
	sides = [side for side in SIDES if side not in unavailable]
	sources = {side: _side_source(result, side) for side in sides}
	status = {key: result[key] for key in STATUS_PART_KEYS}
	status["_permission_columns"] = {side: sources[side]["permission_columns"] for side in sides}
	status["_top_proofs"] = {side: _top_proofs(result, side, sources[side]) for side in sides}
	pieces = {"status": status}
	for side in sides:
		pieces.update(_side_pieces(result, side, sources[side], scope))
	return pieces


def _side_pieces(result, side, source, scope):
	name = SIDE_PARTS[side]
	rows = detail.sort_rows(source["rows"])
	proofs = {row["id"]: row["_permission"] for row in rows}
	if len(proofs) != len(rows):
		raise RuntimeError("A side has two rows with one id.")
	part = {k: v for k, v in source.items() if k not in PRIVATE_SOURCE_KEYS}
	part["rows"] = [{k: v for k, v in row.items() if k != "_permission"} for row in rows]
	pieces = {name: part, f"index-{name}": detail.index_side(part["rows"], result, side, scope)}
	if proofs:
		pieces[f"proofs-{name}"] = proofs
	return pieces


def assemble_result(parts: dict) -> dict:
	"""The status part plus `sources`; a side without a part is None. Its private keys stay in it.

	Rows get their proofs back and every source carries its whole index (tests and diagnostics only).
	"""
	sources = {}
	for side, name in SIDE_PARTS.items():
		part = parts.get(name)
		if part is None:
			sources[side] = None
			continue
		rows = with_proofs(part["rows"], parts.get(f"proofs-{name}", {}))
		sources[side] = {**part, "rows": rows, "index": parts[f"index-{name}"]}
	return {**parts["status"], "sources": sources}


def with_proofs(rows: list, proofs: dict) -> list:
	"""Copies of `rows` with their stored proof as `_permission`.

	A proof the store does not have, or one that names another voucher or party, is a defect of the
	stored data (not a changed scope): RuntimeError. A proof that is not a dict passes on; the row check
	refuses it as damaged.
	"""
	merged = []
	for row in rows:
		if row["id"] not in proofs:
			raise RuntimeError("A stored proof is missing.")
		proof = proofs[row["id"]]
		if isinstance(proof, dict) and any(proof.get(key) != row[key] for key in PROOF_IDENTITY):
			raise RuntimeError("A stored proof belongs to another row.")
		merged.append({**row, "_permission": proof})
	return merged


def _require_complete(result):
	missing = [key for key in STATUS_PART_KEYS if key not in result]
	if result.get("schema") != RESULT_SCHEMA and "schema" not in missing:
		missing.append("schema")
	if "projection" in result and "month_totals" not in result["projection"]:
		missing.append("projection.month_totals")
	if missing:
		raise KeyError(*missing)


def _side_source(result, side):
	source = result["sources"].get(side)
	if source is None:
		raise RuntimeError("An available side has no source.")
	return source


def _top_proofs(result, side, source):
	"""Identity and proof of every row of each day's top invoice, so a read can check them without the part.

	A Journal Entry can have several parties: all of them are kept.
	"""
	prefix = side.lower()
	vouchers = set()
	for day in result["projection"]["days"].values():
		top = day.get(f"{prefix}_top_invoice")
		if top:
			vouchers.add((top["voucher_type"], top["voucher_no"]))
	rows = [
		{
			**{key: row[key] for key in ("voucher_type", "voucher_no", "party_type", "party")},
			"_permission": row["_permission"],
		}
		for row in source["rows"]
		if (row["voucher_type"], row["voucher_no"]) in vouchers and Decimal(row["remaining"]) > 0
	]
	if {(row["voucher_type"], row["voucher_no"]) for row in rows} != vouchers:
		raise RuntimeError("A top invoice has no stored row.")
	return rows


def load_result(actor: str, sid: str) -> dict | None:
	"""A balances result as stored; a snapshot assembled from its pieces (tests and diagnostics only)."""
	status = load_status(actor, sid)
	if not status or status["state"] != "complete":
		return None
	if status.get("kind", "snapshot") != "snapshot":
		return _read(_key(actor, sid, "result"))
	parts = {"status": _read(_part_key(actor, sid, "status"))}
	if parts["status"] is None:
		return None
	for side, part in SIDE_PARTS.items():
		if side in parts["status"]["unavailable"]:
			continue
		parts[part] = _read(_part_key(actor, sid, part))
		if parts[part] is None:
			return None
		for kind in ("proofs", "index"):
			parts[f"{kind}-{part}"] = _hash_all(_key(actor, sid, f"{kind}-{part}"))
	return assemble_result(parts)


def _hash_all(key):
	stored = frappe.cache.execute_command("HGETALL", key)
	return {field.decode(): json.loads(value) for field, value in stored.items()}


def load_part(actor: str, sid: str, part: str) -> dict:
	"""One stored part of a complete snapshot. A part that is gone means a discard or an eviction."""
	if part not in PARTS:
		raise ValueError("Unknown snapshot part.")
	status = load_status(actor, sid)
	complete = status and status["state"] == "complete" and status.get("kind", "snapshot") == "snapshot"
	value = _read(_part_key(actor, sid, part)) if complete else None
	if value is None:
		raise gone(actor, sid)
	return value


def load_proofs(actor: str, sid: str, side: str, ids: list) -> dict:
	"""The stored proofs of exactly these row ids: one script, one HMGET. Never reads more than `ids`.

	A hash that is gone is a discard or an eviction (`gone`). A field that is missing from a present hash is a
	defect: RuntimeError, the snapshot stays.
	"""
	present, *rest = _eval(_HMGET, [_key(actor, sid, f"proofs-{SIDE_PARTS[side]}")], ids)
	if not present:
		raise gone(actor, sid)
	values = rest[0]
	if any(value is None for value in values):
		raise RuntimeError("A stored proof is missing.")
	return {row_id: json.loads(value) for row_id, value in zip(ids, values, strict=True)}


def load_index_entry(actor: str, sid: str, side: str, field: str) -> dict | None:
	"""One index entry of a side (one HGET); None when the hash has no such field. A gone hash is `gone`."""
	present, *rest = _eval(_HGET, [_key(actor, sid, f"index-{SIDE_PARTS[side]}")], [field])
	if not present:
		raise gone(actor, sid)
	return None if rest[0] is None else json.loads(rest[0])


def was_discarded(actor: str, sid: str) -> bool:
	"""True for the owner of a snapshot that a read refused and discarded in the last five minutes."""
	return _valid_sid(sid) and bool(frappe.cache.execute_command("EXISTS", _key(actor, sid, "discarded")))


def gone(actor, sid):
	"""The error for a missing snapshot or part: a discard is a changed scope, anything else is expired."""
	if was_discarded(actor, sid):
		return ScopeChanged()
	return SnapshotExpired()


def mark_running(actor: str, sid: str) -> bool:
	status = load_status(actor, sid)
	if not status:
		return False
	owner = _owner(actor)
	return bool(
		_eval(
			_RUN,
			[_key(actor, sid, "status"), _active(status["scope_hash"]), _index(owner), _actors()],
			[sid, _now().isoformat(), RUNNING_TTL, owner, INDEX_TTL],
		)
	)


def publish(actor: str, sid: str, payload: dict) -> bool:
	"""Publish a balances result as one key; a snapshot goes through `publish_snapshot`."""
	status = load_status(actor, sid)
	if status and status.get("kind", "snapshot") == "snapshot":
		raise ValueError("A snapshot is published as parts.")
	if not status or status["state"] != "running":
		return False
	owner, completed = _owner(actor), _now()
	return bool(
		_eval(
			_PUBLISH,
			[
				_key(actor, sid, "status"),
				_active(status["scope_hash"]),
				_key(actor, sid, "result"),
				_index(owner),
				_actors(),
			],
			[
				sid,
				_json(payload),
				completed.isoformat(),
				(completed + timedelta(seconds=RESULT_TTL)).isoformat(),
				RESULT_TTL,
				owner,
				INDEX_TTL,
			],
		)
	)


def publish_snapshot(actor: str, sid: str, parts: dict) -> bool:
	"""Store every part and hash and complete the status in one script; False when the run is not current."""
	known = {*PARTS, *HASHES}
	if "status" not in parts or not set(parts) <= known:
		raise ValueError("A snapshot needs a status part and only known parts.")
	status = load_status(actor, sid)
	if status and status.get("kind", "snapshot") != "snapshot":
		raise ValueError("Only a snapshot is published as parts.")
	if not status or status["state"] != "running":
		return False
	owner, completed = _owner(actor), _now()
	names, hashes = [n for n in PARTS if n in parts], [n for n in HASHES if n in parts]
	return bool(
		_eval(
			_PUBLISH_PARTS,
			[
				_key(actor, sid, "status"),
				_active(status["scope_hash"]),
				_index(owner),
				_actors(),
				*(_part_key(actor, sid, name) for name in names),
				*(_key(actor, sid, name) for name in hashes),
			],
			[
				sid,
				completed.isoformat(),
				(completed + timedelta(seconds=RESULT_TTL)).isoformat(),
				RESULT_TTL,
				owner,
				INDEX_TTL,
				len(names),
				*(_json(parts[name]) for name in names),
				*(arg for name in hashes for arg in _hash_args(parts[name])),
			],
		)
	)


def _hash_args(fields):
	"""A hash as ARGV: its pair count, then field and JSON value of each pair."""
	return [len(fields), *(arg for field, value in fields.items() for arg in (field, _json(value)))]


def fail(actor: str, sid: str, code: str) -> bool:
	status = load_status(actor, sid)
	if not status:
		return False
	owner = _owner(actor)
	code = code if isinstance(code, str) and code in SAFE_CODES else "internal_error"
	return bool(
		_eval(
			_FAIL,
			[_key(actor, sid, "status"), _active(status["scope_hash"]), _index(owner), _actors()],
			[sid, code, _now().isoformat(), RESULT_TTL, owner, INDEX_TTL],
		)
	)


def release_active(digest: str, sid: str) -> bool:
	return bool(
		_eval(
			"if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
			[_active(digest)],
			[sid],
		)
	)


def discard(actor: str, sid: str) -> None:
	"""Delete a result. A snapshot leaves a tombstone: a parallel read sees a changed scope, not an expiry."""
	status = load_status(actor, sid)
	if not status:
		return
	_eval(
		_DISCARD,
		[
			_key(actor, sid, "status"),
			_active(status["scope_hash"]),
			_index(_owner(actor)),
			_key(actor, sid, "discarded"),
			*(_key(actor, sid, name) for name in _STORED),
		],
		[sid, status["scope_hash"], RESULT_TTL],
	)


def count_refusal(actor: str, sid: str) -> tuple[int, int]:
	"""Count one discarded snapshot for the actor: (1 if this id is new in the window else 0, ids so far)."""
	added, size = _eval(_REFUSAL, [_refusals(_owner(actor))], [sid, REFUSAL_WINDOW])
	return int(added), int(size)


def _purge_owner(owner):
	_eval(
		_PURGE,
		[_index(owner), _actors()],
		[
			frappe.cache.make_key(f"jarvis:pcal:{owner}:").decode(),
			frappe.cache.make_key("jarvis:pcal:active:").decode(),
			owner,
		],
	)


def purge_actor(actor: str) -> None:
	_purge_owner(_owner(actor))


def purge_site() -> None:
	# RedisWrapper.smembers adds a namespace; these keys already have it.
	for owner in frappe.cache.execute_command("SMEMBERS", _actors()):
		owner = owner.decode()
		if re.fullmatch(r"[a-f0-9]{32}", owner):
			_purge_owner(owner)
