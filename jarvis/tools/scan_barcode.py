"""Resolve a barcode (or serial / batch number) to its Item context.

Wraps ``erpnext.stock.utils.scan_barcode``. Used in mobile / kiosk
workflows where the agent receives a raw scanned value and needs to
know what item it is, what serial / batch is implied, and what
warehouse the scan happened in.

Read-only - no DB writes, just a lookup. The underlying erpnext helper
performs NO permission checks of its own (verified by reading
``erpnext.stock.utils.scan_barcode``: it looks Item Barcode / Serial No /
Batch / Warehouse up via raw ``frappe.db.get_value`` /
``frappe.get_cached_value``, never ``frappe.has_permission``) - this
wrapper explicitly checks Item / Serial No / Batch / Warehouse read
permission on whatever the scan resolved before returning it.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError


def scan_barcode(search_value: str) -> dict:
	"""Look ``search_value`` up across Barcode, Serial No, and Batch
	No tables. Returns whatever ERPNext's resolver finds, typically
	``{item_code, barcode, serial_no, batch_no, ...}`` - shape varies
	by what the scanner matched."""
	if not search_value:
		raise InvalidArgumentError("search_value is required")

	from erpnext.stock.utils import scan_barcode as _scan

	result = _scan(search_value=search_value)
	# The underlying helper returns a typed dataclass (BarcodeScanResult)
	# or a dict depending on the ERPNext release; normalise to a JSON-
	# friendly dict either way.
	if hasattr(result, "__dict__"):
		result = {k: v for k, v in vars(result).items() if not k.startswith("_")}
	elif not isinstance(result, dict):
		result = {"raw": result}

	item_code = result.get("item_code")
	if item_code:
		frappe.has_permission("Item", "read", doc=item_code, throw=True)
	serial_no = result.get("serial_no")
	if serial_no:
		frappe.has_permission("Serial No", "read", doc=serial_no, throw=True)
	batch_no = result.get("batch_no")
	if batch_no:
		frappe.has_permission("Batch", "read", doc=batch_no, throw=True)
	warehouse = result.get("warehouse")
	if warehouse:
		frappe.has_permission("Warehouse", "read", doc=warehouse, throw=True)
	return result
