"""Ephemeral, site/actor-bound snapshots with atomic admission and publication."""

import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_hex

import frappe

RESULT_TTL = 300
QUEUED_TTL = 900
RUNNING_TTL = 360
INDEX_TTL = 1800
SAFE_CODES = {
	"internal_error",
	"timeout",
	"source_changed",
	"scope_changed",
	"forbidden",
	"expired",
	"source_contract_unavailable",
	"source_configuration_unavailable",
	"fiscal_year_unavailable",
	"multibook_unsupported",
	"too_large",
	"invalid_source_amount",
	"reconciliation_mismatch",
	"currency_reconciliation_unavailable",
	"settlement_schedule_mismatch",
	"multiple_invoice_accounts",
	"masked_source_unsupported",
	"job_lost",
	"worker_unavailable",
	"worker_busy",
}

_ADMIT = """
local count = 0
for _, entry in ipairs(redis.call('SMEMBERS', KEYS[3])) do
    local old_sid, old_digest = string.match(entry, '^(%x+):(%x+)$')
    if old_sid then
        local encoded = redis.call('GET', ARGV[6] .. 'status:' .. old_sid)
        if not encoded then
            redis.call('SREM', KEYS[3], entry)
            redis.call('DEL', ARGV[6] .. 'result:' .. old_sid)
            local active = ARGV[7] .. old_digest
            if redis.call('GET', active) == old_sid then redis.call('DEL', active) end
        else
            local row = cjson.decode(encoded)
            if row.state == 'queued' or row.state == 'running' then count = count + 1 end
        end
    end
end
local old = redis.call('GET', KEYS[1])
if old then return {0, old} end
if count >= 2 then return {-1, ''} end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
local row = cjson.decode(ARGV[2])
redis.call('SADD', KEYS[3], ARGV[1] .. ':' .. row.scope_hash)
redis.call('EXPIRE', KEYS[3], ARGV[5])
redis.call('SADD', KEYS[4], ARGV[4])
redis.call('EXPIRE', KEYS[4], ARGV[5])
return {1, ARGV[1]}
"""

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

_PURGE = """
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
    redis.call('DEL', ARGV[1] .. 'status:' .. sid, ARGV[1] .. 'result:' .. sid)
end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[3])
return 1
"""


def _owner(actor):
	if not isinstance(actor, str) or not actor:
		raise ValueError("An actor is required.")
	return sha256(actor.encode()).hexdigest()[:32]


def _key(actor, sid, kind):
	return frappe.cache.make_key(f"jarvis:pcal:{_owner(actor)}:{kind}:{sid}")


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


def admit(actor: str, digest: str, scope: dict, fingerprint: str) -> tuple[str, bool]:
	if (
		not isinstance(scope, dict)
		or scope.get("actor") != actor
		or not isinstance(digest, str)
		or not re.fullmatch(r"[a-f0-9]{64}", digest)
	):
		raise ValueError("Invalid snapshot scope.")
	sid, owner = token_hex(16), _owner(actor)
	status = {
		"snapshot_id": sid,
		"site": str(frappe.local.site),
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
		],
	)
	if created == -1:
		raise ActorBusy("Payment Calendar work is already active.")
	return active.decode(), bool(created)


def _read(key):
	value = frappe.cache.get(key)
	if value is None:
		return None
	try:
		result = json.loads(value)
		return result if isinstance(result, dict) else None
	except (ValueError, TypeError):
		return None


def load_status(actor: str, sid: str) -> dict | None:
	if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
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


def load_result(actor: str, sid: str) -> dict | None:
	status = load_status(actor, sid)
	return _read(_key(actor, sid, "result")) if status and status["state"] == "complete" else None


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
	status = load_status(actor, sid)
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
	status = load_status(actor, sid)
	if not status:
		return
	_eval(
		"""redis.call('DEL', KEYS[1], KEYS[2])
		if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
		redis.call('SREM', KEYS[4], ARGV[1] .. ':' .. ARGV[2]); return 1""",
		[
			_key(actor, sid, "status"),
			_key(actor, sid, "result"),
			_active(status["scope_hash"]),
			_index(_owner(actor)),
		],
		[sid, status["scope_hash"]],
	)


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
