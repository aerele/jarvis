"""Redis state of the nightly shadow run: the lease, the night's chain and the per-company records.

Every key starts with `jarvis:pcal:`, which `persistent_cache_keys` keeps across the watchdog's full cache
clear, and every key has a TTL. No key or value holds a company name: a company is its HMAC digest.
Values are read with the local cache off: a unit runs in another process than the dispatcher.
"""

from __future__ import annotations

import json
from secrets import token_hex
from time import time

import frappe

from jarvis.payment_calendar import read_log
from jarvis.payment_calendar.errors import TIMEOUTS

PREFIX = "jarvis:pcal:shadow:"
RECORD_TTL = read_log.COUNTER_TTL_S
NIGHT_TTL = 2 * 86_400
# RQ kills a hung job at its timeout plus a grace of 60 s and a monitor interval: the lease must outlive that.
LEASE_TTL = 900

_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


def lease_key() -> str:
	return PREFIX + "lease"


def night_key(night: str) -> str:
	return f"{PREFIX}night:{night}"


def record_key(digest: str, night: str) -> str:
	return f"{PREFIX}{digest}:{night}"


def take_lease() -> str | None:
	"""A random token when the lease was free, else None. The lease outlives a killed unit by `LEASE_TTL`."""
	token = token_hex(16)
	taken = frappe.cache.execute_command(
		"SET", frappe.cache.make_key(lease_key()), token, "NX", "EX", LEASE_TTL
	)
	return token if taken else None


def release_lease(token: str | None) -> None:
	"""Delete the lease only when it still holds `token` (a later holder keeps its own)."""
	if token:
		frappe.cache.execute_command("EVAL", _RELEASE, 1, frappe.cache.make_key(lease_key()), token)


def lease_value() -> str | None:
	value = frappe.cache.execute_command("GET", frappe.cache.make_key(lease_key()))
	return value.decode() if isinstance(value, bytes) else value


def lease_held() -> bool:
	return lease_value() is not None


def start_night(
	night: str,
	count: int,
	*,
	started: float | None = None,
	units_hash: str | None = None,
	restarts: int = 0,
) -> None:
	"""Open the night: its unit count, a digest of its unit list, its start and the cursor at 0.

	The 1,800 s budget runs from `started`. `restarts` counts how often a changed list restarted the night.
	"""
	key = frappe.cache.make_key(night_key(night))
	fields = {"count": count, "started": started if started is not None else time(), "cursor": 0}
	fields.update(restarts=restarts, **({"units": units_hash} if units_hash else {}))
	frappe.cache.execute_command("DEL", key)
	for name, value in fields.items():
		frappe.cache.execute_command("HSET", key, name, value)
	frappe.cache.execute_command("EXPIRE", key, NIGHT_TTL)


def night_info(night: str) -> dict | None:
	raw = frappe.cache.execute_command("HGETALL", frappe.cache.make_key(night_key(night)))
	if not raw:
		return None
	values = {_text(field): _text(value) for field, value in raw.items()}
	return {
		"count": int(values["count"]),
		"started": float(values["started"]),
		"cursor": int(values["cursor"]),
		"units": values.get("units"),
		"restarts": int(values.get("restarts", 0)),
	}


def cursor(night: str) -> int:
	info = night_info(night)
	return info["cursor"] if info else 0


def set_cursor(night: str, value: int) -> None:
	frappe.cache.execute_command("HSET", frappe.cache.make_key(night_key(night)), "cursor", value)


def put_record(digest: str, night: str, field: str, record: dict) -> None:
	"""Store one unit record as JSON. A Redis error raises: a lost marker or record must not pass silently."""
	key = frappe.cache.make_key(record_key(digest, night))
	frappe.cache.execute_command("HSET", key, field, json.dumps(record, default=str))
	frappe.cache.execute_command("EXPIRE", key, RECORD_TTL)


def records(digest: str, night: str) -> dict[str, dict]:
	"""Every unit record of one company and night, by `<side>:<scope>`; {} when the cache is empty or down."""
	try:
		raw = frappe.cache.execute_command("HGETALL", frappe.cache.make_key(record_key(digest, night)))
		return {_text(field): json.loads(_text(value)) for field, value in (raw or {}).items()}
	except TIMEOUTS:
		raise
	except Exception:
		return {}


def _text(value) -> str:
	return value.decode() if isinstance(value, bytes) else str(value)
