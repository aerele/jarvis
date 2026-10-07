"""The guarded Custom Field writes (owner decisions R2-4 REVISED AGAIN and R2-10):
ONE new Custom Field, or an edit of an existing one that changes no column, each
only through a confirmation card. The lock, the schema diff and the wiring into
park / confirm / reconcile are shared (``_guarded_structure``); this module holds
what is particular to a Custom Field: the park checks, and the clean-up.

Why the checks are spelled out instead of trial-running the save: Custom Field's
``on_update`` calls ``frappe.db.updatedb`` (custom_field.py, Frappe 15 :216, Frappe
16 :222), which commits the field row and only then runs ``ALTER TABLE``
(mariadb/database.py ``updatedb`` 15 :453-469, 16 :461-477; ``sql_ddl`` commits
first, database.py 15 :408-412, 16 :451-458). A trial would either be refused by
Frappe before the ALTER or change the table for real. So each known way to fail is
refused at park, naming the field, and once more under the lock at Confirm.

Three rules the first review of this unit paid for:

- Every check reads the values AS THE WRITE STORES THEM (``_stored``: the write
  tools' own ``check_values``, then Frappe's cast). ``unique: "true"`` is 1 to the
  save, so it is 1 here; a check that read the raw argument let it through.
- The schema diff runs on a PRIVATE ``Meta`` (``_private_meta``). ``frappe.get_meta``
  hands back the object it caches (by reference, process-wide, on Frappe 16:
  model/meta.py :89-93), so adding the hypothetical field to it made every later
  insert of the form fail, with nothing confirmed.
- The field a confirmation made is known EXACTLY, by the name and timestamp stamped
  on the confirmation row in the save's own transaction (``_guarded_structure.
  stamp_written``), never by owner or clock.

What a failed ALTER leaves is a field row with no column: every list, save and
insert of that form then fails for every user. ``cleanup_half_made_field`` removes
exactly that row.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

import frappe
from frappe.utils import cint, get_datetime, strip_html

from jarvis.exceptions import InvalidFieldValueError, PermissionDeniedError, StructureRefusedError
from jarvis.tools import _guarded_structure as gs
from jarvis.tools._guarded_structure import CleanUp

CF = "Custom Field"
RISK_NEW = "custom_field_new"
RISK_EDIT = "custom_field_edit"

# Not from chat (owner decision D3): a Table field ties a second doctype's rows to
# the form, and an HTML field is nothing but markup drawn for every user.
_REFUSED_FIELDTYPES = frozenset({"Table", "Table MultiSelect", "HTML"})
# Stricter than Frappe (which admits any Unicode letter): what every database,
# report and script can name without quoting.
_FIELDNAME = re.compile(r"^[a-z_][a-z0-9_]*$")
# Frappe's own bound (database/schema.py ``DBTable.validate``, 15 :126, 16 :129).
_MAX_FIELDNAME = 64
# An edit that changes one of these changes the column (or which column the field
# is): refused, with the Desk page of the field.
_COLUMN_KEYS = ("fieldname", "dt", "fieldtype", "length", "precision", "unique", "search_index", "is_virtual")
# Never set from chat on a new field: they say an app shipped it.
_APP_KEYS = ("is_system_generated", "module")
_DESK_ONLY = "Do not retry it with another tool; tell the user where to do it."

# Forms chat never customises (controller decision, review cycle 1), beside core
# and virtual DocTypes, Jarvis's own, and everything the write-risk lists name:
# who may sign in and see what, and the records that say what happened.
_DENIED_TARGETS = frozenset(
	{
		"User",
		"Role",
		"Has Role",
		"User Permission",
		"Custom DocPerm",
		"DocPerm",
		"Server Script",
		"Client Script",
		"System Settings",
		"Version",
		"Error Log",
		"Access Log",
		"Activity Log",
		# Who a record is shared with, live tokens, and the trails (review cycle 2).
		"DocShare",
		"Document Share Key",
		"OAuth Bearer Token",
		"OAuth Authorization Code",
		"OAuth Client",
		"Role Permission for Page and Report",
		"Deleted Document",
		"Permission Log",
		"Scheduled Job Log",
		"Email Queue",
		"Email Queue Recipient",
	}
)
_TABLE_TYPES = ("Table", "Table MultiSelect")

# Shown on the card in full but neither flagged nor refused (owner-accepted): markup
# in a label or description, and a fetch from an ordinary readable field.
# What a property does beyond the column, said on the card (D2). The code line for
# an ``eval:`` expression: Frappe runs it in every user's browser.
_EXPRESSION_KEYS = ("depends_on", "mandatory_depends_on", "read_only_depends_on", "collapsible_depends_on")
_ACCESS_KEYS = ("permlevel", "ignore_user_permissions", "mask", "read_only", "reqd", "hidden", "fetch_from")
FETCH_LINE = "This field copies data from the linked record for everyone who can see this form."
# A fetch never reads these, whatever the reader may see (``is_secret`` adds the
# obviously secret names and Password fields).
_NO_FETCH_TYPES = frozenset({"Password", "Table", "Table MultiSelect", "HTML", "Button"})


def check_new_custom_field(args) -> None:
	"""Refuse a new Custom Field that is known to fail or that chat may not add.

	Raises ``PermissionDeniedError`` (no Custom Field create or no DocType write),
	``InvalidFieldValueError`` naming the field (a fieldname that is already a field
	or a column of the real table, leftover columns included; a fieldtype chat does
	not add; ``unique`` / ``search_index`` / virtual; a ``fetch_from`` that does not
	resolve or reads a secret; a table with no room for the column) or
	``StructureRefusedError`` (a form chat never customises; a table that carries
	other pending structure changes: the schema diff is not exactly this column)."""
	NewCustomField(args).check()


def check_custom_field_edit(args) -> None:
	"""Refuse an edit of a Custom Field unless it changes no column. Raises as
	``check_new_custom_field``; a change of type, length, name, ``unique`` or index,
	and any edit the schema diff shows a statement for, is a ``StructureRefusedError``
	carrying the field's Desk page."""
	CustomFieldEdit(args).check()


def cleanup_half_made_field(dt: str, fieldname: str, *, written: str | None = None) -> str:
	"""Remove the Custom Field row ``fieldname`` on ``dt`` when its column is missing,
	clear the form's cache, and say what was done. ``written`` is the timestamp the
	confirmation stamped when it wrote the row: only that exact row is this
	confirmation's (None: it wrote nothing, so nothing is removed). A field with no
	column by design (a section, a column break) is not half-made."""
	undo = {"dt": dt, "fieldname": fieldname}
	if written:
		undo["written"] = {"name": f"{dt}-{fieldname}", "modified": str(written)}
	return NewCustomField.clean_up(undo).text


# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #
def _field_meta():
	return frappe.get_meta(CF)


@contextmanager
def _own_messages() -> Iterator[None]:
	"""Drop what the checks themselves msgprint (Frappe's validators do), and only
	that: the request's earlier messages stay."""
	log = getattr(frappe.local, "message_log", None)
	mark = len(log) if log else 0
	try:
		yield
	finally:
		log = getattr(frappe.local, "message_log", None)
		if log and len(log) > mark:
			del log[mark:]


def _private_meta(dt: str):
	"""A ``Meta`` of ``dt`` no cache holds, to put a hypothetical field or change on.
	``frappe.get_meta(dt, cached=False)`` is NOT that: it stores the very object it
	returns (Frappe 16 ``client_cache`` by reference; Frappe 15 the request cache)."""
	from frappe.model.meta import Meta

	return Meta(dt)


def _stored(values: dict) -> dict:
	"""``values`` as the write stores them on a Custom Field: the tools' own value
	check (``_field_values.check_values``: "true" / "yes" / "on" are 1, "1,000" is
	1000), then Frappe's number cast. Raises what the write would raise."""
	from jarvis.tools._field_values import check_values

	checked = check_values(CF, dict(values))
	meta = _field_meta()
	out = {}
	for key, value in checked.items():
		df = meta.get_field(key) if isinstance(key, str) else None
		if df is not None and df.fieldtype in ("Check", "Int") and value is not None:
			value = cint(value)
		if key == "fetch_from" and isinstance(value, str):
			# Stored trimmed; anything else odd in it is refused by its own check.
			value = value.strip()
		if df is not None and df.fieldtype == "Select" and isinstance(value, str):
			value = value.strip()  # as the write's own Select check stores it (precision " 2")
		if key == "default" and value is not None and not isinstance(value, str):
			# The column is text. A number is kept as its text; anything else (true, a
			# list) has no one text form, and crashed Frappe's schema diff.
			if isinstance(value, bool) or not isinstance(value, int | float):
				raise InvalidFieldValueError(
					'The field\'s default must be given as text (for example "1" or "Open").'
				)
			value = str(value)
		out[key] = value
	return out


def _differs(key: str, new, stored) -> bool:
	df = _field_meta().get_field(key)
	if df is not None and df.fieldtype in ("Check", "Int"):
		return cint(new) != cint(stored)
	# Exact, not stripped: " Data " is not "Data" to the save (it would fail there).
	return str(new if new is not None else "") != str(stored if stored is not None else "")


def _unknown_keys(given: dict) -> None:
	"""A key Custom Field does not have would be shown on the card and then dropped
	by the save: the card must show exactly what is written."""
	meta = _field_meta()
	# ``name`` too: a Custom Field is named by Frappe from its form and fieldname.
	unknown = sorted(str(k) for k in given if not meta.has_field(k))
	if unknown:
		raise InvalidFieldValueError(
			f"Custom Field has no property named {', '.join(unknown)}. Leave it out or use the right name."
		)


def _allowed_fieldtypes() -> set[str]:
	options = _field_meta().get_field("fieldtype").options or ""
	return {o.strip() for o in options.split("\n") if o.strip()} - _REFUSED_FIELDTYPES


def _target(dt_given) -> str:
	"""The form a field goes on, named exactly. A name in another letter case would
	make Frappe build ``tab<that spelling>``: on a case-sensitive database that is a
	different table, which ``updatedb`` would then CREATE."""
	# Compared as given, not stripped: the save names the field row and the table
	# from the raw value.
	given = dt_given if isinstance(dt_given, str) else ""
	if not given.strip():
		raise InvalidFieldValueError("Say which form the field goes on: set dt to a DocType name.")
	dt = frappe.db.get_value("DocType", given, "name")
	if not dt:
		raise InvalidFieldValueError(f"There is no DocType named {given!r} to add a field to.")
	if dt != given:
		raise InvalidFieldValueError(f"The DocType is named {dt!r}, not {given!r}. Use its exact name.")
	return dt


def _denied_itself(dt: str) -> bool:
	from frappe.model import core_doctypes_list

	from jarvis.tools import _write_risk

	row = frappe.db.get_value("DocType", dt, ["module", "is_virtual"], as_dict=True) or {}
	return bool(
		dt in core_doctypes_list
		or cint(row.get("is_virtual"))
		or dt in _DENIED_TARGETS
		or dt in _write_risk.STRUCTURE_DOCTYPES
		or dt in _write_risk.SENSITIVE_DOCTYPES
		or row.get("module") == "Jarvis"
		or dt.startswith("Jarvis ")
	)


def _check_target(dt: str) -> None:
	"""Forms whose fields are never changed from chat, new field or edit alike: the
	denied forms themselves, and every child table of one (derived from the forms'
	own Table fields, standard and custom: User Email, Workflow Transition, Webhook
	Header ...), since a row of those is part of the denied record."""
	denied = _denied_itself(dt)
	if not denied:
		parents = set(
			frappe.get_all("DocField", {"options": dt, "fieldtype": ["in", _TABLE_TYPES]}, pluck="parent")
		) | set(frappe.get_all(CF, {"options": dt, "fieldtype": ["in", _TABLE_TYPES]}, pluck="dt"))
		denied = any(_denied_itself(parent) for parent in parents)
	if denied:
		raise StructureRefusedError(
			f"The fields of {dt} are not changed from chat: it is a core, security, audit or "
			f"Jarvis DocType (or a table of one). {_DESK_ONLY}",
			doctype=CF,
			desk_path="/app/customize-form",
		)


def _require(doctype: str, ptype: str, doc=None, what: str = "") -> None:
	if not frappe.has_permission(doctype, ptype, doc=doc):
		raise PermissionDeniedError(
			f"You do not have permission to {what}. Changing a form's fields needs permission to "
			"write Custom Fields and the DocType itself (usually the System Manager role)."
		)


def _can_read(doctype: str) -> bool:
	return bool(frappe.has_permission(doctype, "read"))


def _is_single(dt: str) -> bool:
	return bool(cint(frappe.db.get_value("DocType", dt, "issingle")))


def _diff(dt: str, meta) -> tuple[list[str], object]:
	"""The schema diff, with Frappe's own validation errors as a refusal that names
	what it objected to."""
	try:
		return gs.schema_changes(dt, meta)
	except frappe.ValidationError as e:
		raise InvalidFieldValueError(strip_html(str(e)).strip() or "The field is not valid.") from None


def _check_default(fieldname: str, value) -> None:
	"""A default with a quote or a backslash is written into the column definition
	escaped, and reads back differently: Frappe then sees the default as changed on
	every later sync, a standing MODIFY that would refuse every chat field on the
	form from then on."""
	if isinstance(value, str) and ("'" in value or "\\" in value or '"' in value):
		raise InvalidFieldValueError(
			f"The default of the field {fieldname} cannot contain a quote or a backslash from chat. "
			"Use a default without them, or set it in Customize Form in Desk."
		)


def _check_fetch_from(dt: str, meta, fieldname: str, value) -> None:
	"""``fetch_from`` copies a value off the linked record into this form on every
	save, for anyone who can see the form. Allowed only as
	``<a Link field of this form>.<an ordinary field of the linked form>`` that the
	requester may read: a fetch that does not resolve breaks every insert of the
	form (an unknown column), and one that reads a secret hands it to every reader."""
	from jarvis.chat._record_summary import is_secret

	text = str(value or "").strip()
	if not text:
		return
	if any(ch.isspace() for ch in text):
		raise InvalidFieldValueError(
			f"The fetch_from of the field {fieldname} ({text!r}) contains a space or a line break. "
			"Write it like 'customer.customer_name' with nothing else."
		)

	def refuse(why: str):
		return InvalidFieldValueError(
			f"The fetch_from of the field {fieldname} ({text!r}) {why}. Use the form "
			f"'customer.customer_name': a Link field of this form ({dt}), a dot, then a field of the "
			"linked form. Or leave fetch_from out."
		)

	link_name, _, source_name = text.partition(".")
	link = meta.get_field(link_name.strip())
	if not source_name.strip() or link is None or link.fieldtype != "Link" or not link.options:
		raise refuse(f"does not start with a Link field of {dt}")
	target = str(link.options)
	if not frappe.db.exists("DocType", target):
		raise refuse("links to a form that does not exist")
	source = frappe.get_meta(target).get_field(source_name.strip())
	if source is None:
		raise refuse(f"names no field of {target}")
	if (
		source.fieldtype in _NO_FETCH_TYPES
		or is_secret(frappe.get_meta(target), source.fieldname)
		or cint(source.permlevel) > 0
	):
		raise refuse(f"reads a field of {target} that is secret or restricted")
	if not _can_read(target):
		raise refuse(f"reads {target}, which you may not read")


def _risk_lines(values: dict, *, fieldtype: str, before: dict | None = None) -> list[str]:
	"""The lines a card shows after the structural one (D2). ``before`` is the stored
	field for an edit: a property counts when it CHANGES, in either direction
	(lowering a permlevel or un-hiding a field changes who sees what just as much);
	on a new field, when it is set."""

	from jarvis.tools._write_risk import RISK_LINES

	def changed(key: str) -> bool:
		if key not in values:
			return False
		if before is None:
			return bool(values.get(key))
		return _differs(key, values.get(key), before.get(key))

	lines = []
	if any(
		changed(k) and str(values.get(k) or "").strip().lower().startswith("eval:") for k in _EXPRESSION_KEYS
	):
		lines.append(RISK_LINES["code"])
	if any(changed(k) for k in _ACCESS_KEYS) or (fieldtype == "Link" and changed("options")):
		lines.append(RISK_LINES["access"])
	if changed("fetch_from") and str(values.get("fetch_from") or "").strip():
		lines.append(FETCH_LINE)
	return lines


def _check_hidden_mandatory(fieldname: str, field) -> None:
	"""Frappe's own rule (``validate_fields_for_doctype``), which would otherwise
	fail the save at Confirm: a hidden, mandatory field needs a default."""
	if cint(field.get("reqd")) and cint(field.get("hidden")) and not field.get("default"):
		raise InvalidFieldValueError(
			f"The field {fieldname} cannot be both hidden and mandatory without a default: nobody "
			"could fill it in. Give it a default, or leave one of the two off."
		)


def _check_fetches_through(dt: str, meta, link_fieldname: str) -> None:
	"""Every field of the form that fetches through the Link ``link_fieldname`` must
	still resolve, and still be allowed, against ``meta`` (which carries the link's
	NEW target): a fetch left pointing at a field the new target does not have breaks
	every insert of the form, and one now pointing at a secret hands it out."""
	broken = []
	for df in meta.get("fields"):
		through, _, _source = str(df.get("fetch_from") or "").strip().partition(".")
		if through.strip() != link_fieldname:
			continue
		try:
			_check_fetch_from(dt, meta, df.fieldname, df.fetch_from)
		except InvalidFieldValueError:
			broken.append(df.fieldname)
	if broken:
		raise InvalidFieldValueError(
			f"Changing what {link_fieldname} links to would break or expose the fetch of "
			f"{', '.join(broken)} on {dt} (fetch_from through this link). Change those fields first, "
			"or keep the link as it is."
		)


# --------------------------------------------------------------------------- #
# One new Custom Field
# --------------------------------------------------------------------------- #
class NewCustomField:
	risk = RISK_NEW

	def __init__(self, args: dict):
		values = args.get("values")
		self.given = dict(values) if isinstance(values, dict) else {}
		self.dt = self.given.get("dt") if isinstance(self.given.get("dt"), str) else ""

	@property
	def lock_doctype(self) -> str:
		return self.dt or CF

	def canonical_args(self) -> dict:
		"""The call as it is carded, sealed and run: the doctype by its exact name and
		the values in their stored form, so the card shows what the save writes and
		the save writes what the card showed. Called after ``check``."""
		return {"doctype": CF, "values": _stored(self.given)}

	def _probe(self):
		"""The exact document the tool will save: the stored form of the values, on a
		new Custom Field with Frappe's own defaults."""
		meta = _field_meta()
		doc = frappe.new_doc(CF)
		doc.update({k: v for k, v in _stored(self.given).items() if v is not None and meta.has_field(k)})
		return doc

	def _fieldname(self, doc) -> str:
		"""Frappe's own fieldname rule (``CustomField.set_fieldname``, custom_field.py
		15 :126, 16 :132), refused wherever it would not be what the card shows."""
		for key in ("fieldname", "label", "fieldtype"):
			if self.given.get(key) is not None and not isinstance(self.given[key], str):
				raise InvalidFieldValueError(f"The field's {key} must be text.")
		given = self.given.get("fieldname") or ""
		if not given and not str(doc.label or "").strip():
			# Frappe names an unlabelled break at random: the card could not show it.
			raise InvalidFieldValueError("Give the new field a label or a fieldname.")
		try:
			doc.set_fieldname()
		except frappe.ValidationError:
			raise InvalidFieldValueError("Give the new field a label or a fieldname.") from None
		fieldname = doc.fieldname
		if given and fieldname != given:
			raise InvalidFieldValueError(
				f"Fieldname {given!r} cannot be used as it is (Frappe would save it as {fieldname!r}). "
				"Use lowercase letters, digits and underscores, and not a reserved name."
			)
		if not _FIELDNAME.match(fieldname):
			raise InvalidFieldValueError(
				f"Fieldname {given or fieldname!r} is not valid: use lowercase letters, digits and "
				"underscores, starting with a letter. Set fieldname yourself if the label has other characters."
			)
		if len(fieldname) >= _MAX_FIELDNAME:
			raise InvalidFieldValueError(f"A fieldname must be shorter than 64 characters ({fieldname}).")
		return fieldname

	def check(self) -> frappe._dict:
		with _own_messages():
			return self._check()

	def _check(self) -> frappe._dict:
		_require(CF, "create", what="add a field to a form")
		_unknown_keys(self.given)
		dt = _target(self.given.get("dt"))
		_check_target(dt)
		_require("DocType", "write", doc=dt, what=f"change the form {dt}")
		for key in _APP_KEYS:
			if self.given.get(key):
				raise InvalidFieldValueError(
					f"A field added from chat cannot set {key}: that marks it as shipped by an app."
				)
		doc = self._probe()
		fieldtype = str(doc.fieldtype or "")  # exact: " Data " is not a type to the save
		if fieldtype not in _allowed_fieldtypes():
			raise InvalidFieldValueError(
				f"A field of type {fieldtype or '(none)'} cannot be added from chat. "
				+ (
					"That type is set up in Customize Form in Desk."
					if fieldtype in _REFUSED_FIELDTYPES
					else "Use one of Frappe's field types, such as Data, Small Text, Select, Link, Check, Date or Currency."
				)
			)
		fieldname = self._fieldname(doc)
		for key, word in (("unique", "unique"), ("search_index", "an index"), ("is_virtual", "virtual")):
			if cint(doc.get(key)):
				raise InvalidFieldValueError(
					f"The field {fieldname} cannot be made {word} from chat ({key}): that changes more "
					"than one column. Add it without that setting and tell the user it is set in "
					"Customize Form in Desk."
				)
		if fieldtype == "Link" and not frappe.db.exists("DocType", str(doc.options or "").strip()):
			raise InvalidFieldValueError(
				f"The Link field {fieldname} needs options: the exact name of an existing DocType."
			)
		_check_default(fieldname, doc.get("default"))
		_check_hidden_mandatory(fieldname, doc)
		meta = _private_meta(dt)
		_check_fetch_from(dt, meta, fieldname, doc.get("fetch_from"))
		self._check_name_is_free(dt, meta, doc, fieldname)
		single = bool(cint(meta.issingle))
		if not single:
			self._check_table(dt, meta, doc, fieldname)
		return frappe._dict(dt=dt, fieldname=fieldname, fieldtype=fieldtype, single=single)

	def _check_name_is_free(self, dt: str, meta, doc, fieldname: str) -> None:
		if fieldname in {str(df.fieldname or "").lower() for df in meta.get("fields")}:
			raise InvalidFieldValueError(
				f"{dt} already has a field named {fieldname}. Choose another fieldname."
			)
		# The real table, not ``frappe.db.has_column`` (a cache): a column left behind
		# by a deleted field or a failed change has no field row at all.
		if not cint(meta.issingle) and fieldname in gs.table_columns(dt):
			raise InvalidFieldValueError(
				f"{dt}'s table already has a column named {fieldname} (a standard column, or one left "
				"over from a field that was removed). Choose another fieldname."
			)
		if frappe.db.exists("Property Setter", {"doc_type": dt, "field_name": fieldname}):
			raise InvalidFieldValueError(
				f"{dt} has leftover customisations (Property Setters) for a field named {fieldname}; "
				"they would change what the new field's column looks like. Choose another fieldname."
			)
		from frappe.core.doctype.doctype.doctype import check_fieldname_conflicts

		try:
			check_fieldname_conflicts(doc)
		except frappe.ValidationError:
			raise InvalidFieldValueError(
				f"Fieldname {fieldname} clashes with something Frappe itself uses on a document. "
				"Choose another fieldname."
			) from None

	def _check_table(self, dt: str, meta, doc, fieldname: str) -> None:
		"""Room in the row, and a schema diff that is exactly this one column. ``meta``
		is private (``_private_meta``): the hypothetical field goes on it."""
		row = {k: v for k, v in doc.as_dict().items() if _field_meta().has_field(k)}
		row.update(fieldname=fieldname, is_custom_field=1)
		meta.append("fields", row)
		statements, table = _diff(dt, meta)
		column = table.columns.get(fieldname)
		definition = column.get_definition() if column is not None else None
		if definition and not gs.room_for(gs.table_columns(dt), definition):
			raise InvalidFieldValueError(
				f"{dt}'s table has no room for the field {fieldname} ({doc.fieldtype}): its row is at "
				"the database's size limit. A Small Text or Long Text field still fits; otherwise the "
				"user frees up fields in Desk first."
			)
		expected = (
			[" ".join(f"ALTER TABLE `{table.table_name}` ADD COLUMN `{fieldname}` {definition}".split())]
			if definition
			else []
		)
		if statements != expected:
			raise StructureRefusedError(
				f"{dt}'s table has other structure changes waiting besides this field, so a field "
				f"cannot be added to it from chat. {_DESK_ONLY} An administrator brings the table in "
				"line first (bench migrate applies pending structure changes).",
				doctype=CF,
				desk_path="/app/customize-form",
			)

	def risk_lines(self) -> list[str]:
		"""What the card says after the structural line. Called after ``check``."""
		try:
			values = _stored(self.given)
		except Exception:
			values = self.given
		return _risk_lines(values, fieldtype=str(values.get("fieldtype") or "Data"))

	def snapshot(self) -> dict:
		"""What the clean-up needs. Never raises: a call that can no longer be
		resolved fails its own check a moment later."""
		try:
			with _own_messages():
				fieldname = self._fieldname(self._probe())
		except Exception:
			fieldname = ""
		return {"risk": RISK_NEW, "dt": self.dt, "fieldname": fieldname}

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Remove the half-made field this confirmation wrote. ``undo["written"]`` is
		the row's name and timestamp, stamped in the save's own transaction: without
		it nothing of this confirmation committed, and only that exact row is its."""
		dt, fieldname = str(undo.get("dt") or ""), str(undo.get("fieldname") or "")
		written = undo.get("written") if isinstance(undo.get("written"), dict) else None
		if not dt or not fieldname or not written:
			return CleanUp("Nothing was left behind.")
		rows = frappe.db.sql(
			"SELECT name, fieldtype, is_virtual, creation FROM `tabCustom Field` WHERE name=%(n)s AND dt=%(dt)s",
			{"n": written.get("name"), "dt": dt},
			as_dict=True,
		)
		if not rows:
			frappe.clear_cache(doctype=dt)
			return CleanUp(f"Nothing was left behind on {dt}.")
		row = rows[0]
		if str(get_datetime(row.creation)) != str(get_datetime(written.get("modified"))):
			return CleanUp(
				f"A field named {fieldname} exists on {dt}, but this confirmation did not make it; "
				"it was left untouched."
			)
		from frappe.model import no_value_fields

		needs_column = (
			row.fieldtype not in no_value_fields and not cint(row.is_virtual) and not _is_single(dt)
		)
		if needs_column and fieldname.lower() not in gs.table_columns(dt):
			# Raw: the row has no column, so its own delete hooks have nothing to
			# tidy, and nothing can have been stored in it.
			frappe.db.delete(CF, {"name": row.name})
			frappe.db.delete("Property Setter", {"doc_type": dt, "field_name": fieldname})
			frappe.clear_cache(doctype=dt)
			return CleanUp(
				f"The half-made field {fieldname} was removed from {dt}: its column was never added, "
				"so nothing was left behind.",
				changed=True,
			)
		frappe.clear_cache(doctype=dt)
		return CleanUp(
			f"The field {fieldname} was added to {dt} before the failure and was left in place; "
			"check it in Desk.",
			clean=False,
		)


# --------------------------------------------------------------------------- #
# A column-free edit
# --------------------------------------------------------------------------- #
class CustomFieldEdit:
	risk = RISK_EDIT

	def __init__(self, args: dict):
		self.name = args.get("name") if isinstance(args.get("name"), str) else ""
		changes = args.get("changes")
		self.given = dict(changes) if isinstance(changes, dict) else {}
		self._dt = None

	@property
	def lock_doctype(self) -> str:
		if self._dt is None:
			self._dt = frappe.db.get_value(CF, self.name, "dt") or CF
		return self._dt

	def _row(self) -> frappe._dict:
		row = frappe.db.get_value(CF, self.name, "*", as_dict=True) if self.name else None
		if not row:
			raise InvalidFieldValueError(
				f"There is no Custom Field named {self.name!r}. A custom field is named "
				"its form, a hyphen and its fieldname (like 'Customer-custom_region'); a standard field "
				"is changed in Customize Form in Desk."
			)
		if row.name != self.name:
			raise InvalidFieldValueError(f"The Custom Field is named {row.name!r}, not {self.name!r}.")
		return row

	def canonical_args(self) -> dict:
		"""As ``NewCustomField.canonical_args``: exact doctype, stored changes."""
		return {"doctype": CF, "name": self.name, "changes": _stored(self.given)}

	def _refuse(self, why: str) -> StructureRefusedError:
		from jarvis.tools._write_risk import desk_path

		return StructureRefusedError(f"{why} {_DESK_ONLY}", doctype=CF, desk_path=desk_path(CF, self.name))

	def check(self) -> frappe._dict:
		with _own_messages():
			return self._check()

	def _check(self) -> frappe._dict:
		_require(CF, "write", what="change a form's field")
		row = self._row()
		if not self.given:
			raise InvalidFieldValueError("Say what to change on the field: changes is empty.")
		_unknown_keys(self.given)
		_check_target(row.dt)
		_require("DocType", "write", doc=row.dt, what=f"change the form {row.dt}")
		changes = _stored(self.given)
		label = f"{row.fieldname} on {row.dt}"
		if cint(row.is_system_generated):
			raise self._refuse(
				f"The field {label} was added by an app, which sets it again on every update, so it is "
				"changed through Customize Form in Desk, not from chat."
			)
		if cint(row.is_virtual) or (row.fieldtype in _REFUSED_FIELDTYPES and "options" in changes):
			raise self._refuse(
				f"The field {label} is drawn or computed from its own options, so it is changed in Desk, "
				"not from chat."
			)
		touched = [k for k in _COLUMN_KEYS if k in changes and _differs(k, changes[k], row.get(k))]
		if touched:
			raise self._refuse(
				f"Changing {', '.join(touched)} of the field {label} changes its database column, so it "
				"is done in Desk, not from chat."
			)
		for key in _APP_KEYS:
			if key in changes and _differs(key, changes[key], row.get(key)):
				raise InvalidFieldValueError(
					f"The {key} of the field {label} is not changed from chat: it marks a field as "
					"shipped by an app."
				)
		if (
			row.fieldtype == "Link"
			and "options" in changes
			and not frappe.db.exists("DocType", str(changes["options"] or "").strip())
		):
			raise InvalidFieldValueError(
				f"The Link field {label} needs options: the exact name of an existing DocType."
			)
		if "default" in changes:
			_check_default(row.fieldname, changes["default"])
		_check_hidden_mandatory(row.fieldname, {**row, **changes})
		# The form as it would read with this edit, on a private copy: custom fields,
		# standard fields and Property Setters together, as Frappe builds it.
		meta = _private_meta(row.dt)
		df = next(
			(f for f in meta.get("fields") if f.fieldname == row.fieldname and f.get("is_custom_field")),
			None,
		)
		if df is None:
			raise self._refuse(f"The field {label} is not part of its form as Frappe reads it.")
		df.update({k: v for k, v in changes.items() if _field_meta().has_field(k)})
		if "fetch_from" in changes:
			_check_fetch_from(row.dt, meta, row.fieldname, changes["fetch_from"])
		if (
			row.fieldtype == "Link"
			and "options" in changes
			and _differs("options", changes["options"], row.options)
		):
			_check_fetches_through(row.dt, meta, row.fieldname)
		if not cint(meta.issingle):
			statements, _table = _diff(row.dt, meta)
			if statements:
				raise self._refuse(
					f"This edit changes the column of the field {label}, or its table has other "
					"structure changes waiting, so it is done in Desk, not from chat."
				)
		return row

	def risk_lines(self) -> list[str]:
		try:
			changes = _stored(self.given)
		except Exception:
			changes = self.given
		before = (frappe.db.get_value(CF, self.name, "*", as_dict=True) if self.name else None) or {}
		return _risk_lines(changes, fieldtype=str(before.get("fieldtype") or ""), before=before)

	def snapshot(self) -> dict:
		"""The field row as it is now: what a failed confirm is restored to."""
		row = frappe.db.get_value(CF, self.name, "*", as_dict=True) if self.name else None
		before = frappe.parse_json(frappe.as_json(row)) if row else None
		return {
			"risk": RISK_EDIT,
			"name": self.name,
			"dt": (row or {}).get("dt") or "",
			"fieldname": (row or {}).get("fieldname") or "",
			"before": before,
		}

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Put the field back as it was. Only when the row still carries the exact
		timestamp this confirmation wrote it with (``undo["written"]``): a later edit,
		in Desk or by anyone, the same person included, is never overwritten."""
		name, before = str(undo.get("name") or ""), undo.get("before")
		dt, fieldname = str(undo.get("dt") or ""), str(undo.get("fieldname") or "")
		written = undo.get("written") if isinstance(undo.get("written"), dict) else None
		if not name or not isinstance(before, dict) or not written:
			if dt:
				frappe.clear_cache(doctype=dt)
			return CleanUp(
				f"The field {fieldname} on {dt} was not changed." if dt else "Nothing was changed."
			)
		modified = frappe.db.get_value(CF, name, "modified")
		if not modified:
			return CleanUp(f"The field {fieldname} on {dt} no longer exists; nothing was restored.")
		if str(get_datetime(modified)) == str(get_datetime(before.get("modified"))):
			frappe.clear_cache(doctype=dt)
			return CleanUp(f"The field {fieldname} on {dt} was not changed.")
		if str(get_datetime(modified)) != str(get_datetime(written.get("modified"))):
			return CleanUp(
				f"The field {fieldname} on {dt} was changed again in the meantime; it was left as it is.",
				clean=False,
			)
		columns = gs.table_columns(CF)
		values = {k: v for k, v in before.items() if k != "name" and k.lower() in columns}
		assignments = ", ".join(f"`{k}`=%({k})s" for k in values)
		frappe.db.sql(
			f"UPDATE `tabCustom Field` SET {assignments} WHERE name=%(__name)s", {**values, "__name": name}
		)
		frappe.clear_cache(doctype=dt)
		return CleanUp(f"The change to the field {fieldname} on {dt} was undone.", changed=True)
