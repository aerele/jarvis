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
_AGG_TEXT = re.compile(r"^(?P<func>[A-Za-z]{3,5})\s*\((?P<arg>.*)\)$", re.DOTALL)

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

	__slots__ = ("alias", "arg", "field", "func", "original", "star", "table")

	def __init__(self, *, table=None, field=None, func=None, alias=None, star=False, original=None, arg=None):
		self.table = table  # doctype holding the field (None for COUNT(*) and *)
		self.field = field  # field name, "*" for COUNT(*)
		self.func = func  # SUM / COUNT / AVG / MIN / MAX, or None
		self.alias = alias
		self.star = star
		self.original = original
		self.arg = arg  # an aggregate's argument as written ("`tabX`.f", "f", "*")

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
	if func not in AGGREGATES:
		raise FieldRefused(f"{func} is not one of {', '.join(AGGREGATES)}.")
	arg = arg.strip()
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
