"""Resolve a date (or a fiscal year name) to the matching Fiscal Year
record for a company, with start / end dates.

Wraps ``erpnext.accounts.utils.get_fiscal_year``. Critical for GL range
queries the agent composes from natural language ("show me Q3 FY26
sales"): without this the agent invents fiscal-year boundaries based
on calendar year, which is wrong for any tenant whose FY isn't
Jan-Dec.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError


def get_fiscal_year(
	date: str | None = None,
	company: str | None = None,
	fiscal_year: str | None = None,
) -> dict:
	"""Return ``{fiscal_year, year_start_date, year_end_date}``.

	Pass ``date`` to resolve "what FY does this date land in?", or
	``fiscal_year`` to look the named year up directly. ``company`` is
	required when multiple FYs overlap for the date (multi-company
	benches).
	"""
	if not date and not fiscal_year:
		raise InvalidArgumentError("either date or fiscal_year is required")
	if company and not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")
	if fiscal_year and not frappe.db.exists("Fiscal Year", fiscal_year):
		raise InvalidArgumentError(f"unknown Fiscal Year: {fiscal_year}")

	from erpnext.accounts.utils import get_fiscal_year as _gfy

	fy_name, year_start_date, year_end_date = _gfy(
		date=date,
		fiscal_year=fiscal_year,
		company=company,
		verbose=0,  # suppress the UI error helper
		as_dict=False,
	)
	return {
		"fiscal_year": fy_name,
		"year_start_date": str(year_start_date) if year_start_date else None,
		"year_end_date": str(year_end_date) if year_end_date else None,
	}
