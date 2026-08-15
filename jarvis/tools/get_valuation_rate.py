"""Per-unit valuation rate for an item under a given company /
warehouse, computed using the company's configured valuation method
(FIFO / Moving Average / LIFO).

Wraps ``erpnext.stock.get_item_details.get_valuation_rate``. Used by
margin / costing questions where the LLM keeps trying to derive cost
basis from arithmetic on Stock Ledger Entry rows; the underlying
helper applies the company's valuation method correctly and is the
same code path the standard ERPNext valuation reports use.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
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
	if not frappe.has_permission("Item", "read", doc=item_code):
		raise PermissionDeniedError(f"no read permission on Item {item_code}")

	from erpnext.stock.get_item_details import get_valuation_rate as _gvr

	result = _gvr(item_code=item_code, company=company, warehouse=warehouse)
	# ERPNext returns a dict {valuation_rate: <float>} or a bare number
	# depending on the call site; normalise to a single shape.
	if isinstance(result, dict):
		rate = result.get("valuation_rate") or 0
	else:
		rate = result or 0
	return {"rate": float(rate)}
