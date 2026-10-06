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


# key -> (provider, customer value wins?)
PROVIDERS = {
	"fingerprint_salt": (_fingerprint_salt, False),
	"open_workable_status_set": (_open_lead_statuses, True),
}


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
