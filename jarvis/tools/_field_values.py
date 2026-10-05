"""The field type check that runs before a create / update writes (R2-3).

A thin wrapper over Frappe's own coercion: it rejects only what Frappe would reject
or silently corrupt, and it accepts everything Frappe accepts.

- Int: Frappe stores ``cint(value)``, which turns ``"abc"`` (and ``"1,234"``) into 0
  without a word (``base_document.get_valid_dict``). Rejected; ``True``, ``"12.0"``
  and ``12`` pass, and a grouped ``"1,234"`` / ``"1,00,000"`` on a dot-decimal site
  is passed on without its commas.
- Check: Frappe stores ``1 if cint(value) else 0``; read as ``cint(sbool(value))``
  (true / "true" / "Yes" -> 1, false / "No" -> 0, a number as ``cint`` reads it:
  0.5 -> 0, 2 / -1 -> 1). A word ``cint`` would store as 0 is rejected.
- Float / Currency / Percent: Frappe stores ``flt(value)`` (commas dropped, garbage
  becomes 0.0). ``"1,234.5"`` passes; ``"abc"`` is rejected, and so is a comma when
  the site's number format uses it as the decimal separator (``flt`` would read
  ``"1.234,5"`` as 1.2345); a comma before the last dot (``"1,234.5"``) can only be
  grouping and passes.
- Date / Datetime: Frappe does not parse these; the database refuses anything that
  is not ISO (error 1292). A value that starts with a 4-digit year is read
  year-month-day (an impossible month or day is rejected), never day-first; any
  other value is read the way ``getdate`` reads it, day-first on a day-first site
  (``"3 Oct 2026"``, ``"03-10-2026"``), and only when it names a day, a month AND a
  year: dateutil would fill a missing part from today (``"Oct 2026"``, ``"10"``,
  ``"9:30"``), so a partial date is rejected. It is passed on in ISO form, as a Desk
  form sends it. A value with a time zone is converted to the site's zone and the
  offset dropped (MariaDB refuses it); a Date takes the site-local day.
- Time: ``H:M[:S]`` with an optional AM / PM, or ``9 PM``; anything that would not
  read back as written (``"9.30"``, ``"24:00:00"``, a bare ``"9"``) is rejected,
  never turned into midnight.
- Select: the value, stripped, must be one of the stored options, exactly as
  ``base_document._validate_selects`` checks it (untranslated options, so a site in
  another language accepts the same values). Skipped where Frappe skips it
  (``naming_series``, empty options, a falsy value) and for ``status``, which controllers set
  themselves before Frappe validates the Select.

Child rows (Table fields) are checked against the child doctype, and the message
names the table and row. A rejection raises ``InvalidFieldValueError`` (FIXABLE)
naming the field and the bad value, before anything is written."""

from __future__ import annotations

import datetime
import math
import re

import frappe
from frappe.utils import cstr

from jarvis.exceptions import InvalidFieldValueError

_INT_TYPES = frozenset({"Int", "Long Int"})
_FLOAT_TYPES = frozenset({"Float", "Currency", "Percent"})
_DATE_TYPES = frozenset({"Date", "Datetime", "Time"})
# Selects whose value a controller sets during validate, before Frappe checks it.
_CONTROLLER_SET_SELECTS = frozenset({"naming_series", "status"})
_SHOWN = 80  # characters of a rejected value quoted back


def check_values(doctype: str, values: dict, *, where: str = "") -> dict:
	"""``values`` for ``doctype`` with dates in ISO form, or ``InvalidFieldValueError``.

	Only the fields present are checked (an update checks its changes, not the stored
	row). Unknown fieldnames and non-dict input pass through untouched: the write
	itself reports those as it always has. Nothing to rewrite: the SAME objects come
	back (callers such as the File Box classifier see the dicts the insert mutates,
	exactly as before); a rewritten date comes back in a copy."""
	if not isinstance(values, dict) or frappe.flags.in_import:
		return values
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return values
	changed = {}
	for fieldname, value in values.items():
		df = meta.get_field(fieldname) if isinstance(fieldname, str) else None
		if not df:
			continue
		if df.fieldtype == "Table" and isinstance(value, list):
			label = f"{where}{df.label or fieldname}"
			rows = [
				check_values(df.options, row, where=f"{label} row {i}: ") if isinstance(row, dict) else row
				for i, row in enumerate(value, 1)
			]
			if any(new is not old for new, old in zip(rows, value, strict=True)):
				changed[fieldname] = rows
		else:
			checked = _check(df, value, where)
			if checked is not value:
				changed[fieldname] = checked
	return {**values, **changed} if changed else values


def _check(df, value, where: str):
	if value is None or (isinstance(value, str) and not value.strip()):
		return value
	fieldtype = df.fieldtype
	if fieldtype == "Check":
		return _check_box(df, value, where)
	if fieldtype in _INT_TYPES:
		grouped = _ungrouped(value)
		if _whole_number(grouped) is None:
			_reject(df, value, where, "must be a whole number")
		return grouped
	elif fieldtype in _FLOAT_TYPES:
		if not _is_number(value):
			_reject(df, value, where, "must be a number (for example 1234.5)")
	elif fieldtype in _DATE_TYPES:
		return _iso(df, value, where)
	elif fieldtype == "Select":
		_check_select(df, value, where)
	return value


_TRUE = frozenset({"true", "yes", "y", "on"})
_FALSE = frozenset({"false", "no", "n", "off"})


def _check_box(df, value, where: str):
	"""Frappe stores ``1 if cint(value) else 0``. ``cint(sbool(value))`` reads true /
	"true" / "Yes" as 1 and false / "No" as 0, any other number as set or not; a word
	``cint`` would silently store as 0 ("abc") is rejected."""
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, str):
		word = value.strip().lower()
		if word in _TRUE:
			return 1
		if word in _FALSE:
			return 0
	number = _number(value)
	if number is None:
		_reject(df, value, where, "must be checked or not (1 / 0, true / false, yes / no)")
	return 1 if int(number) else 0  # cint: 0.5 is 0, -1 and 2 are set


def _number(value) -> float | None:
	if isinstance(value, bool):
		return float(value)
	if isinstance(value, int | float):
		return float(value) if math.isfinite(value) else None
	if isinstance(value, str):
		try:
			number = float(value.strip())
		except ValueError:
			return None
		return number if math.isfinite(number) else None
	return None


def _whole_number(value) -> int | None:
	"""What ``cint`` would store, or None when ``cint`` would silently store 0."""
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, int):
		return value
	if isinstance(value, float):
		return int(value) if math.isfinite(value) and value.is_integer() else None
	if isinstance(value, str):
		text = value.strip()
		try:
			return int(text)
		except ValueError:
			pass
		try:
			number = float(text)
		except ValueError:
			return None
		return int(number) if math.isfinite(number) and number.is_integer() else None
	return None


# Thousands grouping as written on a dot-decimal site: 1,234 / 1,00,000 / 12,345.50.
_GROUPED = re.compile(r"^[-+]?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?$")


def _ungrouped(value):
	"""A grouped number on a dot-decimal site without its commas (``cint`` would read
	``"1,234"`` as 0); anything else unchanged."""
	if isinstance(value, str) and _GROUPED.match(value.strip()) and _decimal_separator() == ".":
		return value.strip().replace(",", "")
	return value


def _decimal_separator() -> str:
	"""The site number format's decimal separator, read from Frappe's own table (v16
	``number_format.NUMBER_FORMAT_MAP``, v15 ``data.number_format_info``), without
	the v16 deprecation warning of ``get_number_format_info``."""
	try:
		from frappe.utils.number_format import NUMBER_FORMAT_MAP as formats
	except ImportError:
		from frappe.utils.data import number_format_info as formats
	try:
		number_format = frappe.db.get_default("number_format") or "#,###.##"
		return (formats.get(number_format) or (".", ",", 2))[0] or "."
	except Exception:
		return "."


def _is_number(value) -> bool:
	"""Whether ``flt`` reads ``value`` as the number it says."""
	if isinstance(value, bool | int):
		return True
	if isinstance(value, float):
		return math.isfinite(value)
	if not isinstance(value, str):
		return False
	text = value.strip()
	if "," in text and _decimal_separator() == ",":
		# flt drops every comma: "1.234,5" would be stored as 1.2345. A comma before
		# the last dot ("1,234.5") can only be grouping, so it reads as written.
		if "." not in text or text.rindex(",") > text.rindex("."):
			return False
	try:
		number = float(text.replace(",", ""))
	except ValueError:
		return False
	return math.isfinite(number)


def _day_first() -> bool:
	return cstr(frappe.db.get_default("date_format")).lower().startswith("dd")


_SHAPES = {"Date": "YYYY-MM-DD", "Datetime": "YYYY-MM-DD HH:MM:SS", "Time": "HH:MM:SS"}
# A value that starts with a 4-digit year is read year-month-day, never day-first:
# dateutil told the day comes first reads "2026-10-3" as 10 March.
_YEAR_FIRST = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ](.+))?$")
# A clock time: H:M[:S[.ffffff]] with an optional AM / PM, or an hour with AM / PM
# ("9 PM"). Anything else ("9.30", "24:00:00", a bare "9") is rejected rather than
# read as midnight.
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,6}))?)?)?\s*([AaPp]\.?[Mm]\.?)?$")


class _Unreadable(ValueError):
	pass


def _iso(df, value, where: str):
	"""The value in the ISO form the database takes, or a rejection naming the field."""
	if isinstance(value, datetime.datetime | datetime.date | datetime.time | datetime.timedelta):
		return value
	if not isinstance(value, str):
		_reject(df, value, where, f"must be a {df.fieldtype.lower()}")
	parsed = _quietly(_parse, df.fieldtype, value.strip())
	if parsed is None:
		_reject(df, value, where, f"must be a {df.fieldtype.lower()} ({_SHAPES[df.fieldtype]})")
	if parsed is _UNPARSED:
		return value  # Frappe's own "no date" strings (0000-00-00): left to Frappe
	iso = str(parsed)
	return value if iso == value else iso


_UNPARSED = object()


def _parse(fieldtype: str, text: str):
	from frappe.utils import is_invalid_date_string

	if fieldtype == "Time":
		return _clock(text)
	if is_invalid_date_string(text):
		return _UNPARSED
	year_first = _YEAR_FIRST.match(text)
	if year_first:
		year, month, day, rest = year_first.groups()
		date = datetime.date(int(year), int(month), int(day))  # an impossible month / day raises
		moment = datetime.datetime.combine(date, _clock_or_iso_time(rest) if rest else datetime.time())
	else:
		moment = _complete_date(text)
	moment = _site_local(moment)
	return moment.date() if fieldtype == "Date" else moment


# Two defaults that differ in day, month AND year: dateutil fills a part the text
# leaves out from its default, so a part missing from the text shows as a difference.
_DEFAULTS = (datetime.datetime(2001, 1, 1), datetime.datetime(2002, 2, 2))


def _complete_date(text: str) -> datetime.datetime:
	"""``text`` read the way Frappe's ``getdate`` reads it (day-first on a day-first
	site), only when it carries a day, a month and a year. dateutil would fill a
	missing one from today ("Oct 2026" -> the 4th, "10" -> the 10th of this month)."""
	from dateutil import parser

	day_first = _day_first()
	first, second = (parser.parse(text, dayfirst=day_first, default=d) for d in _DEFAULTS)
	if first.date() != second.date():
		raise _Unreadable(text)
	return first


def _clock(text: str) -> datetime.time:
	"""A time that round-trips its input, or ``_Unreadable``."""
	m = _CLOCK.match(text)
	if not m:
		raise _Unreadable(text)
	hour, minute, second, fraction, meridiem = m.groups()
	if minute is None and not meridiem:
		raise _Unreadable(text)  # a bare number is not a time
	hour, minute = int(hour), int(minute or 0)
	if meridiem:
		if not 1 <= hour <= 12:
			raise _Unreadable(text)
		hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
	return datetime.time(hour, minute, int(second or 0), int((fraction or "0").ljust(6, "0")))


def _clock_or_iso_time(text: str) -> datetime.time:
	"""The time part of a year-first datetime (an ISO offset allowed: ``10:00+05:30``)."""
	try:
		return _clock(text)
	except _Unreadable:
		return datetime.time.fromisoformat(text.replace("Z", "+00:00"))


def _site_local(moment: datetime.datetime) -> datetime.datetime:
	"""A datetime with a time zone (``Z``, ``+05:30``) in the site's own zone, offset
	dropped: MariaDB refuses the offset, and Frappe stores site-local times."""
	if moment.tzinfo is None:
		return moment
	from zoneinfo import ZoneInfo

	from frappe.utils import get_system_timezone

	return moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def _quietly(fn, *args):
	"""``fn(*args)``, None on any error; whatever it msgprinted is dropped (getdate
	logs "Invalid Date" before raising, which would otherwise ride the response)."""
	log = getattr(frappe.local, "message_log", None)
	mark = len(log) if log is not None else 0
	try:
		return fn(*args)
	except Exception:
		return None
	finally:
		log = getattr(frappe.local, "message_log", None)
		if log is not None and len(log) > mark:
			del log[mark:]


def _check_select(df, value, where: str) -> None:
	# Frappe skips a falsy value (0, False) the same way (``_validate_selects``).
	if not value or df.fieldname in _CONTROLLER_SET_SELECTS or not df.options:
		return
	options = df.options.split("\n")
	if not any(options):
		return
	if cstr(value).strip() not in options:
		listed = ", ".join(f'"{o}"' for o in options if o)
		_reject(df, value, where, f"must be one of {listed}")


def _reject(df, value, where: str, rule: str) -> None:
	label = df.label or df.fieldname
	name = f"{label} ({df.fieldname})" if label != df.fieldname else df.fieldname
	shown = cstr(value)
	if len(shown) > _SHOWN:
		shown = shown[:_SHOWN] + "..."
	raise InvalidFieldValueError(f"{where}{name} {rule}; got '{shown}'.")
