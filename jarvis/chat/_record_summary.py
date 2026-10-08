"""Record summaries for confirmation cards: which fields to show, how to read them
safely, how to format them, and whether a proposed value actually changes anything.

``confirm_card.build_card`` owns card SHAPE; this module owns everything that
touches a doc or a value. Split out because the branching lives here and it needs
its own test surface.

Safety contract (see docs/confirmation-card-redesign-spec.md):
  - Every value is server-derived - from the perm-filtered dry-run ``would``, a
    perm-CHECKED + perm-filtered doc read, or the caller's own args. Never from
    model-authored prose: the card is the human's INDEPENDENT check on the agent.
  - ``frappe.get_doc`` checks NO permission unless a ``check_permission`` kwarg is
    passed (``frappe/model/document.py:141-145`` -> ``:336-349``), and it defaults
    to None. ``apply_fieldlevel_read_permissions`` is permlevel-only. So a doc read
    for a card MUST call ``has_permission`` itself.
  - Nothing here raises into a card: ``build_card`` is best-effort.
"""

from __future__ import annotations

import difflib
import json
import re

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, getdate

_MAX_VAL = 200  # a scalar display value
_MAX_ROWS = 20  # records / fields per record / child rows
_MAX_COLS = 8  # columns in a rendered child table (NOT _MAX_ROWS: 20 columns is unusable)
_MAX_TABLES = 3  # child tables rendered per record; the rest degrade to "N rows"
_MAX_BODY = 8_000  # a single long-form body (skill instructions, wiki body, one email)
_MAX_BULK_BODY = 2_000  # per-message body in a bulk-email card
_MAX_FLOOR = 8  # fields the meta floor selects for a stored record
# A sensitive update's line diff: SequenceMatcher (quadratic on repeated lines
# such as ``}``) only up to _MAX_DIFF_LINES lines in all, a linear changed-middle
# above that, and none above _MAX_DIFF_CHARS (both full texts still show).
_MAX_DIFF_LINES = 500
_MAX_DIFF_CHARS = 256 * 1024

# Characters a sensitive card shows as a visible marker (R2-8), by Unicode
# CATEGORY, never a hand-picked list: everything ``str.isprintable`` rejects
# (Cc controls, Cf format, Zl / Zp separators, Zs spaces other than U+0020, Cn
# unassigned, Co private use, Cs) except newline and tab, plus printable code
# points that draw as nothing: variation selectors, the combining grapheme
# joiner, Mongolian free variation selectors, Hangul fillers, the braille
# blank, the Khmer inherent vowels and the musical null notehead. A right-to-left override makes a script read differently from what
# runs; a lone carriage return or U+2028 hides a second statement on what looks
# like one line.
_DRAWS_NOTHING_RE = re.compile(
	"[\ufe00-\ufe0f\U000e0100-\U000e01ef\u034f\u180b-\u180f\u3164\u115f\u1160\uffa0\u2800"
	"\u17b4\u17b5\U0001d159]"
)
# The marker is ``⟦U+XXXX⟧``. Its opening bracket is itself always marked, so text
# typed to look like a marker can never pass for a real one.
_MARK_OPEN, _MARK_CLOSE = "\u27e6", "\u27e7"
# Line breaks other than ``\n`` (what ``str.splitlines`` also breaks on): code may
# run what follows them as a new line while the card draws one line.
WIDE_TABLE_NOTE = "This table is wider than the card. Scroll it sideways to see every column."
_WIDE_TABLE_COLS = 4  # more columns than this do not fit a phone
HIDDEN_BREAK_NOTE = "Contains a line break the card shows as a marker: the code may run it as a new line."
_HIDDEN_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")

# A Password field or an obviously-secret key name - masked, never rendered. This
# lives HERE, not in confirm_card, so the dependency runs one way only
# (confirm_card -> _record_summary). The primitive must never import the card
# module: a module-level import back would be circular.
_SECRET_KEY_RE = re.compile(r"password|secret|token|api[_-]?key", re.IGNORECASE)

# ``format_value`` emits HTML for these (newlines -> <br>, markdown -> HTML,
# Text Editor -> a ql-snow div). Both frontends render through ESCAPED
# interpolation, so these would show literal tags. HTML Editor / Code fall through
# format_value raw, so bypassing them too is equivalent and simpler.
_HTML_FIELDTYPES = frozenset(
	{
		"Text",
		"Small Text",
		"Long Text",
		"Markdown Editor",
		"Text Editor",
		"HTML Editor",
		"Code",
	}
)
_TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})


def _fieldtype(df) -> str | None:
	return getattr(df, "fieldtype", None) if df is not None else None


def is_secret(meta, key) -> bool:
	"""A Password field or an obviously-secret key name. ``meta`` may be None.

	Moved here from confirm_card so the primitive never imports the card module.
	"""
	if _SECRET_KEY_RE.search(str(key)):
		return True
	if meta:
		df = meta.get_field(key)
		if df and df.fieldtype == "Password":
			return True
	return False


def fmt(value, df=None, doc=None, limit: int | None = _MAX_VAL) -> str:
	"""A value as a short display string, formatted the way Frappe would show it.
	``limit=None`` keeps the whole value (a sensitive card, see ``fmt_full``).

	``translated=False`` is deliberate: format_value runs ``frappe._(value)`` on
	every string VALUE, not just labels (frappe/utils/formatters.py:59-60), so a
	non-English session could render something other than what is stored. Select
	options still translate inside their own branch, which is the only translation
	wanted.
	"""
	if isinstance(value, list):
		return f"{len(value)} row{'' if len(value) == 1 else 's'}"
	if isinstance(value, dict):
		return "..."
	fieldtype = _fieldtype(df)
	if fieldtype == "Check":
		# format_value has no Check branch (formatters.py:146) -> raw 1/0. cint is
		# mandatory: the string "0" is truthy and would render "Yes".
		out = "Yes" if cint(value) else "No"
	elif df is None or fieldtype in _HTML_FIELDTYPES:
		out = "" if value is None else cstr(value)
	else:
		try:
			out = cstr(frappe.format_value(value, df=df, doc=doc, translated=False))
		except Exception:
			# format_value raises for real: attribute access in get_field_currency
			# (meta.py:886), a bad link in get_cached_value (meta.py:897), an assert
			# in get_field_precision (meta.py:936).
			out = "" if value is None else cstr(value)
	return out if limit is None or len(out) <= limit else out[: limit - 1] + "…"


def _hides(ch: str) -> bool:
	if ch.isprintable():
		return ch == _MARK_OPEN or bool(_DRAWS_NOTHING_RE.match(ch))
	return ch not in "\n\t"


def visible(text: str) -> str:
	"""``text`` with every character that hides (see ``_DRAWS_NOTHING_RE``) shown as
	a ``⟦U+XXXX⟧`` marker. A carriage return directly before a newline is an
	ordinary (CRLF) line ending and stays; a LONE one is marked. Apply ONCE: the
	card's final pass does it (``confirm_card._visible_all``); a second pass would
	mark the markers."""
	out = []
	last = len(text) - 1
	for i, ch in enumerate(text):
		if _hides(ch) and not (ch == "\r" and i < last and text[i + 1] == "\n"):
			out.append(f"{_MARK_OPEN}U+{ord(ch):04X}{_MARK_CLOSE}")
		else:
			out.append(ch)
	return "".join(out)


def visible_char(ch: str) -> str:
	"""One character as a marker, whatever it is (a whitespace-only value on a
	sensitive card, ``confirm_card._visible_all``)."""
	return f"{_MARK_OPEN}U+{ord(ch):04X}{_MARK_CLOSE}"


def has_hidden_break(text: str) -> bool:
	"""Whether ``text`` holds a line break other than ``\n`` (see ``_HIDDEN_BREAKS``);
	a CRLF line ending does not count, a lone carriage return does."""
	return any(ch in _HIDDEN_BREAKS for ch in text.replace("\r\n", "\n"))


def _lines(text: str) -> list[str]:
	"""``text`` split on ``\n`` only, a CRLF ending's carriage return dropped (the last
	part has no newline after it, so a carriage return there is lone and stays)."""
	parts = text.split("\n")
	return [p[:-1] if p.endswith("\r") else p for p in parts[:-1]] + parts[-1:]


def fmt_full(value, df=None, doc=None) -> str:
	"""A value for a sensitive card (R2-8): never clipped, a JSON / list value
	written out rather than "..." / "N rows" (a child table is the caller's
	``table_rows``). The raw text: the card's final pass marks hidden characters.
	Secrets are the caller's job (``secret_state``)."""
	if isinstance(value, dict | list):
		return json.dumps(value, indent=1, ensure_ascii=False, default=str)
	return fmt(value, df, doc, limit=None)


def is_long(text: str) -> bool:
	"""Whether a full value needs its own block: several lines (``\n`` or a hidden
	break), or longer than an ordinary card would show."""
	return "\n" in text or len(text) > _MAX_VAL or has_hidden_break(text)


def secret_state(value, before=None, *, update: bool = False, stored: bool = False) -> str:
	"""What a sensitive card shows for a password or secret: "set" on a create,
	"changed" when an update replaces a stored one, nothing when it is empty, and
	for an all-``*`` value being WRITTEN (Frappe's dummy password, which a save
	leaves alone) "unchanged" on an update / "not set" on a create. A ``stored``
	value is the masked column (all ``*`` when set), so filled means "set". Never
	the value. A secret an update empties is "cleared"."""
	if not _filled(value):
		return "cleared" if update and _filled(before) else ""
	if stored:
		return "set"
	if set(cstr(value)) == {"*"}:
		return "unchanged" if update else "not set"
	return "changed" if update and _filled(before) else "set"


def _filled(value) -> bool:
	return value is not None and cstr(value).strip() != ""


def secret_word(meta, fieldname, value, before=None, *, update: bool = False) -> str:
	"""``secret_state`` for the field ``fieldname`` of ``meta``, by what Frappe really
	does with it: the ONE rule for a secret a write carries, top-level or in a row.

	Only a Password field is masked. Frappe leaves a Password of all asterisks alone
	(base_document.py ``_save_passwords``: its own dummy), so that reads "unchanged"
	(or "not set" on a create). A field that is secret by its NAME only (a Data
	field called ``api_token``) has no mask: all asterisks are an ordinary value and
	are stored, so they read "changed" (or "set"), and the stored value is the real
	one, so an echo of it is "unchanged". A field with no meta is judged as a
	Password, as before."""
	df = meta.get_field(fieldname) if meta else None
	if df is None or df.fieldtype == "Password" or not _filled(value):
		return secret_state(value, before, update=update)
	if update and _filled(before):
		return "unchanged" if cstr(value) == cstr(before) else "changed"
	return "set"


def line_diff(old: str, new: str, context: int = 2, force: bool = False) -> list[dict] | None:
	"""A line diff for a long value on a sensitive update card:
	``[{"op": " " | "-" | "+", "text"} | {"op": "gap", "text": "", "count": n}]``,
	with ``context`` unchanged lines around each change and a ``gap`` naming how
	many unchanged lines are left out. Lines split on ``\n`` ONLY (a CRLF ending's
	carriage return dropped): a hidden break (lone ``\r``, U+2028 ...) stays inside
	its line, where the card marks it.
	None when neither side has several lines (unless ``force``: a hidden break),
	nothing changed, or the texts are over ``_MAX_DIFF_CHARS``. Above
	``_MAX_DIFF_LINES`` the diff is the linear changed-middle (common first and
	last lines kept), because SequenceMatcher is quadratic on repeated lines. The
	card carries both full texts beside it."""
	if old == new or (not force and "\n" not in old and "\n" not in new):
		return None
	if len(old) + len(new) > _MAX_DIFF_CHARS:
		return None
	a, b = _lines(old), _lines(new)
	if a == b:
		return None  # only CRLF / LF line endings differ: the caller names that
	if len(a) + len(b) > _MAX_DIFF_LINES:
		groups = [_changed_middle(a, b, context)]
	else:
		groups = list(difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context))
	out = []
	seen = 0  # lines of ``a`` already shown or counted
	for group in groups:
		if group[0][1] > seen:
			out.append({"op": "gap", "text": "", "count": group[0][1] - seen})
		for tag, i1, i2, j1, j2 in group:
			if tag == "equal":
				out.extend({"op": " ", "text": t} for t in a[i1:i2])
				continue
			if tag in ("replace", "delete"):
				out.extend({"op": "-", "text": t} for t in a[i1:i2])
			if tag in ("replace", "insert"):
				out.extend({"op": "+", "text": t} for t in b[j1:j2])
		seen = group[-1][2]
	if seen < len(a):
		out.append({"op": "gap", "text": "", "count": len(a) - seen})
	return out


def _changed_middle(a: list, b: list, context: int) -> list:
	"""One group of opcodes for a large diff, in linear time: the common first and
	last lines are equal, everything between is replaced."""
	head = 0
	while head < min(len(a), len(b)) and a[head] == b[head]:
		head += 1
	tail = 0
	while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
		tail += 1
	start = max(0, head - context)
	end_a, end_b = len(a) - tail, len(b) - tail
	group = [("equal", start, head, start, head)] if head > start else []
	group.append(("replace", head, end_a, head, end_b))
	keep = min(context, tail)
	if keep:
		group.append(("equal", end_a, end_a + keep, end_b, end_b + keep))
	return group


def _cast_date(value):
	# getdate() returns TODAY for ANY falsy input (frappe/utils/data.py:125-126), so a
	# bare getdate would make "set the due date to today" on an unset field compare
	# equal to itself and VANISH from the card. `not value` rather than `in (None, "")`
	# so 0/False take the same branch instead of silently becoming today.
	if not value:
		return None
	return getdate(value)


def _cast_datetime(value):
	# get_datetime(None) returns now() (data.py:164-165) - two calls microseconds
	# apart are unequal, so None vs None would render a phantom "(empty) -> (empty)".
	if not value:
		return None
	out = get_datetime(value)
	if out is None:
		# get_datetime returns None for an INVALID string rather than raising, so two
		# different pieces of garbage would compare equal. Force the changed branch.
		raise ValueError(f"not a datetime: {value!r}")
	return out


# Cast a value the way the SAVE would, per fieldtype. Deliberately NOT the display
# form: fmt_money at precision 2 renders 100.005 and 100.001 identically, so a
# display compare drops a REAL change from a gated card. flt takes no precision
# here - rounding is exactly what caused that bug.
_CAST = {
	"Currency": flt,
	"Float": flt,
	"Percent": flt,
	"Int": cint,
	"Long Int": cint,
	"Check": cint,
	"Date": _cast_date,
	"Datetime": _cast_datetime,
}


def same_value(a, b, df=None) -> bool:
	"""True when saving ``b`` over a stored ``a`` would not change the stored value.

	Answers the question the no-op guard actually exists for, rather than "do these
	two render the same". Comparing DISPLAY forms hides real changes; comparing raw
	values shows phantom ones (the DB holds a typed 100.0, the model sends "100").
	Casting both sides the way the save does gets both right.

	An uncastable value counts as CHANGED - never hide a row because we could not
	compare it.
	"""
	cast = _CAST.get(_fieldtype(df), cstr)
	try:
		return cast(a) == cast(b)
	except Exception:
		return False


def pick_fields(meta) -> list[str]:
	"""The meta floor: which fields identify a STORED record at a glance.

	Selection only - never used for proposed content, which renders whole (a
	proposed field outside the floor is one the save will write, so hiding it would
	let you approve a value you never saw).

	Order: title, list-view columns, mandatory fields, status, grand_total/total.
	Frappe's own link_preview (frappe/desk/link_preview.py) falls back to ``reqd``
	when no preview fields are set - a record's required fields are its essence -
	and that is the idea borrowed here. ``docstatus`` is not a DocField, so it is
	not selectable; summary_rows renders it as a synthetic row instead.
	"""
	if not meta:
		return []
	out: list[str] = []
	seen: set[str] = set()

	def add(fieldname):
		if not fieldname or fieldname == "name" or fieldname in seen:
			return
		df = meta.get_field(fieldname)
		if df is None or df.fieldtype in _TABLE_FIELDTYPES:
			return
		seen.add(fieldname)
		out.append(fieldname)

	try:
		add(meta.get_title_field())
	except Exception:
		pass
	try:
		for fieldname in meta.get_list_fields() or []:
			add(fieldname)
	except Exception:
		pass
	for df in meta.fields or []:
		if df.reqd:
			add(df.fieldname)
	if getattr(meta, "is_submittable", 0):
		add("status")
	for fieldname in ("grand_total", "total"):
		if meta.has_field(fieldname):
			add(fieldname)
			break

	def is_long(fieldname):
		df = meta.get_field(fieldname)
		return bool(df) and df.fieldtype in _HTML_FIELDTYPES

	ordered = [f for f in out if not is_long(f)] + [f for f in out if is_long(f)]
	return ordered[:_MAX_FLOOR]


_DOCSTATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def summary_rows(doctype: str, name: str, full: bool = False) -> dict | None:
	"""``{"title", "rows"}`` for a STORED record, or None when it is missing or the
	caller cannot read it. ``full`` (a sensitive card) keeps each value it shows
	whole; the selection is still the meta floor below.

	The caller renders a NAME-ONLY row on None. ``title`` is returned only from this
	function's successful, permission-checked path - never source a card header from
	a separate db.get_value(title_field): that read is unchecked and the title field
	is typically the party name, i.e. the data being protected.
	"""
	try:
		# Fresh DB load, NOT get_cached_doc: a cache could serve sandbox-mutated
		# state from the park-time dry-run.
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		# frappe.throw leaves an entry in message_log that would leak into the turn.
		frappe.clear_messages()
		return None
	except Exception:
		frappe.clear_messages()
		return None

	# get_doc checks NO permission unless the check_permission kwarg is passed
	# (document.py:141-145 -> :336-349, defaults to None). has_permission returns a
	# bool; the kwarg throws, and a card must degrade rather than raise.
	try:
		if not doc.has_permission("read"):
			return None
	except Exception:
		return None

	try:
		doc.apply_fieldlevel_read_permissions()
	except Exception:
		return None

	meta = doc.meta
	rows = []
	if full:
		rows, tables = _whole_record(meta, doc)
		return {"title": _title(meta, doc), "rows": rows, "tables": tables}
	for fieldname in pick_fields(meta):
		# A permlevel-restricted field is delattr'd by the filter above
		# (document.py:1264) and is dropped by the confirmed save too - never show a
		# phantom value.
		if not hasattr(doc, fieldname):
			continue
		value = doc.get(fieldname)
		if value is None or (not isinstance(value, list) and cstr(value).strip() == ""):
			continue
		df = meta.get_field(fieldname)
		label = df.label if df and df.label else fieldname
		rows.append(value_row(meta, fieldname, label, value, df, doc, full, stored=True))
		if len(rows) >= _MAX_ROWS:
			break

	if getattr(meta, "is_submittable", 0):
		# docstatus is not a DocField, so pick_fields cannot select it. It is appended
		# AFTER the capped loop, so reserve its slot rather than exceed _MAX_ROWS -
		# "bounded, no unbounded axis" is an invariant, and a cap the code quietly
		# overshoots by one is a cap nobody can trust.
		rows = rows[: _MAX_ROWS - 1]
		rows.append(
			{
				"label": "Docstatus",
				"value": _DOCSTATUS_LABEL.get(cint(doc.get("docstatus")), "?"),
			}
		)

	return {"title": _title(meta, doc), "rows": rows}


def _title(meta, doc) -> str:
	try:
		title_field = meta.get_title_field()
		if title_field and title_field != "name" and hasattr(doc, title_field):
			return fmt(doc.get(title_field), meta.get_field(title_field), doc)
	except Exception:
		pass
	return ""


def _whole_record(meta, doc) -> tuple[list[dict], list[dict]]:
	"""``(rows, tables)``: every non-empty field of a stored record, whole (a
	sensitive delete card: deleting a Server Script shows its script). A value that
	is only whitespace is kept (the card marks it). Child tables are full tables
	(``table_rows``: every row and column, secrets "set", a hidden-break ``note``),
	so their cells get the same marker pass as any value. A permlevel-restricted
	field (dropped by the read filter) is skipped. Bounded by the gate's size
	refusal."""
	from frappe.model import no_value_fields

	rows, tables = [], []
	for df in meta.fields or []:
		if df.fieldtype in no_value_fields and df.fieldtype not in _TABLE_FIELDTYPES:
			continue
		if not hasattr(doc, df.fieldname):
			continue
		value = doc.get(df.fieldname)
		if df.fieldtype in _TABLE_FIELDTYPES:
			table = table_rows(
				meta,
				df.fieldname,
				[r.as_dict() if hasattr(r, "as_dict") else r for r in value or []],
				True,
				stored=True,
			)
			if table:
				tables.append(table)
			continue
		if value is None or cstr(value) == "":
			continue
		rows.append(
			value_row(meta, df.fieldname, df.label or df.fieldname, value, df, doc, True, stored=True)
		)
	if getattr(meta, "is_submittable", 0):
		rows.append({"label": "Docstatus", "value": _DOCSTATUS_LABEL.get(cint(doc.get("docstatus")), "?")})
	return rows, tables


class SecretWord(str):
	"""What a card says for a secret cell whose state is already decided ("set",
	"changed", "cleared", "unchanged"): drawn as it is, never judged again and never
	a value."""


def value_row(
	meta, fieldname, label, value, df=None, doc=None, full: bool = False, limit=_MAX_VAL, stored: bool = False
) -> dict:
	"""One ``{"label", "value"}`` row. A secret is masked ("[hidden]", or "set" on a
	sensitive card); a ``full`` row keeps the whole value and marks a long one
	``multiline`` so the clients give it its own block."""
	if isinstance(value, SecretWord):
		return {"label": label, "value": str(value) if full else "[hidden]"}
	if is_secret(meta, fieldname):
		if not full:
			return {"label": label, "value": "[hidden]"}
		word = secret_state(value, stored=True) if stored else secret_word(meta, fieldname, value)
		return {"label": label, "value": word}
	if not full:
		return {"label": label, "value": fmt(value, df, doc, limit)}
	text = fmt_full(value, df, doc)
	row = {"label": label, "value": text}
	if is_long(text):
		row["multiline"] = True
	if has_hidden_break(text):
		row["note"] = HIDDEN_BREAK_NOTE
	return row


def values_rows(meta, values: dict, limit: int = _MAX_VAL, full: bool = False) -> dict:
	"""``{"rows", "extra"}`` for PROPOSED content (a create's values, an email body).

	Renders EVERY key, in caller order, capped with an explicit remainder (no cap
	and no clip when ``full``, a sensitive card). Never
	filtered to the floor: a proposed field outside the floor is one the save will
	write, so hiding it would let a human approve a value they never saw. Caller
	order is preserved so the card stays comparable against the request.

	No perm filter is possible or needed - the values are the caller's own args.
	"""
	if not isinstance(values, dict):
		return {"rows": [], "extra": 0}
	rows = []
	keys = list(values)
	for fieldname in keys if full else keys[:_MAX_ROWS]:
		df = meta.get_field(fieldname) if meta else None
		label = df.label if df and df.label else fieldname
		rows.append(value_row(meta, fieldname, label, values.get(fieldname), df, None, full, limit))
	return {"rows": rows, "extra": max(0, len(keys) - len(rows))}


def stored_rows(rows) -> list:
	"""Child rows as the caller would write them: a stored or resolved row's own
	bookkeeping (name, parent, idx, owner ...) removed, so a sensitive card's
	table shows the values, not the metadata."""
	from frappe.model import child_table_fields, default_fields

	drop = {*default_fields, *child_table_fields}
	return [{k: v for k, v in r.items() if k not in drop} for r in rows if isinstance(r, dict)]


def table_rows(meta, fieldname: str, rows: list, full: bool = False, stored: bool = False) -> dict | None:
	"""A PROPOSED child table as a real table, or None when there is nothing to show.

	Columns are the child doctype's ``in_list_view`` order UNION every key the caller
	actually set. in_list_view alone would re-break "proposed content renders whole"
	inside this primitive: Sales Invoice Item's list columns are item/qty/rate/amount,
	so a batch also setting income_account on every row would write it invisibly.

	A key that is not a real child field is DROPPED by the save (it fails
	valid_columns), so it is counted in ``unknown_columns`` rather than rendered as
	a value that will persist.

	STORED child tables keep "N rows" (fmt's list branch) - a delete card does not
	need line items to identify the order.

	``full`` (a sensitive card): every row and column, values whole, the rows'
	own bookkeeping dropped, secrets as "set" (``stored``: rows read from the
	database, whose secret columns are masked).
	"""
	if not meta or not isinstance(rows, list) or not rows:
		return None
	if full:
		rows = stored_rows(rows)
	df = meta.get_field(fieldname)
	if df is None or df.fieldtype not in _TABLE_FIELDTYPES or not df.options:
		return None
	try:
		child = frappe.get_meta(df.options)
	except Exception:
		return None

	proposed: list[str] = []
	unknown: set[str] = set()  # a SET: an unknown key never enters `proposed`, so a
	# counter would re-increment once per row (20 rows -> unknown_columns=20).
	for row in rows:
		if not isinstance(row, dict):
			continue
		for key in row:
			if key in proposed or key in unknown:
				continue
			if child.get_field(key) is None:
				unknown.add(key)
				continue
			proposed.append(key)

	listed = [d.fieldname for d in child.fields if d.in_list_view and d.fieldname in proposed]
	columns = listed + [f for f in proposed if f not in listed]
	shown_cols = columns if full else columns[:_MAX_COLS]  # NOT _MAX_ROWS - 20 columns is unusable

	out_rows = []
	hidden_break = False  # a cell holds a lone carriage return / U+2028 ... (full only)
	for row in rows if full else rows[:_MAX_ROWS]:
		if not isinstance(row, dict):
			continue
		cells = []
		for key in shown_cols:
			cell = value_row(child, key, "", row.get(key), child.get_field(key), None, full, stored=stored)
			cells.append(cell["value"])
			hidden_break = hidden_break or "note" in cell
		out_rows.append({"cells": cells})

	out = {
		"label": df.label or fieldname,
		"count": len(rows),
		# ``columns`` is labels (what the card renders); ``fieldnames`` is the
		# parallel machine-readable list - tests assert on it, and phases 3-4 use it.
		"columns": [(child.get_field(c).label or c) for c in shown_cols],
		"fieldnames": shown_cols,
		"rows": out_rows,
		"extra": max(0, len(rows) - len(out_rows)),
		"extra_columns": max(0, len(columns) - len(shown_cols)),
		"unknown_columns": len(unknown),
	}
	notes = [HIDDEN_BREAK_NOTE] if hidden_break else []
	if full and len(shown_cols) > _WIDE_TABLE_COLS:
		# A full card never drops a column, so a wide table scrolls sideways inside
		# the card; nothing else on a phone or the board says so.
		notes.append(WIDE_TABLE_NOTE)
	if notes:
		out["note"] = " ".join(notes)
	return out
