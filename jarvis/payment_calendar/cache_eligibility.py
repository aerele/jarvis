"""Who may see rows of a SHARED cache generation.

Nothing is served yet. This module only defines the rule and counts would-serve outcomes (the serving release calls it).

Invariant I1-cache: rows of a shared generation reach a user ONLY when that user is filter-free.
`eligibility.py` (I1) lets `has_permission` hooks pass, because the per-user reader runs every one of them per
voucher and party in the user's own session (`sources._assemble` -> `visible_names`). A shared generation is
built once, as Administrator, and no per-user step runs when it is served. So the `has_permission` rule comes
back here, and every other rule stays as strict as the reader's. There is no `visible_names` branch: a Company
User Permission scoped to the ledger can hide rows that the voucher and party checks still allow.

A user is filter-free only when all of these hold, and any doubt, error or unknown input says no:
- the user is the session user, and the stored generation row has the shape read here;
- the user holds NO User Permission of any doctype, Company included (
  refuses Company-UP users on shared rows because `visible_names` hides other companies' Employees);
- `permissions.incomplete_scope_reasons()` is empty;
- for Receivable and Payable, `eligibility.reader_ineligibility` is empty for the stored report columns;
- no `permission_query_conditions` hook, no Server Script permission query and no `has_permission` hook outside
  the audited list sits on a doctype of the generation's pre-visibility voucher and party set, on any Party
  Type, on a guarded doctype (`eligibility.GUARDED_DOCTYPES`) or on `"*"`;
- on each stored voucher, party and Party Type doctype (Receivable and Payable; Account and GL Entry for Bank), the
  user can read it, no field is masked and no role is limited to the owner. These three checks do not run on
  Account, Payment Ledger Entry or GL Entry for Receivable and Payable, as in the per-user reader; those
  doctypes are only hook-checked.

Bank is money only: the report-column check does not apply. The serving release still runs `validate_bank_accounts` on each
request.

Interface note (the row contract). `generation` is the Cache Generation row as a mapping: pass
`row.as_dict()` (a `frappe._dict`), never the Document, which is refused as `generation_unknown`. Its keys:
`company`, `side` (Receivable, Payable or Bank) and `payload` (a dict or JSON text). For Receivable and Payable the
refresh writes exactly these payload keys, or nothing is ever served: `voucher_doctypes` and `party_doctypes`
(lists of doctype names, before visibility) and `permission_columns` (`sources._permission_columns`). Bank needs
only a payload object. The build-time hook fingerprint is not compared here: hooks are read live on each call.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

import frappe
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.permissions import get_role_permissions

from jarvis.payment_calendar import compatibility, eligibility, permissions
from jarvis.payment_calendar.errors import TIMEOUTS

AUDITED_HOOKS = eligibility.AUDITED_HOOKS

SIDES = ("Receivable", "Payable", "Bank")
# The bank generation reads accounts and GL rows only: it has no voucher or party set.
BANK_DOCTYPES = ("Account", "GL Entry")

# The keys of a Cache Generation `payload` that this rule reads (written by the refresh).
VOUCHER_KEY = "voucher_doctypes"
PARTY_KEY = "party_doctypes"
COLUMNS_KEY = "permission_columns"


class UnknownGeneration(Exception):
	"""The stored generation row does not have the shape this rule needs."""


@dataclass(frozen=True)
class Shape:
	side: str
	doctypes: frozenset
	columns: list


def filter_free(user: str, company: str, generation) -> tuple[bool, str]:
	"""(True, "") when `user` may see the rows of `generation`; else (False, reason). Fails closed.

	`generation` is the Cache Generation row (a mapping with `company`, `side` and `payload`). The user must
	be the session user: the permission reads below run for the session. A job deadline is not an answer and
	passes through.
	"""
	try:
		shape = read_generation(generation, company)
		if user != frappe.session.user:
			return False, "user_mismatch"
		reasons = ineligibility_reasons(**_live_inputs(user, company, shape))
	except TIMEOUTS:
		raise
	except UnknownGeneration:
		return False, "generation_unknown"
	except Exception:
		return False, "eligibility_unknown"
	return (False, reasons[0]) if reasons else (True, "")


def ineligibility_reasons(
	*,
	user_permissions,
	scope_reasons: list[str],
	reader_reasons: list[str],
	hooks,
	server_script_queries: frozenset[str],
	audited_sources_ok: bool,
	doctypes: frozenset[str],
	masked: frozenset[str],
	owner_only: frozenset[str],
	unreadable: frozenset[str],
) -> list[str]:
	"""Reasons the generation must not be shown to this user, most specific first; empty means filter-free.

	Pure: no frappe calls. `doctypes` is the stored voucher and party set plus every Party Type;
	the guarded doctypes and `"*"` are always added. `masked`, `owner_only` and `unreadable` are the doctypes
	that fail those checks for the user.
	"""
	checked = set(doctypes) | set(eligibility.GUARDED_DOCTYPES)
	reasons = [
		("user_permission", not isinstance(user_permissions, Mapping) or len(user_permissions) > 0),
		("incomplete_scope", bool(scope_reasons)),
		("reader_ineligible", bool(reader_reasons)),
		("query_hook", _has_unaudited(hooks, "permission_query_conditions", checked, audited_sources_ok)),
		("server_script_query", bool(set(server_script_queries) & checked)),
		("permission_hook", _has_unaudited(hooks, "has_permission", checked, audited_sources_ok)),
		("masked_field", bool(masked)),
		("owner_only", bool(owner_only)),
		("unreadable", bool(unreadable)),
	]
	return [name for name, found in reasons if found]


def read_generation(generation, company: str) -> Shape:
	"""The side, doctype set and report columns of a stored row; `UnknownGeneration` on anything else."""
	if not isinstance(generation, Mapping) or generation.get("company") != company:
		raise UnknownGeneration
	side = generation.get("side")
	if side not in SIDES:
		raise UnknownGeneration
	payload = _payload(generation.get("payload"))
	if side == "Bank":
		return Shape(side, frozenset(BANK_DOCTYPES), [])
	doctypes = _doctype_list(payload.get(VOUCHER_KEY)) | _doctype_list(payload.get(PARTY_KEY))
	columns = payload.get(COLUMNS_KEY)
	if not isinstance(columns, list):
		raise UnknownGeneration
	return Shape(side, doctypes, columns)


def audited_sources_ok() -> bool:
	"""True when the audited hook sources still hash as audited."""
	return compatibility.reader_pin_drift(eligibility._AUDITED_SOURCE_NAMES) == []


def _live_inputs(user: str, company: str, shape: Shape) -> dict:
	ok = audited_sources_ok()
	bank = shape.side == "Bank"
	doctypes = shape.doctypes if bank else shape.doctypes | _party_types()
	return {
		"user_permissions": get_user_permissions(user),
		"scope_reasons": permissions.incomplete_scope_reasons(),
		"reader_reasons": [] if bank else _reader_reasons(company, shape, ok),
		"hooks": {
			kind: frappe.get_hooks(kind, {}) for kind in ("permission_query_conditions", "has_permission")
		},
		"server_script_queries": frozenset(get_server_script_map()["permission_query"]),
		"audited_sources_ok": ok,
		"doctypes": doctypes,
		"masked": frozenset(dt for dt in doctypes if frappe.get_meta(dt).get_masked_fields()),
		"owner_only": frozenset(dt for dt in doctypes if _owner_only(dt, user)),
		"unreadable": frozenset(dt for dt in doctypes if not frappe.has_permission(dt, "read", user=user)),
	}


def _reader_reasons(company: str, shape: Shape, ok: bool) -> list[str]:
	return eligibility.reader_ineligibility(company, shape.side, shape.columns, audited_sources_ok=ok)


def _party_types() -> frozenset[str]:
	return frozenset(frappe.get_all("Party Type", pluck="name"))


def _owner_only(doctype: str, user: str) -> bool:
	limited = (get_role_permissions(doctype, user=user) or {}).get("if_owner") or {}
	return bool({"read", "report", "select"} & set(limited))


def _payload(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			raise UnknownGeneration from None
	if not isinstance(raw, Mapping):
		raise UnknownGeneration
	return raw


def _doctype_list(raw) -> frozenset[str]:
	if not isinstance(raw, (list, tuple)) or not all(isinstance(name, str) for name in raw):
		raise UnknownGeneration
	return frozenset(raw)


def _has_unaudited(hooks, kind: str, doctypes: set[str], audited_sources_ok: bool) -> bool:
	"""True when a hook of `kind` on any of `doctypes` is not an audited one, or its shape is unknown."""
	by_doctype = hooks.get(kind, {}) if isinstance(hooks, dict) else None
	if not isinstance(by_doctype, dict):
		return True
	audited = AUDITED_HOOKS.get(kind, frozenset()) if audited_sources_ok else frozenset()
	for doctype in doctypes:
		paths = by_doctype.get(doctype, [])
		if isinstance(paths, str):
			paths = [paths]
		if not isinstance(paths, (list, tuple)) or not all(isinstance(path, str) for path in paths):
			return True
		if any(path not in audited for path in paths):
			return True
	return False
