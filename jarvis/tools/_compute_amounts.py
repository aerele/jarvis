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

For each read: every field reference is resolved to (table, field), whatever its form
(``f``, ``f as x``, ``X.f``, `` `tabX`.f ``, a table field's name as qualifier, inside
an aggregate dict or a ``sum(...)`` string). What the read aggregates in the database
and what it returns row by row is classified. Then ONE grouped query over the read's
own filters, with the user's permissions, finds which currencies, companies and
docstatus values the matching rows have (only the dimensions that matter, and none
when the rows already show them). A child-table read is probed on its parent with its
filters rewritten as ``[child, field, op, value]``, the form Frappe joins.

- A database SUM / AVG / MIN / MAX that mixes is refused (fixable error naming the fix).
- Mixed amounts returned row by row get a warning, once per doctype and field.
- Nothing mixes: no message, and the read's receipt names the currency.
"""

from __future__ import annotations

import json
import re

import frappe

# Distinct (currency, company, docstatus) combinations the probe reads. Two already
# decide; the rest only make a message complete.
PROBE_LIMIT = 200

_AGGREGATES = ("SUM", "AVG", "MIN", "MAX")
_AGG_STRING = re.compile(r"^\s*(sum|avg|max|min|count)\s*\((.+)\)\s*(?:as\s+\S+)?\s*$", re.IGNORECASE)
_REF = re.compile(r"^(?:(?P<table>[^.]+)\.)?(?P<field>\w+)$")
_ALIAS = re.compile(r"\s+as\s+", re.IGNORECASE)
_TABLE_FIELDTYPES = ("Table", "Table MultiSelect")
_COUNT_ALIAS = "jv_rows"


def parse_ref(text) -> tuple[str | None, str] | None:
	"""``(qualifier, field)`` of a plain field reference, or None for anything else.
	Backticks anywhere and an ``as`` alias are dropped."""
	if not isinstance(text, str):
		return None
	s = _ALIAS.split(text.replace("`", "").replace('"', "").strip(), maxsplit=1)[0].strip()
	m = _REF.match(s)
	if not m:
		return None
	table = m.group("table")
	return (table.strip() if table else None), m.group("field")


def _alias(text: str) -> str | None:
	parts = _ALIAS.split(text.replace("`", "").strip(), maxsplit=1)
	return parts[1].strip() if len(parts) == 2 else None


class Tables:
	"""The tables a read can name: its doctype, its child tables (Frappe joins them on a
	parent read), and for a child-table read its parent."""

	def __init__(self, doctype: str, meta, parent_doctype: str | None):
		self.doctype = doctype
		self.meta = meta
		self.parent = parent_doctype
		self.metas = {doctype: meta}
		self._names = {}
		self._add(doctype, doctype)
		for df in meta.fields:
			if df.fieldtype in _TABLE_FIELDTYPES and df.options:
				self._add(df.options, df.options)
				self._names[df.fieldname.casefold()] = df.options
		if parent_doctype:
			self._add(parent_doctype, parent_doctype)

	def _add(self, name: str, doctype: str):
		self._names[name.casefold()] = doctype
		self._names[f"tab{name}".casefold()] = doctype

	def resolve(self, qualifier: str | None) -> str | None:
		if qualifier is None:
			return self.doctype
		return self._names.get(qualifier.strip().casefold())

	def meta_of(self, doctype: str):
		if doctype not in self.metas:
			self.metas[doctype] = frappe.get_meta(doctype)
		return self.metas[doctype]

	def owner_of(self, doctype: str) -> str:
		"""Where a table's per-row currency and company live: a child table's are on the
		read's parent doctype (its own parent on a parent read, ``parent_doctype`` on a
		child read)."""
		if doctype == self.doctype:
			return self.parent or doctype
		return self.doctype


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


def usage(tables: Tables, fields, group_by) -> Usage:
	out = Usage()
	for entry in fields or []:
		if isinstance(entry, dict):
			funcs = [k for k in entry if k != "as"]
			if len(funcs) != 1:
				continue
			func, arg = str(funcs[0]).upper(), entry[funcs[0]]
			kind = _kind_of_ref(tables, arg)
			if kind is None:
				continue
			(out.aggregated if func in _AGGREGATES else out.selected).append(kind)
			continue
		if not isinstance(entry, str):
			continue
		if entry.strip() == "*":
			meta = tables.meta
			for df in meta.fields:
				kind = amount_kind(tables, tables.doctype, df.fieldname) if df.fieldname else None
				if kind:
					out.selected.append(kind)
				if df.fieldname:
					out.row_keys[(tables.doctype, df.fieldname)] = df.fieldname
			continue
		agg = _AGG_STRING.match(entry)
		if agg:
			kind = _kind_of_ref(tables, agg.group(2))
			if kind is not None and agg.group(1).upper() in _AGGREGATES:
				out.aggregated.append(kind)
			continue
		ref = parse_ref(entry)
		if ref is None:
			continue
		table = tables.resolve(ref[0])
		if table is None:
			continue
		out.row_keys[(table, ref[1])] = _alias(entry) or ref[1]
		kind = amount_kind(tables, table, ref[1])
		if kind is not None:
			out.selected.append(kind)
	for part in str(group_by or "").split(","):
		ref = parse_ref(part)
		if ref:
			table = tables.resolve(ref[0])
			if table:
				out.grouped.add((table, ref[1]))
	return out


def _kind_of_ref(tables: Tables, text) -> Kind | None:
	ref = parse_ref(text)
	if ref is None:
		return None
	table = tables.resolve(ref[0])
	return amount_kind(tables, table, ref[1]) if table else None


def filters_name_docstatus(filters, or_filters) -> bool:
	"""Whether the read says which docstatus it wants (any condition on it)."""
	from jarvis.tools.get_list import _filter_fieldnames

	return "docstatus" in (_filter_fieldnames(filters) | _filter_fieldnames(or_filters))


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


def check(run, doctype: str, meta, parent_doctype, fields, rows, filters, kwargs, receipt) -> None:
	"""Check one finished get_list read; warn on ``run`` or fail it. See the module docstring."""
	tables = Tables(doctype, meta, parent_doctype)
	use = usage(tables, fields, kwargs.get("group_by"))
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
		grouped_by_other = use.grouped and dim not in use.grouped
		if key is not None and not grouped_by_other and dim[0] == doctype:
			known[dim] = {r.get(key) for r in rows}
		elif dim[0] == main:
			dims.append(dim)
	want_docstatus = (
		bool(main_meta.is_submittable)
		and (main, "docstatus") not in use.grouped
		and (doctype, "docstatus") not in use.grouped
		and not filters_name_docstatus(filters, or_filters)
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
	from jarvis.tools._compute_sandbox import _translate_field

	call["fields"][-1] = _translate_field(call["fields"][-1])  # the string form on Frappe 15
	for row in frappe.get_list(main, **call):
		for column in columns:
			if column == "docstatus":
				continue
			out.values.setdefault((main, column), set()).add(row.get(column))
		if want_docstatus and row.get("docstatus") != 1:
			out.not_submitted += int(row.get(_COUNT_ALIAS) or 0)
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
