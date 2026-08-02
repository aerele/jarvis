"""The one shared list-filter contract: caller-specific schema + safe compiler.

Plan 08 §6 / Phase 1, as corrected by FABLE-JUDGMENT C08-1..C08-7. Two jobs:

1. :func:`get_list_filter_schema` — for a registered ``view_key``, the catalog of
   fields THIS caller may filter on, derived from live metadata (never from the
   visible columns, never from a copied frontend list), with the per-family
   default and allowed operators mirrored from Frappe's own filter UI.
2. :func:`compile_filters_v2` / :class:`ListFilterQuery` — validate client
   clauses against that same schema and compile them to parameterized SQL.

Two invariants this module exists to hold:

**Permlevel is enforced HERE (C08-1).** Frappe does not enforce field-level
permlevel on *filters* — ``frappe.get_list(filters={"skill_bundle": ["like", "%x%"]})``
happily answers for a user who may not read the field, turning any list into a
character-by-character oracle. In this codebase that reaches the agents IP moat
(``Jarvis Agent Listing.skill_bundle``, permlevel 1, readable only by System
Manager). So a field above the caller's readable permlevel is absent from the
schema AND rejected by the compiler. This is a declared, tested deviation from
Frappe (see ``LIST-FILTERS-DEVIATIONS.md``).

**Server predicates and user clauses never share a string list (C08-5).** The
raw-SQL list endpoints used to build one ``conds: list[str]`` holding both the
fixed visibility predicate and the user's filters, ``AND``-joined. That is the
structure the permlevel rule above cannot survive. :class:`ListFilterQuery`
splits them: :meth:`~ListFilterQuery.server_condition` takes SQL the endpoint
author wrote (plus its values as bound params), :meth:`~ListFilterQuery.apply`
takes client clauses and can only ever turn them into bound parameters. The two
meet exactly once, as already-built strings, in :meth:`~ListFilterQuery.where`.

**Every migrating endpoint wears the same error boundary.** A stable code is
worthless if it dies inside the Python process, so :func:`filter_errors_to_envelope`
converts a :class:`ListFilterError` into the repo's ``{ok: false, error: {code,
message}}`` envelope (the ``dashboards_api._error_envelope`` shape) with a
deliberate 4xx status, and every list endpoint that accepts ``filters_v2``
declares it::

    @frappe.whitelist()
    @require_jarvis_user
    @filter_errors_to_envelope
    def list_widgets_page(..., filters_v2: str | list | None = None) -> dict:

Order matters: the access gate stays OUTSIDE (a PermissionError is a different
contract and must keep its 403), and the wrapper only covers the body. The 4xx is
what stops a client from reading a rejected filter as an empty result set — the
one failure mode a filtered list must never have.

Deliberate deviations from Frappe parity are documented, with rationale, in
``jarvis/chat/LIST-FILTERS-DEVIATIONS.md``, which also carries the
MIGRATION-CHECKLIST every surface works through before it flips to ``MIGRATED``.
Reviewers cite that file; the future golden-matrix test (plan §11.5) is written
against it.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _

from jarvis.chat import list_registry
from jarvis.permissions import require_jarvis_user

#: Bumped when the wire shape of the schema or a clause changes.
CONTRACT_VERSION = 1

# --------------------------------------------------------------------------- #
# Limits (plan §9). These bound CLIENT input only — a server-authored predicate
# (an owner's shared-skill id list, a permission scan's id set) is exempt, per
# C08-5, because it is not attacker-controlled and is often legitimately large.
# --------------------------------------------------------------------------- #
MAX_CLAUSES = 20
MAX_IN_VALUES = 100
MAX_VALUE_CHARS = 1000
MAX_PAGE_LENGTH = 100

#: Schema cache TTL. Short and dumb on purpose: the cache key already carries a
#: digest of the metadata it was built from, so a customization changes the key
#: rather than needing an invalidation hook. The TTL only bounds everything else
#: (role grants, DocPerm edits made outside the desk).
SCHEMA_CACHE_TTL = 300

#: Wall-clock ceiling, in seconds, for ONE compiled list statement.
#:
#: Full-metadata filtering is the point of plan 08, so the expensive capabilities
#: stay: a ``like`` over a Text body is the most valuable filter the wiki has, and
#: it cannot use an index. What is bounded is the COST, not the capability — this
#: catches a Text scan on a huge table, and equally an unindexed combination some
#: later wave invents, without anyone having to predict which.
#:
#: The number: a measured Text ``like`` scan runs ~156ms over 5k rows, so a
#: linear extrapolation puts 10s at roughly 320k rows on that same shape. No
#: tenant is near that, which is the property wanted — in normal use this never
#: fires, and it still stops a pathological query from occupying a worker
#: indefinitely (``max_statement_time`` is 0 on this bench, i.e. no ceiling at
#: all today). NOTE it is per STATEMENT, and a list request runs two (COUNT then
#: SELECT), so the worst case per request is ~2x this.
STATEMENT_TIMEOUT_SECONDS = 10

# --------------------------------------------------------------------------- #
# Stable error codes (plan §6.3 / §11.3: every rejection is coded, and every
# unknown thing fails CLOSED — we never silently strip a clause the way
# db_query strips unknown OPTIONAL_FIELDS filters).
# --------------------------------------------------------------------------- #
ERR_UNKNOWN_VIEW = "list_filter_unknown_view"
ERR_VIEW_NOT_FILTERABLE = "list_filter_view_not_filterable"
ERR_BAD_PAYLOAD = "list_filter_bad_payload"
ERR_UNKNOWN_FIELD = "list_filter_unknown_field"
ERR_INVALID_OPERATOR = "list_filter_invalid_operator"
ERR_INVALID_VALUE = "list_filter_invalid_value"
ERR_TOO_MANY_CLAUSES = "list_filter_too_many_clauses"
ERR_TOO_MANY_VALUES = "list_filter_too_many_values"
ERR_VALUE_TOO_LONG = "list_filter_value_too_long"
#: The schema could not be built at all (metadata missing, table gone). Distinct
#: from "your clause is wrong": it is OUR fault, and it fails closed rather than
#: reaching the client as a raw 500 with a traceback-shaped message.
ERR_SCHEMA_UNAVAILABLE = "list_filter_schema_unavailable"
#: The filter was VALID and simply cost too much to run (see :func:`bounded_sql`).
#: Deliberately its own code: "narrow this" is a different instruction to the user
#: than "fix this", and a panel that conflated them would send someone hunting for
#: a mistake they did not make.
ERR_QUERY_TOO_EXPENSIVE = "list_filter_query_too_expensive"

#: A malformed payload is a bad REQUEST; everything else is a well-formed request
#: carrying an invalid filter, which is Frappe's ValidationError status; and an
#: unbuildable schema is ours, not the caller's.
_HTTP_STATUS_BY_CODE: dict[str, int] = {
	ERR_BAD_PAYLOAD: 400,
	ERR_SCHEMA_UNAVAILABLE: 503,
}
_DEFAULT_ERROR_STATUS = 417


class ListFilterError(frappe.ValidationError):
	"""A rejected filter payload. ``filter_error_code`` is the stable machine
	code; the message is user-facing and never echoes a secret field's value."""

	filter_error_code = ERR_BAD_PAYLOAD


def _fail(code: str, message: str) -> None:
	"""Raise a coded, user-visible filter error (fails closed, always)."""
	err = ListFilterError(message)
	err.filter_error_code = code
	# msgprint(raise_exception=<instance>) keeps the instance (and therefore the
	# code) while still populating `messages[0]` for the SPA's error surface.
	frappe.msgprint(message, raise_exception=err, title=_("Filter"), indicator="red")


def error_envelope(code: str, message: str) -> dict:
	"""The repo's ``{ok: false, error: {code, message}}`` shape (same as
	``dashboards_api._error_envelope``)."""
	return {"ok": False, "error": {"code": code, "message": message}}


def filter_errors_to_envelope(fn):
	"""Endpoint boundary: a :class:`ListFilterError` becomes the ``{ok: false,
	error: {code, message}}`` envelope with a deliberate 4xx/5xx status.

	Without this the stable codes never leave the Python process — Frappe would
	render the ValidationError as a generic 417 whose body carries only the
	human message, so a client could not branch on *why* a filter was rejected.

	The status is the load-bearing half: it is what stops the SPA's list
	composable from reading a rejected filter as ``rows: []`` and silently
	showing an empty, apparently-correct list. Every other exception is left
	alone (an unexpected bug must still surface as a 500).
	"""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except ListFilterError as e:
			code = getattr(e, "filter_error_code", ERR_BAD_PAYLOAD)
			try:
				frappe.local.response["http_status_code"] = _HTTP_STATUS_BY_CODE.get(
					code, _DEFAULT_ERROR_STATUS
				)
			except Exception:
				pass
			return error_envelope(code, frappe.utils.strip_html(str(e)).strip())

	return wrapper


# --------------------------------------------------------------------------- #
# Operator vocabulary — mirrored from frappe/public/js/frappe/ui/filters/filter.js
# (`set_conditions`, `invalid_condition_map`, `get_default_condition`) at the
# pinned v16 ref. Keep this block and the ledger in lockstep with that file.
# --------------------------------------------------------------------------- #
OP_EQ = "="
OP_NE = "!="
OP_LIKE = "like"
OP_NOT_LIKE = "not like"
OP_IN = "in"
OP_NOT_IN = "not in"
OP_IS = "is"
OP_GT = ">"
OP_LT = "<"
OP_GTE = ">="
OP_LTE = "<="
OP_BETWEEN = "Between"
OP_TIMESPAN = "Timespan"

#: Frappe's condition list, MINUS the five nested-set conditions. Those are only
#: ever valid for a Link whose target is a tree DocType; no Jarvis list has one
#: today, and advertising an operator the compiler cannot honour would be a
#: schema that lies. Declared deviation D7 — they arrive with the first tree Link.
CONDITIONS: tuple[str, ...] = (
	OP_EQ,
	OP_NE,
	OP_LIKE,
	OP_NOT_LIKE,
	OP_IN,
	OP_NOT_IN,
	OP_IS,
	OP_GT,
	OP_LT,
	OP_GTE,
	OP_LTE,
	OP_BETWEEN,
	OP_TIMESPAN,
)

_ALL_BUT_EQ = tuple(c for c in CONDITIONS if c != OP_EQ)

#: frappe.ui.Filter.invalid_condition_map, reproduced in FULL (the plan
#: reproduced roughly half of it — C08-2).
INVALID_CONDITIONS: dict[str, tuple[str, ...]] = {
	"Date": (OP_LIKE, OP_NOT_LIKE),
	"Datetime": (OP_LIKE, OP_NOT_LIKE, OP_IN, OP_NOT_IN, OP_EQ, OP_NE),
	"Data": (OP_BETWEEN, OP_TIMESPAN),
	"Time": (OP_BETWEEN, OP_TIMESPAN),
	"Select": (OP_LIKE, OP_NOT_LIKE, OP_BETWEEN, OP_TIMESPAN),
	"Link": (OP_BETWEEN, OP_TIMESPAN, OP_GT, OP_LT, OP_GTE, OP_LTE),
	"Currency": (OP_BETWEEN, OP_TIMESPAN),
	"Color": (OP_BETWEEN, OP_TIMESPAN),
	"Check": _ALL_BUT_EQ,
	"Code": (OP_BETWEEN, OP_TIMESPAN, OP_GT, OP_LT, OP_GTE, OP_LTE, OP_IN, OP_NOT_IN),
	"HTML Editor": (OP_BETWEEN, OP_TIMESPAN, OP_GT, OP_LT, OP_GTE, OP_LTE, OP_IN, OP_NOT_IN),
	"Markdown Editor": (OP_BETWEEN, OP_TIMESPAN, OP_GT, OP_LT, OP_GTE, OP_LTE, OP_IN, OP_NOT_IN),
	"Password": (OP_BETWEEN, OP_TIMESPAN, OP_GT, OP_LT, OP_GTE, OP_LTE, OP_IN, OP_NOT_IN),
	"Rating": (OP_LIKE, OP_NOT_LIKE, OP_BETWEEN, OP_IN, OP_NOT_IN, OP_TIMESPAN),
	"Int": (OP_LIKE, OP_NOT_LIKE, OP_BETWEEN, OP_IN, OP_NOT_IN, OP_TIMESPAN),
	"Float": (OP_LIKE, OP_NOT_LIKE, OP_BETWEEN, OP_IN, OP_NOT_IN, OP_TIMESPAN),
	"Percent": (OP_LIKE, OP_NOT_LIKE, OP_BETWEEN, OP_IN, OP_NOT_IN, OP_TIMESPAN),
}

#: Frappe's ``Filter.set_fieldtype`` (filter.js:530-582) rewrites these stored
#: string-like controls to an EFFECTIVE ``Data`` value control before choosing
#: conditions, and ``hide_invalid_conditions`` (filter.js:410-413) then picks
#: ``invalid_condition_map[original_type] || invalid_condition_map[effective]``.
#: A type WITHOUT its own map row (Small Text, Text, Text Editor, JSON, Dynamic
#: Link, Attach, Phone, Tag, Comments, Barcode, Read Only, Assign) therefore
#: inherits Data's ``(Between, Timespan)`` invalid set — the fallback the original
#: implementation missed (P0-03), so a Small Text ``description`` or a JSON field
#: was advertising ``Between``/``Timespan`` and the frontend rendered a date
#: range over a text column.
#:
#: Types that DO have their own row (Code, HTML Editor, Markdown Editor, Color,
#: Password) keep it — ``original_type`` wins in the lookup above — so they are
#: absent from this map and short-circuit before it. ``Long Text`` is a DELIBERATE
#: Jarvis addition (deviation D15): Frappe leaves it with no row and no effective
#: conversion, i.e. it offers ``Between`` on a prose body, which is the exact
#: non-temporal-date-range nonsense this fix exists to remove.
_EFFECTIVE_FILTER_FIELDTYPE: dict[str, str] = {
	ft: "Data"
	for ft in (
		"Text",
		"Small Text",
		"Text Editor",
		"Attach",
		"Attach Image",
		"Tag",
		"Phone",
		"JSON",
		"Comments",
		"Barcode",
		"Dynamic Link",
		"Read Only",
		"Assign",
		"Long Text",  # deviation D15
	)
}


def invalid_conditions(fieldtype: str) -> tuple[str, ...]:
	"""Frappe's ``invalid_condition_map[original] || invalid_condition_map[effective]``.

	The original type wins when it has its own row; otherwise a string-like control
	falls back to its effective ``Data`` row (see :data:`_EFFECTIVE_FILTER_FIELDTYPE`).
	This is the single source both :func:`allowed_operators` and the golden matrix
	test read, so schema and test cannot drift.
	"""
	if fieldtype in INVALID_CONDITIONS:
		return INVALID_CONDITIONS[fieldtype]
	effective = _EFFECTIVE_FILTER_FIELDTYPE.get(fieldtype)
	if effective:
		return INVALID_CONDITIONS.get(effective, ())
	return ()


#: frappe.model.no_value_type — layout / non-value controls. Table and Table
#: MultiSelect are containers: not selectable themselves, but their child fields
#: are (plan §4.3).
NO_VALUE_FIELDTYPES = frozenset(
	{
		"Section Break",
		"Column Break",
		"Tab Break",
		"HTML",
		"Table",
		"Table MultiSelect",
		"Button",
		"Image",
		"Fold",
		"Heading",
	}
)
TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})

#: Frappe OFFERS Password with a restricted operator set. We do not — a filter is
#: an oracle, and an oracle over a stored secret is a secret read. Deviation D2.
SECRET_FIELDTYPES = frozenset({"Password"})

NUMERIC_FIELDTYPES = frozenset({"Int", "Float", "Currency", "Percent", "Duration", "Rating"})
#: Frappe's `can_be_null = False` set in db_query.prepare_filter_condition.
NON_NULLABLE_FIELDTYPES = frozenset({"Check", "Int", "Float", "Currency", "Percent"})

#: Standard fields, mirroring frappe.model.std_fields (model.js:169-183). `name`
#: is a Link to the DocType itself; docstatus is added only for submittable
#: DocTypes (frappe.ui.FieldSelect.add_field_option).
_STANDARD_FIELDS: tuple[dict, ...] = (
	{"fieldname": "name", "fieldtype": "Link", "label": "ID", "options": "@self"},
	{"fieldname": "owner", "fieldtype": "Link", "label": "Created By", "options": "User"},
	{"fieldname": "idx", "fieldtype": "Int", "label": "Index"},
	{"fieldname": "creation", "fieldtype": "Datetime", "label": "Created On"},
	{"fieldname": "modified", "fieldtype": "Datetime", "label": "Last Updated On"},
	{"fieldname": "modified_by", "fieldtype": "Link", "label": "Last Updated By", "options": "User"},
	{"fieldname": "_user_tags", "fieldtype": "Data", "label": "Tags"},
	{"fieldname": "_liked_by", "fieldtype": "Data", "label": "Liked By"},
	{"fieldname": "_comments", "fieldtype": "Text", "label": "Comments"},
	{"fieldname": "_assign", "fieldtype": "Text", "label": "Assigned To"},
	{"fieldname": "docstatus", "fieldtype": "Int", "label": "Document Status"},
)

#: Columns that only exist when the site actually created them. Frappe's db_query
#: silently DROPS filters on a missing optional field; we fail closed instead
#: (C08-2), so we must not offer one that has no column.
_OPTIONAL_COLUMNS = frozenset({"_user_tags", "_comments", "_assign", "_liked_by"})

#: Picker section for the generic standard/meta fields (ID, Created By, Tags,
#: Comments…). They are filterable and stay filterable — they are just not what
#: someone is looking for when they open the picker, so they get their own
#: section rather than a share of the alphabet.
STANDARD_FIELD_GROUP = "General"

#: Stored as a JSON array (``["a@x.com", "b@x.com"]``), so `=` can never hit.
#: Frappe special-cases their DEFAULT to `like` but still offers `=`/`in`; we
#: restrict them to the like family so the schema cannot promise a match that is
#: structurally impossible. Deviation D3.
_JSON_ARRAY_FIELDS = frozenset({"_assign", "_liked_by"})
_JSON_ARRAY_OPERATORS = (OP_LIKE, OP_NOT_LIKE, OP_IS)


def _fallback_sql(fieldtype: str) -> str:
	"""The ``ifnull(col, <fallback>)`` literal Frappe uses per family. Server
	authored, never user input."""
	if fieldtype in NUMERIC_FIELDTYPES or fieldtype == "Check":
		return "0"
	if fieldtype == "Date":
		return "'0001-01-01'"
	if fieldtype == "Datetime":
		return "'0001-01-01 00:00:00.000000'"
	if fieldtype == "Time":
		return "'00:00:00'"
	return "''"


#: Free-text bodies a human types prose into. D5-a: these default to ``like``,
#: which is a deliberate divergence from Frappe (see the ledger). NOT included:
#: ``Code``/``HTML Editor``/``Markdown Editor`` (machine-shaped content, where an
#: exact match is the useful question) and ``Data`` (which keeps Frappe's
#: large-table rule below).
_FREE_TEXT_FIELDTYPES = frozenset({"Small Text", "Long Text", "Text", "Text Editor"})


def default_operator(fieldname: str, fieldtype: str, is_large_table: bool = False) -> str:
	"""Default condition per field family.

	Frappe's ``get_default_condition`` (filter.js:516-529) gives ``like`` only to
	``Data`` on a non-large table, ``Between`` to Date/Datetime, and ``=`` to
	everything else — so a Small Text ``description`` defaults to an exact match,
	which is almost never what a user typing three words into a Description filter
	means. Plan §5.1 assumed ``like`` for all text-like families and was wrong
	about Frappe; the judgment's ruling (D5-a) is to diverge deliberately for the
	four free-text bodies while keeping Frappe's rule everywhere else. ``=`` stays
	selectable on all of them.
	"""
	if fieldname in _JSON_ARRAY_FIELDS:
		return OP_LIKE
	if fieldtype == "Data" and not is_large_table:
		return OP_LIKE
	if fieldtype in _FREE_TEXT_FIELDTYPES:
		return OP_LIKE
	if fieldtype in ("Date", "Datetime"):
		return OP_BETWEEN
	return OP_EQ


def allowed_operators(fieldname: str, fieldtype: str) -> tuple[str, ...]:
	"""Valid operators for a field family (Frappe's invalid_condition_map,
	inverted), narrowed for the JSON-array standard fields."""
	if fieldname in _JSON_ARRAY_FIELDS:
		return _JSON_ARRAY_OPERATORS
	invalid = invalid_conditions(fieldtype)
	return tuple(c for c in CONDITIONS if c not in invalid)


# --------------------------------------------------------------------------- #
# Schema construction
# --------------------------------------------------------------------------- #
def resolve_view(view_key: str) -> list_registry.ListView:
	"""The registered view, or a closed failure. ``view_key`` is the ONLY thing a
	client names — never a table, doctype or column."""
	view = list_registry.get_view(view_key)
	if not view:
		_fail(ERR_UNKNOWN_VIEW, _("Unknown list view."))
	return view


def _filterable_root(view: list_registry.ListView) -> str:
	"""The DocType whose metadata backs this view's catalog, or a closed failure.

	Projections (File Box, Support, Approvals, the agents catalog) and excluded
	surfaces have no metadata catalog yet: they need a declared projection schema
	(plan §4.4), which is a later phase. Until then they fail closed rather than
	silently answering with a root-doctype catalog that does not describe the
	rows the endpoint actually returns.
	"""
	if not view.is_document_list or not view.root_doctype:
		_fail(
			ERR_VIEW_NOT_FILTERABLE,
			_("This list does not support field filters yet."),
		)
	return view.root_doctype


def _table_columns(doctype: str) -> set[str]:
	"""Real columns of the table. Deliberately NOT exception-swallowing: an empty
	set would silently disable the column check below and quietly widen the
	catalog. A missing table is a schema failure — :func:`get_schema` turns it
	into ``list_filter_schema_unavailable``."""
	return set(frappe.db.get_table_columns(doctype))


def _is_large_table(meta) -> bool:
	"""Frappe's own large-table heuristic (Meta.check_if_large_table). Reported in
	the schema so the UI can explain a changed Data default; it never removes
	``like`` from the operator list (plan §5.1)."""
	try:
		if not hasattr(meta, "is_large_table"):
			meta.check_if_large_table()
		return bool(getattr(meta, "is_large_table", False))
	except Exception:
		return False


def _permlevels(meta, user: str, parenttype: str | None = None) -> set[int]:
	"""Permlevels this user can READ on ``meta`` (child metas inherit the
	parent's permissions, exactly as Frappe's Meta.get_permissions does)."""
	try:
		return {int(p) for p in meta.get_permlevel_access("read", parenttype=parenttype, user=user)}
	except Exception:
		return set()


def _entry(
	doctype: str,
	df: dict,
	*,
	label: str,
	is_standard: bool = False,
	is_child: bool = False,
	is_large_table: bool = False,
	group: str = "",
) -> dict:
	fieldname = df["fieldname"]
	fieldtype = df["fieldtype"]
	options = df.get("options") or ""
	if options == "@self":
		options = doctype
	return {
		"doctype": doctype,
		"fieldname": fieldname,
		"label": label,
		"fieldtype": fieldtype,
		"options": options,
		"is_standard": bool(is_standard),
		"is_child": bool(is_child),
		# What the picker should file this field under. For a child field that is
		# the PARENT's Table-field label ("Sources"), which is the word on the
		# form the user actually knows — the raw child DocType name
		# ("Jarvis Dashboard Source") is an implementation detail they have never
		# seen. Standard/meta fields get their own group so they can be shown as a
		# distinct section instead of interleaved with the doctype's own fields.
		"group": group,
		"json_array": fieldname in _JSON_ARRAY_FIELDS,
		"default_operator": default_operator(fieldname, fieldtype, is_large_table),
		"operators": list(allowed_operators(fieldname, fieldtype)),
	}


def build_field_catalog(
	root_doctype: str,
	user: str | None = None,
	*,
	view: list_registry.ListView | None = None,
	excluded_fields: tuple[str, ...] | list[str] = (),
) -> list[dict]:
	"""The filterable-field catalog for ``root_doctype`` as seen by ``user``.

	Plan §4: applicable standard fields + every stored, non-virtual,
	value-bearing main field the caller may read at its permlevel + eligible
	child-table fields, labelled ``Field (Child DocType)`` and identified by
	(doctype, fieldname) as Frappe does. Hidden / not-in-list-view fields are NOT
	excluded: Frappe filters on readable metadata, not on visible columns.

	``view`` threads the registered surface through, so its
	:attr:`~jarvis.chat.list_registry.ListView.excluded_fields` are withheld here
	rather than by every caller remembering to. That is the P1 control: permlevel
	stops what the DOCTYPE hides, ``excluded_fields`` stops what the ENDPOINT
	hides (Triggers' redacted logic fields being the live case). A withheld field
	is simply absent, so the compiler rejects it with the same
	``list_filter_unknown_field`` as a misspelling — the oracle rule holds.

	Exposed as a public helper (not just via the view registry) so the Phase-0
	audit and the permlevel regression can point it at a DocType directly.
	"""
	user = user or frappe.session.user
	excluded = {str(x) for x in excluded_fields}
	if view is not None:
		excluded |= {str(x) for x in view.excluded_fields}
	# "fieldname" withholds a main/standard field; "Child DocType.fieldname"
	# withholds one child field.
	excluded_main = {x for x in excluded if "." not in x}
	excluded_child = {tuple(x.split(".", 1)) for x in excluded if "." in x}

	meta = frappe.get_meta(root_doctype)
	levels = _permlevels(meta, user)
	if 0 not in levels:
		# No level-0 read at all: nothing is filterable. Endpoints gate access
		# separately; this is the belt to that braces.
		return []

	large = _is_large_table(meta)
	# The doctype's own fields are filed under the LIST's name when the view
	# supplies one ("Skills"), which is what the user sees at the top of the page.
	own_group = (view.label if view is not None and view.label else None) or _(root_doctype)
	columns = _table_columns(root_doctype)
	submittable = bool(getattr(meta, "is_submittable", 0))

	seen: set[tuple[str, str]] = set()
	main: list[dict] = []

	for std in _STANDARD_FIELDS:
		fieldname = std["fieldname"]
		if fieldname == "docstatus" and not submittable:
			continue
		if fieldname in excluded_main:
			continue
		if fieldname in _OPTIONAL_COLUMNS and fieldname not in columns:
			continue
		if columns and fieldname not in columns and fieldname not in _OPTIONAL_COLUMNS:
			continue
		seen.add((root_doctype, fieldname))
		main.append(
			_entry(
				root_doctype,
				std,
				label=_(std["label"]),
				is_standard=True,
				is_large_table=large,
				group=STANDARD_FIELD_GROUP,
			)
		)

	child_containers: list = []
	for df in meta.fields:
		fieldname = df.fieldname or ""
		if not fieldname:
			continue
		if getattr(df, "is_virtual", 0):
			continue
		if df.fieldtype in TABLE_FIELDTYPES:
			# A Table field is the PERMISSION BOUNDARY around its child rows, not a
			# mere layout control (P0-02). Discovering its children when the caller
			# cannot read the container itself — its own permlevel is above the
			# caller, or the endpoint withholds it via excluded_fields — turns a
			# readable child field into an oracle over data the container hides.
			# Both checks run BEFORE the container joins child_containers, so a
			# withheld container suppresses its entire subtree (the excluded-field
			# check used to sit after the `continue`, so it never reached a Table).
			if df.options and fieldname not in excluded_main and int(df.permlevel or 0) in levels:
				child_containers.append(df)
			continue
		if df.fieldtype in NO_VALUE_FIELDTYPES or df.fieldtype in SECRET_FIELDTYPES:
			continue
		if fieldname in excluded_main:
			continue
		if int(df.permlevel or 0) not in levels:
			continue
		if columns and fieldname not in columns:
			continue
		if (root_doctype, fieldname) in seen:
			continue
		seen.add((root_doctype, fieldname))
		main.append(
			_entry(
				root_doctype,
				{"fieldname": fieldname, "fieldtype": df.fieldtype, "options": df.options},
				label=_(df.label or fieldname),
				is_large_table=large,
				group=own_group,
			)
		)

	children: list[dict] = []
	for container in child_containers:
		child_dt = container.options
		# The parent's own word for this table ("Sources"), falling back to the
		# child DocType only when the Table field carries no label.
		container_label = _(container.label) if container.label else _(child_dt)
		# No swallow: a Table pointing at a missing child DocType is a broken
		# schema, and silently dropping its fields would be a silent narrowing.
		child_meta = frappe.get_meta(child_dt)
		child_levels = _permlevels(child_meta, user, parenttype=root_doctype)
		if 0 not in child_levels:
			continue
		child_columns = _table_columns(child_dt)
		child_fields = list(child_meta.fields)
		if container.fieldtype == "Table MultiSelect":
			# Frappe exposes only the Link value field of a Table MultiSelect.
			link = next((d for d in child_fields if d.fieldtype == "Link"), None)
			child_fields = [link] if link else []
		for df in child_fields:
			fieldname = df.fieldname or ""
			if not fieldname:
				continue
			if getattr(df, "is_virtual", 0):
				continue
			# Nested tables are not recursively filterable (plan §4.3).
			if df.fieldtype in NO_VALUE_FIELDTYPES or df.fieldtype in SECRET_FIELDTYPES:
				continue
			if (child_dt, fieldname) in excluded_child:
				continue
			if int(df.permlevel or 0) not in child_levels:
				continue
			if child_columns and fieldname not in child_columns:
				continue
			if (child_dt, fieldname) in seen:
				continue
			seen.add((child_dt, fieldname))
			children.append(
				_entry(
					child_dt,
					{"fieldname": fieldname, "fieldtype": df.fieldtype, "options": df.options},
					label=f"{_(df.label or fieldname)} ({container_label})",
					is_child=True,
					is_large_table=large,
					group=container_label,
				)
			)

	# Order: the doctype's OWN fields, then the 11 generic standard/meta fields,
	# then child tables. Sorting the whole main block alphabetically put a third
	# of a 27-field list — Frappe plumbing like "Assigned To", "Comments",
	# "Liked By" — in between "Scope" and "Title", which is the opposite of how
	# anyone looks for a field. `is_standard` is the stable marker; within each
	# group it stays alphabetical.
	main.sort(key=lambda e: (bool(e["is_standard"]), e["label"], e["doctype"], e["fieldname"]))
	children.sort(key=lambda e: (e["group"], e["label"], e["doctype"], e["fieldname"]))
	return main + children


def _schema_digest(fields: list[dict], limits: dict | None = None) -> str:
	"""A revision over the FULL response contract, not a subset (P1-10).

	The public ``schema_revision`` must change whenever anything the client renders
	from the schema changes, or a caching/ETag consumer would serve a stale panel.
	The old digest hashed only (doctype, fieldname, fieldtype, default_operator,
	operators), so a Select-option edit, a Link-target change, a relabel, a
	standard/child marker flip or a limits change all returned the SAME revision.
	Hash every field key the wire carries, plus the limits block.
	"""
	payload = json.dumps(
		{
			"fields": [
				[
					f["doctype"],
					f["fieldname"],
					f["label"],
					f["fieldtype"],
					f["options"],
					f["default_operator"],
					f["operators"],
					f["is_standard"],
					f["is_child"],
					f["json_array"],
				]
				for f in fields
			],
			"limits": limits or {},
		},
		sort_keys=True,
	)
	return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_key(view_key: str, user: str, meta_fingerprint: str) -> str:
	roles = ",".join(sorted(frappe.get_roles(user)))
	lang = getattr(frappe.local, "lang", "") or ""
	digest = hashlib.sha256(f"{roles}|{lang}|{meta_fingerprint}".encode()).hexdigest()[:24]
	# The user is IN the key, never merely in the digest: a schema is
	# caller-specific and must never be served to a second identity (plan §6.1).
	return f"jarvis:list-filter-schema:{view_key}:{user}:{digest}"


def _meta_fingerprint(root_doctype: str, user: str) -> str:
	"""Digest of everything the catalog is derived from.

	Cheap (``get_meta`` is itself cached and is invalidated by Frappe on any
	Custom Field / Property Setter / DocPerm change), and because it covers the
	fieldnames, fieldtypes, permlevels and the permission rows, a customization
	changes the cache KEY. Invalidation correctness over cleverness: we never
	have to remember to bust anything.
	"""
	parts: list[str] = []
	# No swallow anywhere in here: a fingerprint computed from partial metadata
	# would be a STABLE key for an INCOMPLETE catalog, i.e. a cached wrong answer.
	# get_schema turns any failure into list_filter_schema_unavailable.
	meta = frappe.get_meta(root_doctype)
	metas = [(root_doctype, meta)]
	for df in meta.fields:
		if df.fieldtype in TABLE_FIELDTYPES and df.options:
			metas.append((df.options, frappe.get_meta(df.options)))
	for name, m in metas:
		parts.append(name)
		parts.append(str(getattr(m, "modified", "")))
		for df in m.fields:
			parts.append(f"{df.fieldname}:{df.fieldtype}:{int(df.permlevel or 0)}:{df.options or ''}")
		for perm in getattr(m, "permissions", []) or []:
			parts.append(
				f"P{perm.get('role')}:{int(perm.get('permlevel') or 0)}:{int(perm.get('read') or 0)}"
			)
	return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def get_schema(view_key: str, user: str | None = None) -> dict:
	"""The caller-specific filter schema for ``view_key`` (cached; see
	:data:`SCHEMA_CACHE_TTL`). Internal entry point — the whitelisted wrapper is
	:func:`get_list_filter_schema`."""
	user = user or frappe.session.user
	view = resolve_view(view_key)
	root = _filterable_root(view)
	try:
		return _build_schema(view, root, user)
	except ListFilterError:
		raise
	except Exception:
		# Metadata gone, table dropped mid-migrate, a broken child DocType: the
		# caller gets a stable code and no rows, never a raw 500 whose body the
		# SPA would render as a mystery toast.
		#
		# Logged at most once per view per TTL. A broken DocType breaks EVERY list
		# load for every user on that surface, so an unguarded log_error turns one
		# schema fault into an Error Log flood that buries the entry explaining it
		# (and writes a row per request while the site is already unhealthy).
		_log_schema_failure(view_key, root)
		_fail(ERR_SCHEMA_UNAVAILABLE, _("Filters are unavailable for this list right now."))


#: How long one view's schema failure stays deduped in the Error Log.
SCHEMA_FAILURE_LOG_TTL = 900


def _log_schema_failure(view_key: str, root: str) -> None:
	key = f"jarvis:list-filter-schema-fail:{view_key}"
	try:
		cache = frappe.cache()
		if cache.get_value(key):
			return
		cache.set_value(key, "1", expires_in_sec=SCHEMA_FAILURE_LOG_TTL)
	except Exception:
		# No cache: log anyway. Losing the dedupe is better than losing the fault.
		pass
	try:
		frappe.log_error(
			title="list filter schema unavailable",
			message=f"{view_key} / {root}\n{frappe.get_traceback()}",
		)
	except Exception:
		pass


def _build_schema(view: list_registry.ListView, root: str, user: str) -> dict:
	fingerprint = _meta_fingerprint(root, user)
	key = _cache_key(view.view_key, user, fingerprint)
	try:
		cached = frappe.cache().get_value(key)
	except Exception:
		cached = None
	if isinstance(cached, dict) and cached.get("contract_version") == CONTRACT_VERSION:
		return cached

	fields = build_field_catalog(root, user=user, view=view)
	meta = frappe.get_meta(root)
	limits = {
		"max_clauses": MAX_CLAUSES,
		"max_in_values": MAX_IN_VALUES,
		"max_value_chars": MAX_VALUE_CHARS,
		"max_page_length": MAX_PAGE_LENGTH,
	}
	schema = {
		"contract_version": CONTRACT_VERSION,
		"view_key": view.view_key,
		"label": view.label,
		"root_doctype": root,
		"is_large_table": _is_large_table(meta),
		"schema_revision": _schema_digest(fields, limits),
		"limits": limits,
		"fields": fields,
	}
	try:
		frappe.cache().set_value(key, schema, expires_in_sec=SCHEMA_CACHE_TTL)
	except Exception:
		pass
	return schema


# --------------------------------------------------------------------------- #
# Runtime capability flag + structural telemetry (P1-05).
# --------------------------------------------------------------------------- #
#: Site-config key holding the list of MIGRATED view keys to roll BACK to legacy
#: transport, e.g. ``"jarvis_list_filters_v2_off": ["triggers"]`` in
#: ``site_config.json``. Absent / empty ⇒ every migrated view is ON — the flag is
#: opt-OUT so a new migration is live by default and the switch is only ever a
#: rollback. Mirrors the existing ``frappe.conf.get(FLAG)`` pattern
#: (admission.py). A per-view field would need a DocType write path for what is a
#: rare operator action, so the site-config map is the smaller honest mechanism.
CONF_KEY_V2_OFF = "jarvis_list_filters_v2_off"


def _disabled_views() -> set[str]:
	off = frappe.conf.get(CONF_KEY_V2_OFF) or []
	if isinstance(off, str):
		off = [off]
	try:
		return {str(v) for v in off}
	except TypeError:
		return set()


def view_filters_enabled(view_key: str) -> bool:
	"""Whether ``view_key`` serves the ``filters_v2`` schema right now (default ON).

	OFF is a rollback: the schema endpoint declines, so the client falls back to
	legacy transport WITHOUT the Filter mandate disappearing (the button still
	renders; it just cannot load a catalog). The compile path deliberately does
	NOT consult this — a clause a stale client still sends is answered or coded,
	never silently dropped (do-not-regress rule 8).
	"""
	return view_key not in _disabled_views()


@frappe.whitelist()
@require_jarvis_user
def list_filters_capabilities() -> dict:
	"""Per-view ``{view_key: enabled}`` for every MIGRATED view (P1-05).

	Lets a surface decide transport at mount without probing the schema endpoint
	first. Jarvis-user gated; carries no field data, only the capability map.
	"""
	return {v.view_key: view_filters_enabled(v.view_key) for v in list_registry.filterable_views()}


def _duration_bucket(ms: float) -> str:
	"""Coarse, value-free duration bucket for telemetry."""
	if ms < 5:
		return "<5ms"
	if ms < 25:
		return "<25ms"
	if ms < 100:
		return "<100ms"
	if ms < 500:
		return "<500ms"
	return ">=500ms"


def _filter_logger():
	import logging

	logger = frappe.logger("jarvis.chat.list_filters", allow_site=True)
	if logger.level == 0 or logger.level > logging.INFO:
		logger.setLevel(logging.INFO)
	return logger


def emit_filter_telemetry(
	view_key: str,
	clauses: list[_Clause],
	*,
	duration_ms: float,
	error_code: str | None = None,
) -> None:
	"""Bounded, PII-free structural telemetry (P1-05): view, per-clause
	fieldtype+operator, whether a child clause was used, a duration BUCKET and the
	stable error code — never a filter VALUE (do-not-regress rule 4). Mirrors the
	latency logger: its own INFO logger, so it is visible without flipping levels
	and does not make any other logger chattier."""
	try:
		shape = [{"ft": c.entry["fieldtype"], "op": c.operator} for c in clauses]
		record = {
			"view": view_key,
			"clauses": len(clauses),
			"child": any(c.entry.get("is_child") for c in clauses),
			"shape": shape,
			"bucket": _duration_bucket(duration_ms),
		}
		if error_code:
			record["error"] = error_code
		_filter_logger().info("list_filter %s", json.dumps(record, sort_keys=True))
	except Exception:
		# Telemetry must never break a query.
		pass


@frappe.whitelist()
@require_jarvis_user
@filter_errors_to_envelope
def get_list_filter_schema(view_key: str) -> dict:
	"""Fields THIS caller may filter ``view_key`` on, with per-field operators.

	Jarvis-user gated. Never trust the browser's copy of this: every clause is
	re-validated against a freshly resolved schema at query time.

	Answers only for views whose endpoint actually accepts ``filters_v2``. A
	registered-but-unmigrated view would otherwise hand a client a perfectly
	valid schema it has nowhere to send — the UI would render a filter panel
	whose every clause the list endpoint ignores, which is worse than no panel.
	A view rolled back via the runtime flag (P1-05) declines the same way, so the
	client degrades to legacy transport with the Filter action still visible.
	"""
	view = resolve_view(view_key)
	if view.status != list_registry.MIGRATED or not view_filters_enabled(view_key):
		_fail(ERR_VIEW_NOT_FILTERABLE, _("This list does not support field filters yet."))
	return get_schema(view_key)


def _schema_index(schema: dict) -> dict[tuple[str, str], dict]:
	return {(f["doctype"], f["fieldname"]): f for f in schema.get("fields") or []}


# --------------------------------------------------------------------------- #
# Clause parsing, validation and normalization
# --------------------------------------------------------------------------- #
@dataclass
class _Clause:
	index: int
	entry: dict
	operator: str
	value: Any

	@property
	def dimension(self) -> tuple[str, str]:
		return (self.entry["doctype"], self.entry["fieldname"])


def parse_clauses(clauses: Any) -> list[dict]:
	"""Accept the wire form (JSON string or list of dicts) and shape-check it."""
	if clauses in (None, "", b""):
		return []
	raw = clauses
	if isinstance(raw, str):
		raw = raw.strip()
		if not raw:
			return []
		try:
			raw = json.loads(raw)
		except Exception:
			_fail(ERR_BAD_PAYLOAD, _("Filters are not valid JSON."))
	if isinstance(raw, dict):
		_fail(ERR_BAD_PAYLOAD, _("Filters must be a list of conditions."))
	if not isinstance(raw, list | tuple):
		_fail(ERR_BAD_PAYLOAD, _("Filters must be a list of conditions."))
	out: list[dict] = []
	for item in raw:
		if not isinstance(item, dict):
			_fail(
				ERR_BAD_PAYLOAD,
				_("Each filter must be an object with doctype, fieldname, operator and value."),
			)
		out.append(item)
	return out


def _guard_length(value: Any) -> None:
	"""The §9 length cap, applied to the RAW value before any fieldtype branch.

	It used to live only on the text path, so a megabyte of digits aimed at an Int
	field, or a pathological date string, was parsed first and capped never."""
	if isinstance(value, str | bytes) and len(value) > MAX_VALUE_CHARS:
		_fail(ERR_VALUE_TOO_LONG, _("A filter value may not exceed {0} characters.").format(MAX_VALUE_CHARS))


def _cap_text(value: Any) -> str:
	text = "" if value is None else str(value)
	if len(text) > MAX_VALUE_CHARS:
		_fail(ERR_VALUE_TOO_LONG, _("A filter value may not exceed {0} characters.").format(MAX_VALUE_CHARS))
	return text


def _as_number(entry: dict, value: Any) -> float | int:
	"""Parse a numeric filter value, failing CLOSED on garbage (P1-01).

	``frappe.utils.cint``/``flt`` do NOT raise on a malformed string — ``flt("abc")``
	is ``0.0`` and ``cint("abc")`` is ``0`` — so the old ``try/except`` was inert for
	the common case: ``"abc"`` silently became ``0`` and answered a different
	question, and ``float('inf')``/``nan`` reached the DB binder as a driver error
	outside the stable filter-error contract. So we parse strictly here.

	- A BLANK value stays ``0`` at Frappe parity (``flt("")``==0), a deliberate
	  documented deviation (D14): a blank numeric row is held PENDING on the client
	  and never reaches the wire, so this only fires for a hand-edited URL / direct
	  API call, where ``0`` is the least-surprising legacy behaviour.
	- A nonblank value that is not a finite number is rejected with the stable
	  ``list_filter_invalid_value`` code.
	- ``Int``/``Duration`` keep Frappe's ``cint`` truncation of a fractional value
	  (``cint("3.7")``==3) once it is known finite — documented, not silent.
	"""
	import math

	from frappe.utils import cint, flt

	if value is None or (isinstance(value, str) and not value.strip()):
		return 0  # blank → 0, Frappe parity (D14)

	try:
		probe = float(value)
	except (TypeError, ValueError):
		_fail(ERR_INVALID_VALUE, _("{0} needs a number.").format(entry["label"]))
	if not math.isfinite(probe):
		_fail(ERR_INVALID_VALUE, _("{0} needs a finite number.").format(entry["label"]))

	if entry["fieldtype"] in ("Int", "Duration"):
		return cint(value)  # finite → cint truncates any fraction (Frappe parity)
	return flt(value)


def _select_options(entry: dict) -> list[str] | None:
	options = entry.get("options") or ""
	if entry["fieldtype"] != "Select" or not options or options.startswith("link:"):
		return None
	return [o.strip() for o in options.split("\n")]


_TEMPORAL_FIELDTYPES = frozenset({"Date", "Datetime", "Time"})


def _is_blank_temporal(value: Any) -> bool:
	"""True for anything Frappe's date helpers would silently turn into today/now.

	``getdate()`` / ``get_datetime()`` begin with ``if not string_date:`` and
	return the current date/time — so falsiness is the ONLY thing they check.
	``0``, ``False``, ``[]`` and ``{}`` therefore all mean "today" to them, and
	none of them is a date. This is the single predicate both the scalar path and
	the ``Between`` bounds use, so the two cannot drift apart again."""
	if isinstance(value, str):
		return not value.strip()
	return not value


def _scalar(entry: dict, operator: str, value: Any) -> Any:
	"""Normalize ONE scalar value by fieldtype, mirroring db_query's per-family
	conversions. The result is always a bound parameter, never SQL."""
	from frappe.utils import get_datetime, get_time, getdate

	_guard_length(value)
	fieldtype = entry["fieldtype"]

	if fieldtype in _TEMPORAL_FIELDTYPES:
		# Frappe's getdate(None)/get_datetime(None) return TODAY/NOW, and every
		# other falsy value does the same. Inherited, an omitted or blank date
		# would quietly become "today" — a filter that answers a question nobody
		# asked, and the answer looks plausible. Reject instead.
		if _is_blank_temporal(value):
			_fail(ERR_INVALID_VALUE, _("{0} needs a value.").format(entry["label"]))
		try:
			if fieldtype == "Date":
				parsed = getdate(value)
			elif fieldtype == "Datetime":
				parsed = get_datetime(value)
			else:
				parsed = get_time(value)
		except Exception:
			parsed = None
		if parsed is None:
			# get_datetime/get_time return None (rather than raising) on several
			# unparseable shapes; str(None) would bind the literal "None".
			_fail(ERR_INVALID_VALUE, _("{0} needs a valid date or time.").format(entry["label"]))
		return parsed.strftime("%H:%M:%S.%f") if fieldtype == "Time" else str(parsed)

	if fieldtype == "Check":
		text = str(value).strip().lower()
		if text in ("1", "true", "yes"):
			return 1
		if text in ("0", "false", "no"):
			return 0
		_fail(ERR_INVALID_VALUE, _("{0} is a checkbox: use 0 or 1.").format(entry["label"]))
	if fieldtype in NUMERIC_FIELDTYPES:
		# NOTE: flt("")/cint("") == 0, so a blank numeric filter means "= 0".
		# That is Frappe's behaviour (db_query does flt(f.value)) and is left at
		# parity deliberately — see the ledger's D14 note.
		return _as_number(entry, value)

	text = _cap_text(value)
	options = _select_options(entry)
	if options is not None and operator in (OP_EQ, OP_NE, OP_IN, OP_NOT_IN) and text not in options:
		# Stricter than Frappe, which happily runs an impossible Select filter.
		# Deviation D6.
		_fail(ERR_INVALID_VALUE, _("{0} has no such option.").format(entry["label"]))
	return text


def _like_value(value: Any) -> str:
	"""Frappe's UI wildcard behaviour (filter.js get_selected_value): wrap the
	term unless the user already supplied a wildcard. Backslashes are doubled so
	a literal ``\\`` survives LIKE's own escape processing — exactly what
	db_query does. ``%``/``_`` typed by the user stay wildcards, as in Frappe."""
	text = _cap_text(value).replace("\\", "\\\\")
	if text and not (text.startswith("%") or text.endswith("%")):
		text = f"%{text}%"
	return text


def _between_bounds(entry: dict, value: Any) -> tuple[str, str]:
	"""Frappe's get_between_date_filter expansion — a bare date on a Datetime
	field becomes [00:00:00, 23:59:59.999999] — as bound values rather than
	interpolated literals, but WITHOUT its today-fallback for a missing bound.

	``Between`` is the default operator for Date and Datetime, so it is the first
	path a client reaches, and Frappe answers a half-empty range by quietly
	substituting today at both ends. That is the same silent-wrong-answer class
	the scalar guard rejects, arrived at through the more common door. Both
	bounds must be present and non-blank. Declared deviation, D14.
	"""
	import datetime

	from frappe.utils import get_datetime, getdate

	_guard_length(value)
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
			value = parsed if isinstance(parsed, list) else [value]
		except Exception:
			value = [v.strip() for v in value.split(",")]
	if not isinstance(value, list | tuple):
		value = [value]
	# The cap has to be re-applied per element: the raw payload can be a short
	# JSON string (or a list) whose MEMBERS are megabytes.
	for element in value:
		_guard_length(element)
	if len(value) != 2 or any(_is_blank_temporal(v) for v in value[:2]):
		_fail(ERR_INVALID_VALUE, _("{0} needs a start and an end.").format(entry["label"]))
	frm, to = value[0], value[1]

	if entry["fieldtype"] == "Datetime":

		def _expand(raw, end: bool):
			if isinstance(raw, str) and " " in raw.strip():
				return get_datetime(raw)
			day = getdate(raw)
			return datetime.datetime.combine(
				day, datetime.time(23, 59, 59, 999999) if end else datetime.time()
			)

		try:
			return str(_expand(frm, False)), str(_expand(to, True))
		except Exception:
			_fail(ERR_INVALID_VALUE, _("{0} needs two valid dates.").format(entry["label"]))
	try:
		return str(getdate(frm)), str(getdate(to))
	except Exception:
		_fail(ERR_INVALID_VALUE, _("{0} needs two valid dates.").format(entry["label"]))


def _timespan_bounds(entry: dict, value: Any) -> tuple[str, str]:
	from frappe.utils.data import get_timespan_date_range

	token = str(value or "").strip().lower()
	rng = get_timespan_date_range(token) if token else None
	if not rng:
		_fail(ERR_INVALID_VALUE, _("Unknown timespan."))
	return _between_bounds(entry, [str(rng[0]), str(rng[1])])


def validate_clauses(schema: dict, clauses: Any) -> list[_Clause]:
	"""Every rule from plan §6.3 steps 3-6, in order, failing closed."""
	items = parse_clauses(clauses)
	if len(items) > MAX_CLAUSES:
		_fail(ERR_TOO_MANY_CLAUSES, _("At most {0} filters at a time.").format(MAX_CLAUSES))

	index = _schema_index(schema)
	out: list[_Clause] = []
	for position, item in enumerate(items):
		doctype = str(item.get("doctype") or "").strip()
		fieldname = str(item.get("fieldname") or "").strip()
		operator = str(item.get("operator") or "").strip()
		entry = index.get((doctype, fieldname))
		if not entry:
			# Unknown, removed, virtual, secret or ABOVE THIS CALLER'S PERMLEVEL.
			# One message for all of them: a distinguishable "exists but you may
			# not read it" is itself the oracle we are closing (C08-1).
			_fail(ERR_UNKNOWN_FIELD, _("That field cannot be filtered on this list."))
		if not operator:
			operator = entry["default_operator"]
		if operator not in entry["operators"]:
			_fail(ERR_INVALID_OPERATOR, _("{0} does not support that condition.").format(entry["label"]))

		raw = item.get("value")
		if operator == OP_IS:
			token = str(raw or "").strip().lower()
			if token not in ("set", "not set"):
				_fail(ERR_INVALID_VALUE, _("Use 'set' or 'not set'."))
			value: Any = token
		elif operator in (OP_IN, OP_NOT_IN):
			values = raw
			if isinstance(values, str):
				try:
					parsed = json.loads(values)
					values = parsed if isinstance(parsed, list) else [parsed]
				except Exception:
					values = [v.strip() for v in values.split(",")]
			if not isinstance(values, list | tuple):
				values = [values]
			values = [v for v in values if v not in (None, "")]
			if not values:
				_fail(ERR_INVALID_VALUE, _("{0} needs at least one value.").format(entry["label"]))
			if len(values) > MAX_IN_VALUES:
				_fail(
					ERR_TOO_MANY_VALUES,
					_("At most {0} values in one condition.").format(MAX_IN_VALUES),
				)
			value = tuple(_scalar(entry, operator, v) for v in values)
		elif operator == OP_BETWEEN:
			value = _between_bounds(entry, raw)
		elif operator == OP_TIMESPAN:
			value = _timespan_bounds(entry, raw)
		elif operator in (OP_LIKE, OP_NOT_LIKE):
			value = _like_value(raw)
		else:
			value = _scalar(entry, operator, raw)

		out.append(_Clause(index=position, entry=entry, operator=operator, value=value))
	return out


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #
#: Every parameter this module binds is namespaced, so a server-authored
#: predicate can never collide with (or be shadowed by) a user value.
USER_PARAM_PREFIX = "jf"


def _column(ref: str, fieldname: str) -> str:
	return f"{ref}.`{fieldname}`"


def _and(parts: list[str]) -> str:
	"""AND-join, parenthesising EVERY part.

	Structural, not cosmetic: an unparenthesised join means a predicate
	containing a top-level ``OR`` binds loosely and silently widens the whole
	WHERE. The skills scope predicate is literally ``a OR (b AND c)``, and any
	future endpoint author writing one is one keystroke from that bug. Making the
	joiner defensive removes the footgun from every caller at once."""
	return " AND ".join(f"({p})" for p in parts if p)


def _compile_clause(clause: _Clause, ref: str) -> tuple[str, dict]:
	"""One clause → (SQL with named placeholders, params). Identifiers come only
	from the resolved schema entry; every value is a bound parameter."""
	entry = clause.entry
	fieldtype = entry["fieldtype"]
	fieldname = entry["fieldname"]
	col = _column(ref, fieldname)
	op = clause.operator
	p0 = f"{USER_PARAM_PREFIX}{clause.index}_0"
	p1 = f"{USER_PARAM_PREFIX}{clause.index}_1"

	if op == OP_IS:
		# db_query: `is set` → ifnull(col,'') != '' ; `is not set` → = ''
		return (f"ifnull({col}, '') {'!=' if clause.value == 'set' else '='} ''", {})

	if op in (OP_BETWEEN, OP_TIMESPAN):
		lo, hi = clause.value
		return (f"{col} BETWEEN %({p0})s AND %({p1})s", {p0: lo, p1: hi})

	if op in (OP_IN, OP_NOT_IN):
		sql_op = "IN" if op == OP_IN else "NOT IN"
		# db_query's rule, precisely: for `in` it clears can_be_null unless the
		# value list is empty or contains a falsy member — and we reject empty
		# lists (D9) and strip blank members, so `in` NEVER coalesces. Wrapping it
		# anyway (as this did) defeats index use on every `in` filter for no
		# semantic gain. `not in` keeps the wrapper, because NULL NOT IN (...) is
		# NULL and those rows would silently vanish — which IS frappe's rule.
		if (
			op == OP_IN
			or fieldtype in NON_NULLABLE_FIELDTYPES
			or fieldname in ("name", "creation", "modified")
		):
			return (f"{col} {sql_op} %({p0})s", {p0: tuple(clause.value)})
		return (
			f"ifnull({col}, {_fallback_sql(fieldtype)}) {sql_op} %({p0})s",
			{p0: tuple(clause.value)},
		)

	sql_op = "LIKE" if op == OP_LIKE else "NOT LIKE" if op == OP_NOT_LIKE else op

	# db_query's can_be_null rules, mirrored.
	can_be_null = fieldname not in ("name", "modified", "creation")
	if fieldtype in NON_NULLABLE_FIELDTYPES:
		can_be_null = False
	if op in (OP_GT, OP_GTE) and fieldtype in ("Date", "Datetime"):
		can_be_null = False
	if clause.value and op in (OP_EQ, OP_LIKE):
		can_be_null = False

	if can_be_null:
		col = f"ifnull({col}, {_fallback_sql(fieldtype)})"
	return (f"{col} {sql_op} %({p0})s", {p0: clause.value})


@dataclass
class CompiledFilters:
	"""Validated + compiled client clauses. Immutable once built.

	``fragment()`` can drop one dimension so a facet query reuses the identical
	WHERE **minus the facet's own dimension** — the C08-3 rule (the shipped
	approvals ``document_type`` and learning ``domain`` facets both depend on
	dropping their own filter, which plan §6.3.9's literal "identical WHERE"
	would have broken).
	"""

	root_doctype: str
	ref: str
	main: list[tuple[tuple[str, str], str]] = field(default_factory=list)
	child: list[tuple[tuple[str, str], str, str]] = field(default_factory=list)
	child_alias: dict[str, str] = field(default_factory=dict)
	params: dict = field(default_factory=dict)

	def is_empty(self) -> bool:
		return not self.main and not self.child

	def dimensions(self) -> list[tuple[str, str]]:
		return [dim for dim, _sql in self.main] + [dim for dim, _dt, _sql in self.child]

	def fragment(self, exclude_dimension: tuple[str, str] | None = None) -> str:
		"""One AND-joined SQL string for a caller assembling its own WHERE."""
		return _and(self.fragments(exclude_dimension))

	def fragments(self, exclude_dimension: tuple[str, str] | None = None) -> list[str]:
		"""The compiled clauses as SEPARATE parts.

		:class:`ListFilterQuery` consumes this rather than :meth:`fragment` so
		each user clause is parenthesised exactly once in the final WHERE instead
		of the whole user block being wrapped again around already-wrapped parts.
		"""
		parts = [sql for dim, sql in self.main if dim != exclude_dimension]
		groups: dict[str, list[str]] = {}
		for dim, child_dt, sql in self.child:
			if dim == exclude_dimension:
				continue
			groups.setdefault(child_dt, []).append(sql)
		for child_dt in sorted(groups):
			alias = self.child_alias[child_dt]
			inner = _and(groups[child_dt])
			# ONE EXISTS per child DocType with every clause on that child ANDed
			# INSIDE it: that preserves Frappe's single-join meaning, where two
			# conditions on one child table must be satisfied by the SAME child
			# row (C08-2). EXISTS also keeps parents un-duplicated, so total /
			# has_more / pagination stay correct without count fudging.
			parts.append(
				f"EXISTS (SELECT 1 FROM `tab{child_dt}` `{alias}` "
				f"WHERE `{alias}`.`parent` = {self.ref}.`name` "
				f"AND `{alias}`.`parenttype` = %({alias}_pt)s AND ({inner}))"
			)
		return parts


def compile_validated(root_doctype: str, clauses: list[_Clause], ref: str) -> CompiledFilters:
	child_doctypes = sorted({c.entry["doctype"] for c in clauses if c.entry["is_child"]})
	# Alias per child DocType, assigned from the FULL clause set so a facet's
	# exclusion cannot renumber (and therefore invalidate) a parameter name.
	alias_map = {dt: f"jfc{i}" for i, dt in enumerate(child_doctypes)}

	compiled = CompiledFilters(root_doctype=root_doctype, ref=ref, child_alias=alias_map)
	for dt, alias in alias_map.items():
		compiled.params[f"{alias}_pt"] = root_doctype
	for clause in clauses:
		entry = clause.entry
		if entry["is_child"]:
			alias = alias_map[entry["doctype"]]
			sql, params = _compile_clause(clause, f"`{alias}`")
			compiled.child.append((clause.dimension, entry["doctype"], sql))
		else:
			sql, params = _compile_clause(clause, ref)
			compiled.main.append((clause.dimension, sql))
		compiled.params.update(params)
	return compiled


def compile_list_filters(
	view_key: str,
	clauses: Any,
	*,
	alias: str | None = None,
	user: str | None = None,
) -> CompiledFilters:
	"""Resolve the view, rebuild THIS caller's schema, validate, compile.

	SHORT-CIRCUIT: the payload is parsed first, and an absent or empty one returns
	an empty :class:`CompiledFilters` without touching metadata, the schema cache
	or the database. That is the >99% path — every list load that has no filters
	on it — and it matters twice over: the migrated endpoints keep exactly their
	pre-migration cost, and a metadata problem in the filter layer cannot take
	down an unfiltered list. The view is still resolved (a bad ``view_key`` is a
	contract error whether or not clauses came with it), but that is a dict
	lookup.
	"""
	items = parse_clauses(clauses)
	view = resolve_view(view_key)
	root = _filterable_root(view)
	ref = f"`{alias}`" if alias else f"`tab{root}`"
	if not items:
		return CompiledFilters(root_doctype=root, ref=ref)
	import time

	started = time.monotonic()
	schema = get_schema(view_key, user=user)
	try:
		validated = validate_clauses(schema, items)
	except ListFilterError as e:
		emit_filter_telemetry(
			view_key,
			[],
			duration_ms=(time.monotonic() - started) * 1000,
			error_code=getattr(e, "filter_error_code", ERR_BAD_PAYLOAD),
		)
		raise
	compiled = compile_validated(root, validated, ref)
	emit_filter_telemetry(view_key, validated, duration_ms=(time.monotonic() - started) * 1000)
	return compiled


def compile_filters_v2(
	view_key: str,
	clauses: Any,
	*,
	alias: str | None = None,
	user: str | None = None,
	exclude_dimension: tuple[str, str] | None = None,
) -> tuple[str, dict]:
	"""``(sql_fragment, params)`` for an endpoint that assembles its own WHERE.

	The fragment is ``""`` when there are no clauses. Endpoints that also carry a
	fixed visibility predicate should use :func:`new_query` instead, so the
	predicate and the user clauses stay structurally separate (C08-5).
	"""
	compiled = compile_list_filters(view_key, clauses, alias=alias, user=user)
	return compiled.fragment(exclude_dimension), dict(compiled.params)


# --------------------------------------------------------------------------- #
# The structural barrier
# --------------------------------------------------------------------------- #
class ListFilterQuery:
	"""Assembles a list WHERE clause from two things that never mix.

	* :meth:`server_condition` — SQL the ENDPOINT AUTHOR wrote (``owner = %(me)s``,
	  a DocShare id set, a scope predicate) plus its values as named params.
	  These are the list's non-removable scope: user filters are ANDed inside it
	  and can never widen it.
	* :meth:`apply` — client clauses. They go through
	  :func:`validate_clauses`, so the only thing that survives from the request
	  is a bound parameter VALUE; identifiers and operators come from the
	  caller's schema.

	There is no method that appends client text to the server list, and none that
	appends server SQL to the compiled-user side. ``where()`` is the single
	junction, and it joins two already-built strings.

	Server-authored ``IN`` lists are deliberately NOT capped by
	:data:`MAX_IN_VALUES`: that cap exists to bound attacker-controlled payloads,
	and a legitimate "the 4,000 skills shared with you" list is not one (C08-5).
	"""

	def __init__(self, view_key: str, *, alias: str | None = None, user: str | None = None):
		self.view = resolve_view(view_key)
		self.view_key = self.view.view_key
		self.user = user or frappe.session.user
		self.alias = alias
		self._server_conds: list[str] = []
		self._server_params: dict = {}
		self._compiled: CompiledFilters | None = None

	# -- server side ------------------------------------------------------- #
	def server_condition(self, sql: str, **params: Any) -> ListFilterQuery:
		"""Add a server-authored predicate. ``sql`` must use named placeholders
		(``%(name)s``) and must not touch the user parameter namespace."""
		if "%s" in sql:
			raise ValueError("server_condition needs named placeholders, not %s")
		for name in params:
			if name.startswith(USER_PARAM_PREFIX):
				raise ValueError(f"'{USER_PARAM_PREFIX}*' is the user parameter namespace: rename {name!r}")
			if name in self._server_params and self._server_params[name] != params[name]:
				raise ValueError(f"duplicate server parameter {name!r}")
		self._server_conds.append(sql)
		self._server_params.update(params)
		return self

	# -- user side --------------------------------------------------------- #
	def apply(self, clauses: Any) -> ListFilterQuery:
		"""Validate + compile client clauses. Idempotent per query object."""
		if self._compiled is not None:
			raise ValueError("filters_v2 already applied to this query")
		self._compiled = compile_list_filters(self.view_key, clauses, alias=self.alias, user=self.user)
		return self

	@property
	def compiled(self) -> CompiledFilters | None:
		return self._compiled

	def dimensions(self) -> list[tuple[str, str]]:
		return self._compiled.dimensions() if self._compiled else []

	# -- the single junction ----------------------------------------------- #
	def where(self, *, exclude_dimension: tuple[str, str] | None = None) -> str:
		parts = list(self._server_conds)
		if self._compiled:
			parts.extend(self._compiled.fragments(exclude_dimension))
		return _and(parts) or "1=1"

	def params(self, extra: dict | None = None) -> dict:
		"""The merged bind map. ``extra`` carries the endpoint's non-predicate
		params (``start``, ``page_length``); a collision with an already-bound
		name is a programming error and raises rather than silently overwriting a
		predicate's value — which would change what the WHERE means."""
		out = dict(self._server_params)
		if self._compiled:
			out.update(self._compiled.params)
		for name, value in (extra or {}).items():
			if name in out:
				raise ValueError(f"parameter {name!r} is already bound by this query")
			out[name] = value
		return out


def new_query(view_key: str, *, alias: str | None = None, user: str | None = None) -> ListFilterQuery:
	"""Start a list query for ``view_key`` (see :class:`ListFilterQuery`)."""
	return ListFilterQuery(view_key, alias=alias, user=user)


# --------------------------------------------------------------------------- #
# Bounded execution
# --------------------------------------------------------------------------- #
#: MariaDB ER_STATEMENT_TIMEOUT.
_STATEMENT_TIMEOUT_ERRNO = 1969


def _is_statement_timeout(exc: BaseException) -> bool:
	"""Did this exception come from the statement ceiling, not from bad SQL?

	Checked by errno first and by message only as a fallback, and across the
	wrapper chain, because the driver exception can arrive re-raised.
	"""
	seen = (exc, getattr(exc, "orig", None), getattr(exc, "__cause__", None))
	for candidate in seen:
		if candidate is None:
			continue
		args = getattr(candidate, "args", ()) or ()
		if args and args[0] == _STATEMENT_TIMEOUT_ERRNO:
			return True
	return "max_statement_time exceeded" in str(exc)


def _statement_timeout_supported() -> bool:
	# `SET STATEMENT ... FOR` is MariaDB syntax. On anything else we degrade to
	# an unbounded query rather than emit SQL the server will reject — a list
	# that works is better than a list that 500s over a safety net.
	return getattr(frappe.db, "db_type", "mariadb") == "mariadb"


def reject_unsupported_legacy_filters(view_key: str) -> None:
	"""For a migrated surface whose endpoint has no legacy ``filters`` semantics.

	The migration contract requires the legacy argument to still EXIST (the
	compatibility window is not over and
	``test_migrated_views_actually_accept_filters_v2`` checks for it), but a
	surface whose curated controls are named parameters has nothing to do with a
	value passed there. Accepting and ignoring it means a caller that believes it
	is filtering gets a confidently wrong, wider-than-asked-for list — the exact
	silent-widening this contract exists to prevent — so say so instead.
	"""
	_fail(
		ERR_BAD_PAYLOAD,
		_("This list does not accept the legacy filters argument; use filters_v2."),
	)


def bounded_sql(query: str, values: Any = None, **kwargs: Any):
	"""``frappe.db.sql`` under a per-STATEMENT wall-clock ceiling.

	Every migrated list runs its rows AND its count through here, so a runaway
	filter cannot occupy a worker indefinitely on either pass.

	``SET STATEMENT max_statement_time=N FOR <query>`` is deliberate, over the
	obvious ``SET SESSION``: Frappe pools and reuses connections, so a session
	variable set here would silently apply to whatever ran next on that
	connection — including background jobs — and any "reset it afterwards" is
	one early return or exception away from leaking. The per-statement form has
	no state to leak and nothing to reset; the scope IS the statement.

	A breach becomes :data:`ERR_QUERY_TOO_EXPENSIVE`, so it reaches the client as
	the same coded envelope as any other filter rejection instead of a raw 500 —
	and as a code the panel can render as "narrow this", which is the only action
	that helps. The statement is aborted; the connection and the (read-only)
	transaction are untouched.
	"""
	# `values=None` is NOT frappe's default (it is an empty tuple), and passing
	# None through makes the driver attempt `query % None` and raise before the
	# query ever runs. Forward the argument only when there is one.
	args = () if values is None else (values,)
	if not _statement_timeout_supported():
		return frappe.db.sql(query, *args, **kwargs)
	# Formatted as a FLOAT, deliberately. `int()` truncated a sub-second bound to
	# 0, and MariaDB reads 0 as "no limit" — so the one edit this feature invites
	# (tightening the bound because 10s felt slow) would have silently removed
	# the ceiling instead of lowering it. MariaDB's max_statement_time takes
	# fractional seconds, so there is nothing to round for.
	seconds = float(STATEMENT_TIMEOUT_SECONDS)
	bounded = f"SET STATEMENT max_statement_time={seconds} FOR {query.lstrip()}"
	try:
		return frappe.db.sql(bounded, *args, **kwargs)
	except Exception as e:
		if not _is_statement_timeout(e):
			raise
		_fail(
			ERR_QUERY_TOO_EXPENSIVE,
			_("That filter is too broad to run on this list — narrow it and try again."),
		)


def apply_filters_v2(
	view_key: str,
	clauses: Any,
	server_conditions: list[tuple[str, dict]] | None = None,
	*,
	alias: str | None = None,
	user: str | None = None,
) -> ListFilterQuery:
	"""One-shot :class:`ListFilterQuery` for rows / total / facet reuse.

	``server_conditions`` is a list of ``(sql, params)`` pairs. The returned query
	answers ``where()`` for the row and count passes and
	``where(exclude_dimension=...)`` for a facet over its own dimension.
	"""
	query = new_query(view_key, alias=alias, user=user)
	for sql, params in server_conditions or []:
		query.server_condition(sql, **(params or {}))
	return query.apply(clauses)


# --------------------------------------------------------------------------- #
# Shared list plumbing (C08-5).
#
# `_lk` / `_clamp_page` / `_bool01` / `_load_filters` / `_order_by` were copied
# into six list modules and had begun to drift. These are the canonical copies;
# the two pilot endpoints import them, and macros_api keeps private aliases so
# its existing importers (dashboards_api, app_learning_api) are untouched until
# their own wave.
# --------------------------------------------------------------------------- #
def escape_like(s: str) -> str:
	"""Escape LIKE wildcards in a free-text SEARCH term (``\\`` is the default
	escape). Note this is the opposite of :func:`_like_value`: a search box is
	not a query language, a ``like`` filter clause is."""
	return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clamp_page(start, page_length) -> tuple[int, int]:
	try:
		start = max(0, int(start or 0))
	except (TypeError, ValueError):
		start = 0
	try:
		pl = int(page_length or 20)
	except (TypeError, ValueError):
		pl = 20
	return start, max(1, min(pl, MAX_PAGE_LENGTH))


def bool01(v) -> int:
	try:
		iv = int(v)
	except (TypeError, ValueError):
		frappe.throw(_("Filter value must be 0 or 1."))
	if iv not in (0, 1):
		frappe.throw(_("Filter value must be 0 or 1."))
	return iv


def load_legacy_filters(filters, allowed: set) -> dict:
	"""Parse a legacy ``filters`` argument (JSON string or dict), whitelist keys
	(unknown → throw), and drop empty values (an empty value = 'not filtering';
	``0`` is kept)."""
	if isinstance(filters, str):
		if filters.strip():
			try:
				raw = frappe.parse_json(filters)
			except Exception:
				raw = {}
		else:
			raw = {}
	else:
		raw = filters or {}
	if not isinstance(raw, dict):
		raw = {}
	out: dict = {}
	for k, v in raw.items():
		if k not in allowed:
			frappe.throw(_("Unknown filter: {0}").format(k))
		if v in (None, ""):
			continue
		out[k] = v
	return out


def order_by(sort_field, sort_dir, sortable: dict, default_field, default_dir, prefix="") -> str:
	"""Build a safe ORDER BY: only whitelisted columns, direction normalized to
	asc/desc, a ``name`` tiebreak for stable OFFSET pagination. No user input is
	ever interpolated (columns come from ``sortable``; dir is a literal)."""
	col = sortable.get(sort_field or "")
	if not col:
		return f"{prefix}`{sortable[default_field]}` {default_dir}, {prefix}`name` asc"
	d = "desc" if (sort_dir or "").lower() == "desc" else "asc"
	return f"{prefix}`{col}` {d}, {prefix}`name` asc"
