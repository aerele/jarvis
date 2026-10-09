"""The guarded Workflow writes (owner decision R2-10; plan rev 4 item 4, rev 3 items
2-3): create ONE Workflow, or update one, each only through a confirmation card
with no trial run. The lock, the schema diff and the wiring into park / confirm /
reconcile are shared (``_guarded_structure``); this module holds what is particular
to a Workflow: what its save does, the park checks, the card's lines, the clean-up.

What saving a Workflow does (frappe/workflow/doctype/workflow/workflow.py; the two
versions differ only in how the same statements are written):

- ``validate`` -> ``set_active`` (Frappe 15 :114-121, 16 :116-123): when the
  workflow is active, EVERY workflow of that form is set inactive by one UPDATE,
  then this one is saved active. So activating replaces the active workflow.
- ``on_update`` -> ``create_custom_field_for_workflow_state`` (15 :45-68,
  16 :46-69): when the form has no field named ``workflow_state_field``, a hidden
  Link field is inserted as a Custom Field. That insert runs
  ``frappe.db.updatedb``, which COMMITS (the workflow, its rows and the
  deactivation of the others with it) and only then runs ``ALTER TABLE``. A failed
  ALTER therefore leaves a live workflow whose state field has no column: every
  list and save of the form fails. When the field already exists nothing commits
  and nothing is altered: the save is one ordinary transaction.
- ``on_update`` -> ``update_default_workflow_status`` (15 :70-85, 16 :71-87): for
  the first state of each document status, every record of that status whose state
  is empty gets that state, in one UPDATE per status, whether or not the workflow
  is active. Records that carry some other state keep it
  (``get_workflow_state_count``, 15 :124-136, 16 :126-138, is how Desk counts them,
  and how the card does).

What a Workflow can CARRY, all of it shown on the card and said in its lines:

- ``condition`` on a transition is a Python expression Frappe evaluates on the
  server for every user who opens or moves a record (frappe/model/workflow.py
  ``is_transition_condition_satisfied``, 15 :91-95, 16 :97-101:
  ``frappe.safe_eval`` with ``frappe.db.get_value`` / ``get_list``, the session
  and a few date helpers; RestrictedPython, no builtins: utils/safe_exec.py
  ``safe_eval`` 15 :132-149, 16 :136-149). It is code: the card says "This runs
  code for every user." and shows it whole, and one that does not compile is
  refused (it would break the form for everyone). Frappe 16's
  ``evaluate_as_expression`` on a state's ``update_value`` is the same thing
  (workflow.py :104-116).
- A transition's ``transition_tasks`` (Frappe 16) runs Server Scripts and Webhooks
  (workflow.py :153-204): not set from chat.
- ``allow_self_approval`` (Frappe's default is ON; from chat it is off unless
  asked for), the approving role, ``update_field`` /
  ``update_value`` (written to the record on every move into the state),
  ``doc_status`` (a state that submits or cancels), ``send_email_alert``.

The rules the first review of the Custom Field unit paid for hold here too: the
card, the sealed call and the write are ONE stored form (``canonical_args``: every
row complete, defaults spelled out, names exact); nothing a park does is readable
by another request (no cached object is touched; the state field's schema diff
runs on a private copy, ``_custom_field_guard.check_generated_fields``); and the
clean-up knows what this confirmation wrote from the stamp on its row.

When a confirmed Workflow write does not end as done (it failed, or its worker
died), what it wrote is put back: a new workflow is removed, an edited one is
restored to its before-image, the workflow that was active is made active again,
the state field is removed when its column was never added, and the states the
save filled in are emptied again. A reported failure never leaves a workflow live.
"""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass, field

import frappe
from frappe.utils import cint, cstr, get_datetime, strip_html

from jarvis.exceptions import (
	InvalidFieldValueError,
	JarvisError,
	PermissionDeniedError,
	StructureRefusedError,
)
from jarvis.tools import _custom_field_guard as cfg
from jarvis.tools import _guarded_structure as gs
from jarvis.tools._guarded_structure import CleanUp

WF = "Workflow"
RISK_NEW = "workflow_new"
RISK_EDIT = "workflow_edit"
TABLES = ("states", "transitions")
# The Workflow Builder's own drawing of the workflow: not written from chat.
_BUILDER_KEYS = ("workflow_data",)
# What the state field may be when the form already has one by that name.
_STATE_FIELDTYPES = ("Link", "Data")
# Filling the state on more records than this cannot be snapshotted for an undo.
FILL_NAMES_MAX = 5000
# The card counts records left in a state the workflow lacks up to here, then says
# "more than".
KEPT_COUNT_MAX = 10000
_ROW_SYSTEM_KEYS = frozenset(
	{
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"doctype",
		"docstatus",
		"owner",
		"creation",
		"modified",
		"modified_by",
	}
)
_DESK_ONLY = "Do not retry it with another tool; tell the user where to do it."
# What a Data / Link / Select column holds (Frappe's default varchar).
_MAX_TEXT = 140
# What marks a transition once it runs tasks: changing any of these changes when
# and for whom its scripts and webhooks run.
_TASK_KEYS = ("state", "action", "next_state", "allowed", "condition", "transition_tasks")
_LINKS = {
	"states": (
		("state", "Workflow State"),
		("allow_edit", "Role"),
		("next_action_email_template", "Email Template"),
	),
	"transitions": (
		("state", "Workflow State"),
		("action", "Workflow Action Master"),
		("next_state", "Workflow State"),
		("allowed", "Role"),
	),
}
_MANDATORY = {"states": ("state", "allow_edit"), "transitions": ("state", "action", "next_state", "allowed")}


def _meta():
	return frappe.get_meta(WF)


def _child_meta(table: str):
	return frappe.get_meta(_meta().get_field(table).options)


def _row_keys(table: str) -> list[str]:
	"""The value fields of a state / transition row on THIS Frappe version (Frappe
	16 has more: ``evaluate_as_expression``, ``transition_tasks`` ...)."""
	from frappe.model import no_value_fields

	return [df.fieldname for df in _child_meta(table).fields if df.fieldtype not in no_value_fields]


def _refuse(why: str, name: str | None = None) -> StructureRefusedError:
	from jarvis.tools._write_risk import desk_path

	return StructureRefusedError(f"{why} {_DESK_ONLY}", doctype=WF, desk_path=desk_path(WF, name or None))


def _exact(doctype: str, value, what: str) -> str:
	"""``value`` as the exact name of an existing ``doctype`` record. Frappe's own
	link check matches without regard to letter case or trailing spaces and then
	stores the name as typed; the card must show the record that is meant."""
	if not isinstance(value, str) or not value.strip():
		raise InvalidFieldValueError(f"{what} is missing: give the name of a {doctype}.")
	found = frappe.db.get_value(doctype, value, "name")
	if not found:
		hint = (
			" Create it first with create_doc (it is an ordinary record), or use an existing one."
			if doctype in ("Workflow State", "Workflow Action Master")
			else ""
		)
		raise InvalidFieldValueError(f"{what}: there is no {doctype} named {value!r}.{hint}")
	if found != value:
		raise InvalidFieldValueError(
			f"{what}: the {doctype} is named {found!r}, not {value!r}. Use its exact name."
		)
	return found


_MADE_FIRST = {"Workflow State": "state", "Workflow Action Master": "action"}


def _all_exist(doc: dict) -> None:
	"""Every state and action the workflow names that does not exist yet, in one
	refusal: a request with three new states is one round trip, not three."""
	missing: dict[str, list[str]] = {}
	for table in TABLES:
		for row in doc[table]:
			for key, target in _LINKS[table]:
				value = row.get(key)
				if target not in _MADE_FIRST or not isinstance(value, str) or not value.strip():
					continue
				if value not in missing.get(target, ()) and not frappe.db.get_value(target, value, "name"):
					missing.setdefault(target, []).append(value)
	if not missing:
		return
	parts = []
	for target, names in missing.items():
		word = _MADE_FIRST[target]
		parts.append(f"{word if len(names) == 1 else word + 's'} {', '.join(repr(n) for n in names)}")
	raise InvalidFieldValueError(
		f"The workflow names what does not exist yet: {'; '.join(parts)}. Each is an ordinary record "
		f"({' / '.join(missing)}): create them first, or use existing ones, then send the workflow again."
	)


def _scalar(df, value, where: str):
	"""One value as the save stores it: a Check as 0 / 1, a document status as the
	text "0" / "1" / "2" (how Frappe stores it), other text trimmed. Anything that is not one plain value is refused."""
	label = f"{where}{df.label or df.fieldname}"
	if df.fieldtype == "Check":
		return cint(value)
	if value is None:
		return None
	if isinstance(value, bool) or not isinstance(value, str | int | float):
		raise InvalidFieldValueError(f"{label} must be one plain value.")
	if df.fieldname == "doc_status":
		text = cstr(value).strip()
		if text in ("0.0", "1.0", "2.0"):
			text = text[0]
		if text not in ("0", "1", "2"):
			raise InvalidFieldValueError(
				f"{label} must be 0 (draft), 1 (submitted) or 2 (cancelled); got {cstr(value)!r}."
			)
		return text
	if df.fieldtype in ("Text", "Small Text", "Long Text", "Code"):
		return (cstr(value) if df.fieldtype != "Code" else cstr(value).strip()) or None
	text = cstr(value).strip()
	if len(text) > (cint(df.length) or _MAX_TEXT):
		raise InvalidFieldValueError(
			f"{label} is longer than the {cint(df.length) or _MAX_TEXT} characters it can hold."
		)
	return text or None


def _default(df):
	if df.fieldtype == "Check":
		return cint(df.default)
	return _scalar(df, df.default, "") if df.default not in (None, "") else None


def _checked(values: dict) -> dict:
	"""The write tools' own value check (words for a Check, a Select's options, the
	rows of a table): what the save would refuse is refused here, the same way."""
	from jarvis.tools._field_values import check_values

	return check_values(WF, dict(values))


def _parent_values(given: dict, *, edit: bool) -> dict:
	"""The workflow's own values (no tables) in stored form."""
	meta = _meta()
	unknown = sorted(str(k) for k in given if not meta.has_field(k))
	if unknown:
		raise InvalidFieldValueError(
			f"Workflow has no property named {', '.join(unknown)}. Leave it out or use the right name."
		)
	for key in _BUILDER_KEYS:
		if given.get(key) not in (None, "", {}, []):
			raise InvalidFieldValueError(
				f"{key} is the Workflow Builder's own drawing and is not written from chat. Leave it out."
			)
	plain = {k: v for k, v in given.items() if k not in TABLES and k not in _BUILDER_KEYS}
	out = {}
	for key, value in _checked(plain).items():
		df = meta.get_field(key)
		if df.fieldtype in ("Section Break", "Column Break", "HTML", "Table", "Table MultiSelect"):
			raise InvalidFieldValueError(f"Workflow's {key} holds no value. Leave it out.")
		out[key] = _scalar(df, value, "")
	if not edit:
		# Frappe's defaults, spelled out: the card shows whether it is active.
		for key in ("is_active", "workflow_state_field", "override_status", "send_email_alert"):
			if out.get(key) is None:
				out[key] = _default(meta.get_field(key))
	return out


def _rows(table: str, given, stored: list[dict] | None) -> list[dict]:
	"""The complete rows of ``table`` as the save stores them: every value field of
	the row, the stored value where an edit leaves one out and Frappe's default on a
	new row (but self-approval off on a new transition, R2-14), so the card's table shows each state and transition whole (approving
	role, self-approval, condition ...). ``stored``: the rows of the workflow being
	edited, kept by their ``name`` exactly as ``update_doc`` merges them."""
	child = _child_meta(table)
	label = _meta().get_field(table).label or table
	keys = _row_keys(table)
	stored_by_name = {r["name"]: r for r in stored or []}
	if given is None:
		given = [{"name": r["name"]} for r in stored or []]
	if not isinstance(given, list) or not all(isinstance(r, dict) for r in given):
		raise InvalidFieldValueError(f"{label} must be a list of rows.")
	given = _checked({table: given})[table]
	names = [r["name"] for r in given if r.get("name")]
	if any(not isinstance(n, str) for n in names):
		raise InvalidFieldValueError(f"{label}: row names must be text, as get_doc returns them.")
	missing = [n for n in names if n not in stored_by_name]
	if missing:
		raise InvalidFieldValueError(
			f"{label} has no row named {', '.join(missing)} on this workflow. Send a kept row with its "
			"name from get_doc and a new row without one."
		)
	if len(set(names)) != len(names):
		raise InvalidFieldValueError(f"{label} lists a row more than once.")
	if stored_by_name and given and not names:
		raise InvalidFieldValueError(
			f"{label}: this would replace the {len(stored_by_name)} saved rows without saying which is "
			"which. Send each kept or changed row with its name from get_doc; a row without a name "
			"is added, a row left out is removed."
		)
	out = []
	for i, row in enumerate(given, 1):
		where = f"{label} row {i}: "
		unknown = sorted(
			str(k)
			for k in row
			if k != "name" and k not in _ROW_SYSTEM_KEYS and not str(k).startswith("__") and k not in keys
		)
		if unknown:
			raise InvalidFieldValueError(
				f"{where}there is no property named {', '.join(unknown)}. Leave it out or use the right name."
			)
		base = stored_by_name.get(row.get("name"))
		values = {}
		for key in keys:
			df = child.get_field(key)
			if key in row:
				values[key] = _scalar(df, row[key], where)
			else:
				values[key] = _scalar(df, base.get(key), where) if base is not None else _default(df)
				if base is None and (table, key) == ("transitions", "allow_self_approval"):
					# Owner decision R2-14: a transition written from chat does not let a
					# person approve their own record unless the request says so (Frappe's
					# default is 1). A stored row keeps what it has.
					values[key] = 0
		out.append({"name": row["name"], **values} if row.get("name") else values)
	return out


# What a condition written from chat may be made of (controller default; the owner
# may relax it). Frappe hands the expression ``frappe.db.get_value`` / ``get_list``
# and the session beside the document, and evaluates it whenever a user opens or
# moves a record; from chat it only compares this document's own fields.
_EXPRESSION_MAX = 500
_LIST_MAX = 100  # items in a literal list on the right of ``in``
_NUMBER_MAX = 10**12
_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
_COMPARISONS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot)


class _NotAllowed(Exception):
	"""Why an expression is not one chat may write (the message is the reason)."""


def expression_problem(
	expression, doctype: str | None = None, *, value: bool = False, state_field: str = "workflow_state"
) -> str | None:
	"""Why ``expression`` is not one a workflow set up from chat may carry, or None.

	A condition is a comparison, or ``and`` / ``or`` / ``not`` of comparisons. What
	is compared: this document's own fields (``doc.field``, ``doc["field"]``,
	``doc.get("field")`` / ``doc.get("field", default)``, each a real field of
	``doctype``), text, numbers, True / False / None, and arithmetic (``+ - * / //
	%``) on numbers and number fields. ``in`` / ``not in`` take a literal list of at
	most a hundred such values on the right. Everything else is refused: any other
	name (``frappe``), call or attribute, arithmetic on text or on a list
	(formatting, repetition), a huge number, a lambda, a comprehension, an f-string,
	a conditional, a hidden or look-alike character, more than 500 characters.

	Refused too, because Frappe evaluates a condition with nothing to catch an
	error and it would reach every user who opens or moves a record: ordering
	(``< > <= >=``) anything but two numbers (a date field against text, text
	against text when the field is empty), and dividing by anything but a plain
	number that is not zero. A date rule is set up in Desk.

	``value``: the expression gives a value instead of a yes / no (Frappe 16: a
	state's update value marked as an expression), so its top level may also be a
	field, a literal or arithmetic."""
	if not isinstance(expression, str) or not expression.strip():
		return "it is empty"
	if len(expression) > _EXPRESSION_MAX:
		return f"it is longer than {_EXPRESSION_MAX} characters"
	if unicodedata.normalize("NFKC", expression) != expression or any(
		not ch.isprintable() or unicodedata.category(ch) == "Cf" for ch in expression
	):
		return "it contains a hidden or look-alike character"
	try:
		tree = ast.parse(expression, mode="eval")
	except (SyntaxError, ValueError, RecursionError):
		return "it is not one Python expression"
	# The state field too: a workflow that is about to add it may already name it.
	fields = {state_field: TEXT, **_fields_of(doctype)} if doctype else None
	try:
		if value and not _is_condition(tree.body):
			_operand(tree.body, fields, text=True)
		else:
			_condition(tree.body, fields)
	except _NotAllowed as e:
		return str(e).replace("this form", doctype) if doctype else str(e)
	return None


# The column types Frappe never leaves empty (database/schema.py ``NOT_NULL_TYPES``)
# and hands to a condition as a number (``as_dict`` casts exactly these). A
# Duration, a Long Int and a Rating can be NULL, and None cannot be ordered or
# added to: they count as text here.
_NUMERIC_TYPES = frozenset({"Int", "Float", "Currency", "Percent", "Check"})
_DATE_TYPES = frozenset({"Date", "Datetime", "Time"})
# What a value is, for the two things Python refuses at run time: ordering (< > <=
# >=) anything but two numbers, and arithmetic on anything but numbers.
NUMBER, DATE, TEXT, EMPTY = "number", "date", "text", "empty"
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _fields_of(doctype: str) -> dict[str, str]:
	"""Every name a document of ``doctype`` carries a value under (its fields, custom
	ones among them, and Frappe's own), and what that value is: a number, a date or
	time, or text. Only a number may stand in arithmetic (text times a number is
	repetition), and only numbers may be ordered: a column of ``_NUMERIC_TYPES`` is
	never empty, while a date, a text field and the other number-like types can be,
	and Frappe hands a date over as a date, so ordering any of them raises for
	every user who opens the record."""
	from frappe.model import default_fields, no_value_fields

	meta = frappe.get_meta(doctype)
	fields = dict.fromkeys(default_fields, TEXT)
	fields.update(docstatus=NUMBER, idx=NUMBER, creation=DATE, modified=DATE)
	for df in meta.fields:
		if df.fieldtype not in no_value_fields:
			fields[df.fieldname] = (
				NUMBER if df.fieldtype in _NUMERIC_TYPES else DATE if df.fieldtype in _DATE_TYPES else TEXT
			)
	return fields


def _is_condition(node) -> bool:
	return isinstance(node, ast.Compare | ast.BoolOp) or (
		isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
	)


def _condition(node, fields) -> None:
	"""A comparison, or ``and`` / ``or`` / ``not`` of conditions."""
	if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
		for part in node.values:
			_condition(part, fields)
		return
	if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
		_condition(node.operand, fields)
		return
	if not isinstance(node, ast.Compare):
		raise _NotAllowed(
			"it is not a comparison (such as doc.status == 'Open'), nor and / or / not of comparisons"
		)
	left = _operand(node.left, fields, text=True)
	for op, right in zip(node.ops, node.comparators, strict=True):
		if isinstance(op, ast.In | ast.NotIn):
			_literal_list(right)
			left = TEXT  # a list is never ordered in a chained comparison
			continue
		if not isinstance(op, _COMPARISONS):
			raise _NotAllowed("it uses a comparison that is not allowed")
		kind = _operand(right, fields, text=True)
		if isinstance(op, _ORDERING):
			_orderable(left, kind)
		left = kind


def _orderable(left: str, right: str) -> None:
	"""``<`` ``>`` ``<=`` ``>=`` only between two numbers: Python raises on a date
	against text, on text against a number, and on an empty value against anything,
	and Frappe evaluates a condition with nothing to catch that."""
	if left == NUMBER and right == NUMBER:
		return
	if DATE in (left, right):
		raise _NotAllowed(
			"it orders a date or time field with a less-than or greater-than comparison, which fails "
			"when a record is opened (the field can be empty, and a date is not compared with text); "
			"a date rule is set up in the Workflow form in Desk"
		)
	# No angle brackets in a refusal: the tool layer strips what looks like a tag.
	raise _NotAllowed(
		"it orders something other than two numbers with a less-than or greater-than comparison, "
		"which fails when a record is opened; use == or != for text, or an Int, Float, Currency "
		"or Percent field"
	)


def _literal_list(node) -> None:
	"""The right side of ``in`` / ``not in``: a short list of plain values."""
	if not isinstance(node, ast.List | ast.Tuple | ast.Set):
		raise _NotAllowed("the right side of in / not in is not a literal list such as ('Open', 'Held')")
	if len(node.elts) > _LIST_MAX:
		raise _NotAllowed(f"a list in it has more than {_LIST_MAX} values")
	for item in node.elts:
		if not isinstance(item, ast.Constant):
			raise _NotAllowed("a list in it holds something other than plain values")
		_literal(item, text=True)


def _literal(node, *, text: bool) -> str:
	v = node.value
	if v is None:
		if not text:
			raise _NotAllowed("it does arithmetic on None")
		return EMPTY
	if isinstance(v, bool):
		return NUMBER
	if isinstance(v, str):
		if not text:
			raise _NotAllowed("it does arithmetic on text")
		return TEXT
	if isinstance(v, int | float) and abs(v) <= _NUMBER_MAX:  # nan and inf fail the comparison
		return NUMBER
	raise _NotAllowed("it holds a value that is not text, a reasonable number, True, False or None")


def _operand(node, fields, *, text: bool) -> str:
	"""One side of a comparison: a field of the document, a plain value, or
	arithmetic on numbers and fields. ``text``: a text literal may stand here (never
	inside arithmetic, where it would format or repeat). Returns what the value is
	(``NUMBER`` / ``DATE`` / ``TEXT`` / ``EMPTY``)."""
	if isinstance(node, ast.Constant):
		return _literal(node, text=text)
	kind = _field_read(node, fields, numeric=not text)
	if kind:
		return kind
	if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
		_operand(node.operand, fields, text=False)
		return NUMBER
	if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITHMETIC):
		_operand(node.left, fields, text=False)
		_operand(node.right, fields, text=False)
		if isinstance(node.op, ast.Div | ast.FloorDiv | ast.Mod) and not _nonzero_number(node.right):
			raise _NotAllowed(
				"it divides by something other than a plain number that is not zero (a field can "
				"hold 0, and the division then fails when a record is opened)"
			)
		return NUMBER
	if isinstance(node, ast.Name):
		raise _NotAllowed(f"it uses the name {node.id}, and only doc may be used")
	if isinstance(node, ast.Call):
		raise _NotAllowed("it calls something other than doc.get('fieldname')")
	if isinstance(node, ast.Attribute | ast.Subscript):
		raise _NotAllowed("it reaches beyond a field of doc")
	raise _NotAllowed("it uses more than comparisons of doc's fields")


def _nonzero_number(node) -> bool:
	"""A divisor chat may write: a number literal other than zero, signed or not."""
	if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
		node = node.operand
	if not isinstance(node, ast.Constant):
		return False
	v = node.value
	return isinstance(v, int | float) and not isinstance(v, bool) and v != 0


def _field_read(node, fields, *, numeric: bool = False) -> str | None:
	"""What the one field of the document ``node`` reads is (``doc.x``, ``doc["x"]``,
	``doc.get("x")`` or ``doc.get("x", plain default)``), or None when ``node`` is
	not such a read. Raises when it reads a name the form does not have, or
	(``numeric``: inside arithmetic) a field that does not hold a number. A
	``doc.get`` whose default is not what the field holds is text: it is never
	ordered."""

	def is_doc(n) -> bool:
		return isinstance(n, ast.Name) and n.id == "doc"

	name, default = None, None
	if isinstance(node, ast.Attribute) and is_doc(node.value):
		name = node.attr
	elif isinstance(node, ast.Subscript) and is_doc(node.value):
		key = node.slice
		name = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
		if name is None:
			raise _NotAllowed("it reads doc by something other than a fieldname in quotes")
	elif isinstance(node, ast.Call):
		get = node.func
		if not (isinstance(get, ast.Attribute) and get.attr == "get" and is_doc(get.value)):
			return None
		plain = (
			not node.keywords
			and 1 <= len(node.args) <= 2
			and all(isinstance(a, ast.Constant) for a in node.args)
			and isinstance(node.args[0].value, str)
		)
		if not plain:
			raise _NotAllowed("it calls doc.get with something other than a fieldname and a plain default")
		for given in node.args[1:]:
			default = _literal(given, text=True)
		name = node.args[0].value
	if name is None:
		return None
	if name.startswith("_") or (fields is not None and name not in fields):
		raise _NotAllowed(f"{name} is not a field of this form")
	# With no form to read (``fields`` None) nothing is known to be a number.
	kind = fields[name] if fields is not None else TEXT
	if numeric and fields is not None and kind != NUMBER:
		raise _NotAllowed(f"it does arithmetic on {name}, which is not a number field")
	if default is not None and default != kind:
		if numeric:
			raise _NotAllowed(f"it does arithmetic on {name} with a default that is not a number")
		return TEXT
	return kind


def _role_lines(states: list[dict], transitions: list[dict]) -> list[str]:
	"""Said by name on the card: a state or an action open to everyone signed in,
	and one only the Administrator can use."""
	lines = []
	for role, who in (("All", "Anyone signed in"), ("Administrator", "Only the Administrator")):
		edits = [s["state"] for s in states if s.get("allow_edit") == role]
		if edits:
			lines.append(f"{who} can change records in {', '.join(edits)}.")
		for t in transitions:
			if t.get("allowed") == role:
				lines.append(f"{who} can take {t['action']} on records in {t['state']}.")
	return lines


def _compiles(expression: str) -> str | None:
	"""Why ``expression`` cannot be evaluated by ``frappe.safe_eval``, or None. Run
	through Frappe's own gates without evaluating anything: the syntax check and the
	restricted compile (utils/safe_exec.py)."""
	from frappe.utils import safe_exec

	try:
		code = unicodedata.normalize("NFKC", expression)
		safe_exec._validate_safe_eval_syntax(code)
		safe_exec.compile_restricted(
			code, filename="<workflow>", policy=safe_exec.FrappeTransformer, mode="eval"
		)
	except Exception as e:
		return strip_html(str(e)).strip() or type(e).__name__
	return None


def _state_field_row(fieldname: str) -> dict:
	"""The Custom Field Frappe inserts for a missing state field (workflow.py
	``create_custom_field_for_workflow_state``)."""
	return {
		"fieldname": fieldname,
		"label": fieldname.replace("_", " ").title(),
		"hidden": 1,
		"allow_on_submit": 1,
		"no_copy": 1,
		"fieldtype": "Link",
		"options": "Workflow State",
	}


def _other_active(dt: str, name: str | None) -> list[str]:
	"""The workflows of ``dt`` that are active now, beside ``name``."""
	rows = frappe.get_all(WF, filters={"document_type": dt, "is_active": 1}, pluck="name", order_by="name")
	return [n for n in rows if n != name]


def _fill_states(states: list[dict]) -> dict[int, str]:
	"""Document status -> the state Frappe fills empty records of that status with:
	the first state listed for it (``update_default_workflow_status``)."""
	first: dict[int, str] = {}
	for row in states:
		status = cint(row.get("doc_status"))
		if status not in first and row.get("state"):
			first[status] = row["state"]
	return first


def _empty_state(table, fieldname: str):
	return table[fieldname].isnull() | (table[fieldname] == "")


def _count_fill(dt: str, fieldname: str, statuses: list[int], has_column: bool) -> int:
	"""How many records the save fills a state on, counted no further than one past
	the most chat will fill (``FILL_NAMES_MAX``): a form of millions of rows is
	never counted whole inside a request."""
	if not statuses:
		return 0
	table = frappe.qb.DocType(dt)
	query = frappe.qb.from_(table).select(table.name).where(table.docstatus.isin(statuses))
	if has_column:
		query = query.where(_empty_state(table, fieldname))
	return len(query.limit(FILL_NAMES_MAX + 1).run())


def _count_kept(dt: str, fieldname: str, states: list[str]) -> int:
	"""Records whose state is not one of this workflow's (what Frappe's
	``get_workflow_state_count`` counts, the number Desk shows before a workflow is
	saved), counted no further than one past ``KEPT_COUNT_MAX``."""
	if not states:
		return 0
	table = frappe.qb.DocType(dt)
	rows = (
		frappe.qb.from_(table)
		.select(table.name)
		.where(table[fieldname].notin(states))
		.where(table[fieldname] != "")  # an empty state is filled, not kept (as Frappe counts)
		.limit(KEPT_COUNT_MAX + 1)
		.run()
	)
	return len(rows)


@dataclass
class _Plan:
	"""What a Workflow write comes to: the stored form of the call, and what its
	save will do."""

	name: str
	dt: str
	fieldname: str
	doc: dict  # the workflow as it will read: its values and complete rows
	call: dict  # the stored form of the call's own values / changes
	adds_field: bool = False
	active: bool = False
	was_active: bool = False
	replaces: list = field(default_factory=list)
	fill: int = 0
	kept: int = 0


class _WorkflowWrite:
	"""What a create and an update share. A subclass says what the call gives
	(``_given``), the stored workflow (``_stored``) and its permission."""

	risk = ""
	# The fill and the previous active workflow stay on the confirmation row after
	# a successful confirm, for the operator's recovery aid (``undo_confirmation``).
	KEEP_UNDO = True

	def __init__(self):
		self._plan_cache: _Plan | None = None
		self._dt_cache: str | None = None

	# -- what the subclasses supply ---------------------------------------------
	def _given(self) -> dict:
		raise NotImplementedError

	def _stored(self) -> dict | None:
		raise NotImplementedError

	def _permitted(self, stored: dict | None) -> None:
		raise NotImplementedError

	# -- the handler interface ---------------------------------------------------
	@property
	def lock_doctype(self) -> str:
		"""The form the workflow is for: its table is what a new state field alters,
		and a Custom Field change to that form takes the same lock."""
		if self._dt_cache is None:
			self._dt_cache = self._target_name() or WF
		return self._dt_cache

	def check(self) -> _Plan:
		with cfg._own_messages():
			self._plan_cache = self._analyse()
		return self._plan_cache

	def _plan(self) -> _Plan:
		return self._plan_cache or self.check()

	def lead_line(self) -> str | None:
		"""The structural line only when a column is added; otherwise what the write
		is: a change of rules, with no change to the database structure."""
		plan = self._plan()
		if plan.adds_field:
			return None
		return f"Confirming changes the workflow rules of {plan.dt} for every user."

	def risk_lines(self) -> list[str]:
		"""What the card says after its first line. Called after ``check``. What is
		particular to this workflow and this site comes first (what it replaces is in
		the head lines; then what happens to existing records, what submits, who can
		approve their own); the lines every workflow card carries come last."""
		from jarvis.tools._write_risk import RISK_LINES

		plan = self._plan()
		states, transitions = plan.doc.get("states") or [], plan.doc.get("transitions") or []
		lines = self._head_lines(plan)
		if plan.fill or plan.kept:
			lines.append(_records_line(plan))
		if plan.active and states and not transitions:
			lines.append(
				"It has no transitions: no record of this form can be moved from one state to another."
			)
		for status, verb in (("1", "submits"), ("2", "cancels")):
			names = [s["state"] for s in states if s.get("doc_status") == status]
			if names:
				lines.append(f"Moving a record to {', '.join(names)} {verb} it.")
		own = sum(1 for t in transitions if cint(t.get("allow_self_approval")))
		if own:
			lines.append(
				f"Self-approval is allowed on {own} of {len(transitions)} transitions: a person who "
				"holds the role can approve a record they created themselves."
			)
		elif transitions:
			# Only what Frappe enforces (``has_approval_access``): the action is refused
			# for the record's owner, and never for the Administrator.
			lines.append(
				"Self-approval is off on every transition: the workflow action refuses the person "
				"who created the record (the Administrator excepted)."
			)
		for s in states:
			if s.get("update_field"):
				lines.append(
					f"Moving a record to {s['state']} also sets its {s['update_field']} to "
					f"{cstr(s.get('update_value'))!r}."
				)
		if not plan.adds_field and plan.fieldname != "workflow_state":
			df = frappe.get_meta(plan.dt).get_field(plan.fieldname)
			if df is not None and not (df.fieldtype == "Link" and df.options == "Workflow State"):
				lines.append(
					f"The state is kept in the existing field {df.label or plan.fieldname} "
					f"({plan.fieldname}): the workflow writes state names into it."
				)
		lines.extend(_role_lines(states, transitions))
		kept_code = self._kept_code(plan)
		if kept_code:
			lines.append(f"{'; '.join(kept_code)}: set up in Desk and kept as it is. It is not checked here.")
			lines.append(RISK_LINES["code"])
		lines.append(RISK_LINES["access"])
		if cint(plan.doc.get("send_email_alert")):
			lines.append("It emails the people who can act next on each record.")
		return lines

	def _kept_code(self, plan: _Plan) -> list[str]:
		"""The conditions and expression values this write keeps that chat could not
		have written (richer than a comparison of the document's own fields: set up
		in Desk, kept unchanged, never checked here). Everything else on the card
		passed ``expression_problem``."""
		out = []
		for t in plan.doc.get("transitions") or []:
			if t.get("condition") and expression_problem(t["condition"], plan.dt, state_field=plan.fieldname):
				out.append(f"The condition on {t.get('action')} from {t.get('state')}")
		for st in plan.doc.get("states") or []:
			if (
				cint(st.get("evaluate_as_expression"))
				and st.get("update_value")
				and expression_problem(st["update_value"], plan.dt, value=True, state_field=plan.fieldname)
			):
				out.append(f"The update value of {st.get('state')}")
		return out

	def _head_lines(self, plan: _Plan) -> list[str]:
		"""What the save does to the form: the field it adds, whether the workflow is
		active, which one it replaces. First on the card, and what ``card_holds``
		holds the card to at Confirm."""
		lines = []
		if plan.adds_field:
			lines.append(_adds_line(plan))
		if plan.active:
			# "stays" for an edit of the workflow that is active already.
			lines.append(
				f"It {'stays' if plan.was_active else 'becomes'} the active workflow of {plan.dt}: every "
				f"user is held to these states and transitions{'' if plan.was_active else ' at once'}."
			)
			if plan.replaces:
				lines.append(_replaces_line(plan))
		elif plan.was_active:
			lines.append(
				f"It stops being the active workflow of {plan.dt}: its records are no longer held to it."
			)
		else:
			lines.append("It is saved inactive: nothing is enforced until it is made active.")
		return lines

	def card_holds(self, card_line: str) -> bool:
		"""Under the lock, before the claim: does the card the person confirmed still
		say what this save will do to the form? Which workflow it replaces, and
		whether it adds a field, can change between the park and the Confirm (someone
		activates another workflow in Desk); the counts may drift and are not held
		against the card."""
		plan = self._plan()
		# The lines up to the replaces one come first, in a fixed order, and carry no
		# text the request wrote (the form, the field, workflows that exist); the
		# request's own values (an update value, state names) only come after them.
		head = " ".join([gs.lead_line(self), *self._head_lines(plan)])
		if not card_line.startswith(head):
			return False
		return bool(plan.replaces) or not card_line[len(head) :].startswith(f" {_REPLACES}")

	def snapshot(self) -> dict:
		"""What the clean-up needs, taken under the lock with the claim. Never raises:
		a call that can no longer be resolved fails its own check a moment later, and
		a snapshot that could not be read whole says so (``gs.snapshot_failed``), which
		stops the write before its record is saved."""
		out = {"risk": self.risk, "name": "", "dt": "", "before": None, "was_active": [], "fill": None}
		try:
			# The plan ``card_holds`` built a moment ago, under this same lock.
			plan = self._plan()
			stored = self._stored()
			out.update(name=plan.name, dt=plan.dt, was_active=_other_active(plan.dt, plan.name))
			if stored is not None:
				out["before"] = _before_image(plan.name)
			out["fill"] = _fill_snapshot(plan)
		except JarvisError:
			return gs.snapshot_refused(out)
		except Exception:
			return gs.snapshot_failed(out, f"Workflow {self.risk}")
		return out

	# -- analysis ----------------------------------------------------------------
	def _target_name(self) -> str:
		stored = None
		try:
			stored = self._stored()
		except Exception:
			stored = None
		value = (stored or {}).get("document_type") or self._given().get("document_type")
		return value if isinstance(value, str) else ""

	def _analyse(self) -> _Plan:
		"""Every park check, and the plan the card and the confirm are built from.
		Reads only: nothing here writes a row or touches an object another request
		can read."""
		given = self._given()
		stored = self._stored()
		self._permitted(stored)
		edit = stored is not None
		call = _parent_values(given, edit=edit)
		name = self._name(call, stored)
		call = self._fixed_keys(call, stored)
		doc = {**{k: v for k, v in (stored or {}).items() if k not in TABLES}, **call}
		dt = self._target(doc.get("document_type"), name if edit else None)
		for table in TABLES:
			doc[table] = _rows(table, given.get(table) if table in given else None, (stored or {}).get(table))
			if edit or doc[table]:
				call[table] = doc[table]
		fieldname = self._check_states(doc, dt, name if edit else None)
		active = bool(cint(doc.get("is_active")))
		plan = _Plan(
			name=name,
			dt=dt,
			fieldname=fieldname,
			doc=doc,
			call=call,
			active=active,
			was_active=bool(cint((stored or {}).get("is_active"))),
			replaces=_other_active(dt, name) if active else [],
		)
		plan.adds_field = self._check_state_field(dt, fieldname, name if edit else None)
		self._count(plan, name if edit else None)
		return plan

	def _name(self, call: dict, stored: dict | None) -> str:
		raise NotImplementedError

	def _fixed_keys(self, call: dict, stored: dict | None) -> dict:
		return call

	def _target(self, value, name: str | None) -> str:
		"""The form a workflow is for, named exactly, and one chat may put rules on."""
		dt = _exact("DocType", value, "The workflow's document_type")
		if cfg.denied_target(dt):
			raise _refuse(
				f"A workflow on {dt} is not set up from chat: it is a core, security, audit or Jarvis "
				"DocType (or a table of one).",
				name,
			)
		row = frappe.db.get_value("DocType", dt, ["istable", "issingle", "is_virtual"], as_dict=True) or {}
		if cint(row.get("istable")) or cint(row.get("issingle")):
			kind = "a child table" if cint(row.get("istable")) else "a single settings document"
			raise InvalidFieldValueError(
				f"{dt} is {kind}: a workflow needs a form whose records are saved one by one. "
				"Choose the form the records belong to."
			)
		if cint(row.get("is_virtual")) or not frappe.db.table_exists(dt):
			raise _refuse(
				f"{dt} has no table of its own (a virtual form, or one not migrated yet), so a workflow "
				"on it is not set up from chat.",
				name,
			)
		if not frappe.has_permission(dt, "read"):
			raise PermissionDeniedError(
				f"You do not have permission to read {dt}, so you cannot set its workflow."
			)
		return dt

	def _check_states(self, doc: dict, dt: str, name: str | None) -> str:
		"""The states and transitions themselves. Returns the state fieldname."""
		states, transitions = doc["states"], doc["transitions"]
		labels = {t: (_meta().get_field(t).label or t) for t in TABLES}
		_all_exist(doc)
		for table in TABLES:
			for i, row in enumerate(doc[table], 1):
				where = f"{labels[table]} row {i}"
				for key in _MANDATORY[table]:
					if not row.get(key):
						raise InvalidFieldValueError(f"{where}: {key} is missing.")
				for key, target in _LINKS[table]:
					if row.get(key):
						_exact(target, row[key], f"{where} ({key})")
		listed = [s["state"] for s in states]
		twice = sorted({n for n in listed if listed.count(n) > 1})
		if twice:
			raise InvalidFieldValueError(
				f"The state {', '.join(twice)} is listed more than once. List each state once."
			)
		if cint(doc.get("is_active")) and not states:
			raise InvalidFieldValueError(
				"An active workflow needs at least one state: without one, no record of the form could "
				"be saved. Add its states, or save it inactive."
			)
		if not cint(frappe.db.get_value("DocType", dt, "is_submittable")):
			moved = [s["state"] for s in states if s.get("doc_status") in ("1", "2")]
			if moved:
				raise InvalidFieldValueError(
					f"{dt} cannot be submitted, so the state {', '.join(moved)} cannot submit or cancel "
					"it. Use doc_status 0 for every state."
				)
		self._check_frappe_rules(doc, dt)
		self._check_updates(states, dt)
		self._check_roles(states, transitions, labels)
		self._check_code(states, transitions, labels, name, dt, cstr(doc.get("workflow_state_field")))
		self._check_tasks(transitions, labels, name)
		fieldname = doc.get("workflow_state_field")
		if not isinstance(fieldname, str) or not fieldname:
			raise InvalidFieldValueError(
				"workflow_state_field is missing. Leave it out to use workflow_state."
			)
		return fieldname

	def _check_frappe_rules(self, doc: dict, dt: str) -> None:
		"""Frappe's own rule on the document statuses of a transition
		(``Workflow.validate_docstatus``), on a document that is never saved.
		Newer Frappe 16 reads the form's meta there, so the probe names it."""
		probe = frappe.new_doc(WF)
		probe.document_type = dt
		for table in TABLES:
			for row in doc[table]:
				probe.append(table, {k: v for k, v in row.items() if k != "name"})
		try:
			probe.validate_docstatus()
		except frappe.ValidationError as e:
			raise InvalidFieldValueError(
				strip_html(str(e)).strip() or "The transitions are not valid."
			) from None

	def _check_updates(self, states: list[dict], dt: str) -> None:
		from frappe.model import no_value_fields

		from jarvis.tools._write_risk import _FIELD_SENSITIVE

		meta = frappe.get_meta(dt)
		for s in states:
			target = s.get("update_field")
			if not target:
				continue
			df = meta.get_field(target)
			if (
				df is None
				or df.fieldtype in no_value_fields
				or df.fieldtype in ("Table", "Table MultiSelect")
			):
				raise InvalidFieldValueError(
					f"The state {s['state']} sets {target!r}, which is not a field of {dt} that holds a "
					"value. Use a real fieldname or leave update_field out."
				)
			# Employee status switches the linked login on and off (``_toggles_login``).
			if (dt in _FIELD_SENSITIVE and target in _FIELD_SENSITIVE[dt][1]) or (dt, target) == (
				"Employee",
				"status",
			):
				# Carded once here, then written on every transition with no card.
				raise InvalidFieldValueError(
					f"The state {s['state']} sets {target!r}, a field of {dt} that gives access. A "
					"workflow that writes it is not set up from chat: leave update_field out, or tell "
					"the user to set it up in the Workflow form in Desk."
				)

	def _check_roles(self, states: list[dict], transitions: list[dict], labels: dict) -> None:
		"""Guest is everyone who is NOT signed in: never the role that may edit a
		record or take an action."""
		rows = [(labels["states"], i, s.get("allow_edit")) for i, s in enumerate(states, 1)]
		rows += [(labels["transitions"], i, t.get("allowed")) for i, t in enumerate(transitions, 1)]
		for label, i, role in rows:
			if role == "Guest":
				raise InvalidFieldValueError(
					f"{label} row {i}: Guest is everyone who is not signed in, and may not edit or move "
					"records in a workflow set up from chat. Choose a role people hold."
				)

	def _check_code(
		self, states: list[dict], transitions: list[dict], labels: dict, name: str | None, dt: str, field: str
	) -> None:
		"""What Frappe evaluates on the server (a transition's condition; Frappe 16:
		an update value marked as an expression) is, from chat, one restricted
		expression over the document (``expression_problem``), and must compile. An
		edit may keep an expression a row already has, exactly as it is: it was set
		up in Desk, and the card still shows it and says it is code."""
		stored = _stored_workflow(name) if name else None
		kept = {table: {r["name"]: r for r in (stored or {}).get(table) or []} for table in TABLES}
		checks = [("transitions", "condition", "condition", t) for t in transitions if t.get("condition")]
		checks += [
			("states", "update_value", "update value", s)
			for s in states
			if cint(s.get("evaluate_as_expression")) and s.get("update_value")
		]
		for table, key, word, row in checks:
			was = kept[table].get(row.get("name"))
			if (
				was is not None
				and (was.get(key) or None) == row[key]
				and (key == "condition" or cint(was.get("evaluate_as_expression")))
			):
				continue
			i = (transitions if table == "transitions" else states).index(row) + 1
			problem = expression_problem(
				row[key], dt, value=key != "condition", state_field=field or "workflow_state"
			) or _compiles(row[key])
			if problem:
				raise InvalidFieldValueError(
					f"{labels[table]} row {i}: the {word} {row[key]!r} is not set from chat ({problem}). "
					"From chat it may only compare this document's own fields (for example: "
					"doc.grand_total > 10000); anything more is set up in the Workflow form in Desk."
				)

	def _check_tasks(self, transitions: list[dict], labels: dict, name: str | None) -> None:
		"""``transition_tasks`` (Frappe 16) runs Server Scripts and Webhooks on the
		transition: attached in Desk, never from chat. An edit may keep a row that
		already runs tasks, exactly as it is: its states, action, role and condition
		decide when and for whom those tasks run."""
		if "transition_tasks" not in _row_keys("transitions"):
			return
		stored = {}
		if name:
			stored = {
				r.name: r
				for r in frappe.get_all(
					"Workflow Transition",
					filters={"parent": name, "parenttype": WF},
					fields=["name", *_TASK_KEYS],
				)
			}
		for i, t in enumerate(transitions, 1):
			if not t.get("transition_tasks"):
				continue
			was = stored.get(t.get("name"))
			if was is None or t["transition_tasks"] != was.transition_tasks:
				raise InvalidFieldValueError(
					f"{labels['transitions']} row {i}: transition tasks run scripts and webhooks on the "
					"transition and are not attached from chat. Leave transition_tasks out and tell the "
					"user to attach them in the Workflow form in Desk."
				)
			if any((t.get(k) or None) != (was.get(k) or None) for k in _TASK_KEYS):
				raise InvalidFieldValueError(
					f"{labels['transitions']} row {i} runs tasks (scripts or webhooks), so it is not "
					"changed from chat. Leave that row as it is and tell the user to change it in the "
					"Workflow form in Desk."
				)

	def _check_state_field(self, dt: str, fieldname: str, name: str | None) -> bool:
		"""Whether saving adds the state field to the form (and its column to the
		table), after checking that it can."""
		if not cfg._FIELDNAME.match(fieldname) or fieldname in dir(frappe.qb.DocType(dt)):
			# Frappe fills the states through its query builder, where a name such as
			# ``select`` or ``join`` is a method of the table, not a column.
			raise InvalidFieldValueError(
				f"{fieldname!r} cannot be used as the workflow state field. Leave workflow_state_field "
				"out to use workflow_state."
			)
		df = frappe.get_meta(dt).get_field(fieldname)
		if df is None:
			if not frappe.has_permission(cfg.CF, "create"):
				raise PermissionDeniedError(
					f"Saving this workflow adds the field {fieldname} to {dt}, which needs permission to "
					"create Custom Fields (usually the System Manager role)."
				)
			try:
				cfg.check_generated_fields(dt, [_state_field_row(fieldname)])
			except InvalidFieldValueError as e:
				raise InvalidFieldValueError(
					f"{e} Name another workflow_state_field, or leave it out to use workflow_state."
				) from None
			except StructureRefusedError:
				raise _refuse(
					f"{dt}'s table has other structure changes waiting, so a workflow that adds its state "
					"field cannot be saved from chat. An administrator brings the table in line first "
					"(bench migrate applies pending structure changes).",
					name,
				) from None
			return True
		if (
			df.fieldtype not in _STATE_FIELDTYPES
			or (df.fieldtype == "Link" and df.options != "Workflow State")
			or cint(df.get("is_virtual"))
		):
			raise InvalidFieldValueError(
				f"{dt} already has a field named {fieldname} that cannot hold a workflow state "
				f"({df.fieldtype}). Leave workflow_state_field out to use workflow_state, or name a "
				"Link field to Workflow State."
			)
		if fieldname.lower() not in gs.table_columns(dt):
			raise _refuse(
				f"The field {fieldname} of {dt} has no column in its table, so a workflow cannot use it "
				"yet. An administrator repairs the form first (bench migrate).",
				name,
			)
		return False

	def _count(self, plan: _Plan, name: str | None) -> None:
		"""What the save does to existing records, as Frappe will do it."""
		states = plan.doc["states"]
		first = _fill_states(states)
		plan.fill = _count_fill(plan.dt, plan.fieldname, sorted(first), not plan.adds_field)
		if not plan.adds_field:
			plan.kept = _count_kept(plan.dt, plan.fieldname, [s["state"] for s in states])
		# Whether the column is new or not: Frappe fills them all in one UPDATE,
		# inside the request that confirms.
		if plan.fill > FILL_NAMES_MAX:
			raise _refuse(
				f"More than {FILL_NAMES_MAX} records of {plan.dt} have no workflow state, and saving "
				"this workflow would fill them all in at once. That many is not done from chat, so "
				"this workflow is saved in Desk.",
				name,
			)


def _records_line(plan: _Plan) -> str:
	"""What the save does to the records that exist: how many get a state, and how
	many are left in a state the workflow does not have (Frappe's
	``validate_workflow`` then refuses every save of such a record)."""
	kept = (
		f"more than {gs.counted(KEPT_COUNT_MAX, 'record')} keep states"
		if plan.kept > KEPT_COUNT_MAX
		else f"{gs.counted(plan.kept, 'record')} {'keeps a state' if plan.kept == 1 else 'keep states'}"
	)
	stuck = ""
	if plan.kept and plan.active:
		stuck = (
			f", and {'it' if plan.kept == 1 else 'they'} cannot be saved or moved until "
			f"{'its' if plan.kept == 1 else 'their'} state is changed to one of this workflow's"
		)
	return (
		f"It fills the state on {gs.counted(plan.fill, 'record')} that "
		f"{'has' if plan.fill == 1 else 'have'} none; {kept} this workflow lacks{stuck}."
	)


_REPLACES = "It replaces the active workflow"


def _replaces_line(plan: _Plan) -> str:
	return f"{_REPLACES} {', '.join(plan.replaces)}."


def _adds_line(plan: _Plan) -> str:
	return f"Adds the hidden field {plan.fieldname} to {plan.dt}."


def _stored_workflow(name: str) -> dict | None:
	"""The workflow ``name`` as stored: its values and its rows, read fresh."""
	row = frappe.db.get_value(WF, name, "*", as_dict=True) if name else None
	if not row:
		return None
	out = dict(row)
	for table in TABLES:
		out[table] = [
			dict(r)
			for r in frappe.get_all(
				_meta().get_field(table).options,
				filters={"parent": row.name, "parenttype": WF, "parentfield": table},
				fields=["*"],
				order_by="idx",
			)
		]
	return out


def _before_image(name: str) -> dict | None:
	stored = _stored_workflow(name)
	return frappe.parse_json(frappe.as_json(stored)) if stored else None


def _fill_snapshot(plan: _Plan) -> dict | None:
	"""Which records the save will fill a state on, so that can be undone: their
	names, or ``names: None`` for every record when the column itself is new."""
	first = _fill_states(plan.doc["states"])
	if not first:
		return None
	out = {"field": plan.fieldname, "values": sorted(set(first.values())), "names": None}
	if plan.adds_field:
		return out
	table = frappe.qb.DocType(plan.dt)
	rows = (
		frappe.qb.from_(table)
		.select(table.name)
		.where(table.docstatus.isin(sorted(first)))
		.where(_empty_state(table, plan.fieldname))
		.limit(FILL_NAMES_MAX + 1)
		.run()
	)
	out["names"] = [r[0] for r in rows][:FILL_NAMES_MAX]
	return out


# --------------------------------------------------------------------------- #
# One new Workflow
# --------------------------------------------------------------------------- #
class NewWorkflow(_WorkflowWrite):
	risk = RISK_NEW

	def __init__(self, args: dict):
		super().__init__()
		values = args.get("values")
		self.given = dict(values) if isinstance(values, dict) else {}

	def _given(self) -> dict:
		return self.given

	def _stored(self) -> None:
		return None

	def _permitted(self, stored) -> None:
		if not frappe.has_permission(WF, "create"):
			raise PermissionDeniedError(
				"You do not have permission to create a Workflow (usually the System Manager role)."
			)

	def _name(self, call: dict, stored) -> str:
		name = call.get("workflow_name")
		if not isinstance(name, str) or not name:
			raise InvalidFieldValueError("Give the workflow a name: workflow_name is missing.")
		if frappe.db.exists(WF, name):
			raise InvalidFieldValueError(f"A workflow named {name!r} already exists. Choose another name.")
		from frappe.model.naming import validate_name

		try:
			validate_name(WF, name)
		except frappe.ValidationError:
			raise InvalidFieldValueError(
				f"{name!r} cannot be a workflow's name. Use letters, digits, spaces and hyphens."
			) from None
		return name

	def canonical_args(self) -> dict:
		"""The call as it is carded, sealed and run. Called after ``check``."""
		return {"doctype": WF, "values": dict(self._plan().call)}

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Remove the workflow this confirmation made, and put the form back: the
		workflow that was active is active again, a state field whose column was
		never added is removed, and the states the save filled in are emptied."""
		name, dt = str(undo.get("name") or ""), str(undo.get("dt") or "")
		written = undo.get("written") if isinstance(undo.get("written"), dict) else None
		if not name or not written:
			return CleanUp("Nothing was changed.")
		row = frappe.db.get_value(WF, written.get("name"), ["creation", "modified"], as_dict=True)
		stamp = str(get_datetime(written.get("modified")))
		if row and str(get_datetime(row.creation)) != stamp:
			return CleanUp(
				f"A workflow named {name} exists, but this confirmation did not make it; it was left untouched."
			)
		if row and str(get_datetime(row.modified)) != stamp:
			return CleanUp(
				f"The workflow {name} was changed again in the meantime; it was left as it is.",
				clean=False,
				needs_person=True,
			)
		if row and _used_since(dt, undo.get("fill"), written.get("modified")):
			return _left_in_use(name)
		said = []
		if row:
			_delete_workflow(name)
			said.append(f"The workflow {name} was removed.")
		return _put_back(undo, dt, said, changed=bool(row))


# --------------------------------------------------------------------------- #
# An update of one Workflow
# --------------------------------------------------------------------------- #
class WorkflowEdit(_WorkflowWrite):
	risk = RISK_EDIT

	def __init__(self, args: dict):
		super().__init__()
		self.name = args.get("name") if isinstance(args.get("name"), str) else ""
		changes = args.get("changes")
		self.given = dict(changes) if isinstance(changes, dict) else {}

	def _given(self) -> dict:
		return self.given

	def _stored(self) -> dict:
		stored = _stored_workflow(self.name)
		if stored is None:
			raise InvalidFieldValueError(f"There is no Workflow named {self.name!r}.")
		if stored["name"] != self.name:
			raise InvalidFieldValueError(f"The Workflow is named {stored['name']!r}, not {self.name!r}.")
		return stored

	def _permitted(self, stored) -> None:
		if not frappe.has_permission(WF, "write", doc=self.name):
			raise PermissionDeniedError(
				"You do not have permission to change this Workflow (usually the System Manager role)."
			)

	def _name(self, call: dict, stored) -> str:
		if not self.given:
			raise InvalidFieldValueError("Say what to change on the workflow: changes is empty.")
		return self.name

	def _fixed_keys(self, call: dict, stored) -> dict:
		"""A workflow is not renamed or moved to another form from chat; an echoed,
		unchanged value is dropped."""
		for key in ("workflow_name", "document_type"):
			if key in call and call[key] != stored.get(key):
				raise _refuse(
					f"Changing the {key} of the workflow {self.name} makes it another workflow, so it is "
					"done in Desk, not from chat.",
					self.name,
				)
			call.pop(key, None)
		# The states of the records that exist live in this field: another field
		# starts every record without one.
		key = "workflow_state_field"
		if key in call and call[key] != (stored.get(key) or "workflow_state"):
			raise _refuse(
				f"Changing the state field of the workflow {self.name} leaves the state of every "
				"existing record behind in the old field, so it is done in Desk, not from chat.",
				self.name,
			)
		return call

	def canonical_args(self) -> dict:
		"""As ``NewWorkflow.canonical_args``. The changes always carry both tables
		whole, so the card shows every state and transition of the workflow as it
		will read, also when only a switch (active) changes."""
		return {"doctype": WF, "name": self.name, "changes": dict(self._plan().call)}

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Put the workflow back as it was, only when it still carries the exact
		timestamp this confirmation wrote it with: a later edit is never overwritten.
		Then the form, as ``NewWorkflow.clean_up``."""
		name, dt = str(undo.get("name") or ""), str(undo.get("dt") or "")
		before = undo.get("before")
		written = undo.get("written") if isinstance(undo.get("written"), dict) else None
		if not name or not isinstance(before, dict) or not written:
			return CleanUp(f"The workflow {name} was not changed." if name else "Nothing was changed.")
		modified = frappe.db.get_value(WF, name, "modified")
		if not modified:
			return CleanUp(f"The workflow {name} no longer exists; nothing was restored.")
		if str(get_datetime(modified)) == str(get_datetime(before.get("modified"))):
			return _put_back(undo, dt, [f"The workflow {name} was not changed."], changed=False)
		if str(get_datetime(modified)) != str(get_datetime(written.get("modified"))):
			return CleanUp(
				f"The workflow {name} was changed again in the meantime; it was left as it is.", clean=False
			)
		if _used_since(dt, undo.get("fill"), written.get("modified")):
			return _left_in_use(name)
		_restore_workflow(name, before)
		return _put_back(undo, dt, [f"The change to the workflow {name} was undone."], changed=True)


# --------------------------------------------------------------------------- #
# Putting things back
# --------------------------------------------------------------------------- #
def _delete_workflow(name: str) -> None:
	"""Raw: no hook of a half-made workflow has anything to tidy."""
	for table in TABLES:
		frappe.db.delete(_meta().get_field(table).options, {"parent": name, "parenttype": WF})
	frappe.db.delete(WF, {"name": name})


def _restore_workflow(name: str, before: dict) -> None:
	gs.write_row(WF, {k: v for k, v in before.items() if k not in TABLES}, name=name)
	for table in TABLES:
		child = _meta().get_field(table).options
		frappe.db.delete(child, {"parent": name, "parenttype": WF, "parentfield": table})
		for row in before.get(table) or []:
			gs.write_row(child, row)


def _put_back(undo: dict, dt: str, said: list[str], *, changed: bool) -> CleanUp:
	"""What every Workflow clean-up ends with: the previous active workflow, the
	fields the save made, the states it filled, the caches."""
	clean = True
	if changed:
		for other in undo.get("was_active") or []:
			if not frappe.db.exists(WF, {"document_type": dt, "is_active": 1}) and frappe.db.exists(
				WF, other
			):
				frappe.db.set_value(WF, other, "is_active", 1, update_modified=False)
				said.append(f"The workflow {other} is active again.")
	for note in cfg.clean_up_generated(undo.get("made")):
		said.append(note.text)
		clean = clean and note.clean
		changed = changed or note.changed
	if changed:
		written = undo.get("written") if isinstance(undo.get("written"), dict) else {}
		emptied = _empty_fill(dt, undo.get("fill"), written.get("modified"))
		if emptied:
			said.append(f"The state filled in on {gs.counted(emptied, 'record')} was emptied again.")
	_clear(dt, [str(undo.get("name") or ""), *(undo.get("was_active") or [])])
	return CleanUp(" ".join(said) or "Nothing was changed.", clean=clean, changed=changed)


def _used_since(dt: str, fill, since=None) -> bool:
	"""Whether people have worked with the workflow since this confirmation saved it
	(``since``: the timestamp it was written with). Taking the workflow away then
	would leave records in a state no workflow owns. Two signs, each one bounded
	query:

	- any record of the form that carries a state and was saved or created after
	  ``since``: a record created under this workflow holds one of ITS states, which
	  the workflow that would be active again may lack. The state alone cannot tell: the save fills
	  the first state of every document status, so a record approved since (Draft
	  to Approved, submitted) carries exactly the state the save would have filled
	  on a record that was submitted all along. The fill itself does not touch
	  ``modified``; a person's save does. An edit that moved nothing counts too:
	  that errs towards leaving the workflow for a person;
	- a record whose state the save filled in (every record, when the column was
	  new) now carries a state the save does not fill, whenever it was saved."""
	if not isinstance(fill, dict) or not fill.get("values") or not dt:
		return False
	fieldname = str(fill.get("field") or "")
	if fieldname.lower() not in gs.table_columns(dt):
		return False
	table = frappe.qb.DocType(dt)
	has_state = table[fieldname].isnotnull() & (table[fieldname] != "")

	if since and (
		frappe.qb.from_(table)
		.select(table.name)
		.where(has_state)
		.where(table.modified > get_datetime(since))
		.limit(1)
		.run()
	):
		return True
	names = fill.get("names")
	for chunk in [None] if names is None else [names[i : i + 500] for i in range(0, len(names), 500)]:
		query = (
			frappe.qb.from_(table)
			.select(table.name)
			.where(has_state)
			.where(table[fieldname].notin(fill["values"]))
			.limit(1)
		)
		if chunk is not None:
			query = query.where(table.name.isin(chunk))
		if query.run():
			return True
	return False


def _left_in_use(name: str) -> CleanUp:
	return CleanUp(
		f"The workflow {name} was saved and records have moved on in it since, so it was left as it "
		"is, active or not. It needs a person: check the workflow and its records in Desk.",
		clean=False,
		needs_person=True,
	)


def _empty_fill(dt: str, fill, since=None) -> int:
	"""Empty the states the save filled in: only on the records that had none
	(every record when the column itself was new), only where the state is still
	one that was filled in, and never on a record saved after ``since`` (the
	timestamp the workflow was written with): its state is a person's."""
	if not isinstance(fill, dict) or not fill.get("values") or not dt:
		return 0
	fieldname = str(fill.get("field") or "")
	if fieldname.lower() not in gs.table_columns(dt):
		return 0
	table = frappe.qb.DocType(dt)
	names = fill.get("names")
	done = 0
	chunks = [None] if names is None else [names[i : i + 500] for i in range(0, len(names), 500)]
	for chunk in chunks:
		query = (
			frappe.qb.update(table).set(table[fieldname], None).where(table[fieldname].isin(fill["values"]))
		)
		if chunk is not None:
			query = query.where(table.name.isin(chunk))
		if since:
			query = query.where(table.modified <= get_datetime(since))
		query.run()
		done += gs._stamp_rows()
	return done


def _clear(dt: str, workflows: list[str]) -> None:
	for name in workflows:
		if name:
			frappe.clear_document_cache(WF, name)
	if dt:
		frappe.cache.hdel("workflow", dt)
		frappe.clear_cache(doctype=dt)
