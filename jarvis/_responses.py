"""Customer-facing envelope helpers.

The bench's whitelisted endpoints all return one of two shapes:

  success:  {"ok": True, "data": {...}}
  failure:  {"ok": False, "error": {"code": <str>, "message": <str>}}

This used to be open-coded in three places (oauth/api.py had ``_ok``
+ ``_err``, jarvis/api.py had ``_error`` + inline ``{"ok": True,
"data": ...}``, plus a handful of inline literals across the
module). The 2026-06-16 review's "two-helper boilerplate" item asked
for one source of truth so the shape can't drift across endpoints.

Lives at jarvis/_responses.py rather than a per-subpackage file
because customer endpoints span multiple subpackages (api,
oauth.api, onboarding, etc.) and a leading underscore signals
"internal helper".

Module-local helpers that wrap these (e.g. ``_surface`` in
onboarding.py adding sync-status fields) are fine - they layer on
top of the canonical shape rather than diverging from it.
"""

from __future__ import annotations


def ok(data: dict | list | None) -> dict:
	"""Success envelope. ``data`` is the typed payload; pass {} for
	"action completed, nothing to report"."""
	return {"ok": True, "data": data}


def err(code: str, message: str, *, detail: str = "", hint: str = "") -> dict:
	"""Failure envelope. ``code`` is the stable identifier the bench
	client maps onto a Python exception (FleetError, InvalidArgument,
	NoRunningTenant, etc.). ``message`` is short user-safe text - NEVER
	include traceback content or secrets (token bytes, paths).

	``detail`` and ``hint`` are OPTIONAL enrichment for the human-facing
	chat UI (a specific safe reason + a "what you can do" line). They are
	added to ``error`` only when non-empty, so an un-enriched failure keeps
	the exact ``{"code", "message"}`` shape every existing consumer branches
	on. Same secret-safety rule as ``message`` - callers must pass only
	user-safe text (see ``jarvis.api._translate_write_error``)."""
	error = {"code": code, "message": message}
	if detail:
		error["detail"] = detail
	if hint:
		error["hint"] = hint
	return {"ok": False, "error": error}
