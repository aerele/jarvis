"""Amount checks for the compute sandbox's ``get_list`` reads.

Three ways a total comes out wrong without any error, each caught here from the meta
and at most one extra permissioned query per read:

1. **Currencies.** A Currency field whose ``options`` names another column (``currency``,
   ``party_account_currency`` ...) is in a per-row currency. On a child table the
   currency column usually lives on the parent (a Sales Invoice Item's ``amount`` is in
   its Sales Invoice's ``currency``). Adding such amounts across rows in different
   currencies mixes units.
2. **Companies.** A field whose options are ``Company:<field>:default_currency`` (the
   ``base_*`` fields) is in its row's company currency. Rows of companies with
   different default currencies mix units too.
3. **Draft and cancelled rows.** On a submittable doctype a read that does not say
   which docstatus it wants also totals drafts and cancelled documents.

For each read: the fields arrive already parsed by the allowlist in ``_compute_fields``
(every entry a known (table, field), or refused). What the read aggregates in the
database and what it returns row by row is classified. Then ONE grouped query over the
read's own filters, with the user's permissions, finds which currencies, companies and
docstatus values the matching rows have. The returned rows are the source only where
they really hold one value each: the read groups by that column, or it has no aggregate
and no grouping at all (one row per record). A plain column next to an ungrouped SUM
holds one arbitrary row's value, so the probe decides. docstatus is never taken from
the rows: only a filter pinning it to submitted (``1``, ``['=', 1]``, ``['in', [1]]``)
skips the probe; an explicit ``['in', [0, 1]]`` is a choice and gets a warning that
drafts are included; anything else (``['>=', 0]``, ``['!=', 2]``) is probed. A
child-table read is probed on its parent with its filters rewritten as
``[child, field, op, value]``, the form Frappe joins.

- A database SUM / AVG / MIN / MAX that mixes is refused (fixable error naming the fix).
- Mixed amounts returned row by row get a warning, once per doctype and field.
- Nothing mixes: no message, and the read's receipt names the currency.
"""

from __future__ import annotations

import json

import frappe

from jarvis.tools._compute_fields import Spec, Tables

# Distinct (currency, company, docstatus) combinations the probe reads. Two already
# decide; the rest only make a message complete.
PROBE_LIMIT = 200

_AMOUNT_AGGREGATES = ("SUM", "AVG", "MIN", "MAX")
_COUNT_ALIAS = "jv_rows"
_DOCSTATUS = frozenset({0, 1, 2})


class Kind:
	"""What unit one amount field is in."""

	__slots__ = ("column", "field", "owner", "table", "unit")

	def __init__(self, field: str, table: str, unit: str, owner: str, column: str):
		self.field = field  # the amount field
		self.table = table  # the doctype that holds it
		self.unit = unit  # "row" (a per-row currency) or "company"
		self.owner = owner  # the doctype that holds the currency / company column
		self.column = column  # that column

	@property
	def dimension(self) -> tuple[str, str]:
		return (self.owner, self.column)


def amount_kind(tables: Tables, table: str, field: str) -> Kind | None:
	"""The unit of ``table.field`` from the meta, or None when it is not such an amount."""
	meta = tables.meta_of(table)
	df = meta.get_field(field)
	if not df or df.fieldtype != "Currency":
		return None
	options = (df.options or "").strip()
	if not options:
		return None
	if options.startswith("Company:"):
		parts = options.split(":")
		column = parts[1] if len(parts) > 1 and parts[1] else "company"
		owner = table if meta.has_field(column) else tables.owner_of(table)
		if not tables.meta_of(owner).has_field(column):
			return None
		return Kind(field, table, "company", owner, column)
	if ":" in options:
		return None
	columns = set(meta.get_valid_columns())
	if options in columns:
		return Kind(field, table, "row", table, options)
	owner = tables.owner_of(table)
	if owner != table and tables.meta_of(owner).has_field(options):
		return Kind(field, table, "row", owner, options)
	return None


class Usage:
	"""How one read uses its amounts."""

	def __init__(self):
		self.aggregated: list[Kind] = []
		self.selected: list[Kind] = []
		self.row_keys: dict[tuple[str, str], str] = {}  # (table, field) -> key in each row
		self.grouped: set[tuple[str, str]] = set()
		self.has_aggregate = False


def usage(tables: Tables, specs: list[Spec], groups: list[Spec]) -> Usage:
	out = Usage()
	for spec in specs:
		if spec.star:
			for df in tables.meta.fields:
				if not df.fieldname:
					continue
				out.row_keys[(tables.doctype, df.fieldname)] = df.fieldname
				kind = amount_kind(tables, tables.doctype, df.fieldname)
				if kind:
					out.selected.append(kind)
			continue
		if spec.func is not None:
			out.has_aggregate = True
			if spec.func in _AMOUNT_AGGREGATES and spec.table:
				kind = amount_kind(tables, spec.table, spec.field)
				if kind is not None:
					out.aggregated.append(kind)
			continue
		out.row_keys[(spec.table, spec.field)] = spec.key
		kind = amount_kind(tables, spec.table, spec.field)
		if kind is not None:
			out.selected.append(kind)
	out.grouped = {(g.table, g.field) for g in groups}
	return out


def _rows_hold(use: Usage, dim: tuple[str, str]) -> bool:
	"""Whether each returned row holds the one real value of ``dim``: the read groups by
	it, or it neither aggregates nor groups (one row per record)."""
	if dim in use.grouped:
		return True
	return not use.has_aggregate and not use.grouped


def _docstatus_conditions(filters) -> list | None:
	"""``(op, value)`` of every docstatus condition in ANDed ``filters``; None for a
	shape this does not know."""
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except ValueError:
			return None
	if not filters:
		return []
	if isinstance(filters, dict):
		items = list(filters.items())
	elif isinstance(filters, list | tuple):
		conds = filters if isinstance(filters[0], list | tuple | dict) else [filters]
		items = []
		for cond in conds:
			if isinstance(cond, dict):
				items += list(cond.items())
			elif isinstance(cond, list | tuple) and len(cond) in (2, 3, 4):
				field = cond[1] if len(cond) == 4 else cond[0]
				rest = cond[2:] if len(cond) == 4 else cond[1:]
				items.append((field, list(rest) if len(rest) == 2 else rest[0]))
			else:
				return None
	else:
		return None
	out = []
	for field, value in items:
		if not isinstance(field, str):
			continue
		if field.replace("`", "").rsplit(".", 1)[-1].strip() != "docstatus":
			continue
		if isinstance(value, list | tuple) and len(value) == 2 and isinstance(value[0], str):
			out.append((value[0].strip().casefold(), value[1]))
		else:
			out.append(("=", value))
	return out


def _docstatus_ints(value) -> set | None:
	values = value if isinstance(value, list | tuple | set) else [value]
	out = set()
	for v in values:
		if isinstance(v, bool):
			return None
		if isinstance(v, str) and v.strip().isdigit():
			v = int(v.strip())
		if not isinstance(v, int):
			return None
		out.add(v)
	return out


def _docstatus_allowed(op: str, value) -> tuple[set, bool] | None:
	"""The docstatus values one condition admits, and whether it names them outright."""
	ints = _docstatus_ints(value)
	if ints is None:
		return None
	if op in ("=", "==", "in"):
		return ints, True
	if op in ("!=", "not in"):
		return _DOCSTATUS - ints, False
	if len(ints) != 1:
		return None
	v = next(iter(ints))
	compare = {">": v.__lt__, ">=": v.__le__, "<": v.__gt__, "<=": v.__ge__}.get(op)
	if compare is None:
		return None
	return {d for d in _DOCSTATUS if compare(d)}, False


def docstatus_choice(filters) -> str | None:
	"""What ``filters`` decide about docstatus: ``"submitted"`` (pinned to 1),
	``"included"`` (drafts or cancelled named outright alongside submitted, a deliberate
	choice that gets a warning), ``"chosen"`` (named outright, without submitted), or
	None (not decided: the probe counts what the rows hold). ``or_filters`` never count:
	the OR can admit drafts through its other branches."""
	conds = _docstatus_conditions(filters)
	if not conds:
		return None
	allowed, outright = set(_DOCSTATUS), False
	for op, value in conds:
		admitted = _docstatus_allowed(op, value)
		if admitted is None:
			return None
		allowed &= admitted[0]
		outright = outright or admitted[1]
	if allowed <= {1}:
		return "submitted"
	if not outright:
		return None
	return "included" if 1 in allowed else "chosen"


def _as_child_filters(filters, child: str, parent: str) -> list | None:
	"""A child-table read's filters in the ``[child, field, op, value]`` form a get_list on
	the parent joins. None when a condition has a shape this does not know."""
	if not filters:
		return []
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except ValueError:
			return None
	if isinstance(filters, dict):
		filters = [filters]
	if not isinstance(filters, list | tuple):
		return None
	if filters and not isinstance(filters[0], list | tuple | dict):
		filters = [filters]
	out = []
	for cond in filters:
		if isinstance(cond, dict):
			for key, value in cond.items():
				if isinstance(value, list | tuple) and len(value) == 2 and isinstance(value[0], str):
					out.append([child, key, value[0], value[1]])
				else:
					out.append([child, key, "=", value])
		elif isinstance(cond, list | tuple) and len(cond) == 3:
			out.append([child, *cond])
		elif isinstance(cond, list | tuple) and len(cond) == 4 and cond[0] in (child, parent):
			out.append(list(cond))
		elif isinstance(cond, list | tuple) and len(cond) == 2:
			out.append([child, cond[0], "=", cond[1]])
		else:
			return None
	return out


def _and_list(items) -> str:
	items = list(items)
	return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class Probe:
	"""What the matching rows hold: values per (doctype, column) and the docstatus counts."""

	def __init__(self):
		self.values: dict[tuple[str, str], set] = {}
		self.not_submitted = 0
		self.known_docstatus = False


def check(run, tables: Tables, specs, groups, rows, filters, kwargs, receipt) -> None:
	"""Check one finished get_list read; warn on ``run`` or fail it. See the module docstring."""
	doctype = tables.doctype
	use = usage(tables, specs, groups)
	if not (use.aggregated or use.selected):
		return
	or_filters = kwargs.get("or_filters")
	main = tables.parent or doctype  # the doctype that holds the currency / company columns
	main_meta = tables.meta_of(main)

	known: dict[tuple[str, str], set] = {}
	dims: list[tuple[str, str]] = []
	for kind in use.aggregated + use.selected:
		dim = kind.dimension
		if dim in known or dim in dims:
			continue
		key = use.row_keys.get(dim)
		if key is not None and dim[0] == doctype and _rows_hold(use, dim):
			known[dim] = {r.get(key) for r in rows}
		elif dim[0] == main:
			dims.append(dim)
	choice = docstatus_choice(filters) if main_meta.is_submittable else "submitted"
	want_docstatus = (
		choice is None
		and (main, "docstatus") not in use.grouped
		and (doctype, "docstatus") not in use.grouped
	)
	probe = _probe(run, tables, main, dims, want_docstatus, filters, or_filters, kwargs)
	known.update(probe.values)

	messages: list[tuple[str, bool, tuple]] = []  # (text, refuse, warn key)
	currencies_found: set = set()
	for kind, summed in [(k, True) for k in use.aggregated] + [(k, False) for k in use.selected]:
		values = sorted(v for v in known.get(kind.dimension, ()) if v)
		if kind.unit == "company":
			values = sorted({c for c in (_company_currency(v) for v in values) if c})
		currencies_found.update(values)
		if len(values) < 2:
			continue
		if kind.dimension in use.grouped or (kind.unit == "row" and (kind.table, kind.column) in use.grouped):
			continue
		text = _mixed_message(tables, kind, values, summed)
		messages.append((text, summed, ("unit", kind.table, kind.field)))
	if probe.not_submitted:
		summed = bool(use.aggregated)
		text = (
			f"{probe.not_submitted} draft or cancelled {main} rows match these filters; add docstatus: 1 "
			"for submitted, 0 for drafts, or ['in', [0, 1]] for both."
		)
		messages.append((text, summed, ("docstatus", main)))

	if choice == "included" and (main, "docstatus") not in use.grouped:
		messages.append(
			(
				f"These {main} rows include drafts (docstatus 0) or cancelled documents, as the filters "
				"ask: drafts are included in these amounts. Use docstatus: 1 for submitted only.",
				False,
				("docstatus-included", main),
			)
		)

	if len(currencies_found) == 1:
		receipt["currency"] = next(iter(currencies_found))
	elif currencies_found:
		receipt["currencies"] = sorted(currencies_found)
	for text, refuse, key in messages:
		if refuse:
			run.fail(text)
	for text, _refuse, key in messages:
		run.warn(key, text)


def _probe(run, tables: Tables, main: str, dims, want_docstatus: bool, filters, or_filters, kwargs) -> Probe:
	"""One permissioned grouped read over the read's own filters: the distinct values of
	each dimension (and docstatus) among the matching rows."""
	out = Probe()
	columns = [column for _table, column in dims]
	if frappe.session.user != "Administrator" and columns:
		from frappe.model import get_permitted_fields

		permitted = set(get_permitted_fields(main))
		columns = [c for c in columns if c in permitted]
	if want_docstatus:
		columns.append("docstatus")
	if not columns:
		return out
	if tables.parent:  # a child-table read: probe its parent, joined to the matching child rows
		probe_filters = _as_child_filters(filters, tables.doctype, tables.parent)
		probe_or = _as_child_filters(or_filters, tables.doctype, tables.parent)
		if probe_filters is None or probe_or is None:
			run.warn(
				("child-probe", tables.doctype),
				f"{tables.doctype} amounts are in their {tables.parent}'s currency and company, which "
				"could not be checked for these filters. Read the parent's currency with the rows "
				"(group by parent) before adding amounts across rows.",
			)
			return out
	else:
		probe_filters, probe_or = filters or {}, or_filters
	call = {
		"fields": [*columns, {"COUNT": "*", "as": _COUNT_ALIAS}],
		"filters": probe_filters,
		"group_by": ", ".join(columns),
		"order_by": ", ".join(f"{c} asc" for c in columns),
		"limit": PROBE_LIMIT,
		"ignore_permissions": False,
	}
	if probe_or:
		call["or_filters"] = probe_or
	from jarvis.tools._compute_fields import frappe_field
	from jarvis.tools._compute_sandbox import _DICT_AGGREGATES

	count = Spec(field="*", func="COUNT", alias=_COUNT_ALIAS, arg="*")
	call["fields"][-1] = frappe_field(count, _DICT_AGGREGATES)  # the string form on Frappe 15
	for row in frappe.get_list(main, **call):
		for column in columns:
			if column == "docstatus":
				continue
			out.values.setdefault((main, column), set()).add(row.get(column))
		if want_docstatus and row.get("docstatus") != 1:
			out.not_submitted += int(row.get(_COUNT_ALIAS) or 0)
	return out


def record_units(doctype: str, meta, values: dict, fields) -> set:
	"""The currencies of the amounts among ``fields`` of one record (``values`` holds the
	record's columns): each per-row amount's currency column, each ``base_*`` amount's
	company currency. For a get_doc / get_value read."""
	tables = Tables(doctype, meta, None)
	units = set()
	for field in fields:
		kind = amount_kind(tables, doctype, field) if isinstance(field, str) else None
		if kind is None or kind.owner != doctype:
			continue
		value = values.get(kind.column)
		unit = _company_currency(value) if kind.unit == "company" else value
		if unit:
			units.add(unit)
	return units


def unit_columns(doctype: str, meta, fields) -> list[str]:
	"""The currency / company columns the amounts among ``fields`` are in."""
	tables = Tables(doctype, meta, None)
	out = []
	for field in fields:
		kind = amount_kind(tables, doctype, field) if isinstance(field, str) else None
		if kind is not None and kind.owner == doctype and kind.column not in out:
			out.append(kind.column)
	return out


def _company_currency(company) -> str | None:
	if not company:
		return None
	return frappe.get_cached_value("Company", company, "default_currency")


def _mixed_message(tables: Tables, kind: Kind, values: list[str], summed: bool) -> str:
	table_meta = tables.meta_of(kind.table)
	doctype = tables.doctype
	if kind.unit == "company":
		head = (
			f"{doctype} rows here span companies in {_and_list(values)}. {kind.field} is in each "
			"row's company currency, so "
		)
		head += "one SUM mixes currencies. " if summed else "adding it across rows mixes currencies. "
		return head + f"Filter by {kind.column}, or group by {kind.column}."
	where = kind.column if kind.owner == kind.table else f"{kind.owner} {kind.column}"
	head = f"{doctype} rows here are in {_and_list(values)}. {kind.field} is in each row's {where}, so "
	base = f"base_{kind.field}"
	base_df = table_meta.get_field(base)
	has_base = bool(
		base_df and base_df.fieldtype == "Currency" and (base_df.options or "").startswith("Company:")
	)
	if summed:
		head += "one SUM mixes currencies. "
		fix = f"Group by {kind.column}"
		fix += f", or sum the company-currency field {base} instead." if has_base else ""
	else:
		head += "adding it across rows mixes currencies. "
		fix = f"Total it per {kind.column}"
		fix += f", or use the company-currency field {base}." if has_base else ""
	if not has_base:
		has_rate = bool(tables.meta_of(kind.owner).get_field("conversion_rate"))
		fix += (
			", or convert each row with conversion_rate first." if has_rate else " and report each currency."
		)
	return head + fix
