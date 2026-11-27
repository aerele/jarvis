"""What a compute ``get_list`` may name in ``fields``, ``group_by`` and ``order_by``: an allowlist.

Every entry is parsed into a known (table, field) on the queried doctype, one of its
child tables or (for a child-table read) its parent, or into one of the accepted
aggregate forms. Anything else is refused with the accepted forms, so a spelling
nobody anticipated fails closed instead of slipping past the amount checks.

Accepted:

- a field: ``"grand_total"``, ``"grand_total as gt"``, ```"`grand_total`"``, a qualified
  one ``"Sales Invoice Item.amount"`` written ``"`tabSales Invoice Item`.amount"``,
  ``"tabX.field"``, ``"items.amount"`` (a table field's name), optionally backticked
  and with an ``as`` alias;
- ``"*"``: every field of the queried doctype;
- an aggregate: ``{"SUM" | "COUNT" | "AVG" | "MIN" | "MAX": "<field>", "as": "<alias>"}``
  over one field written as above (no alias inside), ``{"COUNT": "*", "as": "n"}``, or
  the same as text, ``"sum(grand_total) as total"``, ``"count(*) as n"``;
- ``group_by``: fields, comma separated; ``order_by``: fields or the aliases above, each
  with an optional ``asc`` / ``desc``.

Every text is at most MAX_ENTRY_CHARS long (MAX_CLAUSE_CHARS for a whole ``group_by`` /
``order_by``) before any regular expression sees it, and the patterns have no nested
or overlapping repetition.
"""

from __future__ import annotations

import re

import frappe

# Longest field, alias, aggregate or group_by / order_by part accepted. A qualified
# field with a long child-table name and an alias is well under 100 characters.
MAX_ENTRY_CHARS = 200
# Longest whole group_by / order_by.
MAX_CLAUSE_CHARS = 1_000
# Most entries in fields, group_by or order_by.
MAX_ENTRIES = 200

AGGREGATES = ("SUM", "COUNT", "AVG", "MIN", "MAX")
_TABLE_FIELDTYPES = ("Table", "Table MultiSelect")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
# A qualifier: `anything but a backtick` or a bare identifier. A field: an identifier,
# optionally backticked. Each part is delimited by a character the next cannot start
# with, so matching is linear.
_REF = re.compile(
	r"^(?:(?:`(?P<qb>[^`]{1,140})`|(?P<qw>"
	+ _IDENT
	+ r"))\.)?(?:`(?P<fb>"
	+ _IDENT
	+ r")`|(?P<fw>"
	+ _IDENT
	+ r"))$"
)
_ALIAS_NAME = re.compile(r"^(?:`(?P<b>\w{1,64})`|\"(?P<q>\w{1,64})\"|'(?P<s>\w{1,64})'|(?P<w>\w{1,64}))$")
# func(arg): the function name, then everything up to the last ")".
_AGG_TEXT = re.compile(r"^(?P<func>[A-Za-z_]{2,24})\s*\((?P<arg>.*)\)$", re.DOTALL)
# One segment of a dotted filter key: `anything but a backtick`, or an identifier.
_SEGMENT = re.compile(r"`([^`]{1,140})`|(" + _IDENT + r")")

# SQL date functions people reach for to bucket by period: refused with how to do it.
_DATE_FUNCTIONS = frozenset(
	{
		"MONTH",
		"YEAR",
		"DAY",
		"DAYOFMONTH",
		"WEEK",
		"QUARTER",
		"DATE",
		"DATE_FORMAT",
		"EXTRACT",
		"MONTHNAME",
		"YEARWEEK",
	}
)
_DATE_HINT = (
	"To total by month (or year, week), group_by='posting_date' with {'SUM': ...} and bucket the "
	"rows in Python with getdate(r.posting_date).month, or read one date range per month with "
	"filters={'posting_date': ['between', [start, end]]}."
)
_DISTINCT_HINT = (
	"To count distinct values, group by the field and count the rows: "
	"get_list(doctype, fields=['customer', {'COUNT': '*', 'as': 'n'}], group_by='customer'), then len(rows)."
)

# Filter operators Frappe takes. Anything else is refused.
OPERATORS = frozenset(
	{
		"=",
		"!=",
		"<",
		">",
		"<=",
		">=",
		"like",
		"not like",
		"in",
		"not in",
		"between",
		"is",
		"timespan",
		"descendants of",
		"not descendants of",
		"ancestors of",
		"not ancestors of",
		"descendants of (inclusive)",
	}
)
# Link fields a filter may follow from one doctype to the next: two hops at most.
MAX_LINK_HOPS = 2

ACCEPTED = (
	"A field is 'grand_total', 'grand_total as gt' or a child table's 'Sales Invoice Item.amount' "
	"written '`tabSales Invoice Item`.amount'. An aggregate is {'SUM': 'grand_total', 'as': 'total'} "
	"(SUM, COUNT, AVG, MIN or MAX over one field of this doctype or its child table) or "
	"{'COUNT': '*', 'as': 'n'}, or the same as text, 'sum(grand_total) as total'."
)


class FieldRefused(ValueError):
	"""A fields / group_by / order_by entry outside the allowlist."""


class Spec:
	"""One accepted entry: a field (``func`` None), an aggregate, or ``*``."""

	__slots__ = ("alias", "arg", "field", "func", "linked", "original", "star", "table")

	def __init__(
		self, *, table=None, field=None, func=None, alias=None, star=False, original=None, arg=None, linked=()
	):
		self.table = table  # doctype holding the field (None for COUNT(*) and *)
		self.field = field  # field name, "*" for COUNT(*)
		self.func = func  # SUM / COUNT / AVG / MIN / MAX, or None
		self.alias = alias
		self.star = star
		self.original = original
		self.arg = arg  # an aggregate's argument as written ("`tabX`.f", "f", "*")
		self.linked = linked  # doctypes a filter key reached through link fields, in order

	@property
	def key(self) -> str | None:
		"""The key this entry comes back under in each row."""
		return self.alias or (self.field if self.func is None else None)


class Tables:
	"""The tables a read may name: its doctype, its child tables (Frappe joins them on a
	parent read), and for a child-table read its parent."""

	def __init__(self, doctype: str, meta, parent_doctype: str | None):
		self.doctype = doctype
		self.meta = meta
		self.parent = parent_doctype
		self.metas = {doctype: meta}
		self._names: dict[str, str] = {}
		self._add(doctype)
		for df in meta.fields:
			if df.fieldtype in _TABLE_FIELDTYPES and df.options:
				self._add(df.options)
				self._names[df.fieldname.casefold()] = df.options
		if parent_doctype:
			self._add(parent_doctype)

	def _add(self, name: str):
		self._names[name.casefold()] = name
		self._names[f"tab{name}".casefold()] = name

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


def _short(text: str, what: str) -> str:
	if len(text) > MAX_ENTRY_CHARS:
		raise FieldRefused(
			f"{what} of {len(text):,} characters is not accepted here; one entry is at most "
			f"{MAX_ENTRY_CHARS} characters."
		)
	return text.strip()


def _split_alias(text: str) -> tuple[str, str | None]:
	"""``(expression, alias)`` around one `` as `` (any case) before the last word,
	without a regular expression."""
	words = text.split()
	lowered = [w.casefold() for w in words]
	if "as" not in lowered:
		return text, None
	if lowered.count("as") != 1 or lowered.index("as") != len(words) - 2:
		raise FieldRefused("An alias goes last, as one word: 'grand_total as gt'.")
	alias = _alias_name(words[-1])
	return " ".join(words[:-2]), alias


def _alias_name(text: str) -> str:
	m = _ALIAS_NAME.match(text.strip())
	if not m:
		raise FieldRefused(f"alias {text[:80]!r} is not accepted here: one word of letters, digits and _.")
	return m.group("b") or m.group("q") or m.group("s") or m.group("w")


def _ref(tables: Tables, text: str) -> tuple[str, str]:
	"""``(table, field)`` of a plain field reference (no alias)."""
	m = _REF.match(text.strip())
	if not m:
		raise FieldRefused("")
	qualifier = m.group("qb") or m.group("qw")
	table = tables.resolve(qualifier)
	if table is None:
		raise FieldRefused(
			f"{qualifier!r} is not {tables.doctype} or one of its child tables, so its fields cannot be "
			"read here."
		)
	return table, m.group("fb") or m.group("fw")


def _refused(entry, detail: str = "") -> FieldRefused:
	shown = repr(entry)
	shown = shown if len(shown) <= 120 else shown[:117] + "..."
	return FieldRefused(
		f"fields entry {shown} is not accepted here. {detail + ' ' if detail else ''}{ACCEPTED}"
	)


def parse_field(tables: Tables, entry) -> Spec:
	"""One ``fields`` entry as a Spec, or FieldRefused."""
	if isinstance(entry, dict):
		return _parse_aggregate_dict(tables, entry)
	if not isinstance(entry, str):
		raise _refused(entry)
	text = _short(entry, "A fields entry")
	if text == "*":
		return Spec(star=True, original=entry)
	try:
		head, alias = _split_alias(text)
		agg = _AGG_TEXT.match(head) if "(" in head else None
		if agg:
			return _aggregate(tables, agg.group("func"), agg.group("arg"), alias, entry)
		table, field = _ref(tables, head)
	except FieldRefused as e:
		raise _refused(entry, str(e)) from None
	return Spec(table=table, field=field, alias=alias, original=entry)


def _parse_aggregate_dict(tables: Tables, entry: dict) -> Spec:
	funcs = [k for k in entry if k != "as"]
	if len(funcs) != 1 or not isinstance(funcs[0], str) or not set(entry) <= {funcs[0], "as"}:
		raise _refused(entry, "An aggregate dict has one function key and an optional 'as'.")
	alias = entry.get("as")
	try:
		if alias is not None:
			if not isinstance(alias, str):
				raise FieldRefused("'as' takes one word.")
			alias = _alias_name(_short(alias, "An alias"))
		arg = entry[funcs[0]]
		if not isinstance(arg, str):
			raise FieldRefused("The function takes one field name as text, not a list or other value.")
		return _aggregate(tables, funcs[0], _short(arg, "An aggregate's field"), alias, entry)
	except FieldRefused as e:
		raise _refused(entry, str(e)) from None


def _aggregate(tables: Tables, func: str, arg: str, alias: str | None, original) -> Spec:
	func = func.upper()
	if func in _DATE_FUNCTIONS:
		raise FieldRefused(f"SQL date functions like {func}() are not available. {_DATE_HINT}")
	if func not in AGGREGATES:
		raise FieldRefused(f"{func} is not one of {', '.join(AGGREGATES)}.")
	arg = arg.strip()
	if arg.casefold().startswith("distinct "):
		raise FieldRefused(f"COUNT(DISTINCT ...) is not available. {_DISTINCT_HINT}")
	if arg == "*":
		if func != "COUNT":
			raise FieldRefused("Only COUNT takes '*'.")
		return Spec(field="*", func=func, alias=alias, original=original, arg="*")
	if " as " in f" {arg.casefold()} ":
		raise FieldRefused("The alias goes in 'as', not inside the function.")
	try:
		table, field = _ref(tables, arg)
	except FieldRefused as e:
		raise FieldRefused(str(e) or "The function takes one field, no expression.") from None
	return Spec(table=table, field=field, func=func, alias=alias, original=original, arg=arg)


def check_child_aggregates(tables: Tables, specs: list[Spec]) -> None:
	"""Refuse an aggregate over a child table's field on a parent read: Frappe does not
	join the child table for it (MariaDB answers "unknown column"). Called after the
	private-table check, so the hint never names a table the user may not read."""
	for spec in specs:
		if spec.func and spec.table and spec.table not in (tables.doctype, tables.parent):
			raise FieldRefused(
				f"fields entry {str(spec.original)[:80]} is not accepted here. To total a {spec.table} field, "
				f"read {spec.table} itself with parent_doctype: get_list('{spec.table}', "
				f"parent_doctype='{tables.doctype}', filters=..., fields=[{{'{spec.func}': '{spec.field}', 'as': 't'}}])."
			)


def parse_fields(tables: Tables, fields) -> list[Spec]:
	if len(fields) > MAX_ENTRIES:
		raise FieldRefused(f"fields takes at most {MAX_ENTRIES} entries.")
	return [parse_field(tables, entry) for entry in fields]


def _clause_parts(value, what: str) -> list[str]:
	if value is None or value == "":
		return []
	if not isinstance(value, str):
		raise FieldRefused(f"{what} is text, e.g. {what}='customer'.")
	if len(value) > MAX_CLAUSE_CHARS:
		raise FieldRefused(
			f"{what} of {len(value):,} characters is not accepted here; the limit is {MAX_CLAUSE_CHARS}."
		)
	parts = [p.strip() for p in value.split(",")]
	if len(parts) > MAX_ENTRIES or any(not p for p in parts):
		raise FieldRefused(f"{what} is a comma-separated list of fields.")
	return [_short(p, f"A {what} part") for p in parts]


def parse_group_by(tables: Tables, group_by) -> list[Spec]:
	out = []
	for part in _clause_parts(group_by, "group_by"):
		try:
			table, field = _ref(tables, part)
		except FieldRefused as e:
			raise FieldRefused(
				f"group_by part {part[:80]!r} is not accepted here. {str(e) + ' ' if str(e) else ''}"
				"group_by names fields, e.g. group_by='customer, currency'."
			) from None
		out.append(Spec(table=table, field=field, original=part))
	return out


def parse_order_by(tables: Tables, order_by, aliases: set[str]) -> list[Spec]:
	out = []
	for part in _clause_parts(order_by, "order_by"):
		words = part.split()
		head = part
		if len(words) >= 2 and words[-1].casefold() in ("asc", "desc"):
			head = part[: part.casefold().rindex(words[-1].casefold())].strip()
		bare = head.strip("`")
		if bare in aliases and re.fullmatch(_IDENT, bare):
			out.append(Spec(alias=bare, original=part))
			continue
		try:
			table, field = _ref(tables, head)
		except FieldRefused as e:
			raise FieldRefused(
				f"order_by part {part[:80]!r} is not accepted here. {str(e) + ' ' if str(e) else ''}"
				"order_by names fields or aliases, each with asc or desc: order_by='total desc, name asc'."
			) from None
		out.append(Spec(table=table, field=field, original=part))
	return out


def frappe_field(spec: Spec, dict_aggregates: bool):
	"""The value Frappe's get_list takes for one accepted entry: a field as written; an
	aggregate as the dict form (Frappe 16) or the text form (Frappe 15)."""
	if spec.star or spec.func is None:
		return spec.original.strip()
	if dict_aggregates:
		return {spec.func: spec.arg, **({"as": spec.alias} if spec.alias else {})}
	alias = spec.alias or f"{spec.func.lower()}_{'all' if spec.arg == '*' else spec.field}"
	column = spec.arg if spec.arg == "*" or "." in spec.arg else f"`{spec.arg.strip('`')}`"
	return f"{spec.func.lower()}({column}) as `{alias}`"


# --------------------------------------------------------------------------- #
# Filters                                                                      #
# --------------------------------------------------------------------------- #


class Condition:
	"""One filter condition: the field it tests (a Spec, with the doctypes any link hops
	reached in ``linked``), its operator and value."""

	__slots__ = ("op", "spec", "value")

	def __init__(self, spec: Spec, op: str, value):
		self.spec = spec
		self.op = op
		self.value = value


def _segments(text: str) -> list[str]:
	"""The dot-separated segments of a filter key, backticks honoured; FieldRefused for
	anything else."""
	out, pos = [], 0
	while True:
		m = _SEGMENT.match(text, pos)
		if not m:
			raise FieldRefused("")
		out.append(m.group(1) or m.group(2))
		pos = m.end()
		if pos == len(text):
			return out
		if text[pos] != ".":
			raise FieldRefused("")
		pos += 1


def _link_target(tables: Tables, doctype: str, fieldname: str) -> str | None:
	df = tables.meta_of(doctype).get_field(fieldname)
	if df and df.fieldtype == "Link" and df.options:
		return df.options
	return None


def filter_key(tables: Tables, key) -> Spec:
	"""The (table, field) a filter key tests. Accepted: a field, a qualified field (as in
	``fields``), a child table's field through its table field (``items.item_code``), and
	up to MAX_LINK_HOPS link fields followed to the doctype they point to
	(``customer.territory``): the Spec's table is then that target doctype and
	``linked`` lists every doctype the hops reached."""
	if not isinstance(key, str):
		raise FieldRefused(f"filter field {key!r:.60} is not accepted here: a filter field is text.")
	text = _short(key, "A filter field").strip()
	try:
		parts = _segments(text)
	except FieldRefused:
		raise FieldRefused(
			f"filter field {text[:80]!r} is not accepted here: name a field, e.g. 'customer'."
		) from None
	if len(parts) == 1:
		return Spec(table=tables.doctype, field=parts[0], original=key)
	if len(parts) > MAX_LINK_HOPS + 1:
		raise FieldRefused(
			f"filter field {text[:80]!r} is not accepted here: it follows more than {MAX_LINK_HOPS} links."
		)
	table = tables.resolve(parts[0])
	linked: list[str] = []
	rest = parts[1:]
	if table is None:
		table = _link_target(tables, tables.doctype, parts[0])
		if table is None:
			raise FieldRefused(
				f"filter field {text[:80]!r} is not accepted here: {parts[0]!r} is not a link field, a child "
				f"table or {tables.doctype}."
			)
		linked.append(table)
	for hop in rest[:-1]:
		target = _link_target(tables, table, hop)
		if target is None:
			raise FieldRefused(
				f"filter field {text[:80]!r} is not accepted here: {hop!r} is not a link field of {table}."
			)
		table = target
		linked.append(table)
	return Spec(table=table, field=rest[-1], original=key, linked=tuple(linked))


def _condition(tables: Tables, key, value) -> Condition:
	spec = filter_key(tables, key)
	if isinstance(value, list | tuple):
		if not value or not isinstance(value[0], str) or len(value) > 2:
			raise FieldRefused(
				f"filter {str(key)[:60]!r}: a list value is [operator, value], e.g. "
				f"{{'{str(key)[:40]}': ['in', [1, 0]]}}."
			)
		op = value[0].strip().casefold()
		if op not in OPERATORS:
			raise FieldRefused(
				f"filter operator {value[0][:40]!r} is not one of: {', '.join(sorted(OPERATORS))}."
			)
		return Condition(spec, op, value[1] if len(value) == 2 else None)
	return Condition(spec, "=", value)


def parse_filters(tables: Tables, filters) -> list[Condition]:
	"""Every condition of ``filters`` (a dict, a list of conditions or one condition,
	already parsed from JSON text): ``{field: value}``, ``{field: [op, value]}``,
	``[field, op, value]``, ``[doctype, field, op, value]`` (doctype the queried one, a
	child table or the parent), ``[field, value]``. Anything else is refused."""
	if filters is None or filters == {} or filters == []:
		return []
	if isinstance(filters, dict):
		items = list(filters.items())
		if len(items) > MAX_ENTRIES:
			raise FieldRefused(f"filters take at most {MAX_ENTRIES} conditions.")
		return [_condition(tables, k, v) for k, v in items]
	if not isinstance(filters, list | tuple):
		raise FieldRefused("filters are a dict or a list of [field, operator, value] conditions.")
	conds = filters if isinstance(filters[0], list | tuple | dict) else [filters]
	if len(conds) > MAX_ENTRIES:
		raise FieldRefused(f"filters take at most {MAX_ENTRIES} conditions.")
	out = []
	for cond in conds:
		if isinstance(cond, dict):
			out += parse_filters(tables, cond)
			continue
		if not isinstance(cond, list | tuple) or len(cond) not in (2, 3, 4):
			raise FieldRefused(
				f"filter condition {str(cond)[:80]} is not accepted here: write [field, operator, value]."
			)
		if len(cond) == 4:
			if not isinstance(cond[0], str) or tables.resolve(cond[0]) is None:
				raise FieldRefused(
					f"filter condition {str(cond)[:80]}: {str(cond[0])[:60]!r} is not {tables.doctype} or one "
					"of its child tables."
				)
			key = f"`tab{tables.resolve(cond[0])}`.{cond[1]}" if isinstance(cond[1], str) else cond[1]
			out.append(_condition(tables, key, [cond[2], cond[3]]))
		elif len(cond) == 3:
			out.append(_condition(tables, cond[0], [cond[1], cond[2]]))
		else:
			out.append(_condition(tables, cond[0], cond[1]))
	return out
