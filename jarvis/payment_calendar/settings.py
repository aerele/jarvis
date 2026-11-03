"""A company-owned floor, separate from per-viewer account preferences."""

from decimal import Decimal, InvalidOperation

import frappe

from jarvis.payment_calendar.permissions import require_company, require_floor_write
from jarvis.payment_calendar.sources import money

DOCTYPE = "Jarvis Payment Calendar Settings"


def get_settings(company: str) -> dict:
	from erpnext.accounts.utils import get_currency_precision

	values = require_company(company)
	row = frappe.db.get_value(
		DOCTYPE, {"company": company}, ["name", "currency", "safety_floor", "revision"], as_dict=True
	)
	configured = bool(row and row.currency == values.default_currency and row.safety_floor is not None)
	return {
		"company": company,
		"configuration_id": row.name if row else None,
		"currency": values.default_currency,
		"safety_floor": str(money(row.safety_floor, get_currency_precision() or 2)) if configured else None,
		"revision": int(row.revision or 0) if row else 0,
		"configured": configured,
	}


def validate_floor(value) -> Decimal:
	try:
		if value is None or isinstance(value, bool):
			raise InvalidOperation
		amount = Decimal(str(value))
		if not amount.is_finite() or amount < 0:
			raise InvalidOperation
	except (InvalidOperation, ValueError, TypeError) as exc:
		raise frappe.ValidationError("Minimum cash must be a non-negative amount.") from exc
	return amount


def update_safety_floor(company: str, value, expected_revision: int) -> dict:
	require_floor_write(company)
	validate_floor(value)
	if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
		raise frappe.ValidationError("The current settings revision is required.")
	# An existing Company row also serializes the first creation, when there
	# is no settings row yet for SELECT FOR UPDATE to lock.
	frappe.db.get_value("Company", company, "name", for_update=True)
	existing = frappe.db.get_value(
		DOCTYPE, {"company": company}, ["name", "revision"], as_dict=True, for_update=True
	)
	if (int(existing.revision or 0) if existing else 0) != expected_revision:
		raise frappe.TimestampMismatchError("Minimum cash changed. Refresh the settings before saving.")
	doc = (
		frappe.get_doc(DOCTYPE, existing.name)
		if existing
		else frappe.get_doc({"doctype": DOCTYPE, "company": company})
	)
	doc.safety_floor = str(validate_floor(value))
	doc.save()
	return get_settings(company)
