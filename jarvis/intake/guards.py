"""Document Intake: who may use it and what it may create.

Rules are written by admins, but the files come from any logged-in user, so every
paid step first asks: may this DocType be an intake target at all, and may THIS
uploader create it?
"""

import frappe
from frappe import _
from frappe.utils import cint

# Config and access doctypes on top of the write-risk lists. A draft of any of these is
# live configuration (most are not submittable), so no admin rule may produce one.
_EXTRA_REFUSED = frozenset(
	{
		"Role",
		"User Permission",
		"DocPerm",
		"Custom DocPerm",
		"Custom Field",
		"Property Setter",
		"Webhook",
		"Workflow",
		"Print Format",
		"Print Style",
		"Letter Head",
		"Server Script",
		"Client Script",
		"DocType",
		"Module Def",
		"System Settings",
		"Jarvis Settings",
	}
)
# Risky only through the optional portal-users child table (``_write_risk._FIELD_SENSITIVE``
# lists nothing else for them). A page cannot fill that table, so a draft cannot grant access.
_PORTAL_ONLY = frozenset({"Customer", "Supplier"})
TARGET_REFUSED_SUMMARY = "This rule's target is not allowed"
# ``create_refusal`` and the draft stage word it "You can't create <doctype>".
CREATE_REFUSAL_PREFIX = "You can't create"


def refused_doctype(doctype: str) -> bool:
	"""True when ``doctype`` may be neither an intake target nor a Record source: a
	structure or sensitive doctype, a Single, or a child table."""
	# Late import: the write-risk module pulls in the tool layer.
	from jarvis.tools import _write_risk

	if doctype in _EXTRA_REFUSED or doctype in _write_risk.STRUCTURE_DOCTYPES:
		return True
	if doctype in _PORTAL_ONLY:
		# Only the portal-users table is risky for these: refuse if a role list shows up too.
		if _write_risk._role_tables(doctype):
			return True
	# Any other classified doctype (code, scripts, HTML/JS content, access grants,
	# Jarvis's own configuration, Employee, Auto Repeat) is refused (ruling R24).
	elif _write_risk.risk_class(doctype):
		return True
	row = frappe.db.get_value("DocType", doctype, ["issingle", "istable"], as_dict=True)
	return bool(row and (cint(row.issingle) or cint(row.istable)))


def is_desk_user(user: str) -> bool:
	"""Intake serves desk users. A Website User (portal, signup) may hold an upload
	right on a folder, but never gets an AI call from it."""
	return bool(user) and frappe.db.get_value("User", user, "user_type") == "System User"


def create_refusal_text(target: str) -> str:
	return f"{CREATE_REFUSAL_PREFIX} {target}"


def create_refusal(user: str, target: str) -> str | None:
	"""Why ``user`` may not create a draft of ``target``, or None when they may."""
	if not is_desk_user(user) or not frappe.has_permission(target, "create", user=user):
		return create_refusal_text(target)
	return None
