"""Net outstanding receivable for a customer at a company.

Wraps ``erpnext.selling.doctype.customer.customer.get_customer_outstanding``.
The underlying helper sums GL entries against the customer's
receivable accounts and optionally adds open Sales Orders' invoiceable
balance - the same calculation as the Customer Credit Limit check.

Customer read perm is enforced before delegating, and the company is
gated by Company User Permission scope (not Company-doctype read - see
``jarvis.tools._company_scope``), so a user who can't read the customer
- or who is restricted to a different company - can't probe an
outstanding balance outside what they can see. The underlying helper
itself applies no company-level permission filter.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools._company_scope import assert_company_permitted


def get_customer_outstanding(
	customer: str,
	company: str,
	cost_center: str | None = None,
	ignore_outstanding_sales_order: bool = False,
) -> dict:
	"""Return ``{outstanding, customer, company}`` where ``outstanding``
	is the net receivable from ``customer`` at ``company``. If
	``ignore_outstanding_sales_order=True``, exclude open Sales Orders
	from the calculation."""
	if not customer:
		raise InvalidArgumentError("customer is required")
	if not company:
		raise InvalidArgumentError("company is required")
	if not frappe.db.exists("Customer", customer):
		raise InvalidArgumentError(f"unknown Customer: {customer}")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")
	if not frappe.has_permission("Customer", "read", doc=customer):
		raise PermissionDeniedError(f"no read permission on Customer {customer}")
	assert_company_permitted(company)

	from erpnext.selling.doctype.customer.customer import (
		get_customer_outstanding as _gco,
	)

	outstanding = _gco(
		customer=customer,
		company=company,
		ignore_outstanding_sales_order=bool(ignore_outstanding_sales_order),
		cost_center=cost_center,
	)
	return {
		"outstanding": float(outstanding or 0),
		"customer": customer,
		"company": company,
	}
