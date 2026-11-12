"""Bank name and masked number of bank/cash accounts, read at request time only (D6).

Nothing here is stored in a snapshot result or checked by `validate_result_visibility`. A label is
shown only when the user can read Bank Account and exactly one company Bank Account points at the
ledger account; every other case gives nulls, never an error and never a message.
"""

import frappe

MASK = "•••• "
MIN_NUMBER_LENGTH = 8
NO_LABEL = {"bank": None, "reference": None}


def bank_labels(company: str, accounts: list[str]) -> dict[str, dict]:
	"""`{account: {bank, reference}}` for every name, from one batched Bank Account read."""
	names = list(dict.fromkeys(accounts))
	labels = {name: dict(NO_LABEL) for name in names}
	matches = {}
	for row in _read_bank_accounts(company, names):
		matches.setdefault(row.get("account"), []).append(row)
	for name, rows in matches.items():
		if name in labels and len(rows) == 1:
			labels[name] = _label(rows[0])
	return labels


def bank_breakdown(company: str, bank: dict | None) -> dict | None:
	"""The status `bank` block: stored totals and balances plus read-time names and labels."""
	if not bank:
		return None
	names = [row["account"] for row in bank["accounts"]]
	labels = bank_labels(company, names)
	ledger = _ledger_accounts(company, names)
	return {
		**{key: bank[key] for key in ("total", "currency", "precision")},
		"accounts": [
			{
				"account": row["account"],
				"account_name": ledger.get(row["account"], {}).get("account_name", row["account"]),
				"account_type": ledger.get(row["account"], {}).get("account_type"),
				**labels[row["account"]],
				"balance": row["balance"],
			}
			for row in bank["accounts"]
		],
	}


def _label(row) -> dict:
	number = row.get("bank_account_no") or ""
	tail = number[-4:]
	# A masked stored number ("XXXXXXXX") has no digits to show.
	shows_digits = len(number) >= MIN_NUMBER_LENGTH and tail.isascii() and tail.isdigit()
	reference = MASK + tail if shows_digits else None
	return {"bank": row.get("bank") or None, "reference": reference}


def _read_bank_accounts(company: str, names: list[str]) -> list:
	if not names:
		return []
	logged = len(frappe.local.message_log)
	try:
		if not frappe.has_permission("Bank Account", "read"):
			return []
		return frappe.get_list(
			"Bank Account",
			filters={"account": ["in", names], "company": company, "is_company_account": 1, "disabled": 0},
			fields=["account", "bank", "bank_account_no"],
			limit_page_length=0,
		)
	except Exception as exc:
		# Class only: the exception text can carry account numbers.
		frappe.logger("payment_calendar").warning(f"bank label read failed: {type(exc).__name__}")
		return []
	finally:
		# A refused read must not leak "No permission" noise into the response.
		del frappe.local.message_log[logged:]


def _ledger_accounts(company: str, names: list[str]) -> dict[str, dict]:
	rows = frappe.get_list(
		"Account",
		filters={"name": ["in", names], "company": company},
		fields=["name", "account_name", "account_type"],
		limit_page_length=0,
	)
	return {row["name"]: row for row in rows}
