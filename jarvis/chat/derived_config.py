"""Bench-derived engagement config: values an agent declares in config_keys that the bench
supplies itself instead of asking the customer to type them."""

import hashlib
import hmac

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
	if frappe.get_meta("Purchase Invoice").has_field("tax_withholding_entries"):
		return "entry_table_v16"
	return "legacy_supplier_unverified"


UNSUPPORTED = {
	"tax_withholding_model": {
		"entry_table_v16": "the Tax Withholding Entry table",
		"legacy_supplier_unverified": "legacy tax withholding inputs",
	}
}


def unsupported_reason(namespaces, inst):
	"""Recheck declared schema guards at writeback; a failed check stays closed."""
	for key in sorted(set(namespaces) & set(UNSUPPORTED)):
		try:
			value = PROVIDERS[key][0](inst)
		except Exception:
			return "unavailable " + key.replace("_", " ")
		if value in UNSUPPORTED[key]:
			return UNSUPPORTED[key][value]
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
