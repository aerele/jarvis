"""Bench-derived engagement config: values an agent declares in config_keys that the bench
supplies itself instead of asking the customer to type them."""

import hashlib
import hmac
import re

import frappe

_OPEN_LEAD_TYPES = ("Open", "Ongoing")


def _fingerprint_salt(inst):
	from frappe.utils.password import get_encryption_key

	key = get_encryption_key().encode()
	return hmac.new(key, f"fingerprint-salt:{inst.name}".encode(), hashlib.sha256).hexdigest()[:32]


def _open_lead_statuses(inst):
	if not frappe.db.exists("DocType", "CRM Lead Status"):
		return None
	names = frappe.get_all("CRM Lead Status", filters={"type": ["in", list(_OPEN_LEAD_TYPES)]}, pluck="name")
	return sorted(names) or None


def _tax_withholding_model(inst):
	invoice = frappe.get_meta("Purchase Invoice")
	if not invoice.has_field("tax_withholding_entries"):
		return "legacy_supplier_unverified"
	field = invoice.get_field("tax_withholding_entries")
	if field.fieldtype != "Table" or field.options != "Tax Withholding Entry":
		return "entry_table_unrecognized"
	entry = frappe.get_meta("Tax Withholding Entry")
	required = {
		"company",
		"party_type",
		"party",
		"tax_id",
		"tax_withholding_category",
		"tax_withholding_group",
		"taxable_amount",
		"tax_rate",
		"withholding_amount",
		"taxable_doctype",
		"taxable_name",
		"taxable_date",
		"withholding_doctype",
		"withholding_name",
		"withholding_date",
		"under_withheld_reason",
		"lower_deduction_certificate",
		"status",
	}
	if not all(entry.has_field(name) for name in required):
		return "entry_table_unrecognized"
	if not {"Duplicate", "Cancelled"} <= set((entry.get_field("status").options or "").splitlines()):
		return "entry_table_unrecognized"
	return "entry_table_v16"


SUPPORTED = {"tax_withholding_model": {"entry_table_v16"}}
MIN_BUNDLE_VERSION = {"tax_withholding_model": (0, 4, 0)}


def unsupported_reason(namespaces, inst):
	"""Recheck declared schema guards at writeback; a failed check stays closed."""
	for key in sorted(set(namespaces) & set(SUPPORTED)):
		version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(inst.get("installed_version") or ""))
		if not version or tuple(int(part) for part in version.groups()) < MIN_BUNDLE_VERSION[key]:
			return "auditor bundle version"
		try:
			value = PROVIDERS[key][0](inst)
		except Exception:
			return "unavailable " + key.replace("_", " ")
		if value not in SUPPORTED[key]:
			return key.replace("_", " ")
	return None


# key -> (provider, customer value wins?)
PROVIDERS = {
	"fingerprint_salt": (_fingerprint_salt, False),
	"open_workable_status_set": (_open_lead_statuses, True),
	"tax_withholding_model": (_tax_withholding_model, False),
}


def customer_may_set(key):
	"""False for bench-only keys (the salt): a customer-typed value is never passed on."""
	provider = PROVIDERS.get(key)
	return provider is None or provider[1]


def derive(namespaces, inst, explicit):
	"""Values for the declared namespaces the bench supplies; never overrides a customer value
	where the provider allows one, always overrides where it does not (the salt)."""
	out = {}
	for key in namespaces:
		provider = PROVIDERS.get(key)
		if not provider:
			continue
		fn, customer_wins = provider
		if customer_wins and key in explicit:
			continue
		try:
			value = fn(inst)
		except Exception:
			frappe.log_error(
				title="derived_config provider failed", message=f"{key}\n{frappe.get_traceback()}"
			)
			continue
		if value is not None:
			out[key] = value
	return out
