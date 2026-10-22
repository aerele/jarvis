"""Per-unit valuation rate for an item under a given company /
warehouse, computed using the company's configured valuation method
(FIFO / Moving Average / LIFO).

Wraps ``erpnext.stock.get_item_details.get_valuation_rate``. Used by
margin / costing questions where the LLM keeps trying to derive cost
basis from arithmetic on Stock Ledger Entry rows; the underlying
helper applies the company's valuation method correctly and is the
same code path the standard ERPNext valuation reports use.

The helper applies no permission scoping, so the wrapper requires read on
the Item, a permitted ``company`` (Company User Permission), and, when a
``warehouse`` is named, read on it (User Permissions apply) with a permitted
Company for that Warehouse. A caller with Warehouse User Permissions must
name a warehouse, since the rate without one spans every warehouse.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._company_scope import assert_company_permitted
from jarvis.tools._ledger_access import (
	assert_item_readable,
	assert_warehouse_named_if_restricted,
	assert_warehouse_readable,
)


def get_valuation_rate(
	item_code: str,
	company: str,
	warehouse: str | None = None,
) -> dict:
	"""Return ``{rate}`` where ``rate`` is the valuation rate for
	``item_code`` under ``company`` (optionally narrowed to ``warehouse``).
	"""
	if not item_code:
		raise InvalidArgumentError("item_code is required")
	if not company:
		raise InvalidArgumentError("company is required")
	if not frappe.db.exists("Item", item_code):
		raise InvalidArgumentError(f"unknown Item: {item_code}")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")
	if warehouse and not frappe.db.exists("Warehouse", warehouse):
		raise InvalidArgumentError(f"unknown Warehouse: {warehouse}")
	assert_item_readable(item_code)
	assert_company_permitted(company)
	assert_warehouse_named_if_restricted(warehouse)
	if warehouse:
		assert_warehouse_readable(warehouse)

	from erpnext.stock.get_item_details import get_valuation_rate as _gvr

	result = _gvr(item_code=item_code, company=company, warehouse=warehouse)
	# ERPNext returns a dict {valuation_rate: <float>} or a bare number
	# depending on the call site; normalise to a single shape.
	if isinstance(result, dict):
		rate = result.get("valuation_rate") or 0
	else:
		rate = result or 0
	return {"rate": float(rate)}
