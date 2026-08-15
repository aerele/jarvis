"""Stock quantity for an item at a warehouse as of a given date.

Wraps ``erpnext.stock.utils.get_stock_balance``. That function enforces
``frappe.has_permission("Item", "read", throw=True)`` internally before
querying the stock ledger, so this wrapper just translates argument
shape; permission gating is owned by the underlying ERPNext helper.

This is one of the "computed-truth" reads LLMs hallucinate when forced
to derive from raw Stock Ledger Entry rows: the right answer depends on
posting date + valuation method + inventory dimensions, and an agent
walking the ledger by hand will silently miss in-flight cancellations,
batch / serial reservations, and FIFO chains.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError


def get_stock_balance(
	item_code: str,
	warehouse: str,
	posting_date: str | None = None,
	with_valuation_rate: bool = False,
) -> dict:
	"""Return current stock for ``item_code`` at ``warehouse``.

	``posting_date`` defaults to today when omitted. Returns
	``{qty}`` by default; with ``with_valuation_rate=True`` returns
	``{qty, rate}`` where ``rate`` is the FIFO/moving-average rate
	ERPNext computes alongside the qty (no extra DB pass).
	"""
	if not item_code:
		raise InvalidArgumentError("item_code is required")
	if not warehouse:
		raise InvalidArgumentError("warehouse is required")
	if not frappe.db.exists("Item", item_code):
		raise InvalidArgumentError(f"unknown Item: {item_code}")
	if not frappe.db.exists("Warehouse", warehouse):
		raise InvalidArgumentError(f"unknown Warehouse: {warehouse}")

	from erpnext.stock.utils import get_stock_balance as _gsb

	result = _gsb(
		item_code=item_code,
		warehouse=warehouse,
		posting_date=posting_date,
		with_valuation_rate=bool(with_valuation_rate),
	)
	if with_valuation_rate:
		qty, rate = result
		return {"qty": float(qty or 0), "rate": float(rate or 0)}
	return {"qty": float(result or 0)}
