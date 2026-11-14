"""Document Intake: the per-trigger extraction spec.

``load`` turns a Jarvis Trigger row into an ``IntakeSpec``; ``validate`` checks
it against the target doctype's meta; ``build_schema`` makes the JSON Schema the
vision model must answer with; ``mandatory_fields`` lists what the target needs
on insert.
"""

import math
from dataclasses import dataclass, field

import frappe
from frappe import _
from frappe.model import (
	default_fields,
	display_fieldtypes,
	no_value_fields,
	table_fields,
)
from frappe.utils import cint

MAX_INSTRUCTIONS_LEN = 4000
# Header fields plus table columns one rule may read. More only bloats the prompt and the schema.
MAX_PICKED_FIELDS = 60
# The Jarvis Trigger columns every intake stage reads (draft, review, worker).
TRIGGER_FIELDS = [
	"intake_source",
	"intake_record_doctype",
	"intake_target_doctype",
	"intake_spec",
	"intake_instructions",
	"intake_status_field",
	"intake_link_field",
	"allow_incomplete_drafts",
]

_INTEGER_TYPES = {"Int", "Check"}
_NUMBER_TYPES = {"Float", "Currency", "Percent"}
# Layout/display types never hold an extracted value.
_NON_VALUE_TYPES = set(no_value_fields) | set(display_fieldtypes) | set(table_fields)
# Values a page image cannot carry: files, secrets, code and structured blobs, and links
# whose target type is itself a value.
_NOT_READABLE_TYPES = {"Attach", "Attach Image", "Password", "Code", "JSON", "Dynamic Link"}


@dataclass
class IntakeSpec:
	target: str
	fields: list[str]
	table: dict | None
	match: list[str] = field(default_factory=list)
	defaults: dict = field(default_factory=dict)
	instructions: str = ""
	allow_incomplete: bool = False


def load(trigger_row) -> IntakeSpec:
	"""Build the spec from a Jarvis Trigger doc or a dict of its columns. The
	``intake_spec`` JSON may arrive as a string (DB / API) or already parsed."""
	get = trigger_row.get
	raw = get("intake_spec")
	try:
		data = frappe.parse_json(raw) if isinstance(raw, str) and raw.strip() else raw
	except Exception:
		data = None
	if not isinstance(data, dict):
		data = {}
	return IntakeSpec(
		target=get("intake_target_doctype") or "",
		fields=list(data.get("fields") or []),
		table=data.get("table") or None,
		match=list(data.get("match") or []),
		defaults=dict(data.get("defaults") or {}),
		instructions=(get("intake_instructions") or "").strip(),
		allow_incomplete=bool(cint(get("allow_incomplete_drafts"))),
	)


def _is_mandatory(df) -> bool:
	"""Mirror what Frappe enforces on insert: reqd, user-writable, no default to
	fall back on. Check fields always hold 0/1, naming_series is auto-filled."""
	return bool(
		df.reqd
		and not df.read_only
		and not df.default
		and df.fieldtype not in _NON_VALUE_TYPES
		and df.fieldtype != "Check"
		and df.fieldname != "naming_series"
		and df.fieldname not in default_fields
	)


def depends_mandatory(df, doc_values: dict) -> bool:
	"""True when ``df`` has a ``mandatory_depends_on`` expression that holds for
	``doc_values`` (the merged header values, or one child row). Frappe evaluates it with
	``doc`` in scope and an optional ``eval:`` prefix; a broken expression counts as not
	mandatory, as the desk form treats it."""
	expression = (df.get("mandatory_depends_on") or "").strip()
	if not expression or df.read_only or df.fieldtype in _NON_VALUE_TYPES:
		return False
	if expression.startswith("eval:"):
		expression = expression[5:]
	try:
		return bool(frappe.safe_eval(expression, None, {"doc": frappe._dict(doc_values)}))
	except Exception:
		return False


def _child_doctype(s: IntakeSpec) -> str | None:
	if not s.table:
		return None
	df = frappe.get_meta(s.target).get_field(s.table.get("fieldname"))
	return df.options if df and df.fieldtype == "Table" else None


def mandatory_fields(s: IntakeSpec) -> tuple[list[str], list[str]]:
	"""(header fieldnames, child-table column fieldnames) the target needs.
	Table fields are not header values; the chosen child table's required
	columns come from the child meta. Defaults are NOT subtracted here: callers
	merge ``s.defaults`` before checking what is still missing."""
	header = [df.fieldname for df in frappe.get_meta(s.target).fields if _is_mandatory(df)]
	child = _child_doctype(s)
	columns = [df.fieldname for df in frappe.get_meta(child).fields if _is_mandatory(df)] if child else []
	return header, columns


def _fillable(df) -> bool:
	"""A field a reading can fill: a value type the user can write, not computed."""
	return bool(
		df
		and df.fieldtype not in _NON_VALUE_TYPES
		and df.fieldtype not in _NOT_READABLE_TYPES
		and not df.read_only
		and not df.fetch_from
		and not df.get("is_virtual")
	)


def _value_field(meta, fieldname, where):
	df = meta.get_field(fieldname)
	if not _fillable(df):
		frappe.throw(_("{0}: '{1}' is not a fillable field on {2}.").format(where, fieldname, meta.name))
	return df


def validate(s: IntakeSpec) -> None:
	"""Throw unless the spec is consistent with the target doctype."""
	if not s.target or not frappe.db.exists("DocType", s.target):
		frappe.throw(_("Pick the DocType the drafts are created in."))
	if len(s.instructions) > MAX_INSTRUCTIONS_LEN:
		frappe.throw(_("Instructions must be at most {0} characters.").format(MAX_INSTRUCTIONS_LEN))
	if not s.fields or not all(isinstance(f, str) for f in s.fields):
		frappe.throw(_("List at least one field to read."))

	columns = (s.table.get("columns") or []) if isinstance(s.table, dict) else []
	if len(s.fields) + len(columns) > MAX_PICKED_FIELDS:
		frappe.throw(_("Pick at most {0} fields and columns to read.").format(MAX_PICKED_FIELDS))

	meta = frappe.get_meta(s.target)
	for fieldname in s.fields:
		_value_field(meta, fieldname, _("Fields"))

	if s.table is not None:
		_validate_table(s, meta)

	for fieldname in s.match:
		df = _value_field(meta, fieldname, _("Match"))
		if df.fieldtype != "Link" or fieldname not in s.fields:
			frappe.throw(
				_("Match field '{0}' must be a Link field that is also listed in the fields.").format(
					fieldname
				)
			)

	for fieldname, value in s.defaults.items():
		_validate_default(_value_field(meta, fieldname, _("Defaults")), value)

	if not s.allow_incomplete:
		_validate_coverage(s, meta)


def _is_blank(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def _validate_default(df, value) -> None:
	"""A default is written to every draft as is, so it must be a legal value for
	the field. Blank values are skipped here (they just do not cover anything)."""
	name = df.fieldname
	# Scalars only: a dict/list reaching frappe.db.exists would be read as filters.
	if not isinstance(value, str | int | float | bool):
		frappe.throw(_("Default for '{0}' must be a single text or number value.").format(name))
	if _is_blank(value):
		return
	if df.fieldtype == "Select":
		options = select_options(df)
		if str(value) not in options:
			frappe.throw(_("Default for '{0}' must be one of: {1}.").format(name, ", ".join(options)))
	elif df.fieldtype == "Link":
		# Name lookup only, and only for records the editor can read, so a default
		# cannot be used to probe records the editor may not see.
		exists = isinstance(value, str) and frappe.db.exists(df.options, {"name": value})
		if not exists or not frappe.has_permission(df.options, "read", doc=value):
			frappe.throw(_("Default for '{0}': {1} '{2}' does not exist.").format(name, df.options, value))
	elif df.fieldtype in _INTEGER_TYPES | _NUMBER_TYPES:
		# float() accepts "nan" and "inf", which no field can store.
		try:
			finite = math.isfinite(float(value))
		except (TypeError, ValueError):
			finite = False
		if not finite:
			frappe.throw(_("Default for '{0}' must be a number.").format(name))


def _validate_table(s: IntakeSpec, meta) -> None:
	table = s.table
	if not isinstance(table, dict):
		frappe.throw(_("The table setting is malformed."))
	df = meta.get_field(table.get("fieldname"))
	if not df or df.fieldtype != "Table":
		frappe.throw(_("'{0}' is not a table on {1}.").format(table.get("fieldname"), s.target))
	columns = table.get("columns") or []
	child_meta = frappe.get_meta(df.options)
	for column in columns:
		_value_field(child_meta, column, _("Table columns"))


def _validate_coverage(s: IntakeSpec, meta) -> None:
	"""Strict mode: a draft is only saved when every mandatory field is read or
	defaulted, so refuse a spec that could never produce one."""
	header, columns = mandatory_fields(s)
	# A blank default fills nothing, so it never counts as coverage.
	covered = set(s.fields) | {k for k, v in s.defaults.items() if not _is_blank(v)}
	missing = [f for f in header if f not in covered]
	missing_columns = [c for c in columns if c not in ((s.table or {}).get("columns") or [])]
	# A mandatory table that the spec does not fill can never be satisfied.
	table_name = (s.table or {}).get("fieldname")
	missing_tables = [
		df.fieldname
		for df in meta.fields
		if df.fieldtype == "Table" and df.reqd and df.fieldname != table_name
	]
	if missing or missing_columns or missing_tables:
		parts = missing + [f"{table_name}.{c}" for c in missing_columns] + missing_tables
		frappe.throw(
			_(
				"Mandatory fields are not covered: {0}. Add them to the fields or defaults, or allow incomplete drafts."
			).format(", ".join(parts))
		)


def select_options(df) -> list[str]:
	"""The values a Select field accepts (blank lines dropped)."""
	return [o.strip() for o in (df.options or "").split("\n") if o.strip()]


def _property(df) -> dict:
	"""One schema property. ``null`` is always allowed: an unreadable value comes
	back null instead of a guess."""
	if df.fieldtype in _INTEGER_TYPES:
		base = "integer"
	elif df.fieldtype in _NUMBER_TYPES or df.fieldtype in ("Duration", "Rating"):
		base = "number"
	else:
		base = "string"
	prop = {"type": [base, "null"]}
	if df.fieldtype == "Date":
		prop["format"] = "date"
	elif df.fieldtype == "Datetime":
		prop["format"] = "date-time"
	elif df.fieldtype == "Select" and select_options(df):
		# The model may only answer with a listed option (or null): an off-list value
		# would otherwise fail the whole insert.
		prop["enum"] = [*select_options(df), None]
	return prop


def build_schema(s: IntakeSpec) -> dict:
	"""JSON Schema for the model's reply: either the data object, or
	``{"error": "NO_IMAGE"}`` when the pages hold nothing readable."""
	meta = frappe.get_meta(s.target)
	properties = {name: _property(meta.get_field(name)) for name in s.fields}
	if s.table:
		child_meta = frappe.get_meta(_child_doctype(s))
		columns = s.table.get("columns") or []
		properties[s.table["fieldname"]] = {
			"type": ["array", "null"],
			"items": {
				"type": "object",
				"properties": {c: _property(child_meta.get_field(c)) for c in columns},
				"required": list(columns),
				"additionalProperties": False,
			},
		}
	data = {
		"type": "object",
		"properties": properties,
		"required": list(properties),
		"additionalProperties": False,
	}
	no_image = {
		"type": "object",
		"properties": {"error": {"const": "NO_IMAGE"}},
		"required": ["error"],
	}
	return {"anyOf": [data, no_image]}
