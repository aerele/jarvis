"""Local historical-schema contract. Encoding is obfuscation, not secrecy.

The bundled contract is authoritative and immutable. A private database mirror is
seeded on installation/migration; missing or damaged mirrors fall back locally,
so direct upgrades and workers starting before migration need no network setup.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

import frappe

ROW = "jarvis-legacy-compatibility-v1"
PARENT = "__jarvis_runtime_internal"
KEY = "legacy_compatibility_v1"
_MEMO = "jarvis_legacy_compatibility_v1"
_DIGEST = "db504854ed44e5be824701597cb00e9ff343c4ba76de605d55428f027d010d03"
_PAYLOAD = "eyJjYXB0dXJlX3Byb3ZpZGVyX2NvbHVtbiI6Im9wZW5jbGF3X3Byb3ZpZGVyIiwic2V0dGluZ3NfcGF0Y2giOiJqYXJ2aXMucGF0Y2hlcy52MV8wX3JlbmFtZV9vcGVuY2xhd190b19hZ2VudCIsInNldHRpbmdzX3JlbmFtZXMiOltbIm9wZW5jbGF3X2VuZHBvaW50IiwiamFydmlzX2FkbWluX3VybCJdLFsib3BlbmNsYXdfYXBpX2tleSIsImphcnZpc19hZG1pbl9hcGlfa2V5Il0sWyJvcGVuY2xhd19nYXRld2F5X3VybCIsImFnZW50X3VybCJdLFsib3BlbmNsYXdfZ2F0ZXdheV90b2tlbiIsImFnZW50X3Rva2VuIl0sWyJvcGVuY2xhd19jb21wb3NlX2RpciIsImFnZW50X2NvbXBvc2VfZGlyIl0sWyJvcGVuY2xhd19jb25maWdfcGF0aCIsImFnZW50X2NvbmZpZ19wYXRoIl0sWyJvcGVuY2xhd19sbG1fa2V5X3BhdGgiLCJhZ2VudF9sbG1fa2V5X3BhdGgiXV0sInZlcnNpb24iOjEsIndhdGVybWFya19jb2x1bW4iOiJvcGVuY2xhd19zZXFfd2F0ZXJtYXJrIn0="


@dataclass(frozen=True)
class Contract:
	settings_patch: str
	settings_renames: tuple
	watermark_column: str
	capture_provider_column: str


@lru_cache(maxsize=1)
def _embedded():
	raw = base64.b64decode(_PAYLOAD, validate=True)
	if hashlib.sha256(raw).hexdigest() != _DIGEST:
		raise ValueError("Bundled compatibility contract checksum mismatch")
	doc = json.loads(raw)
	if doc["version"] != 1:
		raise ValueError("Unsupported compatibility contract")
	return raw.decode(), Contract(
		settings_patch=doc["settings_patch"],
		settings_renames=tuple(tuple(pair) for pair in doc["settings_renames"]),
		watermark_column=doc["watermark_column"],
		capture_provider_column=doc["capture_provider_column"],
	)


def get_contract():
	cached = getattr(frappe.local, _MEMO, None)
	if cached is not None:
		return cached
	raw, contract = _embedded()
	saved = frappe.db.get_value("DefaultValue", {"name": ROW, "parent": PARENT, "defkey": KEY}, "defvalue")
	# Only the exact reviewed document is accepted. Unknown identifiers can never
	# influence SQL. Repair occurs at migration, not inside a user's transaction.
	if saved == raw:
		doc = json.loads(saved)
		contract = Contract(
			settings_patch=doc["settings_patch"],
			settings_renames=tuple(tuple(pair) for pair in doc["settings_renames"]),
			watermark_column=doc["watermark_column"],
			capture_provider_column=doc["capture_provider_column"],
		)
	setattr(frappe.local, _MEMO, contract)
	return contract


def seed():
	"""Persist a private mirror within the install/migration transaction."""
	raw, _ = _embedded()
	# Serialize first insertion; DefaultValue's hash autonaming must not be used
	# with ignore_if_duplicate, which may generate a second name on collision.
	frappe.db.get_singles_dict("Jarvis Settings", for_update=True)
	row = frappe.db.get_value(
		"DefaultValue", ROW, ["parent", "defkey", "defvalue"], as_dict=True, for_update=True
	)
	if row is not None and (row.parent != PARENT or row.defkey != KEY):
		raise ValueError("Compatibility storage identity collision")
	if row is None:
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"name": ROW,
				"parent": PARENT,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": KEY,
				"defvalue": raw,
			}
		).db_insert()
	elif row.defvalue != raw:
		frappe.db.set_value("DefaultValue", ROW, "defvalue", raw, update_modified=False)
	# No commit and no shared cache publication of uncommitted database state.
	if hasattr(frappe.local, _MEMO):
		delattr(frappe.local, _MEMO)
