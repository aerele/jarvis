"""Pure scoped-visibility classification for bundled agent domains.

The permission snapshot is a launch-time fact. This module deliberately has no
Frappe or registry dependency so every caller applies the same narrow,
testable rule after it obtains an agent's bundled registry domain.
"""

import json
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
