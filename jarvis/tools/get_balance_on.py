"""GL account balance as of a date, optionally narrowed by party /
cost-center / finance book.

Wraps ``erpnext.accounts.utils.get_balance_on``. This is the same code
path that powers ERPNext's General Ledger report, so the answer is
canonical - including handling for in-account-currency, default finance
book inclusion, and reverse-charge subtleties an LLM walking GL Entry
rows by hand will miss.

Permission gating: the underlying helper checks Account read perm
internally unless ``ignore_account_permission`` is set, which we do
NOT expose to the agent (callers can't override perm checks). It applies
NO company-level filter of its own, so this wrapper additionally gates
``company`` by Company User Permission scope (not Company-doctype read
- see ``jarvis.tools._company_scope``) whenever ``company`` is supplied,
and - in party-only mode, where the balance is otherwise summed across
every company in the bench - requires an explicit, permission-checked
``company`` from a caller who is restricted to specific companies.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._company_scope import assert_company_permitted


def get_balance_on(
	account: str | None = None,
	date: str | None = None,
	party_type: str | None = None,
	party: str | None = None,
	company: str | None = None,
	cost_center: str | None = None,
) -> dict:
	"""Return ``{balance, account, date}`` where ``balance`` is the
	account balance as of ``date`` (defaults to today).

	Either ``account`` or (``party_type`` + ``party``) must be supplied.
	Party-only queries return the party's net receivable / payable
	across all linked GL accounts.
	"""
	if not account and not (party_type and party):
		raise InvalidArgumentError(
			"either account or (party_type + party) is required",
		)
	if account and not frappe.db.exists("Account", account):
		raise InvalidArgumentError(f"unknown Account: {account}")
	if company and not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")
	if party and party_type and not frappe.db.exists(party_type, party):
		raise InvalidArgumentError(f"unknown {party_type}: {party}")

	if company:
		assert_company_permitted(company)
	elif party_type and party and not account:
		# Party-only mode sums the balance across every company in the
		# bench (the underlying helper applies no company filter here at
		# all). A caller restricted to specific companies via a Company
		# User Permission would otherwise get that cross-company exposure
		# blended in - require an explicit, permission-checked company
		# instead of silently aggregating across companies they can't see.
		from frappe.permissions import get_user_permissions

		if get_user_permissions(frappe.session.user).get("Company"):
			raise InvalidArgumentError(
				"company is required: your access is restricted to specific companies, so a "
				"party balance cannot be summed across every company in the bench"
			)

	from erpnext.accounts.utils import get_balance_on as _gbo

	balance = _gbo(
		account=account,
		date=date,
		party_type=party_type,
		party=party,
		company=company,
		cost_center=cost_center,
	)
	return {
		"balance": float(balance or 0),
		"account": account,
		"date": date,
		"party_type": party_type,
		"party": party,
		"company": company,
	}
