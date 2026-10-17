"""Short-lived, owner-bound references to validated queries, never result rows."""

import copy
import hashlib
import json
import secrets

import frappe

from jarvis.exceptions import InvalidArgumentError

TTL = 24 * 60 * 60


def normalized(spec):
	value = copy.deepcopy(spec)
	value.pop("limit", None)
	value.pop("offset", None)
	value.setdefault("select", ["name"])
	return value


def fingerprint(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(spec):
	value = normalized(spec)
	result_id = "q_" + secrets.token_urlsafe(24)
	frappe.cache().set_value(
		"jarvis:query:" + result_id,
		{
			"owner": frappe.session.user,
			"spec": value,
			"created_at": str(frappe.utils.now()),
			"fingerprint": fingerprint(value),
		},
		expires_in_sec=TTL,
	)
	return result_id


def resolve(result_id):
	if not isinstance(result_id, str) or not result_id.startswith("q_") or len(result_id) > 100:
		raise InvalidArgumentError("Result not found.")
	value = frappe.cache().get_value("jarvis:query:" + result_id)
	if not value:
		raise InvalidArgumentError("This result has expired; run the query again.")
	if value.get("owner") != frappe.session.user:
		raise InvalidArgumentError("Result not found.")
	return copy.deepcopy(value["spec"])
