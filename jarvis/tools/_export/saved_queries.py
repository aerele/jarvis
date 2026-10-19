"""Short-lived, owner-bound references to validated queries, never result rows."""

import copy
import hashlib
import json
import secrets

import frappe

from jarvis.compat import cache_get_fresh
from jarvis.exceptions import InvalidArgumentError

TTL = 24 * 60 * 60


def normalized(spec, *, preserve_limit=False):
	value = copy.deepcopy(spec)
	if not preserve_limit:
		value.pop("limit", None)
	value.pop("offset", None)
	value.setdefault("select", ["name"])
	return value


def fingerprint(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(spec, *, preserve_limit=False):
	value = normalized(spec, preserve_limit=preserve_limit)
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
	# Fresh, not the request's local copy: on Frappe 15 an expired key can outlive its TTL there.
	value = cache_get_fresh("jarvis:query:" + result_id)
	if not value:
		raise InvalidArgumentError("This result has expired; run the query again.")
	if value.get("owner") != frappe.session.user:
		raise InvalidArgumentError("Result not found.")
	return copy.deepcopy(value["spec"])
