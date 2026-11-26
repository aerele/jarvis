"""Size guards for the compute sandbox: no single operation may run long or allocate much.

The sandbox's time and memory checks run between statements and loop iterations
(``_compute_sandbox._Run.tick``). One C-level operation runs to its end without
returning to Python, so a check can only come before it: every operation that can
build a huge value in one step has its result size worked out from its operands and
is refused above a cap, before anything is allocated.

Covered (each rewritten by the compile policy or wrapped in the namespace):

- ``a + b``, ``a * b``, ``a ** b``, ``a << b`` and ``a % b`` (and ``+=`` and the other
  augmented forms, through the in-place guard): int results by bit length
  (``a*b`` as bits(a)+bits(b), ``a**b`` as b*log2(a), ``a<<n`` as bits(a)+n),
  str/bytes by length, list/tuple by item count; ``str % args`` by its format widths
  and an estimate of its length.
- builtin ``pow`` (3-argument modular pow stays allowed, with bounded operands),
  ``round`` (``round(1, -10**9)`` builds 10**10**9), ``sum`` with a non-number start
  (``sum(lists, [])`` is quadratic in C).
- f-string replacement fields: widths and precisions in format specs, and each
  formatted piece's length.
- methods that grow text or lists: str/bytes ``ljust rjust center zfill expandtabs
  replace join translate``, ``int.to_bytes``, ``list.extend``.

The caps are module constants, each with its reason. A result over BIG_RESULT_BYTES is
followed at once by a memory check.
"""

from __future__ import annotations

import datetime
import math
import re

# Largest int an operation may produce: 2**16 bits is about 19,700 decimal digits, far
# beyond any amount, and one multiply or power of that size takes well under a
# millisecond. (Python itself refuses to print an int of more than 4,300 digits.)
MAX_INT_BITS = 65_536

# Longest str or bytes an operation may produce. A tool result is capped at 35,000
# characters, so no answer needs more; a million characters is at most 4 MB.
MAX_TEXT_CHARS = 1_000_000

# Most items a list or tuple operation may produce: five times the per-call row cap
# (200,000), at most 8 MB of references.
MAX_SEQUENCE_ITEMS = 1_000_000

# Widest field and longest precision in a %-format or an f-string format spec. A table
# column needs tens of characters; "%1000000000d" would allocate a gigabyte.
MAX_FORMAT_WIDTH = 1_000

# Modular pow: exponent and modulus up to RSA size. Beyond that, one pow(a, b, m) call
# runs for seconds in C.
MAX_MODPOW_BITS = 4_096

# round(x, n): Python computes 10**abs(n) for an int; money needs a handful of digits.
MAX_ROUND_DIGITS = 300

# A result bigger than this is followed by an immediate memory check.
BIG_RESULT_BYTES = 1_000_000

# Most values str() / repr() / print / json.dumps / an f-string may walk, counting
# shared references as often as they appear. [big] * 10**6 or x = [x, x] in a loop
# is small in memory but enormous once written out, and writing it out is one C call.
MAX_WRITTEN_VALUES = 1_000_000

# Most arguments a call may receive through *args (zip(*[r] * 10**6) builds tuples of
# a million items in one C call). Unzipping a few thousand pairs stays well inside.
MAX_STAR_ARGS = 10_000

# Longest strftime pattern: a date format is a few dozen characters.
MAX_STRFTIME_CHARS = 200

# Modules C code inside the sandbox imports for itself (datetime.strftime / strptime).
# The script cannot import anything: names starting with "_" never compile.
_INTERNAL_IMPORTS = frozenset({"time", "_strptime"})

_TEXT = (str, bytes)
_SEQ = (list, tuple)
_NUMBER = (int, float)


class GuardRefused(BaseException):
	"""An operation was refused before it ran. The sandbox turns it into a fixable error.
	A BaseException, so a script's ``except Exception`` cannot carry on as if the value
	had been built."""


def _refuse(message: str):
	raise GuardRefused(message)


def _bits(x) -> int:
	return x.bit_length() if isinstance(x, int) else 0


def _check_int_bits(bits: int, what: str):
	if bits > MAX_INT_BITS:
		_refuse(f"{what} would make an integer of about {bits:,} bits; the limit is {MAX_INT_BITS:,}.")


def _check_text(n: int, what: str):
	if n > MAX_TEXT_CHARS:
		_refuse(f"{what} would make text of {n:,} characters; the limit is {MAX_TEXT_CHARS:,}.")


def _check_items(n: int, what: str):
	if n > MAX_SEQUENCE_ITEMS:
		_refuse(f"{what} would make a list of {n:,} items; the limit is {MAX_SEQUENCE_ITEMS:,}.")


def _check_size(value, n: int, what: str):
	if isinstance(value, _TEXT):
		_check_text(n, what)
	elif isinstance(value, _SEQ):
		_check_items(n, what)


def _size_bytes(value) -> int:
	if isinstance(value, str):
		return len(value) * 4
	if isinstance(value, bytes):
		return len(value)
	if isinstance(value, _SEQ):
		return len(value) * 8
	if isinstance(value, int):
		return value.bit_length() // 8
	return 0


# --------------------------------------------------------------------------- #
# %-formatting                                                                 #
# --------------------------------------------------------------------------- #

_PERCENT = re.compile(
	r"%(?:\((?P<key>[^)]*)\))?(?P<flags>[-#0 +]*)(?P<width>\*|\d+)?(?:\.(?P<prec>\*|\d+))?[hlL]?(?P<type>[a-zA-Z%])"
)


def _len_hint(value) -> int:
	if isinstance(value, _TEXT):
		return len(value)
	if isinstance(value, bool) or value is None:
		return 5
	if isinstance(value, int):
		return value.bit_length() * 31 // 100 + 2
	if isinstance(value, float):
		return 32
	check_written_size(value, "This % format")
	return len(repr(value))


def check_percent_format(fmt, args):
	"""Refuse a %-format with a huge or ``*`` width or precision, or whose result would
	pass MAX_TEXT_CHARS."""
	text = fmt if isinstance(fmt, str) else fmt.decode("latin-1")
	positional = list(args) if isinstance(args, tuple) else [args]
	mapping = args if isinstance(args, dict) else None
	total = len(text)
	index = 0
	for m in _PERCENT.finditer(text):
		if m.group("type") == "%":
			continue
		for part in ("width", "prec"):
			value = m.group(part)
			if value == "*":
				_refuse(
					"a * width or precision in a % format is not available: write the number in the format."
				)
			if value and int(value) > MAX_FORMAT_WIDTH:
				_refuse(f"a % format width or precision above {MAX_FORMAT_WIDTH} is not available.")
		if mapping is not None and m.group("key") is not None:
			arg = mapping.get(m.group("key"), "")
		elif index < len(positional):
			arg = positional[index]
			index += 1
		else:
			arg = ""
		total += max(_len_hint(arg), int(m.group("width") or 0))
	_check_text(total, "This % format")


# --------------------------------------------------------------------------- #
# Format specs (f-strings)                                                     #
# --------------------------------------------------------------------------- #

_SPEC = re.compile(
	r"^(?:.?[<>=^])?[-+ ]?z?#?0?(?P<width>\d+)?[,_]?(?:\.(?P<prec>\d+))?[a-zA-Z%]?$", re.DOTALL
)


def check_format_spec(spec: str):
	"""Refuse a format spec whose width or precision is above MAX_FORMAT_WIDTH. A spec
	that is not the standard mini-language (a date's strftime pattern) may be at most
	MAX_FORMAT_WIDTH characters long."""
	m = _SPEC.match(spec)
	if m:
		for part in ("width", "prec"):
			value = m.group(part)
			if value and int(value) > MAX_FORMAT_WIDTH:
				_refuse(f"a format width or precision above {MAX_FORMAT_WIDTH} is not available.")
	elif len(spec) > MAX_FORMAT_WIDTH:
		_refuse(f"a format spec longer than {MAX_FORMAT_WIDTH} characters is not available.")


# --------------------------------------------------------------------------- #
# Writing a value out                                                          #
# --------------------------------------------------------------------------- #

_CONTAINERS = (list, tuple, dict, set, frozenset)


def check_written_size(value, what: str = "Writing this value out"):
	"""Refuse to write out a value with more than MAX_WRITTEN_VALUES values once shared
	references are expanded. Each distinct container is visited once (its expanded
	count is remembered), so the check costs the distinct values, never the expansion."""
	if not isinstance(value, _CONTAINERS):
		return
	if len(value) > MAX_WRITTEN_VALUES:
		_refuse(f"{what} would write more than {MAX_WRITTEN_VALUES:,} values. Return a summary instead.")
	weights: dict[int, int] = {}
	stack = [(value, False)]
	while stack:
		node, done = stack.pop()
		key = id(node)
		if done:
			children = node.items() if isinstance(node, dict) else node
			total = 1
			for child in children:
				for part in child if isinstance(node, dict) else (child,):
					total += weights.get(id(part), 1) if isinstance(part, _CONTAINERS) else 1
				if total > MAX_WRITTEN_VALUES:
					break
			weights[key] = total
			if total > MAX_WRITTEN_VALUES:
				_refuse(
					f"{what} would write more than {MAX_WRITTEN_VALUES:,} values (shared references "
					"count each time they appear). Return a summary instead."
				)
			continue
		if key in weights:
			continue
		if len(node) > MAX_WRITTEN_VALUES:
			_refuse(f"{what} would write more than {MAX_WRITTEN_VALUES:,} values. Return a summary instead.")
		weights[key] = 1  # in progress: a cycle counts once
		stack.append((node, True))
		children = node.items() if isinstance(node, dict) else node
		for child in children:
			for part in child if isinstance(node, dict) else (child,):
				if isinstance(part, _CONTAINERS) and id(part) not in weights:
					stack.append((part, False))


def internal_import(name, globals=None, locals=None, fromlist=(), level=0):
	"""``__import__`` for C code the sandbox runs (datetime.strftime imports ``time``)."""
	if level == 0 and name in _INTERNAL_IMPORTS:
		import importlib

		return importlib.import_module(name)
	raise ImportError(f"No imports: {name} is not available.")


# --------------------------------------------------------------------------- #
# The guards, bound to one run (for the memory check after a big result)       #
# --------------------------------------------------------------------------- #


class Guards:
	"""The size guards one compute run calls. ``check_memory`` is the run's own."""

	def __init__(self, check_memory):
		self._check_memory = check_memory

	def _after(self, result):
		if _size_bytes(result) > BIG_RESULT_BYTES:
			self._check_memory()
		return result

	# Binary operators --------------------------------------------------------

	def precheck(self, op: str, a, b):
		"""Refuse ``a op b`` when its result would pass a cap. No allocation happens here."""
		if op == "+":
			if isinstance(a, _TEXT + _SEQ) and isinstance(b, _TEXT + _SEQ):
				_check_size(a, len(a) + len(b), "This +")
		elif op == "*":
			if isinstance(a, int) and isinstance(b, int):
				_check_int_bits(_bits(a) + _bits(b), "This *")
			else:
				for seq, n in ((a, b), (b, a)):
					if isinstance(seq, _TEXT + _SEQ) and isinstance(n, int):
						_check_size(seq, len(seq) * max(n, 0), "Repeating a sequence")
		elif op == "**":
			if isinstance(a, int) and isinstance(b, int) and b > 0 and abs(a) > 1:
				# bits(a**b) = b * log2(|a|), to within one bit; bits(a) * b is up to twice that.
				_check_int_bits(int(b * math.log2(abs(a))) + 1, "This power")
		elif op == "<<":
			if isinstance(a, int) and isinstance(b, int) and b > 0:
				_check_int_bits(_bits(a) + b, "This shift")
		elif op == "%":
			if isinstance(a, _TEXT):
				check_percent_format(a, b)

	def binop(self, op: str, a, b):
		self.precheck(op, a, b)
		if op == "+":
			result = a + b
		elif op == "*":
			result = a * b
		elif op == "**":
			result = a**b
		elif op == "<<":
			result = a << b
		else:
			result = a % b
		return self._after(result)

	def inplace(self, op: str, x, y, apply):
		"""``x op= y`` through ``apply`` (the in-place operator), with the same precheck."""
		base = op[:-1]
		if base in ("+", "*", "**", "<<", "%"):
			self.precheck(base, x, y)
		return self._after(apply(op, x, y))

	# Builtins -----------------------------------------------------------------

	def pow(self, base, exp, mod=None):
		if mod is None:
			return self.binop("**", base, exp)
		if isinstance(exp, int) and isinstance(mod, int):
			if _bits(exp) > MAX_MODPOW_BITS or _bits(mod) > MAX_MODPOW_BITS:
				_refuse(
					f"modular pow with an exponent or modulus above {MAX_MODPOW_BITS:,} bits is not available."
				)
		return pow(base, exp, mod)

	def round(self, number, ndigits=None):
		if isinstance(ndigits, int) and abs(ndigits) > MAX_ROUND_DIGITS:
			_refuse(f"round() to more than {MAX_ROUND_DIGITS} digits is not available.")
		return round(number) if ndigits is None else round(number, ndigits)

	def sum(self, iterable, /, start=0):
		if not isinstance(start, _NUMBER):
			_refuse(
				"sum() with a list or text start is not available (it is quadratic): use a "
				"comprehension, e.g. [x for part in parts for x in part]."
			)
		return sum(iterable, start)

	# f-strings ------------------------------------------------------------------

	def repr(self, value):
		check_written_size(value, "repr()")
		return self._after(repr(value))

	def apply(self, func, *args, **kwargs):
		"""A call with ``*args`` / ``**kwargs`` (RestrictedPython's ``_apply_``)."""
		if len(args) + len(kwargs) > MAX_STAR_ARGS:
			_refuse(f"a call with more than {MAX_STAR_ARGS:,} arguments through * is not available.")
		return func(*args, **kwargs)

	def str_type(self):
		"""``str`` for the script: calling it is size-checked, ``isinstance(x, str)`` and
		``str.upper`` / ``str.join`` (guarded) still work."""
		guards = self

		class _StrMeta(type):
			def __instancecheck__(cls, obj):
				return isinstance(obj, str)

			def __subclasscheck__(cls, sub):
				return issubclass(sub, str)

			def __call__(cls, *args, **kwargs):
				if args:
					check_written_size(args[0], "str()")
				return guards._after(str(*args, **kwargs))

			def __getattr__(cls, name):
				if name.startswith("_"):
					raise AttributeError(name)
				if name in _TEXT_METHODS:
					return lambda text, *a, **k: guards.method(text, name)(*a, **k)
				return getattr(str, name)

		return _StrMeta("str", (), {})

	def format_value(self, value, conversion: int, spec):
		"""One f-string replacement field: conversion, spec check, then the piece's size."""
		check_written_size(value, "This f-string field")
		if conversion == 115:
			value = str(value)
		elif conversion == 114:
			value = repr(value)
		elif conversion == 97:
			value = ascii(value)
		if spec:
			check_format_spec(spec)
		text = format(value, spec or "")
		_check_text(len(text), "This f-string field")
		return self._after(text)

	# Methods that grow their object ---------------------------------------------

	def method(self, obj, name: str):
		"""The guarded form of ``obj.name``, or None when it needs no guard."""
		if isinstance(obj, _TEXT):
			guard = _TEXT_METHODS.get(name)
			if guard is not None:
				return lambda *args, **kwargs: self._after(guard(obj, name, args, kwargs))
		elif isinstance(obj, list) and name == "extend":
			return lambda iterable: self._extend(obj, iterable)
		elif isinstance(obj, int) and name == "to_bytes":
			return lambda *args, **kwargs: self._to_bytes(obj, args, kwargs)
		elif isinstance(obj, datetime.date | datetime.time) and name == "strftime":
			return lambda fmt: self._strftime(obj, fmt)
		return None

	def _strftime(self, value, fmt):
		if isinstance(fmt, str) and len(fmt) > MAX_STRFTIME_CHARS:
			_refuse(f"a strftime pattern longer than {MAX_STRFTIME_CHARS} characters is not available.")
		return value.strftime(fmt)

	def _extend(self, lst: list, iterable):
		items = iterable if isinstance(iterable, _SEQ) else list(iterable)
		_check_items(len(lst) + len(items), "This extend")
		lst.extend(items)
		self._after(lst)

	def _to_bytes(self, n: int, args, kwargs):
		length = args[0] if args else kwargs.get("length", 1)
		if isinstance(length, int) and length > MAX_TEXT_CHARS:
			_check_text(length, "This to_bytes")
		return n.to_bytes(*args, **kwargs)


def _arg(args, kwargs, index: int, name: str, default=None):
	return args[index] if len(args) > index else kwargs.get(name, default)


def _pad(obj, name, args, kwargs):
	width = _arg(args, kwargs, 0, "width", 0)
	if isinstance(width, int):
		_check_text(width, f"This {name}")
	return getattr(obj, name)(*args, **kwargs)


def _expandtabs(obj, name, args, kwargs):
	size = _arg(args, kwargs, 0, "tabsize", 8)
	tab = "\t" if isinstance(obj, str) else b"\t"
	if isinstance(size, int):
		_check_text(len(obj) + obj.count(tab) * max(size, 0), "This expandtabs")
	return obj.expandtabs(*args, **kwargs)


def _replace(obj, name, args, kwargs):
	old, new = _arg(args, kwargs, 0, "old"), _arg(args, kwargs, 1, "new")
	count = _arg(args, kwargs, 2, "count", -1)
	if isinstance(old, _TEXT) and isinstance(new, _TEXT) and type(old) is type(obj):
		hits = obj.count(old) if old else len(obj) + 1
		if isinstance(count, int) and count >= 0:
			hits = min(hits, count)
		_check_text(len(obj) + hits * (len(new) - len(old)), "This replace")
	return obj.replace(*args, **kwargs)


def _join(obj, name, args, kwargs):
	items = list(_arg(args, kwargs, 0, "iterable", ()))
	total = sum(len(x) for x in items if isinstance(x, _TEXT)) + len(obj) * max(len(items) - 1, 0)
	_check_text(total, "This join")
	return obj.join(items)


def _translate(obj, name, args, kwargs):
	table = _arg(args, kwargs, 0, "table")
	if isinstance(table, dict):
		longest = max((len(v) for v in table.values() if isinstance(v, _TEXT)), default=1)
		_check_text(len(obj) * max(longest, 1), "This translate")
	return obj.translate(*args, **kwargs)


_TEXT_METHODS = {
	"ljust": _pad,
	"rjust": _pad,
	"center": _pad,
	"zfill": _pad,
	"expandtabs": _expandtabs,
	"replace": _replace,
	"join": _join,
	"translate": _translate,
}
