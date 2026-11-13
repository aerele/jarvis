"""Test-only builder of the #821 template dataset (never imported by application code).

Northstar Manufacturing Pvt Ltd, November 2026, as the design template shows it. The source rows are
fixed literals with explicit voucher names, so the output does not depend on ERPNext reports or on its
version. They run through `project_snapshot`, `store.split_result` (the stored parts) and the API's own
response builders (`snapshot_status_body`, `detail.day_detail`, `detail.bucket_detail`).
"""

import json
from calendar import monthrange
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from jarvis.payment_calendar import api, balances, detail, snapshot, store

COMPANY = "Northstar Manufacturing Pvt Ltd"
ANCHOR = "2026-11-11"
SNAPSHOT_ID = "a" * 32
PLACEHOLDER_TIME = "2026-11-11T05:12:00+00:00"  # 10:42 IST, the template snapshot time
TIME_KEYS = ("generated_at", "expires_at", "queued_at", "started_at", "completed_at")
DEFAULT_FLOOR = "50000.00"
DEFAULT_MONTH = "2026-11"

# Daily totals, 1-30 Nov then 1-10 Dec, from the template's proposal-behavior.js.
NOV_PAYABLES = (
	[0, 8000, 0, 4500, 3000, 0, 4000, 1000, 2000, 0]
	+ [42048, 6994, 40457, 26686, 42818, 12680, 24257, 36950, 102236, 56943]
	+ [37219, 86425, 19373, 0, 0, 2419, 5018, 8108, 23035, 4732]
)
NOV_RECEIVABLES = (
	[0, 28500, 0, 81000, 45000, 122500, 0, 0, 32500, 0]
	+ [86250, 19500, 0, 37000, 64200, 0, 55000, 21000, 0, 117400]
	+ [18000, 45000, 0, 0, 0, 16750, 0, 32000, 85000, 24000]
)
DEC_PAYABLES = [12000, 20000, 0, 15000, 5000, 0, 30000, 8000, 17000, 10000]
DEC_RECEIVABLES = [18000, 5000, 12000, 22000, 0, 0, 14000, 11000, 24000, 6000]
SETTLED_DAYS = (1, 3, 6, 10, 24)
PARTIAL_DAYS = (4, 18, 26)
SUPPLIERS = (
	"Cedar Trading Co.",
	"Atlas Components",
	"Meridian Packaging",
	"Oakridge Industries",
	"Kaveri Supply Co.",
	"Bluebird Logistics",
	"Apex Electricals",
	"Sierra Textiles",
)
CUSTOMERS = ("Evergreen Retail", "Summit Engineering", "Westbridge Exports", "Banyan Distributors")

# (party, voucher, remaining, original) on named dates; everything else is split by amount.
FIXED_ROWS = {
	("Payable", "2026-11-11"): [
		("Cedar Trading Co.", "PINV-2026-0142", "18500", "25000"),
		("Atlas Components", "PINV-2026-0148", "14280", "14280"),
		("Meridian Packaging", "PINV-2026-0151", "9268", "9268"),
	],
	("Receivable", "2026-11-11"): [
		("Evergreen Retail", "SINV-2026-0284", "54000", "54000"),
		("Summit Engineering", "SINV-2026-0291", "32250", "45000"),
	],
	("Payable", "2026-11-19"): [("Oakridge Industries", "PINV-2026-0207", "102236", "102236")],
	# Two invoices at exactly 50 % each: the star must not appear.
	("Payable", "2026-11-17"): [
		("Atlas Components", "PINV-2026-0173", "12128.5", "12128.5"),
		("Cedar Trading Co.", "PINV-2026-0174", "12128.5", "12128.5"),
	],
}
UNDATED_ROWS = {
	"Payable": [
		("Bluebird Logistics", "PINV-2026-0098", "3000", "3000"),
		("Kaveri Supply Co.", "PINV-2026-0099", "2000", "2000"),
	],
	"Receivable": [("Banyan Distributors", "SINV-2026-0090", "12000", "12000")],
}
# Negative residuals (credit notes, journal adjustments): listed only by the `adjustments` bucket.
ADJUSTMENT_ROWS = {
	"Payable": [("Sierra Textiles", "JV-2026-0031", "-3217", "0", "2026-11-09")],
	"Receivable": [("Westbridge Exports", "JV-2026-0032", "-1830", "0", None)],
}
BANKS = ("HDFC Bank", "ICICI Bank", "State Bank of India", "Axis Bank")

# Scenario-only rows that replace the template's rows on one date: (side, due) -> day rows. A row is
# (party, voucher, remaining, original), plus a term label when the invoice is split into terms.
# The default month net is +329,702.00: one payable of 400,000 turns it negative, 329,702 makes it zero.
NET_NEGATIVE_ROWS = {
	("Payable", "2026-11-25"): [("Oakridge Industries", "PINV-2026-0301", "400000", "400000")]
}
NET_ZERO_ROWS = {("Payable", "2026-11-25"): [("Oakridge Industries", "PINV-2026-0302", "329702", "329702")]}
# 25 Nov: one invoice in two terms of 30 % each beats the 40 % invoice, so the sum earns the star.
# 26 Nov: the same two-term shape at 40 % of the day stays below half; its total puts the 26 and 27 Nov
# closings on different compact labels.
GROUPED_TERM_ROWS = {
	("Payable", "2026-11-25"): [
		("Atlas Components", "PINV-2026-0311", "30000", "30000", "Term 1 of 2"),
		("Atlas Components", "PINV-2026-0311", "30000", "30000", "Term 2 of 2"),
		("Cedar Trading Co.", "PINV-2026-0312", "40000", "40000"),
	],
	("Payable", "2026-11-26"): [
		("Meridian Packaging", "PINV-2026-0321", "21000", "21000", "Term 1 of 2"),
		("Meridian Packaging", "PINV-2026-0321", "21000", "21000", "Term 2 of 2"),
		("Kaveri Supply Co.", "PINV-2026-0322", "21000", "21000"),
		("Apex Electricals", "PINV-2026-0323", "21000", "21000"),
		("Sierra Textiles", "PINV-2026-0324", "21000", "21000"),
	],
}
# One invoice at exactly half of the day: strictly more than half is needed for the star.
STAR_HALF_ROWS = {
	("Payable", "2026-11-25"): [
		("Oakridge Industries", "PINV-2026-0331", "50000", "50000"),
		("Sierra Textiles", "PINV-2026-0332", "25000", "25000"),
		("Apex Electricals", "PINV-2026-0333", "25000", "25000"),
	]
}


def _catalog():
	"""The template's 60 accounts: (name, account type, bank, account number, balance)."""
	accounts = [
		("HDFC Current", "Bank", "HDFC Bank", "50100012343281", "221850.00"),
		# A cash account has no Bank Account row, so its bank and reference are null (D6).
		("Petty Cash", "Cash", None, None, "25000.00"),
		("ICICI Savings", "Bank", "ICICI Bank", "30090012349016", "80000.00"),
	]
	for index in range(57):
		balance = f"{10000 + (index + 4) * 500}.00"
		name, number = f"Operating account {index + 4:02d}", f"7010000{4100 + index}"
		accounts.append((name, "Bank", BANKS[index % 4], number, balance))
	return accounts


CATALOG = _catalog()
BALANCES = {name: balance for name, _, _, _, balance in CATALOG}
DEFAULT_ACCOUNTS = ["HDFC Current", "Petty Cash"]
CHIP_REMOVED_ACCOUNTS = ["HDFC Current"]
ACCOUNTS_50 = [name for name, *_ in CATALOG[:50]]


def money(value):
	return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _row(side, due, index, party, voucher, remaining, original, kind="obligation", term=""):
	remaining, original = Decimal(str(remaining)), Decimal(str(original))
	payable = side == "Payable"
	settled = remaining == 0
	return {
		"id": f"{side.lower()}-{due or 'undated'}-{index}",
		"voucher_type": "Purchase Invoice" if payable else "Sales Invoice",
		"voucher_no": voucher,
		"party_type": "Supplier" if payable else "Customer",
		"party": party,
		"party_name": party,
		"account": "Creditors - NM" if payable else "Debtors - NM",
		"posting_date": "2026-10-15",
		"due_date": due,
		"term": term,
		"original": money(original),
		"paid": "0.00" if kind == "adjustment" else money(original - remaining),
		"credit": "0.00",
		"discount": "0.00",
		"remaining": money(remaining),
		"settlement": "Settled" if settled else "Unpaid" if remaining >= original else "Partially paid",
		"kind": kind,
		"_permission": {
			"voucher_type": "Purchase Invoice" if payable else "Sales Invoice",
			"voucher_no": voucher,
			"party": party,
		},
	}


def _adjustment_row(side, index, party, voucher, remaining, original, due):
	# An adjustment is a negative residual with no original invoice: nothing was paid against it.
	row = _row(side, due, index, party, voucher, remaining, original, kind="adjustment")
	return {
		**row,
		"id": f"adjustment-{side.lower()}-{index}",
		"voucher_type": "Journal Entry",
		"settlement": "Adjustment",
		"_permission": {**row["_permission"], "voucher_type": "Journal Entry"},
	}


def _split_rows(side, due, amount, partial):
	"""The template's own split of a day's total into 1, 3 or 4 invoices."""
	count = 4 if amount > 50000 else 3 if amount > 10000 else 1
	names = SUPPLIERS if side == "Payable" else CUSTOMERS
	prefix = "PINV" if side == "Payable" else "SINV"
	base = 1000 if due.startswith("2026-11") else 2000
	day, left, rows = int(due[-2:]), amount, []
	for index in range(count):
		balance = left if index == count - 1 else amount // count
		left -= balance
		original = balance + (1500 if partial and index == 0 else 0)
		voucher = f"{prefix}-2026-{base + day * 10 + index}"
		rows.append((names[(day + index) % len(names)], voucher, balance, original))
	return rows


def _day_rows(side, due, amount, settled, partial, overrides):
	if (side, due) in overrides:
		return overrides[(side, due)]
	if (side, due) in FIXED_ROWS:
		return FIXED_ROWS[(side, due)]
	if not amount:
		# Paid terms stay listed with zero remaining; empty receivable days have no rows.
		settled_row = ("Oakridge Industries", f"PINV-2026-01{due[-2:]}", "0", "12500")
		return [settled_row] if side == "Payable" and settled else []
	return _split_rows(side, due, amount, partial and side == "Payable")


def _term_row(side, due, index, values):
	party, voucher, remaining, original, *term = values
	return _row(side, due, index, party, voucher, remaining, original, term=term[0] if term else "")


def _side_rows(side, overrides):
	totals = NOV_PAYABLES if side == "Payable" else NOV_RECEIVABLES
	december = DEC_PAYABLES if side == "Payable" else DEC_RECEIVABLES
	rows = []
	for month, values in (("2026-11", totals), ("2026-12", december)):
		for number, amount in enumerate(values, start=1):
			due = f"{month}-{number:02d}"
			nov = month == "2026-11"
			day_rows = _day_rows(
				side, due, amount, nov and number in SETTLED_DAYS, nov and number in PARTIAL_DAYS, overrides
			)
			rows += [_term_row(side, due, i, values_) for i, values_ in enumerate(day_rows)]
	rows += [_row(side, None, i, *values_) for i, values_ in enumerate(UNDATED_ROWS[side])]
	rows += [_adjustment_row(side, i, *values_) for i, values_ in enumerate(ADJUSTMENT_ROWS[side])]
	return rows


def template_source(side, overrides=None):
	rows = _side_rows(side, overrides or {})
	total = sum((Decimal(row["remaining"]) for row in rows), Decimal(0))
	return {
		"rows": rows,
		"excluded_rows": [],
		"notes": {},
		"canonical_total": str(total),
		"currency": "INR",
		"precision": 2,
		"complete_for_company": True,
		"scope_reasons": [],
		"permission_columns": [],
	}


def month_scope(month, accounts):
	last_day = monthrange(int(month[:4]), int(month[5:]))[1]
	return {
		"month": month,
		"protocol": 1,
		"actor": "owner@northstar.example",
		"company": COMPANY,
		"accounts": sorted(accounts),
		"anchor": ANCHOR,
		"selectable_start": "2025-11-01",
		"selectable_end": "2027-10-31",
		"window_start": f"{month}-01",
		"window_end": f"{month}-{last_day:02d}",
	}


def build_result(floor=DEFAULT_FLOOR, accounts=DEFAULT_ACCOUNTS, month=DEFAULT_MONTH, day_rows=None):
	"""What the API reads: the worker's own builder over the fixed rows, split into stored parts and assembled."""
	scope, result = build_raw(floor, accounts, month, day_rows)
	return scope, store.assemble_result(store.split_result(result, scope))


def build_raw(floor=DEFAULT_FLOOR, accounts=DEFAULT_ACCOUNTS, month=DEFAULT_MONTH, day_rows=None):
	"""The worker's result before it is split: rows in source order, each with its own proof."""
	scope = month_scope(month, accounts)
	sources = {side: template_source(side, day_rows) for side in ("Payable", "Receivable")}
	balances = [{"account": name, "balance": BALANCES[name]} for name, *_ in CATALOG if name in accounts]
	total = sum((Decimal(row["balance"]) for row in balances), Decimal(0))
	bank = {"total": money(total), "accounts": balances, "currency": "INR", "precision": 2}
	result = snapshot.build_result(
		scope,
		sources,
		bank if balances else None,
		None,
		{},
		currency="INR",
		floor={
			"company": COMPANY,
			"configuration_id": "northstar-floor",
			"currency": "INR",
			"safety_floor": floor,
			"revision": 1,
			"configured": True,
		},
		precision=2,
		generated_at=PLACEHOLDER_TIME,
	)
	return scope, result


def reindex(scope, result):
	"""Back to the stored shape after a test edited the rows or totals of an assembled result."""
	for side, source in result["sources"].items():
		if source is not None:
			source["rows"] = detail.sort_rows(source["rows"])
			source["index"] = detail.index_side(source["rows"], result, side, scope)


def build_status(scope, result):
	status = {
		"snapshot_id": SNAPSHOT_ID,
		"state": "complete",
		"code": None,
		"queued_at": PLACEHOLDER_TIME,
		"started_at": PLACEHOLDER_TIME,
		"completed_at": PLACEHOLDER_TIME,
		"expires_at": PLACEHOLDER_TIME,
		"scope": scope,
	}
	with _stubbed_ledger_sources():
		return api.snapshot_status_body(status, result)


@contextmanager
def _stubbed_ledger_sources():
	"""Account and Bank Account reads from the catalog; the real enrichment code runs on top."""

	def get_list(doctype, *args, filters, **kwargs):
		if doctype == "Account":
			wanted = filters["name"][1]
			return [
				{"name": name, "account_name": name, "account_type": kind}
				for name, kind, *_ in CATALOG
				if name in wanted
			]
		assert doctype == "Bank Account", doctype
		wanted = filters["account"][1]
		return [
			{"account": name, "bank": bank, "bank_account_no": number}
			for name, _, bank, number, _ in CATALOG
			if name in wanted and bank
		]

	with patch("frappe.get_list", side_effect=get_list), patch("frappe.has_permission", return_value=True):
		yield


def scenario(name, **options):
	"""One scenario builder: the default dataset with a different floor, accounts or day rows."""

	def build():
		scope, result = build_result(**options)
		return {"scenario": name, "status": build_status(scope, result)}

	return build


def grouped_terms_scenario(name):
	"""The grouped-terms status, plus the 25 Nov payable day detail that lists the terms."""

	def build():
		scope, result = build_result(day_rows=GROUPED_TERM_ROWS)
		request = {"kind": "day", "date": "2026-11-25", "side": "Payable", "page": 1, "page_size": 100}
		request.update(sort=None, order=None)
		arguments = {key: value for key, value in request.items() if key != "kind"}
		reply = detail.day_detail({"scope": scope}, result, party_page=1, **arguments)
		return {
			"scenario": name,
			"status": build_status(scope, result),
			"day_detail": {"request": request, "data": reply},
		}

	return build


def detail_scenario(name, kind, **request):
	"""A day or bucket reply of the default dataset, with the request that produced it."""
	defaults = {"side": "Payable", "page": 1, "page_size": 100, "sort": None, "order": None}
	request = {"kind": kind, **defaults, **request}

	def build():
		scope, result = build_result()
		arguments = {key: value for key, value in request.items() if key != "kind"}
		read = detail.day_detail if kind == "day" else detail.bucket_detail
		reply = read({"scope": scope}, result, party_page=1, **arguments)
		return {"scenario": name, "request": request, "data": reply}

	return build


def balances_scenario(name):
	"""The finished "Show all accounts" job, read back by `get_account_balances_status` (B5)."""

	def build():
		accounts = [{"account": n, "balance": balance} for n, _, _, _, balance in CATALOG]
		total = sum((Decimal(row["balance"]) for row in accounts), Decimal(0))
		result = {
			"generated_at": PLACEHOLDER_TIME,
			"currency": "INR",
			"precision": 2,
			"accounts": accounts,
			"total": money(total),
		}
		status = {
			"snapshot_id": SNAPSHOT_ID,
			"kind": "balances",
			"state": "complete",
			"code": None,
			"queued_at": PLACEHOLDER_TIME,
			"started_at": PLACEHOLDER_TIME,
			"completed_at": PLACEHOLDER_TIME,
			"expires_at": PLACEHOLDER_TIME,
			"scope": {"company": COMPANY, "accounts": sorted(row["account"] for row in accounts)},
		}
		with _stubbed_ledger_sources():
			data = balances.balances_status_body(status, result)
		request = {"kind": "balances", "company": COMPANY, "job_id": SNAPSHOT_ID}
		return {"scenario": name, "request": request, "data": data}

	return build


SCENARIO_BUILDERS = {
	"default": scenario("default"),
	"floor_140000": scenario("floor_140000", floor="140000.00"),
	"floor_147614": scenario("floor_147614", floor="147614.00"),
	"floor_150000": scenario("floor_150000", floor="150000.00"),
	"floor_200000": scenario("floor_200000", floor="200000.00"),
	"accounts_none": scenario("accounts_none", accounts=[]),
	"accounts_chip_removed": scenario("accounts_chip_removed", accounts=CHIP_REMOVED_ACCOUNTS),
	"accounts_50": scenario("accounts_50", accounts=ACCOUNTS_50),
	"month_net_negative": scenario("month_net_negative", day_rows=NET_NEGATIVE_ROWS),
	"month_net_zero": scenario("month_net_zero", day_rows=NET_ZERO_ROWS),
	"grouped_invoice_terms": grouped_terms_scenario("grouped_invoice_terms"),
	"star_exactly_50pct": scenario("star_exactly_50pct", day_rows=STAR_HALF_ROWS),
	"month_oct": scenario("month_oct", month="2026-10"),
	"month_dec": scenario("month_dec", month="2026-12"),
	"default_day_payable": detail_scenario("default_day_payable", "day", date="2026-11-11"),
	"default_day_receivable": detail_scenario(
		"default_day_receivable", "day", date="2026-11-11", side="Receivable"
	),
	"default_bucket_immediate": detail_scenario("default_bucket_immediate", "bucket", bucket="immediate"),
	"default_bucket_now": detail_scenario("default_bucket_now", "bucket", bucket="now"),
	"default_bucket_today": detail_scenario("default_bucket_today", "bucket", bucket="today"),
	"default_bucket_window_payable": detail_scenario(
		"default_bucket_window_payable", "bucket", bucket="window"
	),
	"default_bucket_window_receivable": detail_scenario(
		"default_bucket_window_receivable", "bucket", bucket="window", side="Receivable"
	),
	"default_bucket_window_payable_party_asc": detail_scenario(
		"default_bucket_window_payable_party_asc", "bucket", bucket="window", sort="party", order="asc"
	),
	"balances_complete": balances_scenario("balances_complete"),
}


def normalise(value):
	"""Fixed placeholders for volatile times; JSON-safe values only."""
	if isinstance(value, dict):
		return {k: PLACEHOLDER_TIME if k in TIME_KEYS and v else normalise(v) for k, v in value.items()}
	if isinstance(value, list):
		return [normalise(v) for v in value]
	return value


def render(name):
	"""The exact text of `golden/<name>.json`."""
	return (
		json.dumps(normalise(SCENARIO_BUILDERS[name]()), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
	)
