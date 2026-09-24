"""Builder-defined dashboard filters: parse the ``jarvis-filters`` block,
validate the definitions, and bind viewer-picked values into a saved source
spec at run time.

The invariant this module protects: a viewer only ever supplies typed VALUES
for filters the dashboard's author declared. Field names, operators, tables
and joins stay exactly as saved, so the query tool's permission weave and the
ORM's record/field permissions apply to the bound spec unchanged.

Placeholders (``{"$filter": "<fieldname>"}``) are allowed in value slots only:

  query       where[].value, having[].value (recursively inside exists /
              not exists sub-specs)
  get_list    a value in ``filters`` (dict form or [field, op, value] rows)
  run_report  a value in ``filters``

Binding: value present -> substituted; empty and optional -> the containing
predicate is dropped; empty and required -> FilterRequiredError.
"""

from __future__ import annotations

import copy
import re

import frappe
from frappe import _

from jarvis.exceptions import InvalidArgumentError

FILTERS_BLOCK_RE = re.compile(
	r'<script[^>]*\bid=["\']jarvis-filters["\'][^>]*>(.*?)</script>',
	re.I | re.S,
)
FILTER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_FILTERS = 8
MAX_LABEL_CHARS = 60
MAX_VALUE_CHARS = 140
_ALLOWED_FILTER_TYPES = ("Link",)
_DEF_KEYS = {"fieldname", "label", "fieldtype", "options", "default", "reqd"}
_ROW_KEYS = ("fieldname", "label", "fieldtype", "options", "default_value", "reqd")
USER_DEFAULT_TOKEN = "$user_default"
PLACEHOLDER_KEY = "$filter"


class FilterRequiredError(Exception):
	"""A required filter has no value; the source must not run."""

	def __init__(self, fieldname: str, label: str):
		super().__init__(f"filter {fieldname!r} is required")
		self.fieldname = fieldname
		self.label = label


# --------------------------------------------------------------------------- #
# parse + validate (save path)
# --------------------------------------------------------------------------- #


def parse_filters_block(html: str) -> list[dict]:
	"""Extract the declared filters from the ``jarvis-filters`` JSON block.
	No block -> no filters. A present-but-broken block throws, mirroring the
	sources block: silently ignoring it would save a dashboard whose specs
	reference filters that never render."""
	m = FILTERS_BLOCK_RE.search(html or "")
	if not m:
		return []
	try:
		parsed = frappe.parse_json(m.group(1).strip())
	except Exception:
		parsed = None
	if not isinstance(parsed, dict) or not isinstance(parsed.get("filters"), list):
		frappe.throw(_('The jarvis-filters block must be JSON of the shape {"filters": [...]}.'))
	return parsed["filters"]


def normalize_filter_rows(defs: list) -> list[dict]:
	"""Coerce block entries into child-row dicts. Unknown keys are KEPT so
	``validate_filter_defs`` can reject them by name."""
	if not isinstance(defs, list):
		frappe.throw(_("filters must be a list."))
	rows: list[dict] = []
	for i, d in enumerate(defs):
		if not isinstance(d, dict):
			frappe.throw(_("filters[{0}] must be an object.").format(i))
		row = {
			"fieldname": str(d.get("fieldname") or "").strip(),
			"label": str(d.get("label") or "").strip(),
			"fieldtype": str(d.get("fieldtype") or "").strip(),
			"options": str(d.get("options") or "").strip(),
			"default_value": str(d.get("default") if d.get("default") is not None else "").strip(),
			"reqd": 1 if d.get("reqd") in (1, True, "1") else 0,
		}
		for k in d:
			if k not in _DEF_KEYS:
				row[k] = d[k]
		rows.append(row)
	return rows


def validate_filter_defs(rows: list[dict]) -> None:
	"""Full definition check; raises frappe.ValidationError via frappe.throw."""
	if len(rows) > MAX_FILTERS:
		frappe.throw(_("A dashboard can declare at most {0} filters.").format(MAX_FILTERS))
	seen: set[str] = set()
	for row in rows:
		extra = sorted(k for k in row if k not in _ROW_KEYS)
		if extra:
			frappe.throw(
				_("Filter '{0}': unknown key(s): {1}").format(row.get("fieldname"), ", ".join(extra))
			)
		name = row["fieldname"]
		if not FILTER_NAME_RE.match(name):
			frappe.throw(
				_(
					"Invalid filter fieldname '{0}': use a lowercase letter followed by letters, digits or underscores."
				).format(name)
			)
		if name in seen:
			frappe.throw(_("Duplicate filter fieldname: {0}").format(name))
		seen.add(name)
		if not row["label"] or len(row["label"]) > MAX_LABEL_CHARS:
			frappe.throw(
				_("Filter '{0}': label is required (at most {1} characters).").format(name, MAX_LABEL_CHARS)
			)
		if row["fieldtype"] not in _ALLOWED_FILTER_TYPES:
			frappe.throw(
				_("Filter '{0}': fieldtype must be one of {1}.").format(
					name, ", ".join(_ALLOWED_FILTER_TYPES)
				)
			)
		if row["fieldtype"] == "Link":
			opts = row["options"]
			if not opts:
				frappe.throw(_("Filter '{0}': options (the DocType to pick from) is required.").format(name))
			# istable via the cached meta: one read, no get_doc.
			if not frappe.db.exists("DocType", opts):
				frappe.throw(_("Filter '{0}': unknown DocType: {1}").format(name, opts))
			if frappe.get_meta(opts).istable:
				frappe.throw(
					_("Filter '{0}': {1} is a child table and cannot be a filter target.").format(name, opts)
				)
		if len(row["default_value"]) > MAX_VALUE_CHARS:
			frappe.throw(_("Filter '{0}': default is too long.").format(name))


# --------------------------------------------------------------------------- #
# placeholder walk (shared by validation and binding)
# --------------------------------------------------------------------------- #


def _is_placeholder(v) -> bool:
	return isinstance(v, dict) and set(v.keys()) == {PLACEHOLDER_KEY}


def _placeholder_name(v) -> str:
	name = v.get(PLACEHOLDER_KEY)
	if not isinstance(name, str) or not name:
		raise InvalidArgumentError("$filter placeholder must name a declared filter")
	return name


def _forbid_placeholders(obj, path: str) -> None:
	"""Reject a placeholder anywhere in a structural (non-value) part of a spec."""
	if _is_placeholder(obj):
		raise InvalidArgumentError(f"$filter placeholder is not allowed in {path}")
	if isinstance(obj, dict):
		for k, v in obj.items():
			_forbid_placeholders(v, f"{path}.{k}")
	elif isinstance(obj, list):
		for i, v in enumerate(obj):
			_forbid_placeholders(v, f"{path}[{i}]")


def _walk_query(spec: dict, on_value, path: str = "spec") -> None:
	"""Visit every predicate value slot in a query spec (where/having, nested
	exists sub-specs). Everything else must be placeholder-free."""
	for k, v in spec.items():
		if k in ("where", "having"):
			continue
		_forbid_placeholders(v, f"{path}.{k}")
	for clause in ("where", "having"):
		for i, p in enumerate(spec.get(clause) or []):
			if not isinstance(p, dict):
				continue
			for k, v in p.items():
				if k == "value":
					continue
				_forbid_placeholders(v, f"{path}.{clause}[{i}].{k}")
			val = p.get("value")
			if p.get("op") in ("exists", "not exists") and isinstance(val, dict) and not _is_placeholder(val):
				_walk_query(val, on_value, f"{path}.{clause}[{i}].value")
			else:
				on_value(p, "value")


def _walk_get_list(spec: dict, on_value) -> None:
	for k, v in spec.items():
		if k != "filters":
			_forbid_placeholders(v, f"spec.{k}")
	filters = spec.get("filters")
	if isinstance(filters, dict):
		for k in list(filters.keys()):
			_forbid_placeholders(k, "spec.filters key")
			on_value(filters, k)
	elif isinstance(filters, list):
		for i, row in enumerate(filters):
			if isinstance(row, list) and len(row) >= 3:
				for part in row[:-1]:
					_forbid_placeholders(part, f"spec.filters[{i}]")
				on_value(row, len(row) - 1)
			else:
				_forbid_placeholders(row, f"spec.filters[{i}]")


def _walk_run_report(spec: dict, on_value) -> None:
	for k, v in spec.items():
		if k != "filters":
			_forbid_placeholders(v, f"spec.{k}")
	filters = spec.get("filters")
	if isinstance(filters, dict):
		for k in list(filters.keys()):
			on_value(filters, k)
	else:
		_forbid_placeholders(filters, "spec.filters")


_WALKERS = {"query": _walk_query, "get_list": _walk_get_list, "run_report": _walk_run_report}


def check_placeholders(tool: str, spec: dict, declared: set[str]) -> set[str]:
	"""Return the filter names a spec references. Raises InvalidArgumentError
	on a placeholder outside a value slot or naming an undeclared filter."""
	walker = _WALKERS.get(tool)
	if walker is None:
		raise InvalidArgumentError(f"unsupported tool: {tool}")
	used: set[str] = set()

	def on_value(container, key):
		v = container[key]
		if _is_placeholder(v):
			name = _placeholder_name(v)
			if name not in declared:
				raise InvalidArgumentError(f"$filter '{name}' is not a declared filter")
			used.add(name)
		else:
			_forbid_placeholders(v, "value")  # a placeholder nested inside a literal value

	walker(spec, on_value)
	return used


# --------------------------------------------------------------------------- #
# run time: values, defaults, binding
# --------------------------------------------------------------------------- #


def coerce_values(rows: list[dict], raw: dict | None) -> dict[str, str]:
	"""Viewer-supplied ``{fieldname: value}`` -> normalised strings for every
	declared filter (missing/None -> ""). Unknown names and non-scalar or
	over-long values are rejected."""
	raw = raw or {}
	if not isinstance(raw, dict):
		raise InvalidArgumentError("filters must be an object of {fieldname: value}")
	declared = {r["fieldname"] for r in rows}
	unknown = sorted(set(raw) - declared)
	if unknown:
		raise InvalidArgumentError(f"unknown filter(s): {', '.join(unknown)}")
	out: dict[str, str] = {}
	for r in rows:
		v = raw.get(r["fieldname"])
		if v is None:
			out[r["fieldname"]] = ""
			continue
		if not isinstance(v, str):
			raise InvalidArgumentError(f"filter '{r['fieldname']}' must be a string")
		if len(v) > MAX_VALUE_CHARS:
			raise InvalidArgumentError(f"filter '{r['fieldname']}' value is too long")
		out[r["fieldname"]] = v.strip()
	return out


def resolve_defaults(rows: list[dict], user: str) -> dict[str, str]:
	"""Literal defaults as-is; ``$user_default`` -> the user's default for the
	Link target DocType (empty when none is set)."""
	out: dict[str, str] = {}
	for r in rows:
		d = r.get("default_value") or ""
		if d == USER_DEFAULT_TOKEN:
			d = frappe.defaults.get_user_default(r.get("options") or "", user) or ""
		out[r["fieldname"]] = str(d)
	return out


def bind_spec(tool: str, spec: dict, rows: list[dict], values: dict[str, str]) -> dict:
	"""Deep-copy ``spec`` with every placeholder resolved per the module
	docstring. ``values`` must already be coerced (every declared name present)."""
	by_name = {r["fieldname"]: r for r in rows}
	bound = copy.deepcopy(spec)
	to_drop: list[tuple] = []  # (container, key) pairs to remove after the walk

	def on_value(container, key):
		v = container[key]
		if not _is_placeholder(v):
			return
		name = _placeholder_name(v)
		row = by_name.get(name)
		if row is None:
			raise InvalidArgumentError(f"$filter '{name}' is not a declared filter")
		val = values.get(name, "")
		if val:
			container[key] = val
		elif row.get("reqd"):
			raise FilterRequiredError(name, row.get("label") or name)
		else:
			to_drop.append((container, key))

	_WALKERS[tool](bound, on_value)
	_drop_predicates(bound, tool, to_drop)
	return bound


def _drop_predicates(bound: dict, tool: str, to_drop: list[tuple]) -> None:
	"""Remove the predicates whose placeholder resolved to an empty optional
	value. query: the predicate dict leaves its where/having list (empty lists
	are removed). get_list list-form: the row leaves ``filters``. dict forms:
	the key is deleted."""
	dicts_hit = [c for c, _ in to_drop if isinstance(c, dict) and tool == "query"]
	for container, key in to_drop:
		if isinstance(container, dict) and tool != "query":
			del container[key]
	if tool == "query":
		_prune_query(bound, dicts_hit)
	elif tool == "get_list" and isinstance(bound.get("filters"), list):
		hit_rows = [c for c, _ in to_drop if isinstance(c, list)]
		bound["filters"] = [r for r in bound["filters"] if not any(r is h for h in hit_rows)]
		if not bound["filters"]:
			del bound["filters"]


def _prune_query(node: dict, hit: list) -> None:
	for clause in ("where", "having"):
		lst = node.get(clause)
		if not isinstance(lst, list):
			continue
		kept = []
		for p in lst:
			if any(p is h for h in hit):
				continue
			val = p.get("value") if isinstance(p, dict) else None
			if isinstance(p, dict) and p.get("op") in ("exists", "not exists") and isinstance(val, dict):
				_prune_query(val, hit)
			kept.append(p)
		if kept:
			node[clause] = kept
		else:
			del node[clause]
