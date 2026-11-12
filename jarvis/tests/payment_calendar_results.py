"""Test-only schema-2 snapshot results: small to read, complete enough for `store.split_result`."""

SIDES = ("Payable", "Receivable")
# The scope fields a split reads: the anchor and the window the stored index is built for.
SCOPE = {"anchor": "2026-11-11", "window_start": "2026-11-01", "window_end": "2026-11-30"}


def obligation(side, voucher, party, remaining="100.00", due="2026-11-11", voucher_type=None):
	payable = side == "Payable"
	return {
		"id": f"{side}:{voucher}:{party}:{due}",
		"voucher_type": voucher_type or ("Purchase Invoice" if payable else "Sales Invoice"),
		"voucher_no": voucher,
		"party_type": "Supplier" if payable else "Customer",
		"party": party,
		"party_name": party,
		"due_date": due,
		"original": remaining,
		"remaining": remaining,
		"kind": "obligation",
		"_permission": {
			"voucher_type": voucher_type or ("Purchase Invoice" if payable else "Sales Invoice"),
			"voucher_no": voucher,
			"party": party,
			"due_date": due,
		},
	}


def top_invoice(voucher, party, remaining="100.00", voucher_type="Purchase Invoice"):
	return {"voucher_type": voucher_type, "voucher_no": voucher, "party_name": party, "remaining": remaining}


def source(rows):
	"""A side source as `read_obligations` returns it, including the keys a stored part must drop."""
	return {
		"rows": rows,
		"excluded_rows": [
			{"voucher_type": "Purchase Invoice", "voucher_no": "EXCLUDED-1", "_permission": {}}
		],
		"notes": {},
		"canonical_total": "100.00",
		"currency": "INR",
		"precision": 2,
		"complete_for_company": True,
		"scope_reasons": [],
		"permission_columns": [{"fieldname": "party", "fieldtype": "Link", "options": "Supplier"}],
	}


def schema2_result(payable=None, receivable=None, days=None, unavailable=None, **overrides):
	"""A `build_result`-shaped dict; `payable` / `receivable` are row lists (None for an unavailable side)."""
	unavailable = unavailable or {}
	if payable is None and "Payable" not in unavailable:
		payable = [obligation("Payable", "PINV-1", "Supplier A")]
	if receivable is None and "Receivable" not in unavailable:
		receivable = [obligation("Receivable", "SINV-1", "Customer A")]
	if days is None:
		days = {"2026-11-11": {"payable_top_invoice": top_invoice("PINV-1", "Supplier A")}}
	return {
		"schema": 2,
		"sources": {
			"Payable": source(payable) if payable is not None else None,
			"Receivable": source(receivable) if receivable is not None else None,
		},
		"projection": {"days": days, "month_totals": None, "precision": 2},
		"horizon": None,
		"currency": "INR",
		"floor": {"safety_floor": "20.00"},
		"bank": None,
		"bank_code": None,
		"unavailable": unavailable,
		"generated_at": "2026-11-11T05:12:00+00:00",
		**overrides,
	}
