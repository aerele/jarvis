"""The metadata plan of the batch permission check: is a doctype safe to evaluate in batch, and why not.

`build_plan(doctype)` looks only at metadata (meta, hooks, controller classes, columns, pins) and returns a
`BatchPlan`: `batch`, or `per_doc` with one fixed reason. The pins are checked first. Any other error in the
build is a `per_doc` plan with the reason `plan_error`: a doctype that the plan cannot describe takes the exact
per-document loop, it never fails the read. The entry module `batch_permission` owns the per-request plan memo
and the live gate.

Schema facts (the column list of a table, "this master has a restricted row") live on `frappe.local`, so they
end with the request or job. A process-wide memo would miss a Custom Field that an operator adds later, and the
batch would show a document that the per-document check hides.

Test seam: `_entry()` is the entry module `batch_permission`, imported at call time because that module imports
this one. A test patches `_table_columns`, `_mro_reason`, `_Table`, `queue_event`, `CHUNK`... on the entry
module (patching the module that defines the name changes nothing), and the code here reads them through it.
"""

from dataclasses import dataclass

import frappe
import frappe.model.document
import frappe.permissions

from jarvis.payment_calendar import compatibility
from jarvis.payment_calendar.errors import TIMEOUTS


def _entry():
	"""The entry module, read at call time (import cycle): a name that a test rebinds there is seen here."""
	from jarvis.payment_calendar import batch_permission

	return batch_permission


@dataclass(frozen=True)
class BatchPlan:
	mode: str  # "batch" | "per_doc"
	reason: str | None
	real_hooks: tuple[str, ...]  # hooks that run on a lazy document per name (not known to be stand-in safe)


_COMPANY_RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."


_INHERITED_HOOK = _COMPANY_RESTRICTION + "has_inherited_permission"


_PARTY_HOOK = _COMPANY_RESTRICTION + "has_permission"


_PLAYGROUND_HOOK = "jarvis_playground.visitors.access.has_permission"


# Hooks whose code reads document values only through `get`: they run on the stand-in.
_SHIM_HOOKS = frozenset({_PARTY_HOOK, _PLAYGROUND_HOOK})


_KNOWN_HOOKS = _SHIM_HOOKS | {_INHERITED_HOOK}


# A class in a controller's MRO that defines one of these changes how a document loads or reads a value.
_GUARDED_METHODS = frozenset(
	{
		"__init__", "__new__", "__getattr__", "__getattribute__", "get", "set", "update", "load_from_db",
		"init_from_dict", "get_all_children", "_load_child_table_from_db", "load_children_from_db", "__setup__",
		"append", "extend", "_init_child", "init_valid_columns", "get_valid_columns", "init_child_tables",
		"_table_fieldnames", "_non_computed_table_fieldnames", "meta",
	}
)  # fmt: skip


# Names of the `frappe.local` attributes that hold the schema facts of the current request or job:
# table -> {column: (data type, collation)}, and master doctype -> "has a restricted row".
_COLUMNS = "pcal_permission_columns"
_RESTRICTED = "pcal_permission_restricted_masters"


def _fact(name: str) -> dict:
	"""The schema-fact memo `name` of this request or job; Frappe drops it with `frappe.local`."""
	memo = getattr(frappe.local, name, None)
	if memo is None:
		memo = {}
		setattr(frappe.local, name, memo)
	return memo


def _forget_facts() -> None:
	"""Drop the schema facts now (the re-check of a mismatch reads them again; a test ends its request)."""
	for name in (_COLUMNS, _RESTRICTED):
		if hasattr(frappe.local, name):
			delattr(frappe.local, name)


def has_restricted_row(master: str) -> bool:
	"""True when some row of the master doctype has `restrict_to_companies` set. One scan per request or job.

	There is no index on the column, so the scan reads the table. A job reads inside one snapshot and a request
	is short: a restriction that is set while either runs cannot change an answer that the run has already given.
	"""
	found = _fact(_RESTRICTED)
	if master not in found:
		rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only the quoted table name of a Link target doctype; no value
			f"SELECT 1 FROM {_quoted(master)} WHERE `restrict_to_companies` = 1 LIMIT 1"
		)
		found[master] = bool(rows)
	return found[master]


def build_plan(doctype: str) -> BatchPlan:
	"""The plan of one doctype for the session user: the pins first, then the metadata checks.

	The pin check calls no Frappe API of one major, so on an unsupported major it answers before any call that
	would not exist there. An error in the metadata checks (a missing API, a database error, an import error)
	gives `plan_error`; only a stop (timeout) propagates.
	"""
	drift = _pin_reason()
	if drift:
		return BatchPlan("per_doc", drift, ())
	try:
		return _metadata_plan(doctype)
	except TIMEOUTS:
		raise
	except Exception as error:
		_entry().queue_event(
			"pcal_permission_plan_error", f"the plan build failed ({type(error).__name__}); per-document loop"
		)
		return BatchPlan("per_doc", "plan_error", ())


def _metadata_plan(doctype: str) -> BatchPlan:
	up_keys = set(frappe.permissions.get_user_permissions(frappe.session.user))
	ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
	chain = _hook_chain(doctype)
	real = tuple(method for method in chain if method not in _KNOWN_HOOKS)
	reason = _plan_reason(doctype, up_keys, ptype, chain)
	return BatchPlan("per_doc" if reason else "batch", reason, real)


def _plan_reason(doctype: str, up_keys: set[str], ptype: str, chain: list[str]) -> str | None:
	if up_keys - {"Company"}:
		return "scope_users"
	if frappe.is_table(doctype):
		return "table_doctype"
	meta = frappe.get_meta(doctype)
	if meta.issingle:
		return "single_parent"
	if meta.is_virtual:
		return "virtual_parent"
	try:
		layout = _Layout(doctype, up_keys)
	except frappe.DoesNotExistError:  # a table field that links to a doctype that is gone
		return "no_table"
	reason = (
		_layout_reason(layout)
		or _master_reason(layout, chain)
		or _custom_ptype_reason(doctype, ptype)
		or _entry()._mro_reason(doctype, layout)
	)
	return reason or _identity_reason(doctype, layout) or _collation_reason(layout, chain)


def _layout_reason(layout) -> str | None:
	if any(table.virtual for table in layout.tables):
		return "computed_table"
	if any(table.meta.is_virtual for table in layout.tables):
		return "virtual_child"
	if not layout.columns or any(not table.columns for table in layout.tables):
		return "no_table"
	if any(name not in layout.columns for name in layout.up_links) or any(
		name not in table.columns for table in layout.relevant for name in table.up_links
	):
		return "no_column"
	return None


def _master_reason(layout, chain: list[str]) -> str | None:
	"""The hooks read `restrict_to_companies`: the column must exist where they read it."""
	if _PARTY_HOOK in chain and "restrict_to_companies" not in layout.columns:
		return "no_column"
	if _INHERITED_HOOK in chain:
		for link in _master_links(layout):
			if "restrict_to_companies" not in _entry()._table_columns(_quoted(link.doctype).strip("`")):
				return "no_column"
	return None


def _custom_ptype_reason(doctype: str, ptype: str) -> str | None:
	if ptype == "select" and "select" in frappe.permissions.get_doctype_ptype_map().get(doctype, []):
		return "custom_ptype"
	return None


def _mro_reason(doctype: str, layout) -> str | None:
	"""A class in the MRO of the lazy controller or of a child controller that can change a loaded value.

	The generated `Lazy<Name>` class is checked by `permission_identity_failures` (its `LAZY_CLASS_ROWS`).
	"""
	allowed = getattr(compatibility, "CONTROLLER_ALLOWLIST", frozenset())
	lazy = frappe.model.document.get_lazy_controller(doctype)
	classes = [lazy] + [frappe.model.document.get_controller(table.doctype) for table in layout.tables]
	module = frappe.model.document
	skip = {lazy, module.Document, module._DocumentBase, module.LazyDocument, object}
	for cls in classes:
		for klass in cls.__mro__:
			if klass in skip:
				continue
			if type(klass) is not type:
				return "controller_override"
			if any(
				name in vars(klass) and (klass.__qualname__, name) not in allowed for name in _GUARDED_METHODS
			):
				return "controller_override"
	return None


def _identity_reason(doctype: str, layout) -> str | None:
	"""A callable that per-doc resolves is not the audited one. A change of the lazy class is an override."""
	lazy = frappe.model.document.get_lazy_controller(doctype)
	children = [frappe.model.document.get_controller(table.doctype) for table in layout.tables]
	failures = _compat_failures("permission_identity_failures", lazy, children)
	if not failures:
		return None
	if any(failure.split(":")[0] in compatibility.LAZY_CLASS_ROWS for failure in failures):
		return "controller_override"
	_entry().queue_event(
		"pcal_permission_identity", "a permission callable is not the audited one: " + ", ".join(failures)
	)
	return "entry_patched"


def _pin_reason() -> str | None:
	drift = _compat_failures("permission_pin_drift")
	if drift:
		_entry().queue_event("pcal_permission_pin_drift", "permission source changed: " + ", ".join(drift))
		return "pin_drift"
	return None


def _compat_failures(name: str, *args) -> list[str]:
	"""The failures a `compatibility` check names. A missing or failing check, or any error in it, is a failure."""
	check = getattr(compatibility, name, None)
	if check is None:
		return [name + " unavailable"]
	try:
		return [str(failure) for failure in check(*args)] or []
	except TIMEOUTS:
		raise
	except Exception:
		return [name + " failed"]


def _collation_reason(layout, chain: list[str]) -> str | None:
	"""Joined columns must compare as Frappe's own queries do: same type and same non-NULL collation."""
	name = layout.columns.get("name")
	pairs = [(_entry()._table_columns("tabDocShare").get("share_name"), name)]
	pairs += [(table.columns.get("parent"), name) for table in layout.relevant]
	if _INHERITED_HOOK in chain:
		for link in _master_links(layout):
			pairs.append(
				(
					_entry()._table_columns("tab" + link.doctype).get("name"),
					layout.columns.get(link.fieldname),
				)
			)
	if all(left and right and left[1] and left == right for left, right in pairs):
		return None
	return "collation"


class _Table:
	"""One table field of the parent and the child columns that a verdict can read."""

	def __init__(self, df, up_keys: set[str]):
		self.fieldname = df.fieldname
		self.doctype = df.options
		self.virtual = bool(df.is_virtual)
		self.meta = frappe.get_meta(self.doctype)
		self.quoted = _quoted(self.doctype)
		self.columns = _entry()._table_columns(self.quoted.strip("`"))
		links = _link_fields(self.meta)
		self.up_links = [link.fieldname for link in links if link.options in up_keys]
		self.links = [link.fieldname for link in links if link.fieldname in self.columns]
		self.relevant = bool(self.up_links)
		self.nested = {nested.fieldname for nested in self.meta.get_table_fields(include_computed=True)}
		self.null_keys = _null_keys(self.meta, self.columns, {"__islocal"})


class _Layout:
	"""The columns and tables of a doctype that a verdict for the user's User Permissions can read."""

	def __init__(self, doctype: str, up_keys: set[str]):
		self.doctype = doctype
		self.meta = frappe.get_meta(doctype)
		self.quoted = _quoted(doctype)
		self.columns = _entry()._table_columns(self.quoted.strip("`"))
		links = _link_fields(self.meta)
		self.up_links = [link.fieldname for link in links if link.options in up_keys]
		# Without any User Permission `has_user_permission` returns at once and reads no value.
		self.links = [link.fieldname for link in links if link.fieldname in self.columns] if up_keys else []
		self.tables = [
			_entry()._Table(df, up_keys) for df in self.meta.get_table_fields(include_computed=True)
		]
		self.relevant = [table for table in self.tables if table.relevant]
		self.table_names = {table.fieldname for table in self.tables}
		self.null_keys = _null_keys(self.meta, self.columns, {"__islocal", "parentfield"})


# Mirrors frappe/permissions.py:has_controller_permissions L491-494 (16.56.0, pin frappe.permissions.has_controller_permissions)
# The hooks of the doctype, then those of `*`, called last to first. Difference: the methods are returned as
# paths, not called.
def _hook_chain(doctype: str) -> list[str]:
	"""The `has_permission` hook methods in the order `has_controller_permissions` calls them."""
	hooks = frappe.get_hooks("has_permission")
	return list(reversed(hooks.get(doctype, []) + hooks.get("*", [])))


def _master_links(layout) -> list:
	"""The real `get_inherited_master_links`, without a link whose columns do not exist (it never reads a value)."""
	links = frappe.get_attr(_COMPANY_RESTRICTION + "get_inherited_master_links")(layout.doctype)
	return [
		link
		for link in links
		if link.fieldname in layout.columns
		and (not link.doctype_fieldname or link.doctype_fieldname in layout.columns)
	]


def _null_keys(meta, columns: dict, known: set[str]) -> frozenset[str]:
	"""Keys a real document answers with `None`: `known`, and each Link field that has no database column."""
	return frozenset(known | {df.fieldname for df in meta.get_link_fields() if df.fieldname not in columns})


def _link_fields(meta) -> list:
	return [df for df in meta.get_link_fields() if not df.ignore_user_permissions]


def _quoted(doctype: str) -> str:
	return frappe.model.document.get_table_name(doctype, wrap_in_backticks=True)


def _table_columns(table: str) -> dict[str, tuple[str, str | None]]:
	"""Column -> (data type, collation) of a table; `{}` when it does not exist. One read per request or job."""
	columns = _fact(_COLUMNS)
	if table not in columns:
		rows = []
		if frappe.db.db_type == "mariadb":
			rows = frappe.db.sql(
				"SELECT COLUMN_NAME AS `column`, DATA_TYPE AS `type`, COLLATION_NAME AS `collation` "
				"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
				(table,),
				as_dict=True,
			)
		columns[table] = {row["column"]: (row["type"], row["collation"]) for row in rows}
	return columns[table]
