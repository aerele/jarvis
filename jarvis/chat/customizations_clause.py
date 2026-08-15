"""Per-site customizations hint for the trusted ``[Context: ...]`` line.

Counts + app names ONLY - the clause is shown site-wide (not user-fenced);
doctype names stay behind the fenced tool. Cached per site; invalidated by
the same doc_events as the schema cache, so it counts ONLY sources those
events cover (reports/print formats/scripts stay tool-only).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from jarvis.site_profile import apps as sp_apps

SETTINGS = "Jarvis Settings"
_CLAUSE_CACHE_KEY = "jarvis:customizations_clause"
_CLAUSE_TTL_S = 300
_CLAUSE_MAX_CHARS = 200

_DIRECTIVE = "call jarvis__describe_customizations before assuming standard fields"


def clause_enabled() -> bool:
	"""Operator toggle; NULL=ON via tabSingles probe (get_single_value coerces
	an unset Check to 0)."""
	rows = frappe.db.sql(
		"select value from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, "enable_customizations_clause"),
	)
	if not rows or rows[0][0] is None:
		return True
	return bool(cint(rows[0][0]))


def customizations_clause() -> str:
	"""The clause (leading "; "), or "" - vanilla site, toggle off, or any
	failure. Never raises."""
	try:
		if not clause_enabled():
			return ""
		cache = frappe.cache()
		cached = cache.get_value(_CLAUSE_CACHE_KEY)
		if cached is not None:
			return cached
		clause = _build_clause()
		cache.set_value(_CLAUSE_CACHE_KEY, clause, expires_in_sec=_CLAUSE_TTL_S)
		return clause
	except Exception:
		frappe.log_error(title="customizations clause build failed", message=frappe.get_traceback())
		return ""


def _build_clause() -> str:
	apps = [_fold(a) for a in sp_apps.custom_apps()]
	modules = sp_apps.custom_module_names()
	n_doctypes, n_cf_doctypes, n_workflows = _counts(modules)
	if not apps and not n_doctypes and not n_cf_doctypes and not n_workflows:
		return ""

	counts = []
	if n_doctypes:
		counts.append(f"{n_doctypes} custom doctypes")
	if n_cf_doctypes:
		counts.append(f"custom fields on {n_cf_doctypes} core doctypes")
	if n_workflows:
		counts.append(f"{n_workflows} workflows")
	counts_part = ", ".join(counts)

	# App-list shrink ladder; the count-only floor keeps the cap without
	# slicing the directive.
	for shown in (len(apps), 2, 1, 0):
		clause = _compose(apps, shown, counts_part)
		if len(clause) <= _CLAUSE_MAX_CHARS:
			return clause
	return _compose(apps, 0, counts_part)


def _compose(apps: list[str], shown: int, counts_part: str) -> str:
	bits = []
	if apps:
		if shown <= 0:
			bits.append(f"{len(apps)} custom apps")
		elif len(apps) > shown:
			bits.append(f"custom app(s) {', '.join(apps[:shown])} +{len(apps) - shown} more")
		else:
			bits.append(f"custom app(s) {', '.join(apps)}")
	if counts_part:
		bits.append(counts_part)
	return f"; site customizations: {' - '.join(bits)} - {_DIRECTIVE}"


def _counts(modules: set[str]) -> tuple[int, int, int]:
	"""collect.py's two-union + is_system_generated + known-module rules,
	reduced to counts."""
	names = set(frappe.get_all("DocType", filters={"custom": 1}, pluck="name"))
	if modules:
		names |= set(frappe.get_all("DocType", filters={"module": ("in", list(modules))}, pluck="name"))
	known_modules = sp_apps.known_module_names()
	cf_doctypes = {
		r["dt"]
		for r in frappe.get_all(
			"Custom Field",
			filters={"is_system_generated": 0},
			fields=["dt", "module"],
		)
		if r.get("dt") and r["dt"] not in names and not (r.get("module") and r["module"] in known_modules)
	}
	n_workflows = frappe.db.count("Workflow", {"is_active": 1})
	return len(names), len(cf_doctypes), cint(n_workflows)


def _fold(name: str) -> str:
	"""Disarm an org-authored string entering the trusted bracket."""
	text = " ".join(str(name or "").split())
	return text.replace("`", "'").replace("]", ")").replace(";", ",")


def clear_clause_cache(doc=None, method=None) -> None:
	"""doc_event handler (same events as clear_schema_cache). Never raises."""
	try:
		cache = frappe.cache()
		cache.delete_value(_CLAUSE_CACHE_KEY)
		# The telemetry doctype set derives from the same schema events.
		from jarvis.telemetry import DOCTYPE_SET_CACHE_KEY

		cache.delete_value(DOCTYPE_SET_CACHE_KEY)
	except Exception:
		pass


def after_migrate() -> None:
	"""Recompute after migrate (installed apps only change here). Never
	blocks a migrate."""
	try:
		clear_clause_cache()
		customizations_clause()
	except Exception:
		frappe.log_error(
			title="customizations clause after_migrate failed",
			message=frappe.get_traceback(),
		)
