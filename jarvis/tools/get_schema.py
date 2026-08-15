import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._doc_actions import get_doc_actions

TABLE_FIELDTYPES = {"Table", "Table MultiSelect"}
_SCHEMA_TTL = 300  # seconds; schema is user-independent + changes rarely
# v2: doctype-level `custom` + per-field `is_custom` (customization discovery).
_SCHEMA_CACHE_VERSION = 2


def _cache_key(doctype: str, verbose) -> str:
	"""The one spelling of the key. Bump _SCHEMA_CACHE_VERSION on any cached
	shape change; old keys age out at the TTL."""
	return f"jarvis_schema:v{_SCHEMA_CACHE_VERSION}:{doctype}:{int(bool(verbose))}"


# Identity: emitted unconditionally because a record without them is anonymous.
# Every other property is emitted ONLY when the DocType configures it to a truthy
# value - including label / options / reqd, which used to be emitted always. A
# falsy docfield property IS the Frappe default (Check -> 0, Data/Text -> "" or
# None), so `"options": ""` on a thousand primitive fields was pure padding: it
# said "no options" in 15 bytes where absence says it in zero. Measured across 6
# real doctypes, dropping that padding pays for every property added below - the
# whole change lands at ~1.07x the old payload instead of ~1.28x.
_IDENTITY_PROPS = ("fieldname", "fieldtype")

# DocField's OWN row metadata - describes the docfield record, not the field it
# defines. Never useful to the agent, and some values (creation/modified) are
# datetimes that would not survive the JSON hop anyway. is_custom_field is
# meta-merge provenance, re-emitted as `is_custom` explicitly in _field_record.
_ROW_META_PROPS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"parent",
		"parentfield",
		"parenttype",
		"idx",
		"docstatus",
		"doctype",
		"oldfieldname",
		"oldfieldtype",
		"__islocal",
		"__unsaved",
		"is_custom_field",
	}
)

# Presentation-only: these change how Desk PAINTS a field and can never affect
# what a write may contain, so for the agent they are pure context cost.
#
# Measured across Sales Order / Sales Invoice / Item / Payment Entry / Delivery
# Note: emitting EVERY configured property costs ~1.29x the old slim record;
# excluding this set brings that to ~1.07x while keeping every property that can
# change what the agent may write. That matters - the slim default exists because
# of the 2026-06-22 gpt-5.5 context overflow, so growth here has to earn itself.
_UI_ONLY_PROPS = frozenset(
	{
		"allow_bulk_edit",
		"allow_in_quick_entry",
		"bold",
		"collapsible",
		"collapsible_depends_on",
		"columns",
		"documentation_url",
		"hide_border",
		"hide_days",
		"hide_seconds",
		"hide_toolbar",
		"in_global_search",
		"in_preview",
		"make_attachment_public",
		"print_hide",
		"print_hide_if_no_value",
		"print_width",
		"remember_last_selected_value",
		"report_hide",
		"search_index",
		"show_dashboard",
		"show_description_on_click",
		"sort_options",
		"translatable",
		"width",
	}
)

_SKIP_PROPS = _ROW_META_PROPS | _UI_ONLY_PROPS | frozenset(_IDENTITY_PROPS)


def _field_record(f) -> dict:
	"""Per-field response shape: ``fieldname`` + ``fieldtype`` always, then every
	docfield property the DocType configures to a truthy value.

	ABSENT MEANS "NOT CONFIGURED" - i.e. the Frappe default, which is always
	falsy. A reader must never read a missing key as unknown. That one rule is
	what lets this carry far more per field than the old fixed five keys while
	costing ~7% more, not ~28%.

	Commonly present, and the reason this exists - the properties that change what
	a write may legally contain:

	- ``label``: human label (absent on layout breaks, which have none).
	- ``options``: target DocType for Link / Table / Table MultiSelect / Dynamic
	  Link; enum values (newline-separated) for Select. Absent on primitives.
	  Without it Link/Select/Table fields are opaque - the agent can't follow
	  them, filter on enum values, or ``get_schema`` the child DocType.
	- ``reqd``: mandatory on insert.
	- ``read_only`` / ``allow_on_submit`` / ``set_only_once``: writable at all,
	  after submit, or on insert only.
	- ``fetch_from``: value is pulled from a linked doc - set the LINK, not this.
	- ``mandatory_depends_on`` / ``read_only_depends_on`` / ``depends_on``: the
	  conditional forms, which ``reqd`` alone does not capture.
	- ``default``, ``unique``, ``no_copy``, ``precision``, ``permlevel``,
	  ``is_virtual``, ``link_filters``.

	Deliberately dropped: DocField row metadata (``_ROW_META_PROPS``) and
	presentation-only properties (``_UI_ONLY_PROPS``) that cannot affect a write.
	Everything else rides along by default, so a property Frappe adds later
	surfaces without a code change - the inverse of an allowlist, which would
	silently omit it.
	"""
	record = {"fieldname": f.fieldname, "fieldtype": f.fieldtype}
	for key, value in f.as_dict().items():
		if key in _SKIP_PROPS:
			continue
		# Falsy == "not configured" for every docfield property: Check defaults to
		# 0, Data/Text to "" or None. Dropping them is the whole budget.
		if not value:
			continue
		# Defensive: docfield columns are all Data/Text/Int/Check, so this only
		# bites if Frappe adds an exotic column - which must not break the JSON
		# hop to the plugin or the pickled cache entry.
		if not isinstance(value, (str, int, float, bool)):
			continue
		record[key] = value
	# Custom Field provenance, emitted only when true (absent = standard).
	if getattr(f, "is_custom_field", False):
		record["is_custom"] = True
	return record


def _workflow_for(doctype: str):
	"""The active Workflow's states for ``doctype``, or None. Live
	introspection so the agent reads the real state machine instead of a
	(possibly drifted) Skill note."""
	wf = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if not wf:
		return None
	doc = frappe.get_doc("Workflow", wf)
	return {
		"name": wf,
		"state_field": doc.workflow_state_field,
		"states": [s.state for s in doc.states],
	}


def _is_custom_doctype(meta) -> bool:
	"""custom=1 OR module of a non-core app; failure reads standard."""
	if getattr(meta, "custom", 0):
		return True
	try:
		from jarvis.site_profile.apps import is_custom_doctype_module

		return is_custom_doctype_module(getattr(meta, "module", None))
	except Exception:
		return False


def _build_schema(doctype: str, verbose: bool) -> dict:
	meta = frappe.get_meta(doctype)
	fields = []
	for f in meta.fields:
		record = _field_record(f)
		if verbose and f.fieldtype in TABLE_FIELDTYPES and f.options:
			child_meta = frappe.get_meta(f.options)
			record["child_fields"] = [_field_record(cf) for cf in child_meta.fields]
		fields.append(record)
	return {
		"doctype": doctype,
		"custom": _is_custom_doctype(meta),
		"is_submittable": bool(meta.is_submittable),
		"autoname": meta.autoname,
		"naming_rule": getattr(meta, "naming_rule", None),
		"title_field": meta.title_field,
		"workflow": _workflow_for(doctype),
		"actions": _safe_actions(doctype),
		"fields": fields,
	}


def _safe_actions(doctype: str) -> list:
	"""Discovery hint: whitelisted server methods this DocType's form exposes as
	buttons (callable via run_method). Extraction is best-effort and must never
	break get_schema, so degrade to [] on any error."""
	try:
		return get_doc_actions(doctype)
	except Exception:
		return []


def _as_bool(value) -> bool:
	"""Coerce a possibly-stringified flag to bool. A JSON client may send
	``"false"``/``"0"``; ``bool("false")`` is True, so treat common falsy
	strings as False rather than trusting plain ``bool()``."""
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def clear_cache_for(doctype: str) -> None:
	"""Drop both cached schema variants (slim + verbose) for a DocType, plus its
	resolved mapper map - that is scraped from the same form JS as ``actions``,
	so a Client Script edit invalidates both."""
	from jarvis.tools._source_mapper import mapper_cache_key

	cache = frappe.cache()
	cache.delete_value(_cache_key(doctype, 0))
	cache.delete_value(_cache_key(doctype, 1))
	cache.delete_value(mapper_cache_key(doctype))


def clear_schema_cache(doc, method=None) -> None:
	"""doc_event handler (wired in hooks.py): when a schema-defining doc is
	saved or trashed, bust the cached schema for the DocType it affects so the
	agent never builds a write off a stale field list within the TTL window."""
	dtype = getattr(doc, "doctype", None)
	dt = None
	if dtype == "DocType":
		dt = doc.name
	elif dtype == "Custom Field":
		dt = getattr(doc, "dt", None)
	elif dtype == "Property Setter":
		dt = getattr(doc, "doc_type", None)
	elif dtype == "Workflow":
		dt = getattr(doc, "document_type", None)
	elif dtype == "Client Script":
		# actions hints are scraped from form JS, which includes Form Client Scripts
		dt = getattr(doc, "dt", None)
	if dt:
		clear_cache_for(dt)


def get_schema(doctype: str, verbose: bool = False, refresh: bool = False) -> dict:
	"""Return live meta for a DocType: identity + the write-relevant
	doctype-level flags + the field list.

	Top level: ``doctype``, ``custom`` (UI-authored or shipped by a non-core
	app), ``is_submittable`` (docstatus lifecycle applies), ``autoname`` /
	``naming_rule`` (how name is assigned), ``title_field``,
	``workflow`` ({name, state_field, states} or None), ``actions``, and ``fields``.
	Every field record carries ``fieldname`` and ``fieldtype``, then EVERY other
	docfield property the DocType configures - and only those. **A key that is
	absent is not configured: read it as the Frappe default, which is always
	falsy (0 / "" ). Never read absence as unknown.** So a plain Data field is
	just ``{"fieldname", "fieldtype", "label"}``, while a mandatory Link carries
	``options`` + ``reqd`` too.

	- ``label``: human label (absent on layout breaks).
	- ``options``: Link/Table/Dynamic Link target DocType, or Select enum values
	  (newline-separated). Absent on primitive fields.
	- ``reqd``: mandatory on insert.
	- ``read_only`` / ``allow_on_submit`` / ``set_only_once``: whether the field
	  can be written at all, after submit, or only on insert.
	- ``fetch_from`` (``"link_field.source_field"``): the value is pulled from a
	  linked doc - set the LINK, not this.
	- ``mandatory_depends_on`` / ``read_only_depends_on`` / ``depends_on``: the
	  condition under which the field becomes required / locked / shown, so
	  ``reqd`` alone does not tell the whole story.
	- ``default``, ``unique``, ``no_copy``, ``precision``, ``permlevel``.
	- ``is_custom``: present (true) on fields added via Custom Field.
	- ``link_filters`` (JSON, e.g. ``[["Item","has_variants","=",0]]``): which
	  target rows this Link will accept. Drop the leading DocType and the rest is
	  a ``get_list`` filter, so it doubles as the query for finding a valid value
	  (``get_list("Item", filters={"has_variants": 0})``). Only a minority of Link
	  fields declare it; absence does not mean the Link is unfiltered.

	Presentation-only properties (print/report/column widths, collapsible,
	in_global_search, ...) are deliberately omitted - they cannot affect a write
	and the field list is already the biggest thing this tool returns.

		``actions`` is a discovery hint: the whitelisted server methods this
		DocType's form exposes as custom buttons (e.g. ``make_sales_invoice`` on a
		Sales Order), each ``{"method", "label", "args"}``, callable via
		``run_method``. Best-effort and possibly incomplete - absence does not
		mean a method is uncallable.

	By default Table / Table MultiSelect fields surface as ordinary records
	(child DocType named via ``options``) WITHOUT recursive ``child_fields`` -
	the agent calls ``get_schema`` on the child when it needs that list (cheap;
	keeps transcripts small). Pass ``verbose=True`` to inline each child's
	schema in one call (one level only; Frappe forbids nested tables). The
	slim default is the durable fix for the 2026-06-22 gpt-5.5 context overflow
	where 8 recursive schema dumps overran the model.

	Result is cached in Redis for ~5 min (schema is the same for every user).
	The cache is busted automatically when a Custom Field / Property Setter /
	DocType / Workflow change fires its doc_event (see hooks.py), so a
	customization shows up immediately rather than after the TTL. The
	read-permission check below runs on EVERY call regardless of cache, so
	caching never leaks a schema to a user who can't read the DocType. Pass
	``refresh=True`` to force a re-read (busts both slim + verbose variants).

	Enforces read permission on the parent DocType for the current user. Child
	tables (under ``verbose=True``) are part of the parent, not checked
	separately.
	"""
	verbose = _as_bool(verbose)
	refresh = _as_bool(refresh)

	if not doctype:
		raise InvalidArgumentError("doctype is required")

	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown DocType: {doctype}")

	if not frappe.has_permission(doctype, ptype="read"):
		raise PermissionDeniedError(f"no read permission on {doctype}")

	cache = frappe.cache()
	key = _cache_key(doctype, verbose)
	if refresh:
		clear_cache_for(doctype)  # bust BOTH variants, not just the current one
	else:
		cached = cache.get_value(key)
		if cached is not None:
			return cached
	result = _build_schema(doctype, verbose)
	cache.set_value(key, result, expires_in_sec=_SCHEMA_TTL)
	return result
