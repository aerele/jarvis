"""The weekly-off collision lookup.

ERPNext's "Get Local Holidays" skips a date that already has a row, so a festival that
falls on a weekly off stays "Sunday". For such a row the caller asks this module what the
country's own calendar calls that date, then runs the name through ``match``.

Best effort by design: a missing `holidays` package, a country or subdivision it does not
know, or any error at all gives None, and the rule is silently skipped. Which country and
subdivision to ask for (the resolved Holiday List's, else the Company's) is the caller's
decision; this module only provides the lookups.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import frappe


def calendar_name(on_date: date, country_code: str | None, subdivision: str | None = None) -> str | None:
	"""The name python-holidays gives ``on_date`` in that country, or None."""
	if not country_code:
		return None
	try:
		return _year_map(country_code.upper(), subdivision or None, on_date.year).get(on_date)
	except Exception:
		return None


@lru_cache(maxsize=64)
def _year_map(code: str, subdivision: str | None, year: int) -> dict:
	"""{date: name} for one country, subdivision and year; {} when it cannot be built.

	The language is always pinned to English. Never use ``frappe.local.lang`` (as ERPNext
	does): "hi" or "mr" names never match. Old `holidays` releases may lack the language,
	so it is retried without one. A subdivision the package does not know (a typo, or a code
	from another standard) is retried as the country alone: national festivals still match."""
	try:
		import holidays

		language = "en_IN" if code == "IN" else "en_US"
		for subdiv in (subdivision, None) if subdivision else (None,):
			for extra in ({"language": language}, {}):
				try:
					return dict(holidays.country_holidays(code, subdiv=subdiv, years=[year], **extra))
				except NotImplementedError:
					break  # a country or subdivision the package does not know
				except Exception:
					continue
	except Exception:
		pass
	return {}


def country_code_for(country_name: str | None) -> str | None:
	"""The ISO code stored on the Country record, uppercased, or None."""
	if not country_name:
		return None
	try:
		code = frappe.get_cached_value("Country", country_name, "code")
	except Exception:
		return None
	return code.upper() if code else None
