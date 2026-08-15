"""Customization-discovery telemetry: JSON lines to a dedicated logger
(never-raise, like jarvis/audit.py). Two kinds:

  {"kind": "tool", ts, site, user_hash, conversation, tool, duration_ms,
   result_chars, custom_target}
  {"kind": "turn", ts, site, conversation, run_id, duration_ms, touched_custom}

analyze.py computes the activation rate from these. Every public function
swallows everything - telemetry must never break a tool call or turn.
"""

from __future__ import annotations

import hashlib
import json
import logging

import frappe

_LOGGER = "jarvis.tool_telemetry"
_TRACKED_TARGET_TOOLS = frozenset({"get_schema", "query", "get_list"})
_TRACKED_TOOL = "describe_customizations"

# Cached custom-doctype names; invalidated with the clause cache.
DOCTYPE_SET_CACHE_KEY = "jarvis:telemetry_custom_doctypes"
_DOCTYPE_SET_TTL_S = 300

_TURN_FLAG_TTL_S = 3600


def record_tool(tool: str, args, conversation: str | None, duration_ms: int, result) -> None:
	"""One line per relevant tool call; fast no-op otherwise. Never raises."""
	try:
		custom_target = False
		if tool == _TRACKED_TOOL:
			pass
		elif tool in _TRACKED_TARGET_TOOLS:
			doctype = args.get("doctype") if isinstance(args, dict) else None
			if not doctype or doctype not in custom_doctype_set():
				return
			custom_target = True
			if conversation:
				_mark_turn_custom(conversation)
		else:
			return
		_emit(
			{
				"kind": "tool",
				"ts": frappe.utils.now(),
				"site": getattr(frappe.local, "site", None),
				"user_hash": _user_hash(frappe.session.user),
				"conversation": conversation,
				"tool": tool,
				"duration_ms": int(duration_ms),
				"result_chars": _result_chars(result),
				"custom_target": custom_target,
			}
		)
	except Exception:
		pass


def record_budget_event(
	tool: str, outcome: str, original_chars: int | None, shown: int | None, total: int | None
) -> None:
	"""One line per agent-boundary result-size guard event (truncation or an
	uncapped-but-oversized result). Unlike ``record_tool``, this is not gated
	on a custom-doctype target - every event is signal for sizing the budget
	itself. The guard outcome rides under ``outcome`` (truncated / uncapped /
	uncapped_nonrow / measure_failed) - NOT ``event`` - so it does not collide
	with this line's own ``kind`` discriminator. Never raises."""
	try:
		_emit(
			{
				"kind": "result_budget",
				"ts": frappe.utils.now(),
				"site": getattr(frappe.local, "site", None),
				"tool": tool,
				"outcome": outcome,
				"original_chars": original_chars,
				"shown": shown,
				"total": total,
			}
		)
	except Exception:
		pass


def record_export_event(tool: str, fmt: str, rows: int, mode: str = "sync", outcome: str = "ok") -> None:
	"""One line per server-side export ATTEMPT (export_query etc.): format, row
	count, sync/background, and ``outcome`` (ok / no_data / denied / rejected).
	Emitting on the fail-closed paths too is the point - the refused large exports
	are exactly the signal that would justify raising the ceiling / building the
	async path. Routes through the same INFO-pinned logger. Never raises."""
	try:
		_emit(
			{
				"kind": "export",
				"ts": frappe.utils.now(),
				"site": getattr(frappe.local, "site", None),
				"tool": tool,
				"format": fmt,
				"rows": int(rows),
				"mode": mode,
				"outcome": outcome,
			}
		)
	except Exception:
		pass


def emit_turn(conversation: str | None, run_id: str | None, duration_ms: int) -> None:
	"""One line per completed turn; reads and clears the per-turn custom
	flag. Never raises."""
	try:
		if not conversation:
			return
		_emit(
			{
				"kind": "turn",
				"ts": frappe.utils.now(),
				"site": getattr(frappe.local, "site", None),
				"conversation": conversation,
				"run_id": run_id,
				"duration_ms": int(duration_ms),
				"touched_custom": _read_and_clear_turn_flag(conversation),
			}
		)
	except Exception:
		pass


def custom_doctype_set() -> frozenset:
	"""Cached custom-doctype names (both unions). Empty on failure -
	under-report rather than error."""
	try:
		cache = frappe.cache()
		cached = cache.get_value(DOCTYPE_SET_CACHE_KEY)
		if cached is not None:
			return frozenset(cached)
		from jarvis.site_profile import apps as sp_apps

		names = set(frappe.get_all("DocType", filters={"custom": 1}, pluck="name"))
		modules = sp_apps.custom_module_names()
		if modules:
			names |= set(frappe.get_all("DocType", filters={"module": ("in", list(modules))}, pluck="name"))
		cache.set_value(DOCTYPE_SET_CACHE_KEY, sorted(names), expires_in_sec=_DOCTYPE_SET_TTL_S)
		return frozenset(names)
	except Exception:
		return frozenset()


def _turn_flag_key(conversation: str) -> str:
	return f"jarvis:turn_custom:{conversation}"


def _mark_turn_custom(conversation: str) -> None:
	try:
		frappe.cache().set_value(_turn_flag_key(conversation), 1, expires_in_sec=_TURN_FLAG_TTL_S)
	except Exception:
		pass


def _read_and_clear_turn_flag(conversation: str) -> bool:
	try:
		cache = frappe.cache()
		key = _turn_flag_key(conversation)
		flag = cache.get_value(key)
		if flag:
			cache.delete_value(key)
		return bool(flag)
	except Exception:
		return False


# Process-local random salt, generated once, used ONLY on the rare site that has
# no encryption_key. Deliberately NOT persisted to the DB: _user_hash runs in the
# hot, best-effort telemetry path, and a DB write there is both a side effect
# telemetry must never have and a cache-clearing hazard (frappe.db.set_default
# clears the defaults cache, which broke an unrelated tool-telemetry turn flag).
# Per-process is enough here - it is not the public site name (the actual defect),
# it is not reversible without the secret, and it is stable within a worker so
# events still group; a best-effort analytics salt need not survive a restart.
_fallback_salt = ""


def _site_salt() -> str:
	"""A stable secret used to salt user hashes so a bare email digest is not
	rainbow-reversible over the small, enumerable address space.

	Prefer Frappe's own ``encryption_key`` (per-site, in site_config.json, present
	on every provisioned site, never emitted to any log). On the rare site missing
	one, use a process-local random salt generated once - crucially NOT the site
	name, which the telemetry line itself emits in cleartext (a public salt is
	equivalent to no salt), and NOT a DB-persisted value (no write in this hot
	path). Read-only via ``frappe.conf`` (never ``get_encryption_key``, which would
	WRITE a key). Best-effort: any failure yields "" so telemetry never breaks."""
	try:
		key = frappe.conf.get("encryption_key")
		if key:
			return key
		global _fallback_salt
		if not _fallback_salt:
			_fallback_salt = frappe.generate_hash(length=32)
		return _fallback_salt
	except Exception:
		return ""


def _user_hash(user: str | None) -> str:
	"""SHA1 of the caller, SALTED with a per-site secret (never the public site
	name) and truncated - stable within a site so events can be grouped, but not a
	bare email digest that a dictionary/rainbow attack could reverse."""
	return hashlib.sha1(f"{_site_salt()}:{user or ''}".encode()).hexdigest()[:12]


def _result_chars(result) -> int:
	try:
		return len(frappe.as_json(result))
	except Exception:
		return 0


def _telemetry_logger() -> logging.Logger:
	# frappe.logger defaults to ERROR in prod (WARNING on a dev server), which
	# would silently drop every telemetry INFO line. Pin to INFO so tool +
	# result_budget telemetry is durable on any bench (mirrors chat/latency.py).
	# Keep the same logger name/file - only the level is pinned.
	logger = frappe.logger(_LOGGER)
	if logger.level == 0 or logger.level > logging.INFO:
		logger.setLevel(logging.INFO)
	return logger


def _emit(entry: dict) -> None:
	_telemetry_logger().info(json.dumps(entry, default=str))
