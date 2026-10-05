"""The write-risk guard: structure changes are refused from chat, sensitive
configuration always gets its own confirmation card (round-2 decisions R2-4
REVISED AGAIN, R2-8, R2-10, R2-12; plan
``2026-10-03-round-2-confirm-flow-crm-drift.md`` task J1-guard).

Two layers, one source of truth (the doctype lists below):

1. ``risk_of`` / ``check`` read a tool call's ARGUMENTS (create / update /
   delete / batches / run_import). ``api._run_tool`` asks before it decides
   between a card and an uncarded run, so a structure write is refused on every
   route and a sensitive one parks even under auto mode, "confirm all", an armed
   macro or an approved skill run. ``apply_action``, the Approval Board edit,
   ``preview_doc`` and ``dispatch_confirmed`` ask too. ``run_method`` is not read
   here (R2-13: it stays open, a card normally and none in the uncarded modes).

2. The ORM guard (``guard_doc_event``, a ``doc_events`` "*" handler) checks the
   SAVE itself, on every route: ``run_method`` can reach ``frappe.client.insert``,
   a whitelisted method can insert a Property Setter, a hook can be skipped with
   ``flags.ignore_validate``. While a Jarvis
   tool call runs (``guard_scope``, entered by ``tools.registry.dispatch`` and
   ``apply_action``), a ROOT write of a structure or sensitive document is refused
   unless the confirmation card the user clicked named exactly that record (the
   allow entries ``dispatch_confirmed(allow_risky=True)`` builds from the card's
   arguments). A NESTED write - one a document's own controller or hooks make
   while another document is being saved, deleted or renamed, as from Desk
   (Employee -> User, Stock Settings -> Property Setter, Server Script ->
   Scheduled Job Type) - passes and is logged. Outside a tool call the guard does
   nothing: Desk, REST and scheduler saves are untouched.

Known limits, by Frappe's design: a doctype's OWN ``before_insert`` /
``before_validate`` controller method runs before any ``doc_events`` handler, so
work done there (Service Level Agreement adds fields in ``before_insert``) runs
before the guard can refuse; the argument layer refuses those doctypes first. A
DocType delete and an ``ignore_on_trash`` delete never call ``on_trash``, so
they are refused by doctype in the argument layer only. Accepted gap (R2-13,
aerele/jarvis#1635): a whitelisted method that changes state WITHOUT saving a
document (``frappe.db.set_value``, raw SQL, defaults) is not seen by this
save-time check and runs uncarded in the uncarded modes.

The guarded structure writes of R2-10 (one new Custom Field, a column-free
Custom Field edit, Workflow, CRM / Domain Settings) are NOT enabled here: they
are refused like every structure write. A later unit enables them by adding its
risk class to ``_ALLOWABLE`` (an allow entry then names that record) and turning
the refusal in ``check`` into its own card.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	SensitiveWriteRefusedError,
	StructureRefusedError,
	WriteRefusedError,
)

# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
# Saves that change the database structure (scout + spike, Frappe 15 and 16).
# Accounting Dimension adds fields to about 40 doctypes from a background job.
STRUCTURE_DOCTYPES = frozenset(
	{
		"Custom Field",
		"DocType",
		"Workflow",
		"Inventory Dimension",
		"Service Level Agreement",
		"CRM Settings",
		"Domain Settings",
		"Permission Type",
		"Accounting Dimension",
	}
)

# Always sensitive, with the risk each one carries (R2-8 + R2-12).
SENSITIVE_DOCTYPES = {
	"Server Script": "code",
	"Client Script": "code",
	"Scheduled Job Type": "code",
	"Website Script": "code",
	"Custom HTML Block": "code",
	"Webhook": "outbound",
	"Notification": "mail",
	"Auto Email Report": "mail",
	"Email Account": "mail",
	"Email Domain": "mail",
	"Custom DocPerm": "access",
	"Property Setter": "access",
	"User": "access",
	"Role": "access",
	"Role Profile": "access",
	"Module Profile": "access",
	"User Permission": "access",
	"Social Login Key": "login",
	"OAuth Client": "login",
	"Connected App": "login",
	"LDAP Settings": "login",
	# Defaults under R2-12 (review cycle 2): an Automation Flow runs scripts, calls
	# webhooks and sends notifications (Frappe 16); a Custom Role decides who opens a
	# Page or Report.
	"Automation Flow": "code",
	"Custom Role": "access",
}

# Jarvis's own configuration: sensitive in the ARGUMENT layer only (create_doc /
# update_doc / delete_doc park a card). Not in the ORM guard: Jarvis's own tools and
# turn machinery write some of these as part of their job.
_ARG_SENSITIVE = {
	"Jarvis Settings": "access",
	"Jarvis User Settings": "access",
	"Jarvis Agent Installation": "access",
	"Jarvis Agent Listing": "access",
	"Jarvis Macro": "access",
	"Jarvis Connector": "outbound",
	"MCP OAuth Client": "login",
	"MCP OAuth Token": "login",
}

# Sensitive by what the record IS: every write to one counts, delete included.
_TYPED_SENSITIVE = {
	"Report": ("code", "report_type", frozenset({"Script Report", "Query Report"})),
	"Jarvis Trigger": ("code", "action_type", frozenset({"Script"})),
}

# Sensitive only when a write puts content into one of these fields (R2-12: "the
# risk is re-checked on update when the field that makes it risky changes").
_CONTENT_SENSITIVE = {
	"Web Page": (
		"code",
		("javascript", "context_script", "main_section", "main_section_md", "main_section_html", "header"),
	),
	"Web Form": ("code", ("client_script", "introduction_text")),
	"Website Theme": ("code", ("js",)),
	"Website Settings": ("code", ("head_html", "brand_html", "banner_html")),
	"Web Template": ("code", ("template",)),
}
# Child-table fields that count for a content-sensitive doctype.
_CONTENT_CHILD_FIELDS = {"Web Page": (("page_blocks", "web_template_values"),)}

# Employee / Customer / Supplier writes that grant access through the record's own
# controller (a User, roles, User Permissions, portal users): sensitive (access) in
# the argument layer when the call sets one of these fields. The nested saves they
# cause pass the ORM guard as from Desk, so the card is where this is shown.
_FIELD_SENSITIVE = {
	# create_user_automatically (ERPNext 16): after insert, ERPNext creates and links
	# a User for the employee.
	"Employee": ("access", ("user_id", "create_user_permission", "create_user_automatically")),
	"Customer": ("access", ("portal_users",)),
	"Supplier": ("access", ("portal_users",)),
}

RISK_LINES = {
	"code": "This runs code for every user.",
	"outbound": "This sends data outside the site.",
	"mail": "This sends email to the listed people.",
	"access": "This changes who can see or edit.",
	"login": "This changes how people sign in.",
}
# Where a person sets up each structure change in Desk (the refusal carries it).
DESK_PATHS = {
	"Custom Field": "/app/customize-form",
	"DocType": "/app/doctype/new",
	"Workflow": "/app/workflow/new",
	"Inventory Dimension": "/app/inventory-dimension/new",
	"Service Level Agreement": "/app/service-level-agreement/new",
	"CRM Settings": "/app/crm-settings",
	"Domain Settings": "/app/domain-settings",
	"Permission Type": "/app/permission-type/new",
	"Accounting Dimension": "/app/accounting-dimension/new",
}

# Which risk classes a confirmation card may authorise at the ORM, and the ORM risk
# each one admits. A later unit (J1b-cf, J1c) adds its guarded structure classes
# here AND changes ``_target_risk`` / ``doc_risk`` so those writes classify as that
# class instead of plain "structure" (and ``check`` stops refusing them).
_ALLOWABLE = {"sensitive": frozenset({"sensitive"})}

_TOOLS_WITH_TARGETS = frozenset(
	{
		"create_doc",
		"create_docs",
		"update_doc",
		"delete_doc",
		"submit_doc",
		"cancel_doc",
		"amend_doc",
		"apply_workflow_action",
		"run_import",
	}
)
REFUSAL_CODES = frozenset({"structure_refused", "sensitive_refused"})

_LOCAL = "jarvis_write_guard"
_LOCAL_REFUSAL = "jarvis_write_guard_refusal"
_LOGGER = "jarvis.risky_write"
_CONDITIONAL = (*_TYPED_SENSITIVE, *_CONTENT_SENSITIVE, "Auto Repeat")
_NAMED: dict[str, str] = {}


def _all_named() -> dict[str, str]:
	"""Lower-cased name -> canonical name of every doctype this module classifies
	(names only; membership is always read from the live lists)."""
	if not _NAMED:
		names = [*STRUCTURE_DOCTYPES, *SENSITIVE_DOCTYPES, *_CONDITIONAL, *_FIELD_SENSITIVE, *_ARG_SENSITIVE]
		_NAMED.update({n.lower(): n for n in names})
	return _NAMED


def canonical(doctype) -> str:
	"""The classified doctype name for ``doctype`` (Frappe names are unique
	case-insensitively), or the stripped input when it is not one of ours."""
	if not isinstance(doctype, str):
		return ""
	clean = doctype.strip()
	return _all_named().get(clean.lower(), clean)


def risk_class(doctype: str) -> str:
	"""``code`` / ``outbound`` / ``mail`` / ``access`` / ``login`` for a sensitive
	doctype (conditional ones included), else ``""``."""
	dt = canonical(doctype)
	if dt in SENSITIVE_DOCTYPES:
		return SENSITIVE_DOCTYPES[dt]
	if dt in _TYPED_SENSITIVE:
		return _TYPED_SENSITIVE[dt][0]
	if dt in _CONTENT_SENSITIVE:
		return _CONTENT_SENSITIVE[dt][0]
	if dt in _FIELD_SENSITIVE:
		return _FIELD_SENSITIVE[dt][0]
	if dt in _ARG_SENSITIVE:
		return _ARG_SENSITIVE[dt]
	if dt == "Auto Repeat":
		return "mail"
	return ""


def desk_path(doctype: str, name=None) -> str:
	"""The Desk page for a structure change: the record when one is named, else
	the doctype's own setup page."""
	dt = canonical(doctype)
	slug = "/app/" + frappe.scrub(dt).replace("_", "-")
	if name:
		return f"{slug}/{name}"
	return DESK_PATHS.get(dt) or slug


# --------------------------------------------------------------------------- #
# Argument layer
# --------------------------------------------------------------------------- #
@dataclass
class _Target:
	doctype: str
	op: str  # create / update / delete / import / other
	name: str | None = None
	values: dict = field(default_factory=dict)


def _targets(tool: str, args) -> list[_Target]:
	"""Every document a call names, with what it does to it."""
	a = args if isinstance(args, dict) else {}
	out: list[_Target] = []
	if tool in ("create_doc", "create_docs"):
		if isinstance(a.get("docs"), list):
			for d in a["docs"]:
				if isinstance(d, dict):
					values = d.get("values") if isinstance(d.get("values"), dict) else {}
					out.append(_Target(canonical(d.get("doctype")), "create", values.get("name"), values))
		if tool == "create_doc" and a.get("doctype"):
			values = a.get("values") if isinstance(a.get("values"), dict) else {}
			out.append(_Target(canonical(a["doctype"]), "create", values.get("name"), values))
		return out
	doctype = canonical(a.get("doctype"))
	if not doctype:
		return out
	if tool == "update_doc":
		if isinstance(a.get("updates"), list):
			for u in a["updates"]:
				if isinstance(u, dict):
					changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
					out.append(_Target(doctype, "update", u.get("name"), changes))
		else:
			changes = a.get("changes") if isinstance(a.get("changes"), dict) else {}
			out.append(_Target(doctype, "update", a.get("name") or None, changes))
		return out
	if tool == "run_import":
		return [_Target(doctype, "import", values=a)]  # the call's arguments: the file
	op = "delete" if tool == "delete_doc" else "other"
	names = a.get("names") if isinstance(a.get("names"), list) else [a.get("name")]
	for n in names or [None]:
		out.append(_Target(doctype, op, n if isinstance(n, str | int) else None))
	return out


def _is_batch(tool: str, args) -> bool:
	a = args if isinstance(args, dict) else {}
	return tool == "create_docs" or any(
		isinstance(a.get(k), list) and a[k] for k in ("docs", "updates", "names")
	)


def _filled(value) -> bool:
	if value is None:
		return False
	if isinstance(value, str):
		return bool(value.strip())
	return bool(value)


class _ArgView:
	"""A target's resulting values: the call's own values, else (update) the stored
	record, else (create) the field default."""

	def __init__(self, target: _Target):
		self.t = target

	def changed(self, fieldname: str) -> bool:
		return fieldname in self.t.values

	def get(self, fieldname: str):
		if fieldname in self.t.values:
			return self.t.values[fieldname]
		if self.t.op == "create":
			try:
				df = frappe.get_meta(self.t.doctype).get_field(fieldname)
			except Exception:
				return None
			return df.default if df else None
		return self._stored_value(fieldname)

	def _stored_value(self, fieldname: str):
		if not self.t.name and not _is_single(self.t.doctype):
			return None
		try:
			if _is_single(self.t.doctype):
				return frappe.db.get_single_value(self.t.doctype, fieldname)
			return frappe.db.get_value(self.t.doctype, self.t.name, fieldname)
		except Exception:
			return None

	def before_value(self, fieldname: str):
		return None if self.t.op == "create" else self._stored_value(fieldname)

	def child_values(self, table: str, fieldname: str) -> list | None:
		rows = self.t.values.get(table)
		if not isinstance(rows, list):
			return None
		return [r.get(fieldname) for r in rows if isinstance(r, dict)]


class _DocView:
	"""A document being saved, compared with the stored record."""

	def __init__(self, doc):
		self.doc = doc
		self.before = None if _is_new(doc) else doc.get_doc_before_save()

	def changed(self, fieldname: str) -> bool:
		if self.before is None:
			return True
		return self.before.get(fieldname) != self.doc.get(fieldname)

	def get(self, fieldname: str):
		return self.doc.get(fieldname)

	def before_value(self, fieldname: str):
		return None if self.before is None else self.before.get(fieldname)

	def child_values(self, table: str, fieldname: str) -> list | None:
		now = [r.get(fieldname) for r in self.doc.get(table) or []]
		if self.before is not None:
			then = [r.get(fieldname) for r in self.before.get(table) or []]
			if now == then:
				return None
		return now


def _is_new(doc) -> bool:
	try:
		return bool(doc.is_new())
	except Exception:
		return False


def _is_single(doctype: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).issingle)
	except Exception:
		return False


def _conditional_risky(doctype: str, view, op: str) -> bool:
	"""Whether a write to a conditionally sensitive doctype is risky (R2-12)."""
	if doctype in _TYPED_SENSITIVE:
		_risk, fieldname, kinds = _TYPED_SENSITIVE[doctype]
		return view.get(fieldname) in kinds
	if op == "delete":
		return False  # removing script or a mail rule is not the risk these carry
	if doctype == "Auto Repeat":
		touched = view.changed("notify_by_email") or view.changed("recipients")
		return (
			touched
			and bool(frappe.utils.cint(view.get("notify_by_email")))
			and _filled(view.get("recipients"))
		)
	if doctype in _CONTENT_SENSITIVE:
		if doctype == "Web Template" and frappe.utils.cint(view.get("standard")):
			return False  # a standard template ships with an app, not from chat
		_risk, fields = _CONTENT_SENSITIVE[doctype]
		if any(view.changed(f) and _filled(view.get(f)) for f in fields):
			return True
		for table, child_field in _CONTENT_CHILD_FIELDS.get(doctype, ()):
			values = view.child_values(table, child_field)
			if values and any(_filled(v) for v in values):
				return True
	return False


def _target_risk(target: _Target) -> str | None:
	dt = target.doctype
	if dt in STRUCTURE_DOCTYPES:
		return "structure"
	if dt in SENSITIVE_DOCTYPES:
		return "sensitive"
	if dt in _CONDITIONAL:
		if target.op == "import":
			# A Data Import runs in a background job where the ORM guard is inert, and
			# its rows are not known here: any conditional doctype parks a card.
			return "sensitive"
		return "sensitive" if _conditional_risky(dt, _ArgView(target), target.op) else None
	if dt in _ARG_SENSITIVE:
		return "sensitive"
	if dt in _FIELD_SENSITIVE and target.op in ("create", "update", "import"):
		if target.op == "import":
			return "sensitive" if _import_grants(dt, target.values) else None
		_risk, fields = _FIELD_SENSITIVE[dt]
		if any(_grants(target, f) for f in fields):
			return "sensitive"
		return "sensitive" if dt == "Employee" and _toggles_login(_ArgView(target)) else None
	return None


def _import_grants(doctype: str, args: dict) -> bool:
	"""Whether a Data Import of a field-sensitive doctype writes an access-granting
	field (``_FIELD_SENSITIVE``): a column for one of its fields, or for a child row of
	one of its tables (portal users), with a value in any row. An Employee ``status``
	column counts when the import updates existing records (``_toggles_login``: the
	linked login follows "Active"); on new records the user comes from ``user_id``,
	already a counted field.

	A file that cannot be read or parsed at all (a missing or mistyped file, a
	header-only file, a bad mapping key, no permission) is not judged here: nothing
	can be imported from it, and the import's own validation (``api._import_park``,
	then ``run_import``) answers with its real, fixable error. A file that WAS read
	but whose columns cannot be classified fails closed (True)."""
	from jarvis.tools._import_preview import import_columns

	import_type = args.get("import_type") or "Insert New Records"
	try:
		columns = import_columns(
			doctype=doctype,
			file_url=args.get("file_url"),
			filename=args.get("filename"),
			import_type=import_type,
			mapping=args.get("mapping") if isinstance(args.get("mapping"), dict) else None,
		)
	except (InvalidArgumentError, frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError):
		frappe.clear_messages()
		return False  # unreadable / unparseable: the import's own validation refuses it
	except Exception:
		frappe.clear_messages()
		return True  # read, but its columns could not be classified: fail closed
	_risk, fields = _FIELD_SENSITIVE[doctype]
	updates = import_type != "Insert New Records"
	for col in columns:
		if not col["filled"]:
			continue
		if col["tables"]:
			if any(t in fields for t in col["tables"]):
				return True
		elif col["field"] in fields or (doctype == "Employee" and col["field"] == "status" and updates):
			return True
	return False


def _toggles_login(view) -> bool:
	"""An Employee status change that enables or disables its linked User (Employee
	``update_user_status``: the login follows "Active"). Re-enabling a disabled
	login, or cutting one off, is access."""
	if not view.changed("status") or not _filled(view.get("user_id")):
		return False
	before = view.before_value("status")
	return (before == "Active") != (view.get("status") == "Active")


def _grants(target: _Target, fieldname: str) -> bool:
	"""Whether a create / update SETS an access-granting field: filled and, on an
	update, different from what is stored (an echoed unchanged value is not a
	change). A table (portal users) compares the set of users."""
	if fieldname not in target.values or not _filled(target.values.get(fieldname)):
		return False
	if target.op != "update" or not target.name:
		return True
	new = target.values.get(fieldname)
	try:
		if isinstance(new, list):
			stored = frappe.get_all(
				"Portal User",
				filters={"parent": target.name, "parenttype": target.doctype, "parentfield": fieldname},
				pluck="user",
			)
			wanted = {r.get("user") for r in new if isinstance(r, dict) and r.get("user")}
			return wanted != set(stored)
		stored = frappe.db.get_value(target.doctype, target.name, fieldname)
	except Exception:
		return True
	return str(new) != str(stored or "")


def risk_of(tool: str, args) -> str | None:
	"""``None`` / ``"structure"`` / ``"custom_field_new"`` / ``"sensitive"`` for one
	tool call. ``custom_field_new`` is a single create of one Custom Field (not an
	update, a delete or a batch); any structure doctype in a batch is
	``structure``. ``run_method`` is not classified (R2-13); the ORM guard judges
	what it saves."""
	if tool not in _TOOLS_WITH_TARGETS:
		return None
	targets = _targets(tool, args)
	risks = [_target_risk(t) for t in targets]
	if "structure" in risks:
		single = len(targets) == 1 and not _is_batch(tool, args)
		if single and tool == "create_doc" and targets[0].doctype == "Custom Field":
			return "custom_field_new"
		return "structure"
	return "sensitive" if "sensitive" in risks else None


def risk_line(tool: str, args) -> str:
	"""The line a card shows for a sensitive call: the risk of its first sensitive
	target."""
	for t in _targets(tool, args):
		if _target_risk(t) == "sensitive":
			return RISK_LINES[risk_class(t.doctype)]
	return ""


def risk_lines(tool: str, args) -> list[str]:
	"""Every distinct risk line a card must show for a sensitive call, in target
	order (a batch with a Client Script and a Role names both). Display only."""
	out = []
	for t in _targets(tool, args):
		if _target_risk(t) == "sensitive":
			line = RISK_LINES[risk_class(t.doctype)]
			if line not in out:
				out.append(line)
	return out


def check(tool: str, args) -> str | None:
	"""The argument-layer decision for one call: raise ``WriteRefusedError`` for a
	refused write, return ``"sensitive"`` when it must park behind a card in every
	mode, else ``None``."""
	risk = risk_of(tool, args)
	if risk in ("structure", "custom_field_new"):
		first = next((t for t in _targets(tool, args) if t.doctype in STRUCTURE_DOCTYPES), None)
		named = first.name if first is not None and first.op in ("update", "delete", "other") else None
		raise structure_refusal(first.doctype if first else "", named)
	return risk


def first_risky_target(tool: str, args) -> tuple[str, str | None]:
	"""(doctype, name) of the first structure / sensitive target, for the log line."""
	for t in _targets(tool, args):
		if _target_risk(t):
			return t.doctype, t.name
	a = args if isinstance(args, dict) else {}
	return canonical(a.get("doctype")) or "", a.get("name")


# --------------------------------------------------------------------------- #
# Refusals: construction, envelope, log line
# --------------------------------------------------------------------------- #
def structure_refusal(doctype: str = "", name=None) -> StructureRefusedError:
	if not doctype:
		return StructureRefusedError(
			"This change alters the database structure, so it is set up in Desk, not from chat. "
			"Do not retry it with another tool."
		)
	dt = canonical(doctype)
	return StructureRefusedError(
		f"{dt} changes the database structure, so it is set up in Desk, not from chat. "
		"Do not retry it with another tool; tell the user where to do it.",
		doctype=dt,
		desk_path=desk_path(dt, name),
	)


def refused_envelope(e: WriteRefusedError, detail: str = "") -> dict:
	"""The ``{ok: false, error}`` envelope for a refusal: its own code, the refusing
	doctype (``error.doctype``) and for a structure change the Desk page."""
	from frappe.utils import strip_html

	from jarvis._responses import err

	hint = ""
	if isinstance(e, StructureRefusedError):
		hint = (
			f"Set this up in Desk: open {e.desk_path}."
			if e.desk_path
			else "Set this up in Desk; it cannot be done from chat."
		)
	elif isinstance(e, SensitiveWriteRefusedError):
		hint = "Ask for this change on its own in chat so it gets a confirmation card."
	env = err(e.code, strip_html(str(e)).strip(), detail=detail, hint=hint)
	if getattr(e, "desk_path", ""):
		env["error"]["desk_path"] = e.desk_path
	if getattr(e, "doctype", ""):
		env["error"]["doctype"] = e.doctype
	return env


def log_line(risk: str, doctype: str, name, outcome: str, **extra) -> None:
	"""The searchable audit line (E4), as key=value with quoted names:
	``jarvis.risky_write risk=sensitive doctype="Server Script" name="x" user="u"
	outcome=refused``. Never raises."""
	try:
		fields = {
			"risk": risk,
			"doctype": json.dumps(str(doctype or "")),
			"name": json.dumps(str(name or "")),
			"user": json.dumps(str(frappe.session.user)),
			"outcome": outcome,
		}
		fields.update({k: json.dumps(str(v)) for k, v in extra.items()})
		frappe.logger(_LOGGER).info("jarvis.risky_write " + " ".join(f"{k}={v}" for k, v in fields.items()))
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# ORM layer
# --------------------------------------------------------------------------- #
@dataclass
class _Allow:
	doctype: str  # "*" admits any doctype of the risk classes
	name: str | None
	risks: frozenset
	multi: bool = False  # admits every root save that matches, not just one record
	bound: tuple | None = None


@dataclass
class _GuardState:
	allow: list = field(default_factory=list)
	admitted: list = field(default_factory=list)  # (doctype, name) an allow let through


def allow_entries(tool: str, args) -> list[_Allow]:
	"""What a confirmed card authorises at the ORM. Built only by the confirm paths.

	- one entry per structure / sensitive target the card showed, naming the record
	  (a create binds to the first root save of its doctype);
	- a run_import entry admits every root save of its doctype (each row);
	- a confirmed run_method admits every root write of the SENSITIVE classes
	  (never structure) for that call: the user confirmed the call itself, and a
	  method's writes cannot be named in advance (R2-13)."""
	if tool == "run_method":
		return [_Allow("*", None, _ALLOWABLE["sensitive"], multi=True)]
	out = []
	for t in _targets(tool, args):
		admits = _ALLOWABLE.get(_target_risk(t) or "")
		if not admits:
			continue
		if t.op == "import":
			out.append(_Allow(t.doctype, None, admits, multi=True))
			continue
		name = t.name
		if t.op == "create":
			name = None
		elif not name and _is_single(t.doctype):
			name = t.doctype
		out.append(_Allow(t.doctype, str(name) if name is not None else None, admits))
	return out


@contextmanager
def guard_scope(allow: list | None = None) -> Iterator[_GuardState]:
	"""Mark a Jarvis tool call: ROOT writes of structure / sensitive documents are
	refused unless ``allow`` names them. ``allow=None`` joins an enclosing scope
	(so the confirm path's allow survives ``registry.dispatch``); a list always
	opens a fresh one. Set and cleared in try/finally around each call."""
	_install_hooks()
	prev = getattr(frappe.local, _LOCAL, None)
	if allow is None and prev is not None:
		yield prev
		return
	state = _GuardState(list(allow or []))
	setattr(frappe.local, _LOCAL, state)
	try:
		yield state
	finally:
		setattr(frappe.local, _LOCAL, prev)


def take_refusal() -> WriteRefusedError | None:
	"""The refusal the ORM guard raised since the last call, cleared. A caller
	checks it after a tool returns: a method that swallowed the guard's exception
	(``reset_password`` does) must not report success."""
	refusal = getattr(frappe.local, _LOCAL_REFUSAL, None)
	setattr(frappe.local, _LOCAL_REFUSAL, None)
	return refusal


def _active_state() -> _GuardState | None:
	state = getattr(frappe.local, _LOCAL, None)
	if state is not None:
		return state
	# Dry runs that bump the dispatch depth themselves (held_writes.collect_missing,
	# held_sheets._dry_run) are tool calls too.
	from jarvis.tools.registry import in_tool_dispatch

	return _GuardState() if in_tool_dispatch() else None


def doc_risk(doc, event: str) -> str | None:
	"""``structure`` / ``sensitive`` / ``None`` for the document an event fires on."""
	dt = canonical(doc.doctype)
	if dt in STRUCTURE_DOCTYPES:
		return "structure"
	if dt in SENSITIVE_DOCTYPES:
		return "sensitive"
	if dt in _CONDITIONAL:
		op = "delete" if event in ("on_trash", "before_rename") else "update"
		return "sensitive" if _conditional_risky(dt, _DocView(doc), op) else None
	if dt in _FIELD_SENSITIVE and event in ("before_validate", "before_change"):
		return "sensitive" if _doc_grants(dt, _DocView(doc)) else None
	return None


def _doc_grants(dt: str, view) -> bool:
	"""The field rule at the save, so it holds on every route (``frappe.client``
	through run_method too): a root Employee / Customer / Supplier save that sets
	or changes an access-granting field, or an Employee status change that turns
	its user's login on or off. Everyday edits touch none of these."""
	_risk, fields = _FIELD_SENSITIVE[dt]
	for fieldname in fields:
		if fieldname == "portal_users":
			now = {r.get("user") for r in view.doc.get(fieldname) or [] if r.get("user")}
			then = (
				{r.get("user") for r in view.before.get(fieldname) or [] if r.get("user")}
				if view.before is not None
				else set()
			)
			if now != then:
				return True
		elif view.changed(fieldname) and _filled(view.get(fieldname)):
			return True
	return dt == "Employee" and _toggles_login(view)


_FRAME_CODES = None


def _frame_codes():
	"""The code objects of the frames that mean "a document is being written"."""
	global _FRAME_CODES
	if _FRAME_CODES is None:
		import inspect

		from frappe.model import delete_doc, rename_doc
		from frappe.model.document import Document

		def code(fn):
			return getattr(inspect.unwrap(fn), "__code__", None)

		_FRAME_CODES = (
			{code(Document.insert), code(Document._save)} - {None},
			code(delete_doc.delete_doc),
			code(rename_doc.rename_doc),
		)
	return _FRAME_CODES


def _same(a, b) -> bool:
	if a is b:
		return True
	try:
		return a.doctype == b.doctype and a.name is not None and a.name == b.name
	except Exception:
		return False


def nested_under(doc):
	"""The document whose save / delete / rename is on the call stack above this
	one's, or None when ``doc`` is the tool's own (root) write."""
	save_codes, delete_code, rename_code = _frame_codes()
	frame = sys._getframe(1)
	while frame is not None:
		code = frame.f_code
		if code in save_codes:
			other = frame.f_locals.get("self")
			if other is not None and not _same(other, doc):
				return other
		elif code is delete_code:
			other = frame.f_locals.get("doc")
			if other is not None and not _same(other, doc):
				return other
			loc_dt, loc_name = frame.f_locals.get("doctype"), frame.f_locals.get("name")
			if other is None and (loc_dt, loc_name) != (doc.doctype, doc.name):
				return frappe._dict(doctype=loc_dt, name=loc_name)
		elif code is rename_code:
			loc_dt, loc_old = frame.f_locals.get("doctype"), frame.f_locals.get("old")
			if (loc_dt, loc_old) != (doc.doctype, doc.name):
				return frappe._dict(doctype=loc_dt, name=loc_old)
		frame = frame.f_back
	return None


def _consume_allow(state: _GuardState, doc, risk: str) -> bool:
	dt, name = canonical(doc.doctype), doc.name
	for entry in state.allow:
		if risk not in entry.risks or entry.doctype not in (dt, "*"):
			continue
		if entry.multi:
			_admit(state, dt, name)
			return True
		if entry.bound is not None:
			if entry.bound == (dt, name):
				return True
			continue
		if entry.name is None or str(entry.name) == str(name):
			entry.bound = (dt, name)
			_admit(state, dt, name)
			return True
	return False


def _admit(state: _GuardState, dt: str, name) -> None:
	if (dt, name) not in state.admitted:
		state.admitted.append((dt, name))


def _in_sandbox() -> bool:
	from jarvis.tools._preview_sandbox import _dropped_jobs

	return _dropped_jobs() is not None


def _refuse(e: WriteRefusedError):
	setattr(frappe.local, _LOCAL_REFUSAL, e)
	raise e


def guard_doc_event(doc, method=None, *args, **kwargs):
	"""``doc_events["*"]`` handler on before_validate / before_change /
	before_rename / on_trash. A no-op outside a Jarvis tool call."""
	state = _active_state()
	if state is None:
		return
	risk = doc_risk(doc, method or "")
	if not risk:
		return
	dt = canonical(doc.doctype)
	parent = nested_under(doc)
	if parent is not None:
		log_line(risk, dt, doc.name, "nested", under=f"{parent.doctype} {parent.name}")
		return
	if risk == "sensitive" and _in_sandbox():
		return  # a dry run builds the card; everything it wrote is rolled back
	if _consume_allow(state, doc, risk):
		return
	log_line(risk, dt, doc.name, "refused", event=method or "")
	if risk == "structure":
		_refuse(structure_refusal(dt, None if _is_new(doc) else doc.name))
	if state.allow:
		_refuse(
			SensitiveWriteRefusedError(
				f"This action also tried to change {dt} {doc.name or ''}".rstrip()
				+ ", which its confirmation card did not show, so nothing was saved.",
				doctype=dt,
			)
		)
	_refuse(
		SensitiveWriteRefusedError(
			f"{dt} is sensitive configuration ({RISK_LINES[risk_class(dt)].rstrip('.').lower()}), so it "
			"needs its own confirmation card. Ask for it with create_doc, update_doc or delete_doc so the "
			"user sees it in full; it cannot be changed as part of another action.",
			doctype=dt,
		)
	)


# --------------------------------------------------------------------------- #
# Deletes that run no hook, and background jobs (review cycle 3)
# --------------------------------------------------------------------------- #
# Frappe deletes a DocType without calling any document hook, and
# ``ignore_on_trash`` skips on_trash: both still go through
# ``frappe.model.delete_doc.delete_from_table``, which ``delete_doc`` looks up in
# its module at call time (so callers that imported ``delete_doc`` by name are
# covered too). Background jobs a tool call enqueues run in a worker with no guard
# scope: ``guard_queue`` (called from the preview sandbox's ``get_queue`` wrap, the
# one place every enqueue resolves its queue) re-routes them through
# ``execute_guarded_job``, which re-enters a scope carrying the card's allow entries.
# Both wraps are installed once per process and are a pass-through outside a
# Jarvis tool call.
_hooks_installed = False


def _install_hooks() -> None:
	global _hooks_installed
	if _hooks_installed:
		return
	from frappe.model import delete_doc as delete_module

	from jarvis.tools import _preview_sandbox

	real = delete_module.delete_from_table
	if not getattr(real, "_jarvis_guarded", False):

		def delete_from_table(doctype, name, ignore_doctypes, doc):
			_guard_delete(doctype, name, doc)
			return real(doctype, name, ignore_doctypes, doc)

		delete_from_table._jarvis_guarded = True
		delete_module.delete_from_table = delete_from_table
	_preview_sandbox._install_guard()
	_hooks_installed = True


def _guard_delete(doctype, name, doc) -> None:
	"""Judge a delete at ``delete_from_table``: structure refused, sensitive
	refused outside a confirmed allow, whatever flags the delete was called with."""
	state = _active_state()
	if state is None:
		return
	dt = canonical(doctype)
	target = doc if doc is not None else frappe._dict(doctype=dt, name=name)
	if dt in STRUCTURE_DOCTYPES:
		risk = "structure"
	elif dt in SENSITIVE_DOCTYPES or (doc is not None and doc_risk(doc, "on_trash")):
		risk = "sensitive"
	else:
		return
	if nested_under(target) is not None:
		return
	if risk == "sensitive" and (_in_sandbox() or _consume_allow(state, target, risk)):
		return
	log_line(risk, dt, name, "refused", event="delete")
	if risk == "structure":
		_refuse(structure_refusal(dt, name if dt != "DocType" else None))
	_refuse(
		SensitiveWriteRefusedError(
			f"{dt} is sensitive configuration, so deleting it needs its own confirmation card. "
			"Ask for it with delete_doc.",
			doctype=dt,
		)
	)


def _allow_payload(state: _GuardState) -> list[dict]:
	"""A state's allow entries as plain data for a job: never structure, and never
	an unbound create (it names no record yet, so it would admit the first record
	of its doctype the job saves)."""
	return [
		{"doctype": e.doctype, "name": e.name, "risks": sorted(e.risks), "multi": e.multi, "bound": e.bound}
		for e in state.allow
		if "structure" not in e.risks and (e.multi or e.name is not None or e.bound is not None)
	]


class _GuardedQueue:
	"""An RQ queue whose jobs run through ``execute_guarded_job``, whichever rq
	entry point queues them (``enqueue_call``, ``enqueue``, ``enqueue_at``,
	``enqueue_in``, ``enqueue_many``, ``create_job``). A Frappe job keeps its own
	arguments (``method``, ``kwargs``), so ``get_jobs`` dedupe and job
	introspection see the real job; the allow entries ride beside them. A bare
	function queued straight onto rq is wrapped as a Frappe job for this site."""

	def __init__(self, queue, payload: list[dict]):
		self._queue = queue
		self._payload = payload

	def __getattr__(self, attr):
		return getattr(self._queue, attr)

	def _guard(self, func, args, kwargs):
		from frappe.utils.background_jobs import execute_job

		# Frappe 16 passes the dotted path, Frappe 15 the function itself.
		if func in (_FRAPPE_EXECUTE_JOB, _GUARDED_EXECUTE_JOB, execute_job, execute_guarded_job):
			queue_args = dict(kwargs or {})
		else:
			queue_args = {
				"site": frappe.local.site,
				"user": frappe.session.user,
				"method": func,
				"event": None,
				"job_name": func if isinstance(func, str) else getattr(func, "__qualname__", "job"),
				"is_async": True,
				"kwargs": dict(kwargs or {}),
				"_jarvis_args": list(args or ()),
			}
			args = None
		queue_args["_jarvis_allow"] = self._payload  # always this scope's entries
		return _GUARDED_EXECUTE_JOB, args, queue_args

	def enqueue_call(self, func, args=None, kwargs=None, **options):
		func, args, kwargs = self._guard(func, args, kwargs)
		return self._queue.enqueue_call(func, args=args, kwargs=kwargs, **options)

	def create_job(self, func, args=None, kwargs=None, **options):
		func, args, kwargs = self._guard(func, args, kwargs)
		return self._queue.create_job(func, args=args, kwargs=kwargs, **options)

	# rq's own entry points, run with this proxy as ``self`` so they reach the
	# guarded ``enqueue_call`` / ``create_job`` above.
	def enqueue(self, *args, **kwargs):
		return type(self._queue).enqueue(self, *args, **kwargs)

	def enqueue_at(self, *args, **kwargs):
		return type(self._queue).enqueue_at(self, *args, **kwargs)

	def enqueue_in(self, *args, **kwargs):
		return type(self._queue).enqueue_in(self, *args, **kwargs)

	def enqueue_many(self, *args, **kwargs):
		return type(self._queue).enqueue_many(self, *args, **kwargs)


def guard_queue(queue):
	"""``queue`` unchanged outside a Jarvis tool call (and inside a dry run, whose
	jobs are dropped anyway); a ``_GuardedQueue`` inside one."""
	state = _active_state()
	if state is None or _in_sandbox():
		return queue
	return _GuardedQueue(queue, _allow_payload(state))


def execute_guarded_job(_jarvis_allow=None, _jarvis_args=None, **queue_args):
	"""The worker side: Frappe's ``execute_job`` with the job's method wrapped in a
	guard scope carrying the confirmed card's allow entries (none for an uncarded
	call). The wrapper presents the real method's module and name, so
	``frappe.local.job.method``, before_job / after_job hooks and the Error Log
	title name the real job. A refusal the job swallows still fails it."""
	from frappe.utils.background_jobs import execute_job

	allow = [
		_Allow(
			e["doctype"],
			e["name"],
			frozenset(e["risks"]),
			multi=e["multi"],
			bound=tuple(e["bound"]) if e.get("bound") else None,
		)
		for e in _jarvis_allow or []
		if "structure" not in e.get("risks", ())
	]
	real = queue_args["method"]

	def guarded(**kwargs):
		method = frappe.get_attr(real) if isinstance(real, str) else real
		with guard_scope(allow):
			take_refusal()
			result = method(*(_jarvis_args or ()), **kwargs)
			hidden = take_refusal()
			if hidden is not None:
				raise hidden
		return result

	if isinstance(real, str):
		guarded.__module__, _, guarded.__qualname__ = real.rpartition(".")
	else:
		guarded.__module__, guarded.__qualname__ = real.__module__, real.__qualname__
	guarded.__name__ = guarded.__qualname__.rpartition(".")[2]
	queue_args["method"] = guarded
	return execute_job(**queue_args)


_FRAPPE_EXECUTE_JOB = "frappe.utils.background_jobs.execute_job"
_GUARDED_EXECUTE_JOB = "jarvis.tools._write_risk.execute_guarded_job"


# --------------------------------------------------------------------------- #
# No-commit fence (uncarded create / update)
# --------------------------------------------------------------------------- #
def _no_commit(*args, **kwargs) -> None:
	"""``frappe.db.commit`` while an uncarded create / update runs."""


@contextmanager
def no_commit_fence() -> Iterator[None]:
	"""Neutralise ``frappe.db.commit`` for an uncarded create / update. A save that
	runs DDL (``sql_ddl`` commits first) then reaches Frappe's implicit-commit check
	with writes pending, and Frappe raises ``ImplicitCommitError`` BEFORE the ALTER
	runs, so a doctype outside the structure list still cannot change the schema
	uncarded. Everything else commits with the request as usual.

	- ``transaction_writes`` is bumped on entry so a save whose FIRST statement is
	  DDL is refused too (the check only fires with writes pending).
	- Frappe 16's ``_disable_transaction_control`` is restored on exit: ``sql_ddl``
	  zeroes it and restores it without a ``finally``, so a refused DDL inside a
	  doc_events handler would leave it at -1 (the handler's finally decrements)
	  and silently disable every later commit and rollback in the request."""
	db = frappe.db
	had_own = "commit" in db.__dict__
	real = db.commit
	control = getattr(db, "_disable_transaction_control", None)
	db.commit = _no_commit
	db.transaction_writes = (getattr(db, "transaction_writes", 0) or 0) + 1
	try:
		yield
	finally:
		if had_own:
			db.commit = real
		else:
			del db.commit
		db.transaction_writes = max(0, (getattr(db, "transaction_writes", 1) or 1) - 1)
		if control is not None:
			db._disable_transaction_control = control


_IMPLICIT_COMMIT_TEXT = "can cause implicit commit"


def is_implicit_commit(e: BaseException | None) -> bool:
	"""``ImplicitCommitError`` anywhere in the cause chain, or its message (erpnext
	``install_country_fixtures`` and hrms ``run_regional_setup`` re-raise it as a
	plain error)."""
	cls = getattr(frappe.exceptions, "ImplicitCommitError", None)
	seen = 0
	while e is not None and seen < 10:
		if (cls is not None and isinstance(e, cls)) or _IMPLICIT_COMMIT_TEXT in str(e):
			return True
		e = e.__cause__ or e.__context__
		seen += 1
	return False


__all__ = [
	"DESK_PATHS",
	"REFUSAL_CODES",
	"RISK_LINES",
	"SENSITIVE_DOCTYPES",
	"STRUCTURE_DOCTYPES",
	"WriteRefusedError",
	"allow_entries",
	"canonical",
	"check",
	"desk_path",
	"doc_risk",
	"execute_guarded_job",
	"first_risky_target",
	"guard_doc_event",
	"guard_queue",
	"guard_scope",
	"is_implicit_commit",
	"log_line",
	"nested_under",
	"no_commit_fence",
	"refused_envelope",
	"risk_class",
	"risk_line",
	"risk_lines",
	"risk_of",
	"structure_refusal",
	"take_refusal",
]
