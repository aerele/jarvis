"""Pure scoped-visibility classification for bundled agent domains.

The permission snapshot is a launch-time fact. This module deliberately has no
Frappe or registry dependency so every caller applies the same narrow,
testable rule after it obtains an agent's bundled registry domain.
"""

import hashlib
import json
import re
from collections.abc import Mapping

# GL-linked dimensions (other than Company) which make financial aggregates a
# record-scoped slice. Keep this established list byte-for-byte unchanged.
GL_SCOPED_DIMENSIONS = (
	"Account",
	"Cost Center",
	"Project",
	"Fiscal Year",
	"Finance Book",
	"Customer",
	"Supplier",
	"Employee",
	"Shareholder",
)

# Payroll conclusions can also be narrowed by HR dimensions that do not affect
# the generic GL classifier. A payroll run must label any such slice instead
# of presenting a workforce-wide result.
PAYROLL_SCOPED_DIMENSIONS = (
	"Employee",
	"Department",
	"Branch",
	"Designation",
	"Grade",
	"Leave Type",
	"Holiday List",
	"Salary Structure",
	"Salary Component",
)

PAYROLL_DOMAIN = "hrms-payroll"

# A visibility receipt is a SERVER-ONLY launch fact.  It binds the reviewed
# visibility policy to the permission profile that the scheduler captured before
# dispatch.  It is deliberately not carried in scope_json or the prompt: those
# values enter the delegate's context and cannot prove that a workforce-wide view
# was available.
VISIBILITY_RECEIPT_VERSION = 1
VISIBILITY_RECEIPT_STATES = ("complete", "sliced", "unknown")
_PROFILE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def is_payroll_domain(domain) -> bool:
	"""Return whether a bundled domain uses payroll visibility rules."""
	return isinstance(domain, str) and domain.strip().lower() == PAYROLL_DOMAIN


def scoped_dimensions(domain) -> tuple[str, ...]:
	"""Return the dimensions that can narrow this registry domain.

	Unknown domains intentionally retain the established GL behavior. This keeps
	legacy and newly added non-payroll agents compatible when a bundle does not
	declare a recognised domain.
	"""
	return PAYROLL_SCOPED_DIMENSIONS if is_payroll_domain(domain) else GL_SCOPED_DIMENSIONS


def exact_bool(value) -> bool | None:
	"""Return an exact boolean, or ``None`` for an absent or malformed value.

	``bool(0)`` and ``bool("false")`` must not turn untrusted persistence into a
	visibility decision. ``type`` is intentional because ``bool`` subclasses
	integer in Python.
	"""
	return value if type(value) is bool else None


def permissions_scoped_visibility(permissions, domain) -> bool | None:
	"""Classify a permission map as scoped, unscoped, or unknown.

	A valid map with no populated relevant dimension is ``False``. A malformed
	relevant entry is unknown rather than silently unscoped. The profile builder
	always serialises relevant entries as lists, so accepting another shape would
	turn a corrupt launch snapshot into a false clean result.
	"""
	if not isinstance(permissions, Mapping):
		return None
	for dimension in scoped_dimensions(domain):
		if dimension not in permissions:
			continue
		entries = permissions[dimension]
		if not isinstance(entries, list):
			return None
		if entries:
			return True
	return False


def profile_scoped_visibility(permission_profile, domain) -> bool | None:
	"""Classify a serialized permission profile as ``True``, ``False``, or unknown."""
	if isinstance(permission_profile, Mapping):
		profile = permission_profile
	elif isinstance(permission_profile, str):
		try:
			profile = json.loads(permission_profile)
		except (TypeError, ValueError):
			return None
	else:
		return None
	if not isinstance(profile, Mapping):
		return None
	return permissions_scoped_visibility(profile.get("user_permissions"), domain)


def _profile_mapping(permission_profile) -> Mapping | None:
	"""Return one JSON-object profile, or ``None`` for untrusted input."""
	if isinstance(permission_profile, Mapping):
		return permission_profile
	if not isinstance(permission_profile, str):
		return None
	try:
		profile = json.loads(permission_profile)
	except (TypeError, ValueError):
		return None
	return profile if isinstance(profile, Mapping) else None


def profile_hash(permission_profile) -> str | None:
	"""Validate and return the scheduler's permission-profile digest.

	The launch profile is a server fact, but it still reaches durable storage.  A
	missing, malformed, or tampered profile must never turn into a clean visibility
	decision.  Rebuild the digest from its complete canonical payload so a digest
	field by itself cannot bless a changed profile.
	"""
	profile = _profile_mapping(permission_profile)
	if profile is None:
		return None
	# ``_permission_profile`` has one canonical, closed shape.  Treat even an
	# otherwise harmless extra key as a changed profile.  This prevents a future
	# serializer change or a hand-edited stored value from inheriting a clean
	# receipt until this gate has reviewed that new shape.
	if set(profile) != {"hash", "roles", "user_permissions"}:
		return None
	roles = profile.get("roles")
	permissions = profile.get("user_permissions")
	digest = profile.get("hash")
	if (
		not isinstance(roles, list)
		or any(not isinstance(role, str) for role in roles)
		or not isinstance(permissions, Mapping)
		or not isinstance(digest, str)
		or not _PROFILE_HASH_RE.fullmatch(digest)
	):
		return None
	for doctype, entries in permissions.items():
		if not isinstance(doctype, str) or not isinstance(entries, list):
			return None
		if any(not isinstance(entry, str) or not entry for entry in entries):
			return None
	summary = {"roles": roles, "user_permissions": dict(permissions)}
	expected = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
	return expected if expected == digest else None


def make_visibility_receipt(permission_profile, visibility_domain) -> dict:
	"""Build the compact, server-only receipt for one visibility policy.

	Only ``hrms-payroll`` is currently reviewed.  Every other value, and every
	profile that cannot prove its own digest and relevant permission shape, is
	``unknown``.  The caller persists this object outside model-visible scope.
	"""
	domain = visibility_domain.strip() if isinstance(visibility_domain, str) else ""
	receipt = {
		"version": VISIBILITY_RECEIPT_VERSION,
		"visibility_domain": domain,
		"state": "unknown",
	}
	if not is_payroll_domain(domain):
		return receipt
	digest = profile_hash(permission_profile)
	state = profile_scoped_visibility(permission_profile, domain)
	if digest is None or state is None:
		return receipt
	receipt["state"] = "sliced" if state else "complete"
	receipt["profile_hash"] = digest
	return receipt


def parse_visibility_receipt(receipt, visibility_domain) -> dict | None:
	"""Validate a persisted receipt against its reviewed policy selector.

	``None`` means the durable receipt cannot be trusted.  Callers must treat it
	as partial, never as an unscoped success.
	"""
	if isinstance(receipt, str):
		try:
			receipt = json.loads(receipt)
		except (TypeError, ValueError):
			return None
	if not isinstance(receipt, Mapping):
		return None
	domain = visibility_domain.strip() if isinstance(visibility_domain, str) else ""
	if not is_payroll_domain(domain):
		return None
	if receipt.get("version") != VISIBILITY_RECEIPT_VERSION:
		return None
	if receipt.get("visibility_domain") != domain:
		return None
	state = receipt.get("state")
	if state not in VISIBILITY_RECEIPT_STATES:
		return None
	if state in ("complete", "sliced"):
		digest = receipt.get("profile_hash")
		if not isinstance(digest, str) or not _PROFILE_HASH_RE.fullmatch(digest):
			return None
		return {
			"version": VISIBILITY_RECEIPT_VERSION,
			"visibility_domain": domain,
			"state": state,
			"profile_hash": digest,
		}
	# An unknown state deliberately carries no proof that later code can misuse.
	return {
		"version": VISIBILITY_RECEIPT_VERSION,
		"visibility_domain": domain,
		"state": "unknown",
	}
