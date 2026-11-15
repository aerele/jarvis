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
		# Outbound mail, public pages and automation (ruling R29: still a deny-list). Most
		# are already refused through ``_write_risk``; a doctype missing on a Frappe
		# version is harmless here.
		"Communication",
		"Email Template",
		"Email Account",
		"Email Queue",
		"Notification",
		"Assignment Rule",
		"Auto Email Report",
		"Newsletter",
		"Web Page",
		"Web Form",
		"Blog Post",
		"Website Settings",
		"Portal Settings",
		"Contact Us Settings",
		"Energy Point Rule",
		"Milestone Tracker",
		"Document Naming Rule",
		"Workflow Action",
	}
)
# Risky only through the portal-users child table (``_write_risk._FIELD_SENSITIVE`` lists
# nothing else for them). They stay valid targets, but a rule may not pick that table:
# ``spec.sensitive_picks`` refuses it on save and ``draft.create`` again at run time,
# because ERPNext gives each portal user the Customer role when the record is inserted.
_PORTAL_ONLY = frozenset({"Customer", "Supplier"})
TARGET_REFUSED_SUMMARY = "This rule's target is not allowed"
# ``create_refusal`` and the draft stage word it "You can't create <doctype>".
CREATE_REFUSAL_PREFIX = "You can't create"


def refused_by_name(doctype: str) -> bool:
	"""The deny-list half of ``refused_doctype``: structure, sensitive and access doctypes,
	judged by name and meta only. A caller that already knows the doctype is neither a
	Single nor a child table (the picker filters on both in its one query) uses this alone."""
	# Late import: the write-risk module pulls in the tool layer.
	from jarvis.tools import _write_risk

	if doctype in _EXTRA_REFUSED or doctype in _write_risk.STRUCTURE_DOCTYPES:
		return True
	if doctype in _PORTAL_ONLY:
		# Only the portal-users table is risky for these: refuse if a role list shows up too.
		return bool(_write_risk._role_tables(doctype))
	# Any other classified doctype (code, scripts, HTML/JS content, access grants,
	# Jarvis's own configuration, Employee, Auto Repeat) is refused (ruling R24).
	return bool(_write_risk.risk_class(doctype))


def refused_doctype(doctype: str) -> bool:
	"""True when ``doctype`` may be neither an intake target nor a Record source: a
	structure or sensitive doctype, a Single, or a child table."""
	if refused_by_name(doctype):
		return True
	row = frappe.db.get_value("DocType", doctype, ["issingle", "istable"], as_dict=True)
	return bool(row and (cint(row.issingle) or cint(row.istable)))


def refused_child(doctype: str) -> bool:
	"""True when a child table of ``doctype`` must not be filled by a rule: a structure or
	sensitive child, or a plain role list (Has Role and the like)."""
	from jarvis.tools import _write_risk

	name = _write_risk.canonical(doctype)
	return bool(
		name in _EXTRA_REFUSED
		or name in _write_risk.STRUCTURE_DOCTYPES
		or _write_risk.risk_class(name)
		or _write_risk._role_list(name)
	)


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
