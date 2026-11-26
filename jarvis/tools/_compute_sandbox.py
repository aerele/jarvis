"""The sandbox behind the ``compute`` tool: read-only Python that runs as the chat user.

The model writes a short script that reads records and reports, then filters, groups,
sums and ranks them, and assigns the small answer to ``result``. This module compiles
and runs that script. ``jarvis.tools.compute`` is the tool around it.

Why not Frappe's own ``safe_exec``
----------------------------------
``frappe.utils.safe_exec.safe_exec`` refuses to run unless ``server_script_enabled``
is set for the whole bench, and its namespace (``get_safe_globals``) is built for
Server Scripts: it has ``frappe.db.sql``, ``get_all``, ``set_value``, ``enqueue``,
``sendmail``, HTTP helpers and document writes. This sandbox is far narrower: it is
read-only, every data function in it runs under the chat user's own permissions,
and its namespace is an allowlist built here. So it does not depend on that switch,
and it does not reuse that namespace. It reuses only Frappe's RestrictedPython
pieces (the ``FrappeTransformer`` compile policy and its attribute/item guards)
where the installed Frappe has them, with fallbacks for names that differ on
version 15.

What the script can reach
-------------------------
Exactly what ``build_namespace`` puts in its globals: permissioned wrappers over
``frappe.get_list`` / ``get_doc`` / ``get_value`` / ``count`` / ``exists`` /
``get_meta`` and Jarvis's own ``run_report`` path, safe date and number helpers,
``json.loads/dumps``, ``frappe._dict``, the user's name and default company, and a
small set of builtins. There is no ``frappe.db.sql``, query builder, ``get_all``,
write, job, mail, HTTP, file or ``frappe.local``. A name that is not there fails
with a message saying what to write instead.

Read-only, in depth
-------------------
1. The namespace offers no write path.
2. The run happens inside ``_preview_sandbox.preview_sandbox``: a savepoint that is
   ALWAYS rolled back, ``frappe.db.commit`` neutralised, a bare rollback redirected to
   the savepoint, and background jobs recorded instead of sent.
3. While the script runs, ``frappe.db.sql`` refuses every statement that is not a
   read (``_no_writes``) BEFORE it reaches the database, and the whole call fails,
   even when the code (or a report it ran) catches the error. A report that writes
   when it runs therefore cannot be used from compute.

``frappe.flags.read_only`` was considered and not used: on Frappe 16.51 (and 15) it
only changes the mode of the NEXT ``START TRANSACTION`` (``Database.begin``), which
cannot apply to the request's transaction that is already open, and it turns some
writes (Error Log, View Log) into deferred Redis inserts that are saved later,
outside any rollback. 16.51 has no ``frappe.db.read_only()`` context.

Limits
------
Constants below, each with its reason. A limit that is hit fails the whole call even
when the script catches the error (``_Run.fail`` raises a ``BaseException``, and the
failure is recorded on the run), so a partial total can never come back as a
complete one.

Time and memory are checked between steps: before every statement, for every item a
loop takes, after every read and after every big guarded operation. A single C-level
operation cannot be interrupted, so every operation that can grow a value in one step
has its result size worked out beforehand and is refused above a cap
(``_compute_guards``: arithmetic, %-formatting, f-string specs, ``pow``, ``round``,
``sum``, text and list methods); slice assignment is refused.

Why no hard process limit: ``RLIMIT_CPU`` and ``RLIMIT_AS`` apply to the whole worker
process, which serves other requests on other threads; the first kills the process, the
second makes other threads' allocations fail. A subprocess cannot share this request's
database connection, transaction and permission context, which every read here relies
on. Size pre-checks plus checks between steps are the boundary instead.

Lambdas tick at every call and the deadline watch (``_Watch``) stops any call into the
script at the deadline, so a callable driven from C (map, sorted, max with a key) cannot outrun it.

Not covered (bounded only by the worker): what a report's own Python does outside the
database (HTTP, email sent directly, a realtime event), and C work that none of the
guards in ``_compute_guards`` sizes (the module docstring lists what they cover).
"""

from __future__ import annotations

import ast
import datetime
import json
import re
import sys
import threading
import time
import types
from contextlib import contextmanager

import frappe
import RestrictedPython.Guards as _rp_guards
from RestrictedPython import compile_restricted
from RestrictedPython.transformer import IOPERATOR_TO_STR, RestrictingNodeTransformer, copy_locations

from jarvis.exceptions import ComputeCodeError, InvalidArgumentError, PermissionDeniedError
from jarvis.tools._compute_guards import (
	GuardRefused,
	Guards,
	check_nesting,
	check_written_size,
	guarded_type,
	internal_import,
)

try:  # Frappe 15 and 16: the policy safe_exec compiles with (allows the name ``_dict``)
	from frappe.utils.safe_exec import FrappeTransformer as _BasePolicy
except ImportError:  # pragma: no cover - an older Frappe
	_BasePolicy = RestrictingNodeTransformer

try:
	from frappe.utils.safe_exec import UNSAFE_ATTRIBUTES as _FRAPPE_UNSAFE_ATTRIBUTES
except ImportError:  # pragma: no cover
	_FRAPPE_UNSAFE_ATTRIBUTES = frozenset()

try:
	from frappe.utils.inplacevar import protected_inplacevar as _protected_inplacevar
except ImportError:  # pragma: no cover
	_protected_inplacevar = None


# --------------------------------------------------------------------------- #
# Limits                                                                       #
# --------------------------------------------------------------------------- #

# Longest script accepted. A finished computation is a few dozen lines; 20,000
# characters leaves room for long field lists while keeping the stored call (the
# audit trail keeps the code) and the compile cost small.
MAX_CODE_CHARS = 20_000

# The description is one sentence shown in the chat step.
MAX_DESCRIPTION_CHARS = 300

# Wall-clock budget for running the script. The plugin aborts a tool call after
# 30 s (src/frappe-client.ts); 20 s plus one statement that started just before the
# deadline (STATEMENT_TIME_S) still answers inside that abort.
WALL_CLOCK_S = 20

# Non-script frames (Frappe's own code under a read) are interrupted only this long
# after the deadline: a read that is nearly done returns to the script, which then
# stops cleanly between statements instead of in the middle of a database fetch.
GRACE_S = 2

# Per-statement database limit (MariaDB max_statement_time, put back afterwards). Under
# the agent's own 25 s read limit, and short enough that WALL_CLOCK_S + this stays
# under the plugin's 30 s abort.
STATEMENT_TIME_S = 8

# Rows all reads of one call may return together. The point of the tool is totals
# over every matching row; 200,000 rows covers a year of a busy site's invoices while
# bounding memory in a web worker (about a kilobyte a row with a handful of fields).
MAX_ROWS_TOTAL = 200_000

# Rows one read may return, so one careless fields=["*"] cannot take the whole budget.
MAX_ROWS_PER_READ = 100_000

# Captured print() output returned with the result; enough to debug, never a data dump.
MAX_PRINT_CHARS = 2_000

# range() longer than this is refused: no reasonable computation here counts to a
# million in Python, and sum(range(10**12)) would hold the worker in C code that no
# timer can interrupt.
MAX_RANGE = 1_000_000

# How much the worker's resident memory may grow while the script runs (Linux /proc
# only). Read before statements and loop items (at most every MEMORY_CHECK_INTERVAL_S),
# after every read and after every guarded operation whose result passes a megabyte.
# Every single step is bounded (one read: MAX_ROWS_PER_READ rows; one operation: the
# caps in _compute_guards), so the overshoot past this is at most one such step.
MAX_MEMORY_GROWTH_BYTES = 512 * 1024 * 1024
MEMORY_CHECK_INTERVAL_S = 0.005

# Read receipts returned with the result: the doctype, row count and currency of each
# read, and its filters cut at RECEIPT_TEXT_CHARS. 15 covers a sensible script; a loop
# of reads is summarised as {"more": n} instead of costing the model tokens.
MAX_READ_RECEIPTS = 15
RECEIPT_TEXT_CHARS = 100

# The filename the compiled script carries: the deadline watch and error line numbers key on it.
SCRIPT_FILENAME = "<jarvis-compute>"


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


class _Stop(BaseException):
	"""A limit or a refused write stopped the run. A BaseException, so the script's
	``except Exception`` cannot swallow it; ``_Run.failure`` holds the message, so a
	bare ``except:`` cannot hide it either."""


class SandboxUsageError(Exception):
	"""The script used a helper wrongly (a keyword that does not exist, an unknown
	doctype). Becomes a fixable ComputeCodeError naming the line."""


class _Missing:
	pass


_MISSING = _Missing()


# --------------------------------------------------------------------------- #
# One run                                                                      #
# --------------------------------------------------------------------------- #


def _rss_bytes() -> int | None:
	"""Resident memory of this process, or None where /proc is not there."""
	try:
		# nosemgrep: frappe-security-file-traversal -- fixed kernel path, no caller input
		with open("/proc/self/statm") as f:
			return int(f.read().split()[1]) * _PAGE_SIZE
	except Exception:
		return None


try:
	import os as _os

	_PAGE_SIZE = _os.sysconf("SC_PAGE_SIZE")
except Exception:  # pragma: no cover
	_PAGE_SIZE = 4096


class _Run:
	"""The state of one compute call: limits, counters, captured output."""

	def __init__(self, *, wall_clock_s: float = WALL_CLOCK_S):
		self.started = time.monotonic()
		self.deadline = self.started + wall_clock_s
		self.wall_clock_s = wall_clock_s
		self.rows_read = 0
		self.failure: str | None = None
		self.failure_kind = "limit"  # "limit" (fixable) or "write" (not fixable)
		self.printed: list[str] = []
		self.printed_chars = 0
		self.print_truncated = False
		self.rss_at_start = _rss_bytes()
		self.last_memory_check = self.started
		# What the script is doing outside its own lines (a report run), for the
		# deadline message.
		self.activity: str | None = None
		# What the script read (receipts, capped) and the caveats it must act on.
		self.reads: list[dict] = []
		self.reads_more = 0
		self.warnings: list[str] = []
		self._warned: set = set()
		self.last_receipt: dict = {}
		# Currencies of the amounts read record by record (get_doc / get_value), per doctype.
		self.record_units: dict[str, set] = {}

	def note_record_units(self, doctype: str, units: set):
		"""Warn once when amounts read record by record from ``doctype`` are in more than one
		currency: adding them up would mix units."""
		seen = self.record_units.setdefault(doctype, set())
		seen |= units
		if len(seen) > 1:
			names = sorted(seen)
			listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
			self.warn(
				("records", doctype),
				f"This code read amounts from {doctype} records in {listed} (get_doc / get_value). "
				"Amounts in different currencies cannot be added: use the base_ fields or total per currency.",
			)

	def record_read(self, receipt: dict) -> dict:
		"""Keep a read's receipt (the first MAX_READ_RECEIPTS; later ones are counted)."""
		self.last_receipt = receipt
		if len(self.reads) < MAX_READ_RECEIPTS:
			self.reads.append(receipt)
		else:
			self.reads_more += 1
		return receipt

	def receipts(self) -> list[dict]:
		return self.reads + ([{"more": self.reads_more}] if self.reads_more else [])

	def warn(self, key, message: str):
		"""Add a warning once per ``key`` (e.g. doctype + field) for the whole call."""
		if key in self._warned:
			return
		self._warned.add(key)
		self.warnings.append(message)

	def tick(self):
		"""Called before every statement of the script and for every item a loop or
		comprehension takes (compiled in, see _ComputePolicy). A plain call, so it keeps
		stopping the script however often the script catches it: in a loop, the tick
		before the first statement of the body sits outside any try in that body.
		Memory is read at most every MEMORY_CHECK_INTERVAL_S."""
		now = time.monotonic()
		if now > self.deadline:
			self.check_time()
		if now - self.last_memory_check >= MEMORY_CHECK_INTERVAL_S:
			self.last_memory_check = now
			self.check_memory()

	def fail(self, message: str, *, kind: str = "limit"):
		"""Record the first failure and stop the script."""
		if self.failure is None:
			self.failure = message
			self.failure_kind = kind
		raise _Stop(message)

	def check_time(self, grace: float = 0):
		if time.monotonic() <= self.deadline + grace:
			return
		if self.activity:
			self.fail(
				f"{self.activity} did not finish within the {self.wall_clock_s:g} seconds compute has "
				"and was stopped. Narrow its filters (a shorter date range, one company or party) and "
				"run it again."
			)
		self.fail(
			f"The code ran for more than {self.wall_clock_s:g} seconds and was stopped. Read less "
			"(filter by date or status, ask only for the fields you need), let the database total "
			'with group_by and {"SUM": ...} fields, and avoid loops inside loops.'
		)

	def check_memory(self):
		if self.rss_at_start is None:
			return
		now = _rss_bytes()
		if now is not None and now - self.rss_at_start > MAX_MEMORY_GROWTH_BYTES:
			self.fail(
				"The code used too much memory and was stopped. Keep running totals instead of "
				"building large lists, and read only the fields you need."
			)

	def take_rows(self, n: int, what: str):
		"""Count ``n`` rows read by ``what``; fail when the call's total budget is gone.
		A read is the biggest single step a script takes, so memory is checked after it."""
		self.check_memory()
		self.rows_read += n
		if self.rows_read > MAX_ROWS_TOTAL:
			self.fail(
				f"The reads in this code returned more than {MAX_ROWS_TOTAL:,} rows in total (the cap "
				f"was crossed by {what}). Filter the reads, or let the database aggregate "
				'(group_by with {"SUM": "field", "as": "total"} fields) so each read returns a few rows.'
			)

	def remaining_rows(self) -> int:
		return max(0, MAX_ROWS_TOTAL - self.rows_read)

	def add_print(self, text: str):
		if self.print_truncated:
			return
		room = MAX_PRINT_CHARS - self.printed_chars
		if len(text) > room:
			text = text[: max(room, 0)]
			self.print_truncated = True
		self.printed.append(text)
		self.printed_chars += len(text)

	def printed_text(self) -> str:
		text = "".join(self.printed)
		return text + ("\n[print output cut at 2,000 characters]" if self.print_truncated else "")

	def elapsed_ms(self) -> int:
		return int((time.monotonic() - self.started) * 1000)


# --------------------------------------------------------------------------- #
# Compile                                                                      #
# --------------------------------------------------------------------------- #

# Generated names: only the transformer below emits them; check_name refuses any
# name starting with "_" in the script itself.
_AUG_ITEM = "_jv_aug_item_"
_BINOP = "_jv_binop_"
_FORMAT = "_jv_format_"
_TICK = "_jv_tick_"
_COMPARE = "_jv_cmp_"
_KEY = "_jv_key_"
_NEST = "_jv_nest_"
_BUILD = "_jv_build_"
_BUILD_DICT = "_jv_build_dict_"
_CALL = "_jv_call_"
_GUARDED_BINOPS = {
	ast.Add: "+",
	ast.Mult: "*",
	ast.Pow: "**",
	ast.LShift: "<<",
	ast.Mod: "%",
	ast.FloorDiv: "//",
	ast.BitOr: "|",
	ast.BitAnd: "&",
	ast.Sub: "-",
	ast.BitXor: "^",
}
# Statement lists a tick is put in front of.
_BODY_FIELDS = ("body", "orelse", "finalbody")


def _call(name: str, args: list, node: ast.AST) -> ast.Call:
	call = ast.Call(func=ast.Name(name, ast.Load()), args=args, keywords=[])
	copy_locations(call, node)
	return ast.fix_missing_locations(call)


def _tuple(elts: list, node: ast.AST) -> ast.Tuple:
	t = ast.Tuple(elts=elts, ctx=ast.Load())
	copy_locations(t, node)
	return ast.fix_missing_locations(t)


def _is_scalar_constant(node: ast.AST) -> bool:
	"""A literal that is no container (a number, text, None, True / False, an f-string)."""
	if isinstance(node, ast.Constant | ast.JoinedStr):
		return True
	return isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)


def _is_constant(node: ast.AST) -> bool:
	"""A scalar literal, or a list / tuple / set display of scalar literals: comparing
	anything with one costs at most its length."""
	if _is_scalar_constant(node):
		return True
	return isinstance(node, ast.List | ast.Tuple | ast.Set) and all(_is_scalar_constant(e) for e in node.elts)


def _checked(node: ast.AST) -> bool:
	"""An expression the policy already routed through a nesting check."""
	return (
		isinstance(node, ast.Call)
		and isinstance(node.func, ast.Name)
		and node.func.id in (_NEST, _BUILD, _BUILD_DICT)
	)


def _stores_into_object(target: ast.AST) -> bool:
	"""Whether an assignment target writes into a container or object (``d[k]``,
	``row.x``), also inside an unpacking target."""
	return any(isinstance(n, ast.Subscript | ast.Attribute) for n in ast.walk(target))


class _ComputePolicy(_BasePolicy):
	"""Frappe's RestrictedPython policy, plus the ordinary Python a computation needs.

	- ``d[k] += v`` (RestrictedPython refuses augmented assignment of items) becomes
	  ``_jv_aug_item_(_write_(d), k, "+=", v)``: d and k are evaluated once, the item is
	  read through the item guard and written back through the write guard.
	- ``a + b``, ``a * b``, ``a ** b``, ``a << b`` and ``a % b`` become
	  ``_jv_binop_(op, a, b)``, and every f-string replacement field becomes
	  ``_jv_format_(value, conversion, spec)``: the result size is worked out before the
	  operation and refused above the caps in ``_compute_guards``. Each is one C-level
	  operation no timer can interrupt.
	- Every statement, in every block, is preceded by ``_jv_tick_()`` (``_Run.tick``),
	  the wall-clock and memory check. (Every item a loop or comprehension takes ticks
	  too, through ``_getiter_``.) Not a trace function: CPython drops a trace function
	  the first time it raises, so a loop that catches that one exception would then run
	  unchecked.
	- Slice assignment (``l[n:] = l`` doubles a list in one step) is refused.
	- Starred parts are read through the item cap before anything is allocated:
	  ``[*a, *b]`` / ``(*a,)`` / ``{*a}`` become ``_jv_build_(kind, (starred, value), ...)``,
	  ``{**d, k: v}`` becomes ``_jv_build_dict_(...)`` and a call with ``*args`` or
	  ``**kwargs`` becomes ``_jv_call_(func, ...)`` (instead of RestrictedPython's
	  ``_apply_``, which only sees the arguments after Python has unpacked them).
	- Nesting is bounded where a container is built: every list / tuple / dict display and
	  every comprehension result goes through ``_jv_nest_``, and so does every value stored
	  into a container or object (``d[k] = v``, ``row.x = v``). A loop, comprehension,
	  ``with`` or unpacking target cannot store into one (``for d[k] in ...``).
	- Set display items, set comprehension items and dict display / comprehension keys
	  are cost-checked as keys before C hashes and compares them.
	"""

	def _nest(self, node: ast.AST) -> ast.AST:
		return node if _is_constant(node) else _call(_NEST, [node], node)

	def _key(self, node: ast.AST) -> ast.AST:
		return node if _is_scalar_constant(node) else _call(_KEY, [node], node)

	def _refuse_object_target(self, target: ast.AST, what: str):
		if _stores_into_object(target):
			self.error(
				target,
				f"{what} cannot store into an item or attribute: assign to a name, then store it "
				"in the body (d[k] = value).",
			)

	def visit_List(self, node: ast.List):
		node = super().visit_List(node)
		if not isinstance(node.ctx, ast.Load):
			return node
		if any(isinstance(e, ast.Starred) for e in node.elts):
			return self._build("list", node)
		return self._nest(node)

	def visit_Tuple(self, node: ast.Tuple):
		node = super().visit_Tuple(node)
		if not isinstance(node.ctx, ast.Load) or any(isinstance(e, ast.Slice) for e in node.elts):
			return node
		if any(isinstance(e, ast.Starred) for e in node.elts):
			return self._build("tuple", node)
		return self._nest(node)

	def visit_Set(self, node: ast.Set):
		node = super().visit_Set(node)
		if _is_constant(node):
			return node
		return self._build("set", node)

	def _build(self, kind: str, node):
		parts = [
			_tuple([ast.Constant(1), e.value], e)
			if isinstance(e, ast.Starred)
			else _tuple([ast.Constant(0), e], e)
			for e in node.elts
		]
		return _call(_BUILD, [ast.Constant(kind), *parts], node)

	def visit_Dict(self, node: ast.Dict):
		node = super().visit_Dict(node)
		if any(k is None for k in node.keys):
			parts = [
				_tuple([ast.Constant(1), v], v) if k is None else _tuple([ast.Constant(0), k, v], k)
				for k, v in zip(node.keys, node.values, strict=True)
			]
			return _call(_BUILD_DICT, parts, node)
		node.keys = [self._key(k) for k in node.keys]
		if all(_is_constant(v) for v in node.values):
			return node
		return _call(_NEST, [node], node)

	def visit_ListComp(self, node: ast.ListComp):
		node = super().visit_ListComp(node)
		return node if _is_constant(node.elt) else _call(_NEST, [node], node)

	def visit_SetComp(self, node: ast.SetComp):
		node = super().visit_SetComp(node)
		if _is_constant(node.elt):
			return node
		node.elt = _call(_KEY, [node.elt], node.elt)
		return _call(_NEST, [node], node)

	def visit_DictComp(self, node: ast.DictComp):
		node = super().visit_DictComp(node)
		node.key = self._key(node.key)
		return node if _is_constant(node.value) else _call(_NEST, [node], node)

	def visit_comprehension(self, node: ast.comprehension):
		self._refuse_object_target(node.target, "A comprehension's loop variable")
		return super().visit_comprehension(node)

	def visit_For(self, node: ast.For):
		self._refuse_object_target(node.target, "A for loop's variable")
		return super().visit_For(node)

	def visit_withitem(self, node: ast.withitem):
		if node.optional_vars is not None:
			self._refuse_object_target(node.optional_vars, "with ... as")
		return super().visit_withitem(node)

	def visit_Assign(self, node: ast.Assign):
		for target in node.targets:
			if isinstance(target, ast.Tuple | ast.List):
				self._refuse_object_target(target, "Unpacking")
		stores = any(_stores_into_object(t) for t in node.targets)
		result = super().visit_Assign(node)
		if stores:
			for new in result if isinstance(result, list) else [result]:
				if isinstance(new, ast.Assign) and not _is_constant(new.value) and not _checked(new.value):
					new.value = _call(_NEST, [new.value], new.value)
		return result

	def visit_Call(self, node: ast.Call):
		node = super().visit_Call(node)
		if not (isinstance(node.func, ast.Name) and node.func.id == "_apply_"):
			return node
		# RestrictedPython made this _apply_(func, *args, **kwargs): Python would unpack
		# every starred part before _apply_ runs. Pass the parts instead.
		func, *args = node.args
		parts = [
			_tuple([ast.Constant(1), a.value], a)
			if isinstance(a, ast.Starred)
			else _tuple([ast.Constant(0), a], a)
			for a in args
		]
		for kw in node.keywords:
			if kw.arg is None:
				parts.append(_tuple([ast.Constant(3), kw.value], kw.value))
			else:
				parts.append(_tuple([ast.Constant(2), ast.Constant(kw.arg), kw.value], kw.value))
		return _call(_CALL, [func, *parts], node)

	def visit_Module(self, node: ast.Module):
		node = super().visit_Module(node)
		_add_ticks(node)
		return node

	def visit_Subscript(self, node: ast.Subscript):
		if isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Slice):
			self.error(node, "Slice assignment is not available: build a new list instead.")
			return node
		node = super().visit_Subscript(node)
		# A stored or deleted key is hashed and compared in C: its cost is checked first
		# (a read goes through _getitem_, which checks it there).
		if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
			node.slice = _call(_KEY, [node.slice], node)
		return node

	def visit_Lambda(self, node: ast.Lambda):
		# A lambda body is an expression: (tick, body)[1] checks time and memory on
		# every call, so map / sorted / max over a lambda cannot outrun the deadline.
		node = super().visit_Lambda(node)
		body = ast.Subscript(
			value=ast.Tuple(elts=[_call(_TICK, [], node), node.body], ctx=ast.Load()),
			slice=ast.Constant(1),
			ctx=ast.Load(),
		)
		copy_locations(body, node.body)
		node.body = ast.fix_missing_locations(body)
		return node

	def visit_Compare(self, node: ast.Compare):
		# Every operand is cost-checked before C compares it (deep shared structures,
		# long lists of long text). The right side of ``in`` is a lookup when it is a
		# dict or set; ``is`` compares nothing.
		# A comparison with a literal (``r.status == "Paid"``, ``x in ("a", "b")``) costs
		# at most the literal's length whatever the other side is, so it is left alone.
		node = super().visit_Compare(node)
		operands = [node.left, *node.comparators]
		need = [False] * len(operands)
		membership = [True] * len(operands)
		for i, op in enumerate(node.ops):
			if isinstance(op, ast.Is | ast.IsNot):
				continue
			if _is_constant(operands[i]) or _is_constant(operands[i + 1]):
				continue
			need[i] = need[i + 1] = True
			membership[i] = False
			if not isinstance(op, ast.In | ast.NotIn):
				membership[i + 1] = False
		wrapped = [
			_call(_COMPARE, [o, ast.Constant(1 if membership[i] else 0)], node) if need[i] else o
			for i, o in enumerate(operands)
		]
		node.left, node.comparators = wrapped[0], wrapped[1:]
		return node

	def visit_FormattedValue(self, node: ast.FormattedValue):
		node = super().visit_FormattedValue(node)
		spec = node.format_spec if node.format_spec is not None else ast.Constant(None)
		new = ast.FormattedValue(
			value=_call(_FORMAT, [node.value, ast.Constant(node.conversion), spec], node),
			conversion=-1,
			format_spec=None,
		)
		copy_locations(new, node)
		return new

	def visit_AugAssign(self, node: ast.AugAssign):
		if not isinstance(node.target, ast.Subscript):
			return super().visit_AugAssign(node)
		if isinstance(node.target.slice, ast.Slice):
			self.error(node, "Augmented assignment of a slice is not available: assign the slice instead.")
			return node
		node = self.node_contents_visit(node)
		target = node.target  # value is now _write_(<obj>)
		new = ast.Expr(
			value=_call(
				_AUG_ITEM,
				[target.value, target.slice, ast.Constant(IOPERATOR_TO_STR[type(node.op)]), node.value],
				node,
			)
		)
		copy_locations(new, node)
		return new

	def visit_Attribute(self, node: ast.Attribute):
		# frappe._dict, as Server Scripts write it; every other "_" attribute stays refused.
		if node.attr == "_dict" and isinstance(node.ctx, ast.Load):
			node = self.node_contents_visit(node)
			return _call("_getattr_", [node.value, ast.Constant("_dict")], node)
		return super().visit_Attribute(node)

	def visit_BinOp(self, node: ast.BinOp):
		node = super().visit_BinOp(node)
		op = _GUARDED_BINOPS.get(type(node.op))
		if op is None:
			return node
		return _call(_BINOP, [ast.Constant(op), node.left, node.right], node)


def _add_ticks(tree: ast.AST) -> None:
	"""Put ``_jv_tick_()`` in front of every statement of every block of ``tree``.
	Runs on RestrictedPython's output: the generated name is never name-checked."""
	for node in ast.walk(tree):
		blocks = [getattr(node, name, None) for name in _BODY_FIELDS]
		if isinstance(node, ast.Try | getattr(ast, "TryStar", ast.Try)):
			blocks += [handler.body for handler in node.handlers]
		if isinstance(node, ast.Match):
			blocks += [case.body for case in node.cases]
		for body in blocks:
			if not isinstance(body, list) or not body or not isinstance(body[0], ast.stmt):
				continue
			ticked = []
			for stmt in body:
				tick = ast.Expr(value=_call(_TICK, [], stmt))
				copy_locations(tick, stmt)
				ticked += [tick, stmt]
			body[:] = ticked


def compile_code(code: str):
	"""The RestrictedPython code object for ``code``; ComputeCodeError on any error."""
	try:
		return compile_restricted(code, filename=SCRIPT_FILENAME, mode="exec", policy=_ComputePolicy)
	except SyntaxError as e:
		raise ComputeCodeError(_compile_message(e)) from None


def _compile_message(e: SyntaxError) -> str:
	# RestrictedPython raises SyntaxError(<tuple of "Line N: message" strings>).
	raw = e.msg if e.msg else (e.args[0] if e.args else str(e))
	lines = [str(x).strip() for x in raw] if isinstance(raw, list | tuple) else [str(raw).strip()]
	body = "; ".join(line for line in lines[:10] if line)
	if e.lineno and not body.lower().startswith("line"):
		body = f"line {e.lineno}: {body}"
	return f"The code does not compile: {body}"


# --------------------------------------------------------------------------- #
# Static checks: teach before running                                          #
# --------------------------------------------------------------------------- #

_READ_ONLY_HINT = (
	"compute is read-only: it cannot create, change, submit or delete records. Work out the "
	"answer here, then propose any change with the write tools."
)
_SQL_HINT = (
	"is not available: use get_list with filters, and let the database total with group_by and "
	'{"SUM": "field", "as": "total"} fields, or aggregate the rows in Python.'
)

# frappe.<name> (and frappe.db.<name>) that are NOT offered, with what to write instead.
_FORBIDDEN_FRAPPE = {
	"db.sql": "frappe.db.sql " + _SQL_HINT,
	"db.sql_list": "frappe.db.sql_list " + _SQL_HINT,
	"db.multisql": "frappe.db.multisql " + _SQL_HINT,
	"qb": "frappe.qb (the query builder) " + _SQL_HINT,
	"get_all": "frappe.get_all ignores permissions and is not available: use get_list, which here "
	"returns every matching row you can read (no limit needed).",
	"db.get_all": "frappe.db.get_all ignores permissions and is not available: use get_list, which "
	"here returns every matching row you can read.",
	"db.get_values": "frappe.db.get_values is not available: use get_list with filters and fields.",
	"get_cached_doc": "frappe.get_cached_doc is not available: use get_doc(doctype, name).",
	"get_last_doc": "frappe.get_last_doc is not available: use get_list(..., order_by='creation desc', "
	"limit=1) and then get_doc.",
	"get_single": "frappe.get_single is not available: use get_doc(doctype) for a single doctype.",
	"enqueue": "compute cannot start background jobs.",
	"enqueue_doc": "compute cannot start background jobs.",
	"sendmail": "compute cannot send email.",
	"call": "frappe.call is not available: compute runs only the listed read helpers (use run_report "
	"for a saved report).",
	"get_attr": "frappe.get_attr is not available: compute runs only the listed read helpers.",
	"make_get_request": "compute cannot reach the network.",
	"make_post_request": "compute cannot reach the network.",
	"integrations": "compute cannot reach the network.",
	"local": "frappe.local is not available: use frappe.session.user for the user's name.",
	"flags": "frappe.flags is not available.",
	"conf": "site configuration is not available.",
	"cache": "the cache is not available.",
	"form_dict": "request data is not available.",
	"request": "request data is not available.",
	"response": "request data is not available.",
	"get_hooks": "frappe.get_hooks is not available.",
}
for _name in (
	"db.set_value",
	"db.set_single_value",
	"set_value",
	"db.delete",
	"db.insert",
	"db.commit",
	"db.rollback",
	"db.truncate",
	"db.bulk_insert",
	"new_doc",
	"delete_doc",
	"rename_doc",
	"copy_doc",
	"get_mapped_doc",
	"db.add_index",
):
	_FORBIDDEN_FRAPPE[_name] = f"frappe.{_name}: " + _READ_ONLY_HINT

# Builtins that are deliberately missing.
_FORBIDDEN_NAMES = {
	"open": "files are not available in compute.",
	"eval": "eval is not available in compute.",
	"exec": "exec is not available in compute.",
	"compile": "compile is not available in compute.",
	"__import__": "imports are not available: frappe, utils, json and the listed helpers are already in scope.",
	"getattr": "getattr is not available: use row.get('field') or row['field'].",
	"setattr": "setattr is not available: use row['field'] = value.",
	"hasattr": "hasattr is not available: use 'field' in row.",
	"delattr": "delattr is not available.",
	"globals": "globals is not available.",
	"locals": "locals is not available.",
	"vars": "vars is not available.",
	"dir": "dir is not available: get_meta(doctype) lists a doctype's fields.",
	"type": "type is not available: use isinstance(value, (int, float)).",
	"input": "input is not available.",
	"breakpoint": "breakpoint is not available.",
	"help": "help is not available.",
	"memoryview": "memoryview is not available.",
	"object": "object is not available.",
	"super": "super is not available.",
	"frozenset": "frozenset is not available: use set.",
	"bytes": "bytes is not available.",
	"bytearray": "bytearray is not available.",
}

_IMPORT_MESSAGE = (
	"No imports: frappe, utils, json, date helpers (getdate, add_days ...), flt/cint and the "
	"read helpers (get_list, get_doc, run_report ...) are already in scope."
)


def _dotted(node: ast.AST) -> str | None:
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return ".".join(reversed(parts))
	return None


def _bound_names(tree: ast.AST) -> set[str]:
	"""Every name the script binds anywhere (scoping ignored: a loose union)."""
	names: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
			names.add(node.id)
		elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
			names.add(node.name)
		elif isinstance(node, ast.arg):
			names.add(node.arg)
		elif isinstance(node, ast.ExceptHandler) and node.name:
			names.add(node.name)
		elif isinstance(node, ast.alias):
			names.add((node.asname or node.name).split(".")[0])
		elif isinstance(node, ast.Global | ast.Nonlocal):
			names.update(node.names)
		elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
			names.add(node.name)
	return names


def static_errors(tree: ast.AST, available: set[str], frappe_names: dict[str, set[str]]) -> list[str]:
	"""Teaching messages for what the script uses that the sandbox does not offer.
	``frappe_names`` maps "" / "db" / "utils" to the attribute names offered there."""
	errors: list[str] = []
	seen: set[str] = set()
	bound = _bound_names(tree)

	def add(message: str):
		if message not in seen:
			seen.add(message)
			errors.append(message)

	for node in ast.walk(tree):
		line = getattr(node, "lineno", "?")
		if isinstance(node, ast.Import | ast.ImportFrom):
			add(f"line {line}: {_IMPORT_MESSAGE}")
		elif isinstance(node, ast.ClassDef):
			add(f"line {line}: class definitions are not available: use dicts and functions.")
		elif isinstance(node, ast.Attribute):
			if node.attr.startswith("_") and node.attr != "_dict":
				add(f"line {line}: attributes starting with _ are not available ({node.attr}).")
				continue
			if node.attr in ("format", "format_map"):
				add(f'line {line}: .{node.attr}() is not available: use an f-string, e.g. f"{{total:,.2f}}".')
				continue
			dotted = _dotted(node)
			if not dotted or not dotted.startswith("frappe."):
				continue
			rest = dotted.split(".")[1:]
			for depth in (2, 1):
				key = ".".join(rest[:depth])
				if len(rest) >= depth and key in _FORBIDDEN_FRAPPE:
					add(f"line {line}: {_FORBIDDEN_FRAPPE[key]}")
					break
			else:
				first = rest[0]
				if first not in frappe_names[""]:
					add(
						f"line {line}: frappe.{first} is not available here. On frappe: "
						f"{', '.join(sorted(frappe_names['']))}."
					)
				elif first in ("db", "utils") and len(rest) > 1 and rest[1] not in frappe_names[first]:
					add(
						f"line {line}: frappe.{first}.{rest[1]} is not available here. On frappe.{first}: "
						f"{', '.join(sorted(frappe_names[first]))}."
					)
		elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
			name = node.id
			if name in _FORBIDDEN_NAMES and name not in bound:
				add(f"line {line}: {_FORBIDDEN_NAMES[name]}")
			elif name.startswith("_") and name != "_dict":
				add(f"line {line}: names starting with _ are not available ({name}).")
			elif name not in available and name not in bound and name not in ("printed", "print"):
				add(
					f"line {line}: name '{name}' is not defined here. Available: {_available_text(available)}."
				)
	return errors


def _available_text(available: set[str]) -> str:
	return ", ".join(
		sorted(n for n in available if not n.startswith("_") and n not in _PLAIN_BUILTIN_NAMES)
	) + (
		", and the usual builtins (len, sum, min, max, sorted, round, abs, any, all, enumerate, zip, "
		"range, set, dict, list, tuple, str, int, float, bool)"
	)


# --------------------------------------------------------------------------- #
# Guards                                                                       #
# --------------------------------------------------------------------------- #

_UNSAFE_ATTRIBUTES = frozenset(_FRAPPE_UNSAFE_ATTRIBUTES) | {
	"gi_frame",
	"gi_code",
	"gi_yieldfrom",
	"cr_frame",
	"cr_code",
	"cr_origin",
	"cr_await",
	"ag_code",
	"ag_frame",
	"tb_frame",
	"tb_next",
	"f_back",
	"f_builtins",
	"f_code",
	"f_globals",
	"f_locals",
	"f_trace",
	"format",
	"format_map",
}
_BLOCKED_TYPES = (types.ModuleType, types.CodeType, types.TracebackType, types.FrameType)


def _getattr_factory(guards: Guards):
	def _getattr(obj, name, *default):
		"""Attribute reads. Unlike Frappe's guard, a missing attribute RAISES instead of
		reading as None: a misspelled field must not silently total as zero. Methods that
		grow their object (str.ljust, list.extend ...) come back size-guarded."""
		if isinstance(obj, _Namespace) and name == "_dict":
			return obj.lookup(name)
		if not isinstance(name, str) or name.startswith("_"):
			raise AttributeError(f'"{name}" is not available: attributes starting with _ are blocked.')
		if isinstance(obj, _Namespace):
			return obj.lookup(name)
		if name in _UNSAFE_ATTRIBUTES:
			if name in ("format", "format_map"):
				raise AttributeError(f'.{name}() is not available: use an f-string, e.g. f"{{total:,.2f}}".')
			raise AttributeError(f'"{name}" is not available.')
		if isinstance(obj, _BLOCKED_TYPES):
			raise AttributeError(f'"{name}" is not available.')
		guarded = guards.method(obj, name)
		if guarded is not None:
			return guarded
		value = getattr(obj, name, *default)
		if isinstance(value, _BLOCKED_TYPES):
			raise AttributeError(f'"{name}" is not available.')
		return value

	return _getattr


def _getitem(obj, key):
	if isinstance(key, str) and key.startswith("_"):
		raise KeyError(f"keys starting with _ are not available ({key})")
	return obj[key]


def _getitem_factory(guards: Guards):
	def _getitem_(obj, key):
		guards.key(key)
		return _getitem(obj, key)

	return _getitem_


def _write(obj):
	if isinstance(obj, _Namespace | type | types.FunctionType | types.MethodType | types.BuiltinFunctionType):
		raise TypeError("This object cannot be changed.")
	if isinstance(obj, _BLOCKED_TYPES):
		raise TypeError("This object cannot be changed.")
	return obj


def _write_factory(guards: Guards):
	def _write_(obj):
		_write(obj)
		guards.invalidate(obj)  # its cached comparison cost no longer holds
		return obj

	return _write_


def _plain_inplace(op: str, x, y):
	import operator

	ops = {
		"+=": operator.iadd,
		"-=": operator.isub,
		"*=": operator.imul,
		"/=": operator.itruediv,
		"//=": operator.ifloordiv,
		"%=": operator.imod,
		"**=": operator.ipow,
		"<<=": operator.ilshift,
		">>=": operator.irshift,
		"&=": operator.iand,
		"^=": operator.ixor,
		"|=": operator.ior,
	}
	return ops[op](x, y)


def _raw_inplace(op: str, x, y):
	if _protected_inplacevar is not None:
		return _protected_inplacevar(op, x, y)
	return _plain_inplace(op, x, y)


def _inplace_factory(guards: Guards):
	def _inplacevar(op: str, x, y):
		return guards.inplace(op, x, y, _raw_inplace)

	def _aug_item(obj, key, op: str, value):
		"""``obj[key] op= value``; ``obj`` already went through the write guard."""
		obj[key] = _inplacevar(op, _getitem(obj, key), value)

	return _inplacevar, _aug_item


def _range(*args):
	try:
		r = range(*args)
		size = len(r)
	except OverflowError:
		size = MAX_RANGE + 1
		r = None
	if size > MAX_RANGE:
		raise SandboxUsageError(f"range() over {MAX_RANGE:,} numbers is not available.")
	return r


class _TickingIterator:
	"""What ``for`` and comprehensions iterate in the script: the real iterator, with
	``_Run.tick`` before every item."""

	__slots__ = ("_it", "_tick")

	def __init__(self, it, tick):
		self._it = it
		self._tick = tick

	def __iter__(self):
		return self

	def __next__(self):
		self._tick()
		return next(self._it)


def _getiter_factory(run: _Run):
	def _getiter(obj):
		return _TickingIterator(iter(obj), run.tick)

	return _getiter


def _print_factory(run: _Run):
	class _Printer:
		"""RestrictedPython's print collector, writing into the run's capped buffer."""

		def __init__(self, _getattr_=None):
			pass

		def write(self, text):
			run.add_print(str(text))

		def __call__(self):
			return run.printed_text()

		def _call_print(self, *objects, **kwargs):
			for value in objects:
				check_written_size(value, "print()")
			kwargs = {k: v for k, v in kwargs.items() if k in ("sep", "end")}
			print(*objects, file=self, **kwargs)

	return _Printer


# --------------------------------------------------------------------------- #
# Namespace objects                                                            #
# --------------------------------------------------------------------------- #


class _Namespace:
	"""A read-only bag of names (``frappe``, ``frappe.db``, ``json`` ...). Unknown
	names fail with what to write instead; nothing can be assigned to it."""

	__slots__ = ("_label", "_names", "_teach")

	def __init__(self, label: str, names: dict, teach: dict | None = None):
		object.__setattr__(self, "_label", label)
		object.__setattr__(self, "_names", dict(names))
		object.__setattr__(self, "_teach", teach or {})

	def lookup(self, name: str):
		if name in self._names:
			return self._names[name]
		key = f"{self._label.removeprefix('frappe').lstrip('.')}.{name}".lstrip(".")
		if key in self._teach:
			raise AttributeError(self._teach[key])
		raise AttributeError(
			f"{self._label}.{name} is not available here. On {self._label}: {', '.join(sorted(self._names))}."
		)

	def names(self) -> set[str]:
		return set(self._names)

	def __setattr__(self, name, value):
		raise TypeError("This object cannot be changed.")

	def __repr__(self):
		return f"<{self._label}>"


class Row(dict):
	"""A record as a dict whose fields also read as attributes (``row.customer``). A
	missing field RAISES (``row.get("x")`` is the way to read an optional one): a
	misspelled field name must fail, not total as zero.

	``__slots__ = ()`` like Frappe 16's ``frappe._dict``: the same layout, so a read's
	rows become Rows in place (``_rows_in_place``) instead of being copied."""

	__slots__ = ()

	def __getattr__(self, name):
		if name.startswith("_"):
			raise AttributeError(name)
		try:
			return self[name]
		except KeyError:
			fields = ", ".join(sorted(str(k) for k in self)[:40])
			raise AttributeError(
				f"this row has no field '{name}'. Ask for it in fields=[...] (an aggregate comes back "
				f"under its 'as' name). Fields in this row: {fields}."
			) from None

	def __setattr__(self, name, value):
		self[name] = value


def _rows_in_place(rows: list) -> list:
	"""A read's flat rows as Rows without copying them: each ``frappe._dict`` changes
	class in place (same layout). Anything else is copied."""
	for i, row in enumerate(rows):
		if type(row) is frappe._dict:
			try:
				# Not ``row.__class__ = Row``: frappe._dict's __setattr__ stores a key.
				object.__setattr__(row, "__class__", Row)
				continue
			except TypeError:
				pass
		rows[i] = _rows(dict(row))
	return rows


def _rows(value):
	"""Records as ``Row`` (recursively for child tables)."""
	if isinstance(value, dict):
		return Row({k: _rows(v) for k, v in value.items()})
	if isinstance(value, list):
		return [_rows(v) for v in value]
	return value


# --------------------------------------------------------------------------- #
# Permissioned reads                                                           #
# --------------------------------------------------------------------------- #

# Keyword arguments get_list accepts here.
_LIST_KWARGS = frozenset(
	{
		"order_by",
		"group_by",
		"limit",
		"limit_page_length",
		"page_length",
		"start",
		"limit_start",
		"offset",
		"pluck",
		"or_filters",
		"parent_doctype",
		"distinct",
	}
)
# Keyword arguments that would widen or bend what a read returns: refused, never honoured.
_ESCAPE_KWARGS = frozenset(
	{
		"ignore_permissions",
		"ignore_user_permissions",
		"user",
		"as_list",
		"debug",
		"run",
		"update",
		"save_user_settings",
		"save_user_settings_fields",
		"user_settings",
		"ignore_ddl",
		"strict",
		"with_comment_count",
		"with_childnames",
		"join",
		"reference_doctype",
		"ignore_ifnull",
	}
)

_AGGREGATES = ("SUM", "COUNT", "AVG", "MAX", "MIN")
_AGG_STRING = re.compile(
	r"^\s*(sum|count|avg|max|min)\s*\(\s*([^()]+?)\s*\)\s*(?:as\s+[`'\"]?(\w+)[`'\"]?)?\s*$",
	re.IGNORECASE,
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dict_aggregates_supported() -> bool:
	"""Frappe 16's query engine takes ``{"SUM": "field", "as": "x"}`` fields and refuses
	SQL function strings; Frappe 15's takes the string form and has no dict form."""
	try:
		from frappe.database import query

		return hasattr(query, "FUNCTION_MAPPING")
	except ImportError:  # pragma: no cover
		return False


_DICT_AGGREGATES = _dict_aggregates_supported()


# Core doctypes that hold sign-in tokens and keys. (Every doctype of the jarvis app is
# refused by rule, see _is_private_doctype.)
_SECRET_DOCTYPES = frozenset(
	{
		"OAuth Bearer Token",
		"OAuth Authorization Code",
		"OAuth Client",
		"OAuth Client Role",
		"OAuth Scope",
		"OAuth Settings",
		"OAuth Provider Settings",
		"Token Cache",
		"Connected App",
		"Social Login Key",
		"Social Login Keys",
		"User Social Login",
		"LDAP Settings",
		"LDAP Group Mapping",
	}
)


def _is_private_doctype(doctype: str) -> bool:
	"""Never read through compute, whatever the user's roles: every doctype of the
	jarvis app (conversations, memory, skills, settings, MCP OAuth clients and tokens,
	connectors ...) and the core sign-in token and key doctypes. ``doctype`` is the
	canonical name. A Password field alone does not make a doctype private: its value is
	stored outside the row and never comes back from a read. The dedicated tools read
	Jarvis's own records with their own scope checks (recall, find_skills, get_skill ...)."""
	if doctype in _SECRET_DOCTYPES or doctype.casefold().startswith("jarvis "):
		return True
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False
	from frappe.modules.utils import get_module_app

	try:
		return bool(meta.module) and get_module_app(meta.module) == "jarvis"
	except Exception:
		return False


def _refuse_private(doctype: str):
	if _is_private_doctype(doctype):
		raise PermissionDeniedError(
			f"compute reads business records only; {doctype} is not available here (Jarvis's own data "
			"or sign-in tokens and keys). Use the dedicated Jarvis tool for Jarvis's own records."
		)


# Fields that hold credentials or one-time keys, on doctypes compute otherwise reads: a
# System Manager reads User, but its password-reset key is a login. Never selected,
# filtered, sorted or grouped on, and stripped from records and report rows. Besides
# these, every Password field and every text field named like a secret (_SECRET_NAME).
_SECRET_FIELDS = {
	"User": frozenset({"reset_password_key", "api_key", "api_secret"}),
	"Document Share Key": frozenset({"key"}),
	"User Invitation": frozenset({"key"}),
	"Web Form Request": frozenset({"key"}),
	"Webhook Header": frozenset({"value"}),
}
_SECRET_NAME = re.compile(
	r"secret|password|passwd|passcode|private_key|(?:^|_)(?:token|api_key|access_key|otp|nonce|session_id)$",
	re.IGNORECASE,
)
# Field types that can hold a secret (a Check or a Date named "password_..." cannot).
_TEXT_FIELDTYPES = frozenset(
	{"Data", "Small Text", "Text", "Long Text", "Code", "Password", "Read Only", "JSON", "Text Editor"}
)


def _secret_fields(doctype: str) -> frozenset:
	"""The fields of ``doctype`` that hold credentials or one-time keys."""
	names = set(_SECRET_FIELDS.get(doctype, ()))
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype == "Password" or (
			df.fieldtype in _TEXT_FIELDTYPES and df.fieldname and _SECRET_NAME.search(df.fieldname)
		):
			names.add(df.fieldname)
	return frozenset(names)


def _secret_fields_reachable(doctype: str) -> frozenset:
	"""Secret fields of ``doctype`` and of its child tables (a filter can name a child
	table's field)."""
	names = set(_secret_fields(doctype))
	for df in frappe.get_meta(doctype).get_table_fields():
		names |= _secret_fields(df.options)
	return frozenset(names)


def _refuse_secret_fields(doctype: str, names) -> None:
	secret = sorted(set(names) & _secret_fields_reachable(doctype))
	if secret:
		raise PermissionDeniedError(
			f"{doctype} field(s) {', '.join(secret)} hold credentials or one-time keys; compute never "
			"reads, filters, sorts or groups on them."
		)


def _strip_secret_fields(doctype: str, record: dict) -> dict:
	"""``record`` (a document or a row) without its secret fields, child rows included."""
	for name in _secret_fields(doctype):
		record.pop(name, None)
	for df in frappe.get_meta(doctype).get_table_fields():
		for child in record.get(df.fieldname) or ():
			if isinstance(child, dict):
				_strip_secret_fields(df.options, child)
	return record


def _secret_column(column: dict) -> bool:
	name = column.get("fieldname") or ""
	fieldtype = column.get("fieldtype") or "Data"
	return bool(_SECRET_NAME.search(name)) and fieldtype in _TEXT_FIELDTYPES


def _json_arg(name: str, value):
	"""``filters`` / ``or_filters`` / ``fields`` given as JSON text, read as Frappe's API reads
	them; any other text is refused (it would silently match nothing)."""
	if not isinstance(value, str):
		return value
	try:
		parsed = json.loads(value)
	except ValueError:
		parsed = None
	if not isinstance(parsed, dict | list):
		raise SandboxUsageError(
			f"{name} must be a dict or a list (or JSON text of one), e.g. {{'docstatus': 1}} or "
			"[['posting_date', '>=', '2026-10-01']]."
		)
	return parsed


def _canonical_name(doctype: str) -> str:
	"""A doctype name as Frappe names it: case, surrounding and repeated whitespace and
	underscores (``jarvis_conversation``) do not matter."""
	from jarvis.tools._doctype_name import canonical_doctype

	return canonical_doctype(" ".join(doctype.replace("_", " ").split()))


def _did_you_mean(word: str, candidates) -> str:
	import difflib

	matches = difflib.get_close_matches(word, list(candidates), n=3, cutoff=0.6)
	return f" Did you mean {', '.join(matches)}?" if matches else ""


def _readable_doctype_matches(name: str) -> str:
	"""Up to three doctypes close to ``name`` that the user can read."""
	import difflib

	names = frappe.get_all("DocType", filters={"istable": 0}, pluck="name")
	close = difflib.get_close_matches(name, names, n=10, cutoff=0.6)
	readable = [d for d in close if not _is_private_doctype(d) and frappe.has_permission(d, ptype="read")]
	return f" Did you mean {', '.join(readable[:3])}?" if readable else ""


def _doctype(doctype) -> str:
	if not isinstance(doctype, str) or not doctype.strip():
		raise SandboxUsageError(
			"the first argument must be a doctype name, e.g. get_list('Sales Invoice', ...)."
		)
	name = _canonical_name(doctype)
	if not frappe.db.exists("DocType", name):
		raise SandboxUsageError(
			f"unknown doctype '{doctype}'.{_readable_doctype_matches(name)} Check the name with get_schema."
		)
	_refuse_private(name)
	return name


def _translate_field(entry):
	"""One ``fields`` entry in the form the installed Frappe accepts. Both the dict form
	``{"SUM": "base_grand_total", "as": "total"}`` and the string form
	``"sum(base_grand_total) as total"`` are accepted for SUM / COUNT / AVG / MAX / MIN."""
	if isinstance(entry, dict):
		funcs = [k for k in entry if k != "as"]
		if len(funcs) != 1:
			raise SandboxUsageError(
				f'an aggregate field takes one function and an alias, e.g. {{"SUM": "base_grand_total", '
				f'"as": "total"}}; got {entry!r}.'
			)
		func = str(funcs[0]).upper()
		if _DICT_AGGREGATES:
			return {func: entry[funcs[0]], **({"as": entry["as"]} if "as" in entry else {})}
		if func not in _AGGREGATES or not isinstance(entry[funcs[0]], str):
			raise SandboxUsageError(
				f"aggregate fields here take one of {', '.join(_AGGREGATES)} over one field."
			)
		arg = entry[funcs[0]]
		arg = "*" if arg == "*" else f"`{arg.strip('`')}`"
		alias = entry.get("as") or f"{func.lower()}_{arg.strip('`*') or 'all'}"
		return f"{func.lower()}({arg}) as `{alias}`"
	if isinstance(entry, str) and _DICT_AGGREGATES:
		m = _AGG_STRING.match(entry)
		if m:
			func, arg, alias = m.group(1).upper(), m.group(2).strip(), m.group(3)
			bare = re.sub(r"\W+", "_", arg.replace("`", "").split(".")[-1]).strip("_") or "all"
			return {func: arg, "as": alias or f"{func.lower()}_{bare}"}
	return entry


def _field_refs(entry) -> tuple[set[str], bool]:
	"""``(identifiers, computed)`` a fields entry reads; ``computed`` for an aggregate."""
	if isinstance(entry, dict):
		names: set[str] = set()
		for key, value in entry.items():
			if key == "as":
				continue
			names |= set(_IDENT.findall(json.dumps(value, default=str)))
		return names, True
	if isinstance(entry, str):
		head = re.split(r"\s+as\s+", entry, maxsplit=1, flags=re.IGNORECASE)[0]
		return set(_IDENT.findall(head.replace("`", ""))), "(" in head
	return set(), False


_PLAIN_FIELD = re.compile(r"^\s*`?(\w+)`?\s*(?:\s+as\s+\w+\s*)?$", re.IGNORECASE)


def _check_known_fields(doctype: str, fields) -> None:
	"""Refuse a plain field (or an aggregate's field) the doctype does not have. Frappe
	16 drops an unknown SELECT field without a word, so a typo would total as nothing."""
	columns = set(frappe.get_meta(doctype).get_valid_columns())
	unknown = []
	for entry in fields or []:
		if isinstance(entry, str):
			m = _PLAIN_FIELD.match(entry)
			name = m.group(1) if m else None
		elif isinstance(entry, dict):
			values = [v for k, v in entry.items() if k != "as"]
			name = values[0].strip("`") if len(values) == 1 and isinstance(values[0], str) else None
			if name == "*" or (name and not re.fullmatch(r"\w+", name)):
				name = None
		else:
			name = None
		if name and name not in columns:
			unknown.append(name)
	if unknown:
		readable = columns
		if frappe.session.user != "Administrator":
			from frappe.model import get_permitted_fields

			readable = columns & set(get_permitted_fields(doctype))
		hints = "".join(_did_you_mean(name, readable) for name in sorted(set(unknown)))
		raise SandboxUsageError(
			f"{doctype} has no field(s) {', '.join(sorted(set(unknown)))}.{hints} Check the names with "
			"get_meta(doctype) or get_schema; a child table's field is read from the child doctype "
			"with parent_doctype."
		)


def _check_field_access(doctype: str, parent_doctype, fields, filters, or_filters, order_by, group_by):
	"""Refuse a read that names a field the user cannot read (a permission level they
	lack) or computes / filters / sorts / groups over a field masked for them.
	Frappe drops an unreadable SELECT field silently; here it is refused, so the code
	never totals a column that came back empty."""
	from jarvis.tools.get_list import _filter_fieldnames

	plain: set[str] = set()
	computed: set[str] = set()
	for entry in fields or []:
		names, is_computed = _field_refs(entry)
		(computed if is_computed else plain).update(names)
	conditions = _filter_fieldnames(filters) | _filter_fieldnames(or_filters)
	ordering = set(_IDENT.findall(str(order_by or "").replace("`", ""))) | set(
		_IDENT.findall(str(group_by or "").replace("`", ""))
	)
	# Credentials: refused for everyone, Administrator included.
	_refuse_secret_fields(doctype, plain | computed | conditions | ordering)
	if frappe.session.user == "Administrator":
		return
	from frappe.model import get_permitted_fields

	meta = frappe.get_meta(doctype)
	columns = set(meta.get_valid_columns())
	permitted = set(get_permitted_fields(doctype, parenttype=parent_doctype))
	masked: set[str] = set()
	try:
		from jarvis import compat

		masked = {df.fieldname for df in compat.masked_fields_of(meta, parent_doctype)}
	except Exception:
		masked = set()  # Frappe 15: no field masking

	referenced = (plain | computed | conditions | ordering) & columns
	hidden = sorted(referenced - permitted)
	if hidden:
		raise PermissionDeniedError(f"no read permission on {doctype} field(s) {', '.join(hidden)}")
	leaking = sorted((computed | conditions | ordering) & masked)
	if leaking:
		raise PermissionDeniedError(
			f"{doctype} field(s) {', '.join(leaking)} are masked for you, so they cannot be "
			"aggregated, filtered, sorted or grouped on"
		)


def _int_arg(name: str, value) -> int | None:
	if value is None:
		return None
	if isinstance(value, bool) or not isinstance(value, int) or value < 0:
		raise SandboxUsageError(f"{name} must be a whole number of 0 or more.")
	return value


# --------------------------------------------------------------------------- #
# Read receipts                                                                #
# --------------------------------------------------------------------------- #


def _receipt_value(value):
	"""``value`` as JSON data for a receipt; its JSON text cut at RECEIPT_TEXT_CHARS."""
	try:
		text = json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
	except Exception:
		text = str(value)
	if len(text) <= RECEIPT_TEXT_CHARS:
		try:
			return json.loads(text)
		except Exception:
			return text
	return text[:RECEIPT_TEXT_CHARS] + "...(cut)"


def make_reads(run: _Run) -> dict:
	"""The permissioned read helpers, bound to ``run`` in closures (never a keyword or
	argument the script could override or read back)."""
	from jarvis.tools.get_list import (
		_child_table_parents,
		assert_child_query_fields_readable,
		assert_child_table_field_readable,
	)

	def get_list(doctype, fields=None, filters=None, **kwargs):
		return _read_list(doctype, fields, filters, kwargs, source="get_list")

	def _read_list(doctype, fields, filters, kwargs: dict, *, source: str):
		"""One permissioned list read. ``source`` names the helper the script called;
		only a script's own ``get_list`` gets the explicit-limit warning and the
		currency guard (get_value / exists read one row, count no amounts)."""
		run.check_time()
		escape = sorted(_ESCAPE_KWARGS & set(kwargs))
		if escape:
			raise PermissionDeniedError(
				f"get_list does not take {', '.join(escape)} here: compute always reads with your own "
				"permissions. Remove it."
			)
		unknown = sorted(set(kwargs) - _LIST_KWARGS)
		if unknown:
			raise SandboxUsageError(
				f"get_list does not take {', '.join(unknown)}. It takes: doctype, fields, filters, "
				f"{', '.join(sorted(_LIST_KWARGS))}."
			)
		# Frappe's own leniency: filters and fields given the other way round.
		if isinstance(fields, dict) or (
			isinstance(fields, list) and fields and isinstance(fields[0], list | tuple)
		):
			fields, filters = filters, fields
		filters = _json_arg("filters", filters)
		if kwargs.get("or_filters") is not None:
			kwargs = {**kwargs, "or_filters": _json_arg("or_filters", kwargs["or_filters"])}
		if isinstance(fields, str) and fields.lstrip().startswith("["):
			fields = _json_arg("fields", fields)
		doctype = _doctype(doctype)
		parent_doctype = kwargs.get("parent_doctype")
		pluck = kwargs.get("pluck")
		if isinstance(fields, str):
			fields = [f.strip() for f in fields.split(",") if f.strip()]
		if fields is not None and not isinstance(fields, list | tuple):
			raise SandboxUsageError("fields must be a list, e.g. fields=['name', 'customer'].")
		if pluck is not None:
			if fields:
				raise SandboxUsageError("pass either fields or pluck, not both.")
			if not isinstance(pluck, str):
				raise SandboxUsageError("pluck must be one field name.")
			fields = [pluck]
		as_given = list(fields or ["name"])
		fields = [_translate_field(f) for f in as_given]

		meta = frappe.get_meta(doctype)
		if meta.issingle:
			raise SandboxUsageError(
				f"{doctype} is a single doctype (one record, no list): use get_single_value('{doctype}', "
				f"field) or get_doc('{doctype}')."
			)
		if meta.istable:
			if not parent_doctype:
				parents = _child_table_parents(doctype)
				hint = f" (e.g. parent_doctype='{parents[0]}')" if parents else ""
				raise SandboxUsageError(
					f"'{doctype}' is a child table: pass parent_doctype{hint}. Child rows have no "
					"posting_date / customer; read those from the parent and join in Python by row.parent."
				)
			parent_doctype = _doctype(parent_doctype)
		else:
			parent_doctype = None
		if not frappe.has_permission(doctype, ptype="read", parent_doctype=parent_doctype):
			raise PermissionDeniedError(f"no read permission on {doctype}")
		if parent_doctype:
			assert_child_table_field_readable(doctype, parent_doctype)
		assert_child_query_fields_readable(doctype, parent_doctype, fields, filters, kwargs.get("order_by"))
		_check_known_fields(doctype, fields)
		_check_field_access(
			doctype,
			parent_doctype,
			fields,
			filters,
			kwargs.get("or_filters"),
			kwargs.get("order_by"),
			kwargs.get("group_by"),
		)

		limit = None
		for key in ("limit", "limit_page_length", "page_length"):
			if kwargs.get(key) is not None:
				limit = _int_arg(key, kwargs[key]) or None
				break
		offset = 0
		for key in ("offset", "start", "limit_start"):
			if kwargs.get(key) is not None:
				offset = _int_arg(key, kwargs[key]) or 0
				break
		cap = min(MAX_ROWS_PER_READ, run.remaining_rows())
		capped = limit is None or limit > cap
		fetch = cap + 1 if capped else limit

		call = {
			"fields": fields,
			"filters": filters or {},
			"limit": fetch,
			"ignore_permissions": False,
		}
		for key in ("or_filters", "order_by", "group_by", "distinct"):
			if kwargs.get(key) is not None:
				call[key] = kwargs[key]
		if parent_doctype:
			call["parent_doctype"] = parent_doctype
		if offset:
			call["offset" if _DICT_AGGREGATES else "limit_start"] = offset
		try:
			rows = frappe.get_list(doctype, **call)
		except frappe.PermissionError:
			raise
		except frappe.ValidationError as e:
			raise SandboxUsageError(f"get_list('{doctype}'): {_clean(e)}") from None
		if capped and len(rows) > cap:
			if cap < MAX_ROWS_PER_READ:
				run.take_rows(len(rows), f"get_list('{doctype}')")
			run.fail(
				f"get_list('{doctype}') matched more than {cap:,} rows, the most one read may return "
				"here. Filter it (a date range, a status), or let the database aggregate: "
				'group_by="customer" with fields=["customer", {"SUM": "base_grand_total", "as": "total"}].'
			)
		run.take_rows(len(rows), f"get_list('{doctype}')")
		if any(isinstance(f, str) and "*" in f and "(" not in f for f in fields):
			for row in rows:
				_strip_secret_fields(doctype, row)

		receipt = {"doctype": doctype, "rows": len(rows)}
		if filters:
			receipt["filters"] = _receipt_value(filters)
		if source == "get_list" and limit is not None:
			receipt["limited"] = True
			receipt["limit"] = limit
			# A limit with an order_by is a deliberate top-N; without one it cuts a total.
			if len(rows) == limit and not kwargs.get("order_by"):
				run.warn(
					("limit", doctype),
					f"get_list('{doctype}') stopped at its limit of {limit} rows; totals from it are "
					"partial. Drop the limit to read every matching row (or add an order_by for a top-N).",
				)
		run.record_read(receipt)
		if source == "get_list":
			from jarvis.tools import _compute_amounts

			_compute_amounts.check(run, doctype, meta, parent_doctype, fields, rows, filters, kwargs, receipt)
		if pluck is not None:
			return [row.get(pluck) for row in rows]
		return _rows_in_place(rows)

	def get_doc(doctype, name=None):
		run.check_time()
		from jarvis.tools.get_doc import _get_doc_one, _get_single

		doctype = _doctype(doctype)
		try:
			if frappe.get_meta(doctype).issingle:
				doc = _get_single(doctype)
			else:
				if name is None or isinstance(name, dict | list):
					raise SandboxUsageError(
						"get_doc takes (doctype, name). To find a record by its fields use "
						"get_list(doctype, filters={...}, limit=1) first."
					)
				doc = _get_doc_one(doctype, str(name))
		except InvalidArgumentError as e:
			raise SandboxUsageError(str(e)) from None
		_strip_secret_fields(doctype, doc)
		children = sum(len(v) for v in doc.values() if isinstance(v, list))
		run.take_rows(1 + children, f"get_doc('{doctype}')")
		receipt = {"doctype": doctype, "rows": 1 + children}
		from jarvis.tools import _compute_amounts

		meta = frappe.get_meta(doctype)
		units = _compute_amounts.record_units(doctype, meta, doc, [df.fieldname for df in meta.fields])
		if len(units) == 1:
			receipt["currency"] = next(iter(units))
		elif units:
			receipt["currencies"] = sorted(units)
		run.record_read(receipt)
		run.note_record_units(doctype, units)
		return _rows(doc)

	def get_value(doctype, filters=None, fieldname="name", as_dict=False):
		doctype = _doctype(doctype)
		fieldnames = list(fieldname) if isinstance(fieldname, list | tuple) else [fieldname]
		_refuse_secret_fields(doctype, [f for f in fieldnames if isinstance(f, str)])
		if frappe.get_meta(doctype).issingle:
			doc = get_doc(doctype)
			row = Row({f: doc.get(f) for f in fieldnames})
		else:
			if isinstance(filters, str | int):
				filters = {"name": filters}
			from jarvis.tools import _compute_amounts

			meta = frappe.get_meta(doctype)
			extra = [
				c for c in _compute_amounts.unit_columns(doctype, meta, fieldnames) if c not in fieldnames
			]
			if extra and frappe.session.user != "Administrator":
				from frappe.model import get_permitted_fields

				extra = [c for c in extra if c in set(get_permitted_fields(doctype))]
			found = _read_list(doctype, [*fieldnames, *extra], filters, {"limit": 1}, source="get_value")
			if not found:
				return None
			units = _compute_amounts.record_units(doctype, meta, found[0], fieldnames)
			if len(units) == 1:
				run.last_receipt["currency"] = next(iter(units))
			run.note_record_units(doctype, units)
			row = Row({f: found[0].get(f) for f in fieldnames})
		if as_dict:
			return row
		if isinstance(fieldname, list | tuple):
			return [row.get(f) for f in fieldnames]
		return row.get(fieldname)

	def get_single_value(doctype, fieldname):
		return get_value(doctype, None, fieldname)

	def exists(doctype, filters=None):
		doctype = _doctype(doctype)
		if frappe.get_meta(doctype).issingle:
			return bool(frappe.has_permission(doctype, ptype="read"))
		if isinstance(filters, str | int):
			filters = {"name": filters}
		return bool(_read_list(doctype, ["name"], filters, {"limit": 1}, source="exists"))

	def count(doctype, filters=None):
		rows = _read_list(doctype, [{"COUNT": "*", "as": "n"}], filters, {}, source="count")
		n = int(rows[0]["n"] or 0) if rows else 0
		run.last_receipt["rows"] = n  # the rows counted, not the one row the count came in
		return n

	def get_meta(doctype):
		run.check_time()
		from frappe.model import get_permitted_fields

		doctype = _doctype(doctype)
		if not frappe.has_permission(doctype, ptype="read"):
			raise PermissionDeniedError(f"no read permission on {doctype}")
		meta = frappe.get_meta(doctype)
		permitted = set(get_permitted_fields(doctype)) if frappe.session.user != "Administrator" else None
		return Row(
			{
				"name": meta.name,
				"istable": bool(meta.istable),
				"issingle": bool(meta.issingle),
				"is_submittable": bool(meta.is_submittable),
				"title_field": meta.title_field,
				"fields": [
					Row(
						{
							"fieldname": df.fieldname,
							"label": df.label,
							"fieldtype": df.fieldtype,
							"options": df.options,
						}
					)
					for df in meta.fields
					if df.fieldname and (permitted is None or df.fieldname in permitted)
				],
			}
		)

	def run_report(report_name, filters=None):
		run.check_time()
		from jarvis.tools._report_rows import normalise_columns
		from jarvis.tools.run_report import _run_report

		if not isinstance(report_name, str) or not report_name.strip():
			raise SandboxUsageError(
				"run_report takes the report's name, e.g. run_report('Accounts Receivable', {...})."
			)
		ref = frappe.db.get_value("Report", report_name, "ref_doctype")
		if isinstance(ref, str):
			_refuse_private(ref)
		run.activity = f"run_report('{report_name}')"
		try:
			data = _run_report(report_name, filters, no_background=True)
		except frappe.PermissionError as e:
			raise PermissionDeniedError(_clean(e) or f"no permission to run report {report_name}") from None
		except InvalidArgumentError as e:
			if isinstance(e, PermissionDeniedError):
				raise
			raise SandboxUsageError(f"run_report('{report_name}'): {e}") from None
		except frappe.ValidationError as e:
			raise SandboxUsageError(f"run_report('{report_name}') failed: {_clean(e)}{_REPORT_FIX}") from None
		except Exception as e:
			if _failure_kind_too_slow(e):
				raise
			# The report's own error (a bad column, a missing filter), not a write attempt:
			# Frappe's Error Log insert for it is dropped by the write guard.
			raise SandboxUsageError(
				f"run_report('{report_name}') failed: {type(e).__name__}: {_clean(e)}{_REPORT_FIX}"
			) from None
		finally:
			run.activity = None
		columns = normalise_columns(data.get("columns") or [])
		result = list(data.get("result") or [])
		total_row = None
		if data.get("add_total_row") and result:
			total_row = result.pop()
		rows = [_report_row(r, columns) for r in result]
		total = _report_row(total_row, columns) if total_row is not None else None
		secret = {c["fieldname"] for c in columns if _secret_column(c)}
		if secret:
			columns = [c for c in columns if c["fieldname"] not in secret]
			for row in [*rows, total]:
				if isinstance(row, dict):
					for name in secret:
						row.pop(name, None)
		if len(rows) > MAX_ROWS_PER_READ:
			run.fail(
				f"run_report('{report_name}') returned more than {MAX_ROWS_PER_READ:,} rows. Narrow its "
				"filters (a date range, a company, a party)."
			)
		run.take_rows(len(rows), f"run_report('{report_name}')")
		# A Prepared Report's finished copy says when it was made; a stale copy must not
		# look current.
		prepared = bool(data.get("prepared"))
		as_of = str(data["as_of"]) if prepared and data.get("as_of") else None
		receipt = {"report": report_name, "rows": len(rows)}
		if filters:
			receipt["filters"] = _receipt_value(filters)
		if prepared:
			receipt["prepared"] = True
			receipt["as_of"] = as_of
		run.record_read(receipt)
		return Row(
			{
				"columns": [
					Row({k: c[k] for k in ("fieldname", "label", "fieldtype", "options")}) for c in columns
				],
				"rows": rows,
				"total_row": total,
				"prepared": prepared,
				"as_of": as_of,
			}
		)

	def default_company():
		run.check_time()
		user_default = frappe.defaults.get_user_default("Company")
		for company in (user_default, frappe.db.get_single_value("Global Defaults", "default_company")):
			if company and frappe.has_permission("Company", ptype="read", doc=company):
				return company
		return None

	def get_user_default(key):
		if not isinstance(key, str) or key.strip().casefold() not in _DEFAULT_KEYS:
			raise SandboxUsageError(
				f"get_user_default takes one of: {', '.join(sorted(_DEFAULT_KEYS))}. For the company "
				"use default_company()."
			)
		if key.strip().casefold() == "company":
			return default_company()
		return frappe.defaults.get_user_default(key)

	return {
		"get_list": get_list,
		"get_doc": get_doc,
		"get_value": get_value,
		"get_single_value": get_single_value,
		"exists": exists,
		"count": count,
		"get_meta": get_meta,
		"run_report": run_report,
		"default_company": default_company,
		"get_user_default": get_user_default,
	}


# Defaults a script may read: ordinary working defaults, nothing else.
_DEFAULT_KEYS = frozenset(
	{
		"company",
		"currency",
		"fiscal_year",
		"country",
		"customer_group",
		"supplier_group",
		"territory",
		"selling_price_list",
		"buying_price_list",
		"warehouse",
		"cost_center",
	}
)


_REPORT_FIX = (
	" Check the report's filters (get_report_filters lists them and which are required) and run "
	"it again; if the report itself is broken, read the records with get_list instead."
)


def _failure_kind_too_slow(e: BaseException) -> bool:
	from jarvis import _failure_kind

	return _failure_kind.too_slow(e)


def _report_row(row, columns: list[dict]):
	if isinstance(row, dict):
		return _rows(dict(row))
	if isinstance(row, list | tuple):
		return Row({c["fieldname"]: (row[i] if i < len(row) else None) for i, c in enumerate(columns)})
	return row


def _clean(e: BaseException) -> str:
	from frappe.utils import strip_html

	return " ".join(strip_html(str(e)).split())[:600]


# --------------------------------------------------------------------------- #
# The namespace                                                                #
# --------------------------------------------------------------------------- #

_UTIL_NAMES = (
	"getdate",
	"get_datetime",
	"today",
	"nowdate",
	"now",
	"now_datetime",
	"nowtime",
	"add_days",
	"add_months",
	"add_years",
	"add_to_date",
	"date_diff",
	"month_diff",
	"time_diff_in_hours",
	"get_first_day",
	"get_last_day",
	"get_first_day_of_week",
	"get_last_day_of_week",
	"get_quarter_start",
	"get_quarter_ending",
	"get_year_start",
	"get_year_ending",
	"get_timespan_date_range",
	"formatdate",
	"format_date",
	"global_date_format",
	"flt",
	"cint",
	"cstr",
	"rounded",
	"floor",
	"ceil",
	"fmt_money",
	"money_in_words",
	"comma_and",
	"comma_or",
)


# Decimal places a number helper may be asked for: flt / rounded / fmt_money compute
# 10**precision, so flt(x, 10**9) would hold the worker in C. Money needs a few.
MAX_PRECISION = 30
# money_in_words spells the number out: beyond a quadrillion it is no amount.
MAX_WORDS_AMOUNT = 10**15
# Positional index of the precision argument of each helper that takes one.
_PRECISION_ARG = {"flt": 1, "rounded": 1, "fmt_money": 1}


def _precision_guard(name: str, func):
	index = _PRECISION_ARG[name]

	def guarded(*args, **kwargs):
		precision = args[index] if len(args) > index else kwargs.get("precision")
		if isinstance(precision, int) and not isinstance(precision, bool) and abs(precision) > MAX_PRECISION:
			raise SandboxUsageError(f"{name}() with precision above {MAX_PRECISION} is not available.")
		return func(*args, **kwargs)

	return guarded


def _money_in_words_guard(func):
	def guarded(number, *args, **kwargs):
		try:
			too_big = abs(float(number)) >= MAX_WORDS_AMOUNT
		except (TypeError, ValueError, OverflowError):
			too_big = False
		if too_big:
			raise SandboxUsageError("money_in_words() takes amounts below a quadrillion.")
		return func(number, *args, **kwargs)

	return guarded


def _utils() -> dict:
	import frappe.utils

	out = {}
	for name in _UTIL_NAMES:
		func = getattr(frappe.utils, name, None)
		if not callable(func):
			continue
		if name in _PRECISION_ARG:
			func = _precision_guard(name, func)
		elif name == "money_in_words":
			func = _money_in_words_guard(func)
		out[name] = func
	return out


# json.dumps(indent=10**8) multiplies the output by the indent: pretty-printing needs a few.
MAX_JSON_INDENT = 8


def _json_dumps(obj, indent=None, sort_keys=False):
	from frappe.utils.response import json_handler

	if indent is not None and (not isinstance(indent, int) or not 0 <= indent <= MAX_JSON_INDENT):
		raise SandboxUsageError(f"json.dumps indent must be a whole number from 0 to {MAX_JSON_INDENT}.")
	check_written_size(obj, "json.dumps()")
	return json.dumps(obj, indent=indent, sort_keys=sort_keys, default=json_handler)


def _json_loads(text):
	return check_nesting(json.loads(text), "json.loads()")


def _throw(message="", *args, **kwargs):
	raise SandboxUsageError(f"the code stopped itself: {message}")


_PLAIN_BUILTINS = {
	"None": None,
	"True": True,
	"False": False,
	"abs": abs,
	"all": all,
	"any": any,
	"bool": bool,
	"callable": callable,
	"chr": chr,
	"dict": dict,
	"divmod": divmod,
	"enumerate": enumerate,
	"filter": filter,
	"float": float,
	"hash": hash,
	"int": int,
	"isinstance": isinstance,
	"len": len,
	"list": list,
	"map": map,
	"max": max,
	"min": min,
	"next": next,
	"ord": ord,
	"range": _range,
	"repr": repr,
	"reversed": reversed,
	"set": set,
	"slice": slice,
	"sorted": sorted,
	"str": str,
	"tuple": tuple,
	"zip": zip,
	"Exception": Exception,
	"ArithmeticError": ArithmeticError,
	"AssertionError": AssertionError,
	"AttributeError": AttributeError,
	"IndexError": IndexError,
	"KeyError": KeyError,
	"LookupError": LookupError,
	"NameError": NameError,
	"RuntimeError": RuntimeError,
	"StopIteration": StopIteration,
	"TypeError": TypeError,
	"ValueError": ValueError,
	"ZeroDivisionError": ZeroDivisionError,
}
_PLAIN_BUILTIN_NAMES = frozenset(_PLAIN_BUILTINS) | {"pow", "round", "sum", "repr", "str", "divmod"}


def build_namespace(run: _Run, user: str) -> tuple[dict, set[str], dict[str, set[str]]]:
	"""``(globals, available_names, frappe_names)`` for one run. Built fresh per call:
	nothing the script does to its namespace outlives the call."""
	reads = make_reads(run)
	guards = Guards(run.check_memory, run.tick)
	inplacevar, aug_item = _inplace_factory(guards)
	types_ = guards.types()
	make_dict = types_["dict"]
	guarded_dict = guarded_type(
		"_dict", frappe._dict, lambda *args, **kwargs: frappe._dict(make_dict(*args, **kwargs)), {}
	)
	utils = _utils()
	session = _Namespace("frappe.session", {"user": user})
	db = _Namespace(
		"frappe.db",
		{
			"get_list": reads["get_list"],
			"get_value": reads["get_value"],
			"get_single_value": reads["get_single_value"],
			"exists": reads["exists"],
			"count": reads["count"],
		},
		_FORBIDDEN_FRAPPE,
	)
	utils_ns = _Namespace("frappe.utils", utils)
	defaults = _Namespace("frappe.defaults", {"get_user_default": reads["get_user_default"]})
	frappe_ns = _Namespace(
		"frappe",
		{
			"get_list": reads["get_list"],
			"get_doc": reads["get_doc"],
			"get_value": reads["get_value"],
			"get_meta": reads["get_meta"],
			"db": db,
			"utils": utils_ns,
			"session": session,
			"user": user,
			"defaults": defaults,
			"_dict": guarded_dict,
			"parse_json": _json_loads,
			"as_json": _json_dumps,
			"throw": _throw,
			**{k: utils[k] for k in ("flt", "cint", "cstr") if k in utils},
		},
		_FORBIDDEN_FRAPPE,
	)
	json_ns = _Namespace("json", {"loads": _json_loads, "dumps": _json_dumps})
	# Shaped like the module, so datetime.date(...) and datetime.datetime.now() both work.
	datetime_ns = _Namespace(
		"datetime",
		{"date": datetime.date, "datetime": datetime.datetime, "timedelta": datetime.timedelta},
	)

	named = {
		"frappe": frappe_ns,
		"utils": utils_ns,
		"json": json_ns,
		"_dict": guarded_dict,
		"datetime": datetime_ns,
		"date": datetime.date,
		"timedelta": datetime.timedelta,
		**reads,
		**utils,
	}
	builtins = {
		**_PLAIN_BUILTINS,
		**types_,  # str, int, list, tuple, set, dict: size-, hash- and nesting-checked
		"pow": guards.pow,
		"round": guards.round,
		"sum": guards.sum,
		"divmod": guards.divmod,
		"sorted": guards.sorted,
		"min": guards.min,
		"max": guards.max,
		"repr": guards.repr,
		"hash": guards.hash,
		# Unreachable from the script (names starting with "_" never compile); C code
		# such as datetime.strftime imports ``time`` through it.
		"__import__": internal_import,
	}
	glb = {
		"__builtins__": builtins,
		"__name__": "jarvis_compute",
		**named,
		"_getattr_": _getattr_factory(guards),
		"_getitem_": _getitem_factory(guards),
		"_write_": _write_factory(guards),
		"_getiter_": _getiter_factory(run),
		_TICK: run.tick,
		"_iter_unpack_sequence_": _rp_guards.guarded_iter_unpack_sequence,
		"_unpack_sequence_": _rp_guards.guarded_unpack_sequence,
		"_inplacevar_": inplacevar,
		"_apply_": guards.apply,
		"_print_": _print_factory(run),
		_AUG_ITEM: aug_item,
		_BINOP: guards.binop,
		_FORMAT: guards.format_value,
		_COMPARE: guards.compare,
		_KEY: guards.key,
		_NEST: guards.nest,
		_BUILD: guards.build,
		_BUILD_DICT: guards.build_dict,
		_CALL: guards.call,
	}
	available = set(named) | set(builtins)
	frappe_names = {"": frappe_ns.names(), "db": db.names(), "utils": utils_ns.names()}
	return glb, available, frappe_names


# --------------------------------------------------------------------------- #
# Running                                                                      #
# --------------------------------------------------------------------------- #

# The statements a read may send. Everything else is refused before it reaches the
# database. ``set`` only for the statement time limit and user variables; ``rollback``
# only to a savepoint (a bare one would end the transaction the sandbox lives in).
_READ_STATEMENTS = frozenset(
	{"select", "with", "show", "explain", "describe", "desc", "savepoint", "release"}
)
_COMMENT = re.compile(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|#[^\n]*\n\s*)*", re.DOTALL)
_SAFE_SET = re.compile(r"^set\s+(?:session\s+)?(?:max_statement_time\b|@)", re.IGNORECASE)
_ROLLBACK_TO = re.compile(r"^rollback\s+to\s+", re.IGNORECASE)
_OUTFILE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)
# Frappe's own error logging (log_error) inserts one Error Log row when a report or
# read fails. Inside compute that insert is dropped, so the failure itself reaches the
# script, instead of being reported as a write attempt. Only this exact statement shape.
_ERROR_LOG_INSERT = re.compile(r"^insert\s+into\s+`tabError Log`\s*\(", re.IGNORECASE)


def _statement_allowed(query) -> bool:
	text = _COMMENT.sub("", str(query), count=1).lstrip()
	first = text.split(None, 1)[0].lower() if text else ""
	if first in _READ_STATEMENTS:
		return not _OUTFILE.search(text)
	if first == "set":
		return bool(_SAFE_SET.match(text))
	if first == "rollback":
		return bool(_ROLLBACK_TO.match(text))
	return False


@contextmanager
def _no_writes(run: _Run):
	"""While the script runs, ``frappe.db.sql`` refuses every statement that is not a
	read, before it is sent. An instance attribute on this request's connection only,
	removed in ``finally``; every Frappe read and write path goes through ``sql``."""
	db = frappe.db
	had_own = "sql" in vars(db)
	real = db.sql

	def sql(query, *args, **kwargs):
		if not _statement_allowed(query):
			if _ERROR_LOG_INSERT.match(_COMMENT.sub("", str(query), count=1).lstrip()):
				return ()
			kind = (_COMMENT.sub("", str(query), count=1).split(None, 1) or ["?"])[0].upper()
			run.fail(
				f"Something this code ran tried to change the database ({kind}), and compute is "
				"read-only, so the call was stopped and nothing was saved. If it was a report, it "
				"writes when it runs: call run_report directly instead of from compute.",
				kind="write",
			)
		return real(query, *args, **kwargs)

	db.sql = sql
	try:
		yield
	finally:
		if had_own:
			db.sql = real
		else:
			del db.sql


class _Watch:
	"""Deadline enforcement beneath the compiled-in ticks, through ``sys.monitoring``.

	One callback on every Python function start and generator resume, shared by every
	compute run of the process (each thread runs at most one). Before a run's deadline
	the callback disables itself for each code location it sees, so a run pays one
	callback per distinct function, not one per call (a trace function cost every guard
	call a frame object and a Python call: twice the run time of a loop). At the
	deadline a timer thread re-enables the events, and keeps re-enabling them every
	WATCH_REARM_S while the run lasts: from then on any call into the script or the
	guards stops the run, and any call beneath (a report's own Python) GRACE_S later.
	Unlike a trace function, a monitoring callback is not dropped when it raises."""

	lock = threading.Lock()
	runs: dict[int, tuple] = {}
	tool: int | None = None

	@classmethod
	def _on_start(cls, code, offset):
		entry = cls.runs.get(threading.get_ident())
		if entry is None:
			return sys.monitoring.DISABLE
		run, own = entry
		now = time.monotonic()
		if now <= run.deadline:
			return sys.monitoring.DISABLE
		grace = 0 if code.co_filename in own else GRACE_S
		if now > run.deadline + grace:
			run.check_time(grace)
		return None

	@classmethod
	def start(cls, run: _Run, own: frozenset) -> bool:
		monitoring = getattr(sys, "monitoring", None)
		if monitoring is None:
			return False
		with cls.lock:
			if cls.tool is None:
				tool = next((t for t in _WATCH_TOOL_IDS if monitoring.get_tool(t) is None), None)
				if tool is None:
					return False
				monitoring.use_tool_id(tool, "jarvis-compute")
				events = monitoring.events.PY_START | monitoring.events.PY_RESUME
				monitoring.register_callback(tool, monitoring.events.PY_START, cls._on_start)
				monitoring.register_callback(tool, monitoring.events.PY_RESUME, cls._on_start)
				monitoring.set_events(tool, events)
				cls.tool = tool
			cls.runs[threading.get_ident()] = (run, own)
		return True

	@classmethod
	def stop(cls):
		monitoring = sys.monitoring
		with cls.lock:
			cls.runs.pop(threading.get_ident(), None)
			if cls.runs or cls.tool is None:
				return
			tool, cls.tool = cls.tool, None
			monitoring.set_events(tool, 0)
			monitoring.register_callback(tool, monitoring.events.PY_START, None)
			monitoring.register_callback(tool, monitoring.events.PY_RESUME, None)
			monitoring.free_tool_id(tool)


# sys.monitoring tool ids the watch may take: 0-2 and 5 are named for debuggers,
# coverage, profilers and optimisers.
_WATCH_TOOL_IDS = (4, 3)

# How often, past the deadline, the watch re-enables events another thread disabled.
WATCH_REARM_S = 0.05


def _rearm_at_deadline(run: _Run, done: threading.Event):
	if done.wait(max(run.deadline - time.monotonic(), 0)):
		return
	while True:
		sys.monitoring.restart_events()
		if done.wait(WATCH_REARM_S):
			return


def _start_deadline(run: _Run):
	"""Start the second line behind the compiled-in ticks: any call into the script's own
	code or the guards stops the run at the deadline (a function the script passes to a C
	builtin, a guard called from map), and any call beneath it (a report's own Python
	loop) GRACE_S after. See ``_Watch``. Without ``sys.monitoring`` or a free tool id it
	falls back to a trace function (slower, same effect).

	Returns the function that ends it. The caller first drops the run from
	``_Watch.runs`` with C calls only (``_Watch.runs.pop(threading.get_ident(), None)``):
	until then any Python call past the deadline raises, including the clean-up's own."""

	from jarvis.tools import _compute_guards

	own = frozenset({SCRIPT_FILENAME, _compute_guards.__file__})
	if _Watch.start(run, own):
		done = threading.Event()
		threading.Thread(target=_rearm_at_deadline, args=(run, done), daemon=True).start()

		def end():
			done.set()
			_Watch.stop()

		return end

	def tracer(frame, event, arg):
		if event == "call" and threading.get_ident() in _Watch.runs:
			run.check_time(0 if frame.f_code.co_filename in own else GRACE_S)
		return None

	with _Watch.lock:
		_Watch.runs[threading.get_ident()] = (run, own)
	previous = sys.gettrace()
	sys.settrace(tracer)

	def end_trace():
		sys.settrace(previous)

	return end_trace


def script_line(error: BaseException) -> int | None:
	"""The last line of the script on ``error``'s traceback."""
	tb = error.__traceback__
	line = None
	while tb is not None:
		if tb.tb_frame.f_code.co_filename == SCRIPT_FILENAME:
			line = tb.tb_lineno
		tb = tb.tb_next
	return line


def execute(code_obj, glb: dict, run: _Run) -> BaseException | None:
	"""Run the compiled script in ``glb``, read-only, under the limits. Returns the
	exception the script ended with (None when it finished); ``run.failure`` says
	whether a limit or a refused write stopped it."""
	from jarvis.api import statement_time_limit
	from jarvis.tools._preview_sandbox import preview_sandbox

	error = None
	with preview_sandbox():
		with statement_time_limit(STATEMENT_TIME_S):
			with _no_writes(run):
				try:
					end_deadline = _start_deadline(run)
					try:
						# The tool's purpose: code compiled by RestrictedPython above, run in the
						# allowlisted read-only namespace `glb`, as the chat user.
						# nosemgrep: frappe-codeinjection-eval
						exec(code_obj, glb)
					finally:
						# C calls only, before any Python call: see _start_deadline.
						_Watch.runs.pop(threading.get_ident(), None)
						end_deadline()
				except (KeyboardInterrupt, SystemExit):
					raise
				except BaseException as e:
					error = e
	return error
