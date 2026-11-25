"""The batch evaluator: Frappe's own permission functions on a read-only stand-in for each document.

`evaluate_batch` reads the parent rows, the child rows that a User Permission can deny, the shares and the hook
inputs in chunks and returns the visible names. It has no live gate, small-input rule, freeze, sample, trip, event
or stats: `batch_permission` wraps it.

Each block that copies Frappe carries a `# Mirrors frappe/<file>:<function> L<first>-<last> (<version>, pin <name>)`
comment: the lines of the pinned Frappe function that it copies, and each deliberate difference. When a pin
drifts, `grep Mirrors` in this file lists what to compare with the new Frappe source.

Test seam: `_entry()` is the entry module `batch_permission`, imported at call time because that module imports
this one. A test patches `_lazy_doc`, `CHUNK`, `_per_doc`, `check_permission_stop`, `_dispatch_hook` on the entry
module (patching the module that defines the name changes nothing), and the code here reads them through it.
"""

import copy
from collections import Counter
from dataclasses import dataclass, field

import frappe
import frappe.permissions

from jarvis.payment_calendar.batch_permission_plan import (
	_INHERITED_HOOK,
	_PARTY_HOOK,
	_SHIM_HOOKS,
	BatchPlan,
	_forget_facts,
	_hook_chain,
	_Layout,
	_master_links,
	_quoted,
	has_restricted_row,
)


def _entry():
	"""The entry module, read at call time (import cycle): a name that a test rebinds there is seen here."""
	from jarvis.payment_calendar import batch_permission

	return batch_permission


@dataclass
class EvalResult:
	visible: set[str]
	chunks: list[list[str]]
	branches: Counter
	shim_miss: int
	batch_evaluated: int
	flagged: list[str] = field(default_factory=list)  # names that took a non-default branch


class ShimMiss(Exception):
	"""The stand-in cannot answer a read exactly; that name takes the per-document path."""


# Branches a verdict can take besides the plain role-and-User-Permission answer: the sample prefers these names.
_SPECIAL = (
	"child_deny", "child_deny_empty", "share_grant", "everyone_share", "owner_only", "select_read",
	"candidate_allow", "candidate_deny", "hook_deny", "hook_raise", "real_hook_call", "denied_with_children",
)  # fmt: skip


def evaluate_batch(
	doctype: str,
	names: set[str],
	ptype: str,
	plan: BatchPlan,
	*,
	chunks=None,
	fresh: bool = False,
	count_branches: bool = True,
) -> EvalResult:
	"""The batch evaluator alone: no live gate, small-input rule, freeze, sample, trip, event or stats.

	`chunks` re-uses a given partition; `fresh=True` drops the schema facts of this request first (the plan is the caller's).
	`count_branches=False` skips the extra `has_user_permission` calls that only name the child-row branch for
	the counts (the hot path); the tests and the differential keep the default.
	"""
	if fresh:
		_forget_facts()
	run = _Run(doctype, ptype, count_branches)
	chunks = chunks if chunks is not None else _partition(names)
	visible: set[str] = set()
	for chunk in chunks:
		_entry().check_permission_stop()
		visible |= run.evaluate_chunk(chunk)
	return EvalResult(visible, chunks, run.counts, run.shim_miss, run.batch_evaluated, run.flagged)


def _partition(names) -> list[list[str]]:
	ordered = sorted(names)
	size = _entry().CHUNK
	return [ordered[offset : offset + size] for offset in range(0, len(ordered), size)]


class _Run:
	"""One batch evaluation: the layout, hook chain and counters of a doctype for the session user."""

	def __init__(self, doctype: str, ptype: str, count_branches: bool = True):
		self.doctype = doctype
		self.ptype = ptype
		self.user = frappe.session.user
		self.layout = _Layout(doctype, set(frappe.permissions.get_user_permissions(self.user)))
		self.chain = _hook_chain(doctype)
		self.masters = _master_links(self.layout) if _INHERITED_HOOK in self.chain else []
		self.strict = bool(frappe.get_system_settings("apply_strict_user_permissions"))
		self.count_branches = count_branches
		self.counts: Counter = Counter()
		self.shim_miss = 0
		self.batch_evaluated = 0
		self.flagged: list[str] = []
		self._role_perms: dict[bool, tuple[dict, dict]] = {}
		self.candidates: set[str] = set()
		self.columns = self._parent_columns()

	def evaluate_chunk(self, chunk: list[str]) -> set[str]:
		parents = self._parent_rows(chunk)
		children = {table.fieldname: self._child_rows(table, chunk) for table in self.layout.relevant}
		shared = self._shares(chunk)
		self.candidates = self._candidates(chunk)
		visible, fallback = set(), []
		for name in chunk:
			row = parents.get(name)
			if row is None:
				fallback.append(name)
				continue
			doc = self._document(row, children)
			before = self._special()
			try:
				seen = self._visible(doc, shared)
			except ShimMiss:
				self.shim_miss += 1
				self.counts["shim_miss"] += 1
				self.flagged.append(name)
				fallback.append(name)
				continue
			if self._special() > before:
				self.flagged.append(name)
			self.batch_evaluated += 1
			if seen:
				visible.add(name)
		if fallback:
			visible |= _entry()._per_doc(self.doctype, set(fallback), self.ptype)
		return visible

	def _special(self) -> int:
		return sum(self.counts[branch] for branch in _SPECIAL)

	# Mirrors frappe/permissions.py:has_permission L126-226 (16.56.0, pin frappe.permissions.has_permission)
	# The `doc` branch only: get_doc_permissions, then a share, then `select` implied by `read`. Differences:
	# - Administrator, `share`, table, single and virtual doctypes never get here (the caller or the plan).
	# - A share is read for the whole chunk by `_shares`, not per document with `frappe.share.get_shared`.
	# - The `except` is the one of the per-document loop in `permissions.visible_names`: a document that raises
	#   `DoesNotExistError` or `PermissionError` is not visible. Frappe itself would let the error out.
	# - No `push_perm_check_log` and no `debug` log; they change no verdict.
	def _visible(self, doc, shared: dict[str, bool]) -> bool:
		"""One document's verdict, with Frappe's order: hooks, role, User Permission, shares, select -> read."""
		ptype = self.ptype
		try:  # one try per document, as the per-document loop has
			perm = self._verdict(doc, ptype)
			if not perm and ptype == "read":
				perm = self._shared(doc, shared)
			if not perm and ptype == "select":
				perm = self._verdict(doc, "read")
				if perm:
					self.counts["select_read"] += 1
				else:
					perm = self._shared(doc, shared)
			return bool(perm)
		except (frappe.DoesNotExistError, frappe.PermissionError):
			return False

	def _shared(self, doc, shared: dict[str, bool]) -> bool:
		if doc.name not in shared:
			return False
		self.counts["everyone_share" if shared[doc.name] else "share_grant"] += 1
		return True

	# Mirrors frappe/permissions.py:get_doc_permissions L236-275 (16.56.0, pin frappe.permissions.get_doc_permissions)
	# Hooks, role permissions, `if_owner` merge, User Permissions, in that order. Differences:
	# - Only the answer for `ptype` is computed: `submit` and `import` (L250-254) and `create` (L271) are not.
	# - A denial by hook returns False at once; Frappe returns `{ptype: 0}`, which is the same answer for `ptype`.
	# - `has_user_permission` is Frappe's own function, called on the stand-in; the branch counts are extra.
	def _verdict(self, doc, ptype: str) -> bool:
		if not self._hooks_allow(doc, ptype):
			return False
		owner = (doc.get("owner") or "").lower() == self.user.lower()
		perms, if_owner = self._permissions(owner)
		if not frappe.permissions.has_user_permission(doc, self.user, ptype=ptype):
			if doc.children:
				self.counts["denied_with_children"] += 1
			if self.count_branches:
				self._count_user_permission_denial(doc, ptype)
			perms = if_owner if owner else {}
			if owner and if_owner.get(ptype):
				self.counts["owner_only"] += 1
		return bool(perms.get(ptype))

	# Mirrors frappe/permissions.py:get_doc_permissions L243-264 (16.56.0, pin frappe.permissions.get_doc_permissions)
	# The deep copy of `get_role_permissions` and the `if_owner` merge. Difference: the result is kept for the
	# whole run, one per value of `owner`, because the user and the meta do not change inside a run.
	def _permissions(self, owner: bool) -> tuple[dict, dict]:
		"""The role permissions of the user for an owner or not, as `get_doc_permissions` merges them."""
		if owner not in self._role_perms:
			raw = frappe.permissions.get_role_permissions(self.layout.meta, user=self.user, is_owner=owner)
			merged = copy.deepcopy(raw)
			if merged.get("has_if_owner_enabled"):
				merged.update(merged.get("if_owner", {}))
			self._role_perms[owner] = (merged, merged.get("if_owner", {}))
		return self._role_perms[owner]

	# Mirrors frappe/permissions.py:has_controller_permissions L491-500 (16.56.0, pin frappe.permissions.has_controller_permissions)
	# Same hook order (`_hook_chain`), same dispatch (`frappe.call`), the first falsy answer denies. Differences:
	# - The inherited hook runs only for a name in `candidates`; for any other name it returns True by its code.
	# - The hooks that read values only through `get` run on the stand-in; any other hook gets a lazy real document.
	# - `DoesNotExistError` and `PermissionError` from a hook are counted and re-raised for `_visible`.
	def _hooks_allow(self, doc, ptype: str) -> bool:
		"""`has_controller_permissions`: raw hooks, same order, first falsy denies, same dispatch."""
		for method in self.chain:
			if method == _INHERITED_HOOK and doc.name not in self.candidates:
				continue
			target = doc if method in _SHIM_HOOKS or method == _INHERITED_HOOK else self._lazy(doc)
			try:
				allowed = _entry()._dispatch_hook(method, doc=target, ptype=ptype, user=self.user)
			except (frappe.DoesNotExistError, frappe.PermissionError):
				self.counts["hook_raise"] += 1
				raise
			if method == _INHERITED_HOOK:
				self.counts["candidate_allow" if allowed else "candidate_deny"] += 1
			if not allowed:
				self.counts["hook_deny"] += 1
				return False
		return True

	def _lazy(self, doc):
		_entry().check_permission_stop()
		if doc.lazy is None:
			self.counts["real_hook_call"] += 1
			doc.lazy = _entry()._lazy_doc(self.doctype, doc.name)
		return doc.lazy

	# Mirrors frappe/permissions.py:has_user_permission L432-478 (16.56.0, pin frappe.permissions.has_user_permission)
	# The parent link check, then each child row, with the empty-value rule of strict mode (L432). It only names
	# the branch for the counts: it never changes a verdict and the hot path (`count_branches=False`) skips it.
	def _count_user_permission_denial(self, doc, ptype: str) -> None:
		"""Name the branch of a denial: a child row, or a child row with an empty value (strict mode)."""
		if not doc.children:
			return
		has_permission = frappe.permissions.has_user_permission
		if not has_permission(doc.with_children([]), self.user, ptype=ptype):
			return
		self.counts["child_deny"] += 1
		filled = [row for row in doc.children if all(row.get(name) for name in row.up_links)]
		if self.strict and has_permission(doc.with_children(filled), self.user, ptype=ptype):
			self.counts["child_deny_empty"] += 1

	def _document(self, row: dict, children: dict[str, dict[str, list]]):
		layout = self.layout
		rows = []
		for table in layout.relevant:
			rows.extend(children[table.fieldname].get(row["name"], ()))
		return _PermDoc(layout, row, rows)

	# --- SQL reads --------------------------------------------------------------------------

	def _parent_columns(self) -> list[str]:
		layout = self.layout
		wanted = ["name", "owner", *layout.links]
		if _PARTY_HOOK in self.chain:
			wanted.append("restrict_to_companies")
		for link in self.masters:
			wanted += [link.fieldname, link.doctype_fieldname]
		return list(dict.fromkeys(name for name in wanted if name in layout.columns))

	# Mirrors frappe/model/document.py:Document.load_from_db L514-522 (16.56.0, pin frappe.model.document.Document.load_from_db)
	# The fast path reads `SELECT * ... WHERE name = %s`. Differences: one statement for the chunk, only the
	# columns that a verdict reads, and a name that the statement does not return (the database spells it
	# differently) goes to the per-document path.
	def _parent_rows(self, chunk: list[str]) -> dict[str, dict]:
		columns = ", ".join(f"`{name}`" for name in self.columns)
		rows = frappe.db.sql(
			f"SELECT {columns} FROM {self.layout.quoted} WHERE `name` IN %(names)s",
			{"names": tuple(chunk)},
			as_dict=True,
		)
		return {row["name"]: row for row in rows}

	# Mirrors frappe/model/document.py:Document._load_child_table_from_db L612-623 (16.56.0, pin frappe.model.document.Document._load_child_table_from_db)
	# `parent`, `parenttype`, `parentfield`, order by `idx`. Differences: one statement for the chunk (a join to
	# the parent rows, so the `parent` comparison keeps the database collation) and only the link columns.
	def _child_rows(self, table, chunk: list[str]) -> dict[str, list]:
		columns = "".join(f", c.`{name}`" for name in ("idx", "parentfield", *table.links))
		rows = frappe.db.sql(
			f"SELECT p.`name` AS `__parent`{columns} FROM {table.quoted} c "
			f"JOIN {self.layout.quoted} p ON c.`parent` = p.`name` "
			"WHERE p.`name` IN %(names)s AND c.`parenttype` = %(parenttype)s AND c.`parentfield` = %(parentfield)s "
			"ORDER BY c.`idx`",
			{"names": tuple(chunk), "parenttype": self.doctype, "parentfield": table.fieldname},
			as_dict=True,
		)
		grouped: dict[str, list] = {}
		for row in rows:
			grouped.setdefault(row.pop("__parent"), []).append(_PermRow(table, row))
		return grouped

	# Mirrors frappe/share.py:get_shared L196-214 (16.56.0, pin frappe.share.get_shared)
	# A `read` share of the doctype for the user, or for everyone unless the user is Guest (L201-203).
	# Difference: one statement for the chunk instead of one `get_shared(..., limit=1)` per document.
	def _shares(self, chunk: list[str]) -> dict[str, bool]:
		"""name -> True when every matching share is an `everyone` share."""
		everyone = "" if self.user == "Guest" else " OR s.`everyone` = 1"
		rows = frappe.db.sql(
			"SELECT p.`name` AS `name`, s.`everyone` AS `everyone` "
			f"FROM {_quoted('DocShare')} s JOIN {self.layout.quoted} p ON s.`share_name` = p.`name` "
			"WHERE p.`name` IN %(names)s AND s.`share_doctype` = %(doctype)s AND s.`read` = 1 "
			f"AND (s.`user` = %(user)s{everyone})",
			{"names": tuple(chunk), "doctype": self.doctype, "user": self.user},
			as_dict=True,
		)
		shared: dict[str, bool] = {}
		for row in rows:
			shared[row["name"]] = shared.get(row["name"], True) and bool(row["everyone"])
		return shared

	# The real hook is erpnext `company_restriction.has_inherited_permission` (ERPNext 16.55.0, L179-190): it
	# returns True unless a master that the document points at is restricted to other companies. A name that
	# points at no restricted master is therefore allowed without the call; the others are the candidates.
	def _candidates(self, chunk: list[str]) -> set[str]:
		"""Names that point at a master restricted to companies; the real hook decides only for these."""
		found: set[str] = set()
		for link in self.masters:
			if not has_restricted_row(link.doctype):
				continue
			dynamic = f", p.`{link.doctype_fieldname}` AS `__type`" if link.doctype_fieldname else ""
			rows = frappe.db.sql(
				f"SELECT p.`name` AS `name`{dynamic} FROM {self.layout.quoted} p "
				f"JOIN {_quoted(link.doctype)} m ON p.`{link.fieldname}` = m.`name` "
				"WHERE p.`name` IN %(names)s AND m.`restrict_to_companies` = 1",
				{"names": tuple(chunk)},
				as_dict=True,
			)
			found.update(
				row["name"] for row in rows if not link.doctype_fieldname or row["__type"] == link.doctype
			)
		return found


class _Record:
	"""Read-only stand-in for a document or a child row: it answers `get` exactly or raises `ShimMiss`."""

	__slots__ = ("_columns", "_nulls", "_tables", "_values", "doctype", "idx", "name")

	def __init__(self, doctype, values, columns, tables, nulls=frozenset()):
		self.doctype = doctype
		self._values = values
		self._columns = columns
		self._tables = tables
		self._nulls = nulls
		self.idx = values.get("idx")
		self.name = values.get("name")

	# Mirrors frappe/model/document.py:Document.get L2364-2380 (16.56.0, pin frappe.model.document.Document.get)
	# `self.__dict__.get(key)` for a plain key. Differences: only a value read from the database, or a key
	# that a real document holds as `None`, is answered; a dict key, `filters`, `limit`, `default` and a table
	# field (the `LazyDocument.get` descriptor, L3802-3806) cannot be answered and raise `ShimMiss`.
	def get(self, key, filters=None, limit=None, default=None):
		if filters is not None or limit is not None or default is not None or not isinstance(key, str):
			raise ShimMiss(str(key))
		if key == "doctype":
			return self.doctype
		if key in self._values:
			return self._values[key]
		# `None` only where a real document holds none: `__islocal`, `parentfield` of a parent, a Link field
		# without a column. A table, a column that was not read, and any other key cannot be answered.
		if key in self._nulls and key not in self._columns:
			return None
		raise ShimMiss(key)

	def __getattr__(self, attr):
		raise ShimMiss(attr)


class _PermDoc(_Record):
	__slots__ = ("_layout", "children", "lazy", "meta")

	def __init__(self, layout, values, children):
		super().__init__(layout.doctype, values, layout.columns, layout.table_names, layout.null_keys)
		self.meta = layout.meta
		self.children = children
		self.lazy = None
		self._layout = layout

	# Mirrors frappe/model/document.py:Document.get_all_children L1496-1511 (16.56.0, pin frappe.model.document.Document.get_all_children)
	# Difference: only the call that `has_user_permission` makes (`include_computed=True`, no `parenttype`) is
	# answered, with the rows of the tables that a User Permission can deny, in `idx` order.
	def get_all_children(self, parenttype=None, *, include_computed=False):
		if parenttype is not None or not include_computed:
			raise ShimMiss("get_all_children")
		return self.children

	def with_children(self, children):
		doc = _PermDoc(self._layout, self._values, children)
		doc.lazy = self.lazy
		return doc


class _PermRow(_Record):
	__slots__ = ("up_links",)

	def __init__(self, table, values):
		super().__init__(table.doctype, values, table.columns, table.nested, table.null_keys)
		self.up_links = table.up_links
