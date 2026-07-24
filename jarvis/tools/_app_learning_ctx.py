"""Self-gate + per-run budgets for the Custom App Learning *scribe* delegate's
source-read (``list_app_modules`` / ``read_app_source``) and wiki-writeback
(``record_app_wiki``) tools.

The gate mirrors ``jarvis.tools.record_agent_run`` EXACTLY: the run is resolved
from the CALLER's opaque ``session_key`` (the delegate's HTTPS bearer, NEVER a
model-supplied id — the LLM cannot author it, api._dispatch_from_session stashes
it), and the tool refuses unless that session is bound to a RUNNING
``Jarvis Agent Run`` whose listing ``nature`` is ``Scribe`` AND the impersonated
run-as identity is admin-tier. So a source-read/wiki-write can only ever happen
inside a bona-fide app-learning scribe run, and only for the admin tier.

Two per-run budgets, tracked per ``session_key`` (a fresh key per run), keep a
looping delegate bounded well inside the 90-minute run ceiling:
  * a source-BYTES budget so it cannot read source forever, and
  * a page cap so it cannot flood the wiki.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source
from jarvis.permissions import has_jarvis_admin_access
from jarvis.tools._agent_run_ctx import get_session_key

RUN = "Jarvis Agent Run"
LISTING = "Jarvis Agent Listing"

# Outer safety ceilings (module-level so tests can shrink them). Neither is a
# target — the delegate is SELECTIVE (list, then read only what matters); these
# are the runaway guards under the 90-minute timeout + minimal tools_allow.
PER_RUN_SOURCE_BYTES_BUDGET = 5 * 1024 * 1024  # ~5 MB of source per run
PER_RUN_PAGE_CAP = 15  # wiki pages written per run
# TTL > the 90-min run ceiling AND the 3-h stale-run reaper cutoff, so a live
# run's budget never expires mid-run; a stale key self-evicts long after.
_BUDGET_TTL_S = 3 * 60 * 60


def resolve_scribe_run(allow_terminal: bool = False) -> dict:
	"""Resolve + authorise the caller as a bona-fide app-learning scribe run.

	Returns ``{name, agent, session_key, status}`` for the ``Jarvis Agent Run``
	bound to the caller's session_key. Raises ``InvalidArgumentError`` when there
	is no session_key, no run bound to it, the run has finalized, its agent's
	nature is not ``Scribe``, or the impersonated identity is not admin-tier.
	This is the identical resolution shape ``record_agent_run`` uses (never a
	model-supplied id).

	``allow_terminal=True`` keeps the same identity/nature gate but does NOT raise
	on an already-finalized run — the IDEMPOTENT finish fast-path uses it so a
	double-finish (or a server-reconciled run) returns the terminal state rather
	than erroring. The source-read + writeback tools keep the default (a finalized
	run cannot read source or write pages)."""
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"this tool must be called by an app-learning scribe delegate over its "
			"run session (no session_key in context)"
		)
	run_row = frappe.db.get_value(
		RUN,
		{"session_key": session_key},
		["name", "status", "agent"],
		as_dict=True,
	)
	if not run_row:
		raise InvalidArgumentError("no agent run is bound to this session")
	if run_row.status != "running" and not allow_terminal:
		raise InvalidArgumentError("this agent run has already finalized")
	nature = (frappe.db.get_value(LISTING, run_row.agent, "nature") or "").strip().title()
	if nature != "Scribe":
		raise InvalidArgumentError(
			"app-source read and wiki writeback are available only to app-learning "
			"scribe agents"
		)
	# Defense in depth: the install + run gates already refuse a non-admin run-as
	# identity for this agent, but the tool ALSO checks the (impersonated) caller
	# is admin-tier so source access is never served to a lesser identity even if
	# an install were mis-configured.
	if not has_jarvis_admin_access(frappe.session.user):
		raise InvalidArgumentError("app-learning source access requires an admin-tier run-as identity")
	return {
		"name": run_row.name,
		"agent": run_row.agent,
		"session_key": session_key,
		"status": run_row.status,
	}


def assert_custom_app(app: str) -> None:
	"""The custom-apps allowlist gate (ABSOLUTE): raise unless ``app`` is an
	installed NON-core app with a source dir on this bench. Core apps
	(frappe/erpnext/hrms/india_compliance/jarvis) are excluded via
	``app_source.EXCLUDED_APPS`` and are NEVER served."""
	name = (app or "").strip()
	if not name:
		raise InvalidArgumentError("app is required")
	if name in app_source.EXCLUDED_APPS or name not in app_source._installed_custom_apps():
		raise InvalidArgumentError(
			f"{name!r} is not a learnable custom app (core apps are never served)"
		)
	# Canonicalize + validate the app's source ROOT here (existence + the
	# symlinked/relocated-root rejection), so a defeated trust root is refused at
	# the gate BEFORE any manifest walk or file read is even attempted.
	try:
		app_source._resolve_app_root(name)
	except ValueError as e:
		raise InvalidArgumentError(str(e))


# --------------------------------------------------------------------------- #
# per-run budgets (cache-backed, keyed by session_key)
# --------------------------------------------------------------------------- #
def _src_bytes_key(session_key: str) -> str:
	return f"jarvis:app_learning:src_bytes:{session_key}"


def source_bytes_used(session_key: str) -> int:
	try:
		return int(frappe.cache().get_value(_src_bytes_key(session_key)) or 0)
	except Exception:
		return 0


def add_source_bytes(session_key: str, n: int) -> int:
	total = source_bytes_used(session_key) + int(n or 0)
	try:
		frappe.cache().set_value(_src_bytes_key(session_key), total, expires_in_sec=_BUDGET_TTL_S)
	except Exception:
		pass
	return total


def _pages_key(session_key: str) -> str:
	return f"jarvis:app_learning:pages:{session_key}"


def pages_written(session_key: str) -> int:
	try:
		return int(frappe.cache().get_value(_pages_key(session_key)) or 0)
	except Exception:
		return 0


def add_pages_written(session_key: str, n: int) -> int:
	total = pages_written(session_key) + int(n or 0)
	try:
		frappe.cache().set_value(_pages_key(session_key), total, expires_in_sec=_BUDGET_TTL_S)
	except Exception:
		pass
	return total
