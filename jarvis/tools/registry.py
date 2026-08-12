"""Tool registry + dispatch.

Tool modules live as siblings in jarvis.tools.<name>; the function
in each module is named the same as the file. Adding a new tool is
a two-step change: drop the module in, list the name in
``_TOOL_NAMES``. No additional import line needed here.

Previous shape: 11 explicit ``from jarvis.tools.<name> import <name>``
lines + an 11-entry dict literal. The repetition was a 2026-06-16
punch-list item. Now: one tuple of names, one comprehension that
walks ``importlib.import_module``. Each module is imported once at
registry-load time (Python caches it), so dispatch keeps the same
O(1) lookup behaviour.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable

from jarvis.exceptions import InvalidArgumentError, ToolNotFoundError

_TOOL_NAMES: tuple[str, ...] = (
	"get_schema",
	# Live, permission-fenced index of the site's customizations (custom
	# apps/doctypes, custom fields on core doctypes, workflows, reports).
	"describe_customizations",
	"get_doc",
	"get_list",
	# Creation context assembler: field map (mandatory/auto/readonly) + the
	# existing records most similar to the one being created, so the agent
	# decides field values from real examples instead of interrogating the
	# user. Read-only; picks no values. See jarvis-persona AGENTS.md.
	"get_creation_context",
	# Link-resolution for the create flow: which referenced records exist,
	# which have near-matches to reuse, which are missing (feed create_docs).
	"resolve_links",
	"run_report",
	"get_report_filters",
	"run_method",
	"query",
	"update_doc",
	"create_doc",
	# Batch, atomic create for a doc's missing dependencies (one gated card).
	"create_docs",
	"preview_doc",
	# CSV / file import: preview_import (read-only) shows what a raw file load would
	# produce; run_import (gated) stages a Frappe Data Import and starts it. For data
	# that needs any processing first, create_doc(docs=[...]) stays the path - import
	# is only for loading a file AS-IS.
	"preview_import",
	"run_import",
	"submit_doc",
	"cancel_doc",
	"delete_doc",
	"amend_doc",
	# Workflow (state-machine) support: read the actions the current user may
	# take on a doc, then apply one. apply_workflow_action is gated in api.py;
	# submit_doc/cancel_doc refuse workflow-governed doctypes and point here.
	"get_workflow_transitions",
	"apply_workflow_action",
	# Tier 1 artifact producers: hand the agent a file/URL the customer
	# can act on, instead of a wall of JSON.
	"download_pdf",
	"report_pdf",
	"export_excel",
	# Composed report/summary (not a single record) → downloadable PDF / HTML /
	# PNG. download_pdf prints an existing record; this renders agent-composed
	# content so a "give me a PDF of this report" ask produces a real download.
	"export_document",
	"read_file",
	"get_file_pages",
	"attach_to_doc",
	"download_vcard",
	# Tier 2a ERPNext computed reads: numbers the LLM keeps hallucinating
	# because they need business-logic-aware math (FIFO/Moving-Avg,
	# fiscal-year boundaries, party-level GL aggregation).
	"get_stock_balance",
	"get_valuation_rate",
	"scan_barcode",
	"get_balance_on",
	"get_customer_outstanding",
	"get_party_dashboard_info",
	"get_exchange_rate",
	"get_fiscal_year",
	"get_itemised_tax_breakup",
	# Tier 2b HRMS + Frappe computed reads: leave/shift/holiday lookups
	# the LLM gets wrong because they need policy-aware math, plus
	# Frappe linked-doc walking + naming-series preview.
	"get_leave_balance_on",
	"get_leaves_for_period",
	"get_leave_details",
	"get_holidays_for_employee",
	"get_employee_shift",
	"get_linked_docs",
	"get_submitted_linked_docs",
	"get_naming_series_preview",
	# Tier 3 desk-mirror actions: parity with the buttons a Desk user
	# can click - email, comment, share, assign, tag, follow. All
	# mutating; descriptors in tool-defs.ts carry ALWAYS-CONFIRM
	# language for the side-effectful ones.
	"send_email",
	"add_comment",
	"update_comment",
	"share_doc",
	"unshare_doc",
	"assign_to",
	"unassign_from",
	"add_tag",
	"remove_tag",
	"follow_document",
	"unfollow_document",
	# Tier 4 analytics + visualization: dataset stats + dashboard/chart
	# creation (independent reimplementations of capability gaps).
	"summarize_dataset",
	"create_dashboard_chart",
	"create_dashboard",
	# Phase-3 delegate writeback: the auditor/operator delegate passes its
	# deterministic evaluator output here VERBATIM. It resolves the Jarvis Agent
	# Run from the caller's session_key, validates every finding (token/doctype/
	# ref/amount/severity) as the run-as user, persists them (token in rule_id,
	# company stamped), coverage-scoped auto-resolves (A16), rechecks the GL
	# consistency watermark (A17), and finalizes the Run. See A2/A16/A17.
	"record_agent_run",
	# Phase-4 delegate → saved dashboard: the delegate authors a self-contained,
	# A2-safe findings dashboard HTML and persists it here as a Jarvis Dashboard
	# (User-scoped to the run's owner), linked on Run.dashboard. Resolves the run
	# from the caller's session_key like record_agent_run; renders in the CSP
	# sandbox. See Phase 4 / A2.
	"save_agent_dashboard",
	# Skill + wiki self-service (voice & wiki feature): search/read/save the
	# customer's saved skills and org-wiki pages mid-turn. The write pair
	# (create_custom_skill, update_wiki) is confirmation-gated in api.py.
	"find_skills",
	"get_skill",
	"create_custom_skill",
	"read_wiki",
	"update_wiki",
	# Custom App Learning scribe delegate: self-gated source reads +
	# audited-not-gated wiki writeback. list_app_modules / read_app_source only
	# serve a running app-learning SCRIBE run (session-key-bound, admin-tier,
	# custom-apps allowlist, realpath-containment + per-run byte budget);
	# record_app_wiki applies pages server-side through apply_extracted_page_updates
	# (in _WRITE_TOOLS, NOT _GATED_WRITES — like record_agent_run);
	# finish_app_learning_run is the scribe's TERMINAL finalizer (flips the run to
	# completed with the pages tally so a success never sits running until the
	# stale-run reaper marks it failed) — same self-gate, audited-not-gated.
	"list_app_modules",
	"read_app_source",
	"record_app_wiki",
	"finish_app_learning_run",
)


def _resolve(name: str) -> Callable:
	mod = importlib.import_module(f"jarvis.tools.{name}")
	return getattr(mod, name)


_TOOLS: dict[str, Callable] = {name: _resolve(name) for name in _TOOL_NAMES}


def _accepted_params(fn: Callable) -> set[str]:
	"""Names of the parameters ``fn`` is willing to bind by keyword.

	Computed once per tool function and cached so dispatch doesn't pay
	the inspect cost on every call. If a tool ever uses ``**kwargs``
	we surface that with an empty set + a special VAR_KEYWORD marker
	via the cache (caller skips filtering for those tools).
	"""
	return {
		p.name
		for p in inspect.signature(fn).parameters.values()
		if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
	}


_ACCEPTS_VAR_KW: dict[str, bool] = {
	name: any(p.kind == p.VAR_KEYWORD for p in inspect.signature(fn).parameters.values())
	for name, fn in _TOOLS.items()
}
_ACCEPTED_PARAMS: dict[str, set[str]] = {name: _accepted_params(fn) for name, fn in _TOOLS.items()}


def list_tools() -> list[str]:
	return sorted(_TOOLS.keys())


def dispatch(tool_name: str, args: dict):
	if tool_name not in _TOOLS:
		raise ToolNotFoundError(f"no such tool: {tool_name}")
	if not isinstance(args, dict):
		raise InvalidArgumentError("args must be a dict")
	# Filter args to keys the tool's signature actually binds. Defense-
	# in-depth against an LLM that hallucinates extra keyword args - a
	# tool that does ``def get_doc(doctype, name)`` should not receive a
	# ``permission_check=False`` kwarg even if the LLM emits one.
	# ``api._parse_args`` already strips the legacy ``_user``/``_session``
	# markers; this is the wider allowlist over what the tool's own
	# parameter list declares. Tools that use ``**kwargs`` opt out
	# (currently none, but future tools wanting bag-of-args semantics
	# would skip the filter naturally).
	fn = _TOOLS[tool_name]
	if not _ACCEPTS_VAR_KW.get(tool_name, False):
		accepted = _ACCEPTED_PARAMS[tool_name]
		args = {k: v for k, v in args.items() if k in accepted}
	# Validate the call binds *before* invoking, so a genuine arg/signature
	# mismatch (a missing required arg - the caller's fault) becomes
	# InvalidArgumentError, while a TypeError raised inside the tool body (a real
	# bug, e.g. from a method run via run_method) propagates to the 500 handler
	# instead of being mislabeled as bad input.
	try:
		inspect.signature(fn).bind(**args)
	except TypeError as e:
		raise InvalidArgumentError(str(e))
	return fn(**args)
