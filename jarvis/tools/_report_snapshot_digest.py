"""The permission digest of a report snapshot: a hash of every permission input a read of
its rows depends on. A snapshot is stored with the digest of its owner at run time; a read
or reload compares the digest of the call (B.9.1)."""

import hashlib

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.permissions import get_role_permissions

from jarvis.tools._report_query_source import PARTY_REPORTS, _canonical

_DIGEST_MEMO = "jarvis_report_perm_digest"


def _stable(value):
	"""Order-independent form of a permission structure."""
	if isinstance(value, dict):
		return {str(k): _stable(v) for k, v in value.items()}
	if isinstance(value, list | tuple | set | frozenset):
		return sorted((_stable(v) for v in value), key=_canonical)
	return value


def _share_aggregate(doctype, user):
	"""One bounded aggregate of the read shares this user can see on a doctype."""
	from frappe.query_builder.functions import Count, Max
	from pypika import Field

	share = frappe.qb.DocType("DocShare")
	row = (
		frappe.qb.from_(share)
		.select(Count("*"), Max(share.modified))
		.where(share.share_doctype == doctype)
		.where(Field("read", table=share) == 1)
		.where((share.user == user) | (share.everyone == 1))
		.run()
	)
	count, latest = row[0] if row else (0, None)
	return [int(count or 0), str(latest) if latest else None]


def _doctype_inputs(doctype, user):
	try:
		perms = _stable(get_role_permissions(frappe.get_meta(doctype), user))
	except frappe.DoesNotExistError:
		perms = None
	return {"permissions": perms, "shares": _share_aggregate(doctype, user)}


def _compute_digest(user, report_name):
	report = frappe.db.get_value("Report", report_name, ["name", "ref_doctype"], as_dict=True) or {}
	# The database matches a name case-insensitively: look the party reports up by the
	# canonical name, as `_column_policy` does with `report.name`.
	canonical = report.get("name") or report_name
	doctypes = [report.get("ref_doctype")] if report.get("ref_doctype") else []
	if canonical in PARTY_REPORTS:
		doctypes.append(PARTY_REPORTS[canonical][0])
	payload = {
		"roles": sorted(frappe.get_roles(user)),
		"user_permissions": _stable(get_user_permissions(user)),
		"doctypes": {dt: _doctype_inputs(dt, user) for dt in doctypes},
	}
	return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def forget_perm_digest():
	"""Tests only: drop the per-request memo, as a new request would."""
	setattr(frappe.local, _DIGEST_MEMO, {})


def perm_digest(user, report_name, *, fresh=False):
	"""Digest of every permission input a snapshot read depends on, memoised once per request.
	`fresh=True` recomputes it (and refreshes the memo): the final check of a read, which
	must see a change another request made since the digest of the call was taken.

	Permitted fieldnames and masks are not inputs: `_column_policy` recomputes them on
	every read and the version covers the masked and denied columns. `Report.modified` is
	not an input either: report access (roles, ref doctype permission, disabled) is
	re-checked by `gate_report` on every read, and Frappe's prepared-report automation
	saves the Report in the middle of a slow run."""
	memo = getattr(frappe.local, _DIGEST_MEMO, None)
	if memo is None:
		memo = {}
		setattr(frappe.local, _DIGEST_MEMO, memo)
	key = (user, report_name)
	if fresh or key not in memo:
		memo[key] = _compute_digest(user, report_name)
	return memo[key]
