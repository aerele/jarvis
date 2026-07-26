"""Self-gate + per-run budgets for the Custom App Learning *scribe* delegate's
source-read (``list_app_modules`` / ``read_app_source``) and wiki-writeback
(``record_app_wiki``) tools.

The gate mirrors ``jarvis.tools.record_agent_run`` EXACTLY: the run is resolved
from the CALLER's opaque ``session_key`` (the delegate's HTTPS bearer, NEVER a
model-supplied id — the LLM cannot author it, api._dispatch_from_session stashes
it), and the tool refuses unless that session is bound to a RUNNING
``Jarvis Agent Run`` whose listing is EXACTLY the Custom App Learning capability
(``agent_slug == custom-app-learning``) AND the impersonated run-as identity is
admin-tier. So a source-read/wiki-write can only ever happen inside a bona-fide
app-learning run, and only for the admin tier.

CX5-1 — the AUTHORIZATION is the exact ``agent_slug``, not the coarse ``nature``.
``Scribe`` is a shared shape: any future scribe capability (or a listing an admin
flips to ``Scribe``) would otherwise inherit whole-bench custom-app source-read.
The nature + admin-tier checks are KEPT as defense in depth, but the slug is what
binds the capability.

CX5-2 — the RUN SCOPE. "Installed custom app" is not consent. The admin who
launches a run names the exact apps whose source may leave the bench for the
model; the server validates + stamps that selection into the run's ``scope_json``
(``source_apps``) at launch, and ``assert_custom_app`` refuses any app outside it.
The model can neither author nor widen the selection, and the roster tool shows
only the selected apps.

Two per-run budgets keep a looping delegate bounded well inside the 90-minute run
ceiling:
  * a source-BYTES budget so it cannot read source forever, and
  * a page cap so it cannot flood the wiki.

CX5-3 — both counters are DURABLE COLUMNS on the run row (``source_bytes_read`` /
``pages_written``), read under a ``for_update`` lock and advanced in the same
transaction as the side effect they meter. They were cache-backed; a cache outage
reset them to zero and silently handed a looping delegate a whole fresh budget.
The row read FAILS CLOSED: an unreadable row refuses the call rather than
defaulting to "nothing used yet".
"""

from __future__ import annotations

import json

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source
from jarvis.permissions import has_jarvis_admin_access
from jarvis.tools._agent_run_ctx import get_session_key

RUN = "Jarvis Agent Run"
LISTING = "Jarvis Agent Listing"

# CX5-1: the ONE marketplace capability these tools are bound to. Custom-app
# source-read + Org-wiki writeback is granted to THIS agent, never to "any scribe".
APP_LEARNING_AGENT_SLUG = "custom-app-learning"

# Outer safety ceilings (module-level so tests can shrink them). Neither is a
# target — the delegate is SELECTIVE (list, then read only what matters); these
# are the runaway guards under the 90-minute timeout + minimal tools_allow.
PER_RUN_SOURCE_BYTES_BUDGET = 5 * 1024 * 1024  # ~5 MB of source per run
PER_RUN_PAGE_CAP = 15  # wiki pages written per run


def resolve_scribe_run(allow_terminal: bool = False) -> dict:
	"""Resolve + authorise the caller as a bona-fide app-learning scribe run.

	Returns ``{name, agent, session_key, status}`` for the ``Jarvis Agent Run``
	bound to the caller's session_key. Raises ``InvalidArgumentError`` when there
	is no session_key, no run bound to it, the run has finalized, its agent is not
	the ``custom-app-learning`` capability, its nature is not ``Scribe``, or the
	impersonated identity is not admin-tier. This is the identical resolution shape
	``record_agent_run`` uses (never a model-supplied id).

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
	listing = frappe.db.get_value(LISTING, run_row.agent, ["agent_slug", "nature"], as_dict=True) or {}
	# CX5-1 — THE authorization: this capability is bound to ONE agent slug. A
	# different scribe (or a listing an admin re-natured to Scribe) gets nothing.
	if (listing.get("agent_slug") or "").strip() != APP_LEARNING_AGENT_SLUG:
		raise InvalidArgumentError(f"app-source read is bound to the {APP_LEARNING_AGENT_SLUG} agent")
	# Defense in depth (the slug above is the real gate): the bound listing must
	# still be a Scribe, so a mis-natured listing cannot reach the write path.
	nature = (listing.get("nature") or "").strip().title()
	if nature != "Scribe":
		raise InvalidArgumentError(
			"app-source read and wiki writeback are available only to app-learning scribe agents"
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


def is_app_learning_agent(agent: str) -> bool:
	"""True iff the ``Jarvis Agent Listing`` ``agent`` IS the Custom App Learning
	capability (CX5-1/CX5-2). The launch paths use this to decide whether a run
	needs an explicit admin app selection."""
	if not agent:
		return False
	slug = (frappe.db.get_value(LISTING, agent, "agent_slug") or "").strip()
	return slug == APP_LEARNING_AGENT_SLUG


def allowed_source_apps(run_name: str) -> frozenset:
	"""The apps the ADMIN explicitly authorized for THIS run (CX5-2).

	Server-authored at launch into ``Jarvis Agent Run.scope_json`` under
	``source_apps``; the model can never author or widen it. FAIL-CLOSED by
	construction: a missing run, an unreadable/unparseable ``scope_json``, a
	missing ``source_apps`` key or a non-list value all yield the EMPTY set, and an
	empty set refuses every app — so a run launched before this field existed (or
	one whose scope stamp failed) reads nothing rather than everything."""
	if not run_name:
		return frozenset()
	try:
		raw = frappe.db.get_value(RUN, run_name, "scope_json")
		scope = json.loads(raw or "{}")
	except Exception:
		return frozenset()
	if not isinstance(scope, dict):
		return frozenset()
	apps = scope.get("source_apps")
	if not isinstance(apps, list):
		return frozenset()
	return frozenset(str(a or "").strip() for a in apps if str(a or "").strip())


def assert_custom_app(app: str, run_name: str) -> None:
	"""The two-layer app gate (ABSOLUTE) applied by EVERY source-read and
	wiki-writeback call:

	  1. the custom-apps allowlist — ``app`` must be an installed NON-core app with
	     a containment-valid source root on this bench. Core apps
	     (frappe/erpnext/hrms/india_compliance/jarvis) are excluded via
	     ``app_source.EXCLUDED_APPS`` and are NEVER served; and
	  2. CX5-2 — ``app`` must be in THIS RUN's admin-authorized selection
	     (``scope_json.source_apps``). "Installed" is not consent: an admin
	     authorises the exact apps whose source may be shipped to the model, per
	     run, and a run may never widen itself to a sibling app.
	"""
	name = (app or "").strip()
	if not name:
		raise InvalidArgumentError("app is required")
	if name in app_source.EXCLUDED_APPS or name not in app_source._installed_custom_apps():
		raise InvalidArgumentError(f"{name!r} is not a learnable custom app (core apps are never served)")
	# Canonicalize + validate the app's source ROOT here (existence + the
	# symlinked/relocated-root rejection), so a defeated trust root is refused at
	# the gate BEFORE any manifest walk or file read is even attempted.
	try:
		app_source._resolve_app_root(name)
	except ValueError as e:
		raise InvalidArgumentError(str(e))
	if name not in allowed_source_apps(run_name):
		raise InvalidArgumentError(
			f"{name!r} is not in this run's authorized app selection — an admin must "
			"select an app before its source can be read or written about"
		)


# --------------------------------------------------------------------------- #
# per-run budgets (DURABLE — counters on the run ROW, read under its lock)
# --------------------------------------------------------------------------- #
def lock_run_budget(run_name: str, fields: list[str]) -> frappe._dict:
	"""Read the run row's budget counters UNDER a ``for_update`` ROW LOCK — the
	authority for BOTH per-run budgets (CX5-3).

	The counters used to live in the cache. A cache outage (or an evicted key)
	silently reset them to zero, so a redis blip handed a looping delegate a fresh
	5 MB source budget and a fresh 15-page allowance — an unbounded read/write
	behind an intact-looking gate. They are now columns on ``Jarvis Agent Run``
	(``source_bytes_read`` / ``pages_written``), advanced in the SAME transaction as
	the side effect they meter, so a rollback un-spends them and a retry sees the
	true remaining budget.

	FAIL-CLOSED: the read is NOT wrapped in a try. A lock timeout / connection loss
	PROPAGATES, and a row that has vanished raises — never "0 used, go ahead". The
	lock is held to the request-end commit, so two concurrent calls for the same run
	serialize and cannot both reserve the same remaining allowance."""
	row = frappe.db.get_value(RUN, run_name, ["status", *fields], as_dict=True, for_update=True)
	if not row:
		raise InvalidArgumentError(
			"this agent run row could not be read — refusing (per-run budgets are fail-closed)"
		)
	return row
