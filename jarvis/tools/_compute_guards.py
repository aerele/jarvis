"""Size and cost guards for the compute sandbox: no single operation may run long or allocate much.

The sandbox's time and memory checks run between statements, loop items and lambda
calls (``_compute_sandbox._Run.tick``). One C-level operation runs to its end without
returning to Python, so a check can only come before it: every operation that can
build a huge value, or do a huge amount of work, in one C call has its result size or
its cost worked out from its operands first and is refused above a cap.

Covered (each rewritten by the compile policy or wrapped in the namespace):

- ``a + b``, ``a * b``, ``a ** b``, ``a << b``, ``a % b``, ``a // b`` and the set
  operators (and the augmented forms, through the in-place guard): int results by bit
  length (``a*b`` as bits(a)+bits(b), ``a**b`` as b*log2(a), ``a<<n`` as bits(a)+n),
  str/bytes by length, list/tuple by item count, ``str % args`` by its widths and an
  estimate of its length, int division by operand size.
- builtins: ``pow`` (modular pow with bounded operands), ``round``, ``sum`` with a
  non-number start (quadratic), ``divmod``, ``sorted`` / ``min`` / ``max`` (by the cost
  of comparing what they order), ``repr`` / ``str`` (writing out), and the types
  ``int`` (from text or bytes), ``list`` / ``tuple`` (item count), ``set`` / ``dict``
  (item count and hash collisions); ``isinstance`` with them still works.
- comparisons: every operand of ``==``, ``<``, ``in`` ... and every key used to index
  or store into a dict is checked for the cost of comparing or hashing it.
- f-string replacement fields (widths, precisions, each piece's length).
- methods: str/bytes ``ljust rjust center zfill expandtabs replace join translate``,
  ``int.to_bytes`` / ``int.from_bytes``, ``list.extend sort count index``, the set
  methods that take iterables or keys, ``dict.update get pop setdefault fromkeys``,
  ``strftime``; also when reached through the class (``str.join(sep, items)``).

The cost of a comparison is the number of values it may visit, shared references
counted each time they appear, text counted in TEXT_COST_CHARS chunks. It is measured
with an id-memoised walk (each distinct container once), cached per run for flat
containers (no container inside) until they are written to.

The caps are module constants, each with its reason. A result over BIG_RESULT_BYTES is
followed at once by a memory check.
"""

from __future__ import annotations

import datetime
import math
import re

# Largest int an operation may produce: 2**16 bits is about 19,700 decimal digits, far
# beyond any amount; one multiply, power or division of that size takes well under a
# millisecond. (Python itself refuses to print an int of more than 4,300 digits.)
MAX_INT_BITS = 65_536

# Longest str or bytes an operation may produce. A tool result is capped at 35,000
# characters, so no answer needs more than this; it keeps any one text below 400 KB.
MAX_TEXT_CHARS = 100_000

# Most items a list, tuple, set or dict operation may produce: the per-call row cap
# (200,000), at most 1.6 MB of references.
MAX_SEQUENCE_ITEMS = 200_000

# Most values one comparison, sort, min / max, count / index or hash may visit (shared
# references counted each time, text in TEXT_COST_CHARS chunks). Comparing two
# 200,000-row lists of scalars stays inside; a deep shared structure does not.
MAX_COMPARE_COST = 2_000_000
TEXT_COST_CHARS = 64

# Python hashes an int as int % (2**61 - 1): ints at or above that can be chosen to fall
# in one bucket, which makes building a set or dict of them quadratic in C. Business
# numbers never get there; more than this many such keys in one set or dict is refused.
HASH_SAFE_INT = 2**61 - 1
MAX_HEAVY_HASH_KEYS = 1_000

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
# shared references as often as they appear. [big] * n or x = [x, x] in a loop is small
# in memory but enormous once written out, and writing it out is one C call.
MAX_WRITTEN_VALUES = 1_000_000

# Most arguments a call may receive through *args (zip(*[r] * 10**6) builds tuples of
# a million items in one C call). Unzipping a few thousand pairs stays well inside.
MAX_STAR_ARGS = 10_000

# Longest strftime pattern: a date format is a few dozen characters.
MAX_STRFTIME_CHARS = 200

# Modules C code inside the sandbox imports for itself (datetime.strftime / strptime,
# big-int division). The script cannot import anything: names starting with "_" never
# compile.
_INTERNAL_IMPORTS = frozenset({"time", "_strptime", "_pylong"})

_TEXT = (str, bytes)
_SEQ = (list, tuple)
_NUMBER = (int, float)
_CONTAINERS = (list, tuple, dict, set, frozenset)
_SCALARS = frozenset({int, float, str, bool, type(None), datetime.date, datetime.datetime})
_MISSING = object()


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
	if isinstance(value, (*_SEQ, dict, set, frozenset)):
		return len(value) * 8
	if isinstance(value, int):
		return value.bit_length() // 8
	return 0


# --------------------------------------------------------------------------- #
# Expanded size of a structure                                                 #
# --------------------------------------------------------------------------- #


def _unit_one(value) -> int:
	return 1


def _unit_cost(value) -> int:
	"""Cost of comparing or hashing one scalar: text by length, ints by size."""
	if isinstance(value, _TEXT):
		return len(value) // TEXT_COST_CHARS + 1
	if isinstance(value, int) and not isinstance(value, bool):
		return value.bit_length() // 64 + 1
	return 1


def expanded_size(value, unit, budget: int) -> int | None:
	"""The size of ``value`` with shared references expanded (each occurrence counted),
	or None once it passes ``budget``. Each distinct container is walked once and its
	expanded size remembered, so this costs the distinct values, never the expansion."""
	if not isinstance(value, _CONTAINERS):
		return unit(value)
	if len(value) > budget:
		return None
	weights: dict[int, int] = {}
	stack = [(value, False)]
	while stack:
		node, done = stack.pop()
		key = id(node)
		if done:
			total = 1
			for part in _parts(node):
				total += weights.get(id(part), 1) if isinstance(part, _CONTAINERS) else unit(part)
				if total > budget:
					return None
			weights[key] = total
			continue
		if key in weights:
			continue
		if len(node) > budget:
			return None
		weights[key] = 1  # in progress: a cycle counts once
		stack.append((node, True))
		for part in _parts(node):
			if isinstance(part, _CONTAINERS) and id(part) not in weights:
				stack.append((part, False))
	return weights[id(value)]


def _parts(node):
	if isinstance(node, dict):
		for k, v in node.items():
			yield k
			yield v
	else:
		yield from node


def check_written_size(value, what: str = "Writing this value out"):
	"""Refuse to write out a value with more than MAX_WRITTEN_VALUES values once shared
	references are expanded."""
	if isinstance(value, _CONTAINERS) and expanded_size(value, _unit_one, MAX_WRITTEN_VALUES) is None:
		_refuse(
			f"{what} would write more than {MAX_WRITTEN_VALUES:,} values (shared references count "
			"each time they appear). Return a summary instead."
		)


def _heavy_key(value, depth: int = 0) -> bool:
	"""Whether ``value`` hashes like an int that can be steered into one bucket."""
	if isinstance(value, int) and not isinstance(value, bool):
		return abs(value) >= HASH_SAFE_INT
	if isinstance(value, tuple) and depth < 4:
		return any(_heavy_key(v, depth + 1) for v in value)
	return False


def check_hash_keys(keys, what: str):
	heavy = 0
	for key in keys:
		if _heavy_key(key):
			heavy += 1
			if heavy > MAX_HEAVY_HASH_KEYS:
				_refuse(
					f"{what} with more than {MAX_HEAVY_HASH_KEYS:,} integers of 2**61 or more as keys is not "
					"available (they can all hash alike). Use smaller numbers or text as keys."
				)


def internal_import(name, globals=None, locals=None, fromlist=(), level=0):
	"""``__import__`` for C code the sandbox runs (datetime.strftime imports ``time``)."""
	if level == 0 and name in _INTERNAL_IMPORTS:
		import importlib

		return importlib.import_module(name)
	raise ImportError(f"No imports: {name} is not available.")


# --------------------------------------------------------------------------- #
# %-formatting and format specs                                                #
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
# Guarded types                                                                #
# --------------------------------------------------------------------------- #


def _guarded_type(name: str, real: type, call, attrs: dict):
	"""A stand-in for ``real`` the script calls and tests against: calling it runs
	``call``; ``isinstance`` / ``issubclass`` answer for ``real``; ``name.attr`` comes from
	``attrs`` (guarded) or ``real``."""

	class _Meta(type):
		def __instancecheck__(cls, obj):
			return isinstance(obj, real)

		def __subclasscheck__(cls, sub):
			return isinstance(sub, type) and issubclass(sub, real)

		def __call__(cls, *args, **kwargs):
			return call(*args, **kwargs)

		def __getattr__(cls, attr):
			if attr.startswith("_"):
				raise AttributeError(attr)
			if attr in attrs:
				return attrs[attr]
			return getattr(real, attr)

		def __repr__(cls):
			return f"<class '{name}'>"

	return _Meta(name, (), {})


# --------------------------------------------------------------------------- #
# The guards, bound to one run                                                 #
# --------------------------------------------------------------------------- #

_TEXT_METHOD_NAMES = frozenset(
	{"ljust", "rjust", "center", "zfill", "expandtabs", "replace", "join", "translate"}
)
_LIST_METHODS = frozenset({"extend", "sort", "count", "index"})
_SET_ITER_METHODS = frozenset(
	{
		"update",
		"union",
		"intersection",
		"difference",
		"symmetric_difference",
		"intersection_update",
		"difference_update",
		"symmetric_difference_update",
		"issubset",
		"issuperset",
		"isdisjoint",
	}
)
_SET_KEY_METHODS = frozenset({"add", "discard", "remove"})
_DICT_KEY_METHODS = frozenset({"get", "pop", "setdefault"})


class Guards:
	"""The guards one compute run calls. ``check_memory`` and ``tick`` are the run's own."""

	def __init__(self, check_memory, tick=None):
		self._check_memory = check_memory
		self._tick = tick or (lambda: None)
		# id -> (object, len, cost) for flat containers; the object is held so the id
		# cannot be reused. Dropped when the container is written to (``invalidate``).
		self._costs: dict[int, tuple] = {}

	def _after(self, result):
		if _size_bytes(result) > BIG_RESULT_BYTES:
			self._check_memory()
		return result

	# Cost of comparing / hashing ------------------------------------------------

	def invalidate(self, obj):
		self._costs.pop(id(obj), None)

	def check_cost(self, value, what: str):
		"""Refuse when comparing or hashing ``value`` could visit more than MAX_COMPARE_COST."""
		if type(value) in _SCALARS or not isinstance(value, _CONTAINERS):
			return
		hit = self._costs.get(id(value))
		if hit is not None and hit[0] is value and hit[1] == len(value):
			return
		cost = expanded_size(value, _unit_cost, MAX_COMPARE_COST)
		if cost is None:
			_refuse(
				f"{what} could take a very long time: the values involved are too large or nested "
				"too deeply (shared references count each time). Compare totals or keys instead."
			)
		if not any(isinstance(p, _CONTAINERS) for p in _parts(value)):
			self._costs[id(value)] = (value, len(value), cost)

	def compare(self, value, membership: int = 0):
		"""One operand of a comparison. ``membership`` 1: the right side of ``in``, where a
		dict or set is a hash lookup and only the left side is compared."""
		if membership == 1 and isinstance(value, dict | set | frozenset):
			return value
		if membership == 2:  # ``is``: identity, nothing is compared
			return value
		self.check_cost(value, "This comparison")
		return value

	def key(self, value):
		"""A key used to index or store into a dict (it is hashed and compared)."""
		self.check_cost(value, "Using this as a key")
		return value

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
		elif op in ("%", "//"):
			if op == "%" and isinstance(a, _TEXT):
				check_percent_format(a, b)
			elif isinstance(a, int) and isinstance(b, int):
				_check_int_bits(max(_bits(a), _bits(b)), "This division")
		elif op in ("|", "&", "-", "^"):
			if isinstance(a, set | frozenset | dict) or isinstance(b, set | frozenset | dict):
				for side in (a, b):
					if isinstance(side, set | frozenset | dict):
						_check_items(len(a) + len(b) if op == "|" else len(side), "This set operation")
						check_hash_keys(side, "This set operation")

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
		elif op == "%":
			result = a % b
		elif op == "//":
			result = a // b
		elif op == "|":
			result = a | b
		elif op == "&":
			result = a & b
		elif op == "-":
			result = a - b
		else:
			result = a ^ b
		return self._after(result)

	def inplace(self, op: str, x, y, apply):
		"""``x op= y`` through ``apply`` (the in-place operator), with the same precheck."""
		base = op[:-1]
		if base in ("+", "*", "**", "<<", "%", "//", "|", "&", "-", "^"):
			self.precheck(base, x, y)
		self.invalidate(x)
		return self._after(apply(op, x, y))

	# Materialising iterables ----------------------------------------------------

	def materialize(self, iterable, what: str, cap: int = MAX_SEQUENCE_ITEMS) -> list:
		"""``iterable`` as a list, refused past ``cap`` items without building more. A sized
		iterable is checked by its length first; anything else is read in Python, with the
		run's time and memory checks between chunks."""
		if hasattr(iterable, "__len__") and not isinstance(iterable, dict):
			n = len(iterable)
			if n > cap:
				_refuse(f"{what} would make a list of {n:,} items; the limit is {cap:,}.")
			if isinstance(iterable, list | tuple | range | str | bytes | set | frozenset):
				return list(iterable)
		out = []
		append = out.append
		for i, item in enumerate(iterable):
			if i >= cap:
				_refuse(f"{what} would make a list of more than {cap:,} items; the limit is {cap:,}.")
			if not i & 1023:
				self._tick()
				if i:
					self._check_memory()
			append(item)
		return out

	# Builtins and types -----------------------------------------------------------

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

	def divmod(self, a, b):
		self.precheck("//", a, b)
		return divmod(a, b)

	def _keys(self, items: list, key, what: str) -> list:
		keys = items if key is None else [key(item) for item in items]
		self.check_cost(keys, what)
		return keys

	def sorted(self, iterable, /, *, key=None, reverse=False):
		items = self.materialize(iterable, "sorted()")
		keys = self._keys(items, key, "This sort")
		if key is None:
			return sorted(items, reverse=reverse)
		order = sorted(range(len(items)), key=keys.__getitem__, reverse=reverse)
		return [items[i] for i in order]

	def _extreme(self, pick, name: str, args, key, default):
		if len(args) == 1:
			items = self.materialize(args[0], f"{name}()")
		else:
			items = list(args)
		if not items:
			if default is _MISSING:
				raise ValueError(f"{name}() arg is an empty sequence")
			return default
		keys = self._keys(items, key, f"This {name}()")
		return items[pick(range(len(items)), key=keys.__getitem__)]

	def min(self, *args, key=None, default=_MISSING):
		return self._extreme(min, "min", args, key, default)

	def max(self, *args, key=None, default=_MISSING):
		return self._extreme(max, "max", args, key, default)

	def repr(self, value):
		check_written_size(value, "repr()")
		return self._after(repr(value))

	def apply(self, func, *args, **kwargs):
		"""A call with ``*args`` / ``**kwargs`` (RestrictedPython's ``_apply_``)."""
		if len(args) + len(kwargs) > MAX_STAR_ARGS:
			_refuse(f"a call with more than {MAX_STAR_ARGS:,} arguments through * is not available.")
		return func(*args, **kwargs)

	def _class_methods(self, real: type, names) -> dict:
		"""``real.name(obj, ...)`` reached through the class, guarded like ``obj.name(...)``."""

		def bound(name):
			def call(obj, *args, **kwargs):
				guarded = self.method(obj, name) if isinstance(obj, real) else None
				return (
					guarded(*args, **kwargs)
					if guarded is not None
					else getattr(real, name)(obj, *args, **kwargs)
				)

			return call

		return {name: bound(name) for name in names}

	def types(self) -> dict:
		"""The guarded ``str``, ``int``, ``list``, ``tuple``, ``set`` and ``dict`` for the
		script's builtins."""

		def make_str(*args, **kwargs):
			if args:
				check_written_size(args[0], "str()")
			return self._after(str(*args, **kwargs))

		def make_int(*args, **kwargs):
			if args and isinstance(args[0], _TEXT):
				base = args[1] if len(args) > 1 else kwargs.get("base", 10)
				per_char = math.log2(base) if isinstance(base, int) and base >= 2 else 4
				_check_int_bits(int(len(args[0].strip()) * per_char) + 1, "This int()")
			return int(*args, **kwargs)

		def from_bytes(data, *args, **kwargs):
			_check_int_bits(len(data) * 8, "This from_bytes")
			return int.from_bytes(data, *args, **kwargs)

		def make_list(iterable=()):
			return self._after(self.materialize(iterable, "list()"))

		def make_tuple(iterable=()):
			return self._after(tuple(self.materialize(iterable, "tuple()")))

		def make_set(iterable=()):
			items = self.materialize(iterable, "set()")
			check_hash_keys(items, "set()")
			return self._after(set(items))

		def make_dict(*args, **kwargs):
			if args:
				source = args[0]
				pairs = (
					list(source.items()) if isinstance(source, dict) else self.materialize(source, "dict()")
				)
				check_hash_keys((p[0] for p in pairs if isinstance(p, _SEQ) and p), "dict()")
				_check_items(len(pairs) + len(kwargs), "dict()")
				return self._after(dict(pairs, **kwargs))
			return dict(**kwargs)

		def fromkeys(iterable, value=None):
			items = self.materialize(iterable, "dict.fromkeys()")
			check_hash_keys(items, "dict.fromkeys()")
			return self._after(dict.fromkeys(items, value))

		return {
			"str": _guarded_type("str", str, make_str, self._class_methods(str, _TEXT_METHOD_NAMES)),
			"int": _guarded_type(
				"int", int, make_int, {"from_bytes": from_bytes, **self._class_methods(int, ("to_bytes",))}
			),
			"list": _guarded_type("list", list, make_list, self._class_methods(list, _LIST_METHODS)),
			"tuple": _guarded_type("tuple", tuple, make_tuple, {}),
			"set": _guarded_type(
				"set", set, make_set, self._class_methods(set, _SET_ITER_METHODS | _SET_KEY_METHODS)
			),
			"dict": _guarded_type(
				"dict",
				dict,
				make_dict,
				{"fromkeys": fromkeys, **self._class_methods(dict, _DICT_KEY_METHODS | {"update"})},
			),
		}

	# f-strings ------------------------------------------------------------------

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

	# Methods ------------------------------------------------------------------------

	def method(self, obj, name: str):
		"""The guarded form of ``obj.name``, or None when it needs no guard."""
		if isinstance(obj, _TEXT):
			guard = _TEXT_METHODS.get(name)
			if guard is not None:
				return lambda *args, **kwargs: self._after(guard(self, obj, name, args, kwargs))
		elif isinstance(obj, list):
			if name == "extend":
				return lambda iterable: self._extend(obj, iterable)
			if name == "sort":
				return lambda *, key=None, reverse=False: self._sort(obj, key, reverse)
			if name in ("count", "index"):
				return lambda value, *args: self._search(obj, name, value, args)
		elif isinstance(obj, set | frozenset):
			if name in _SET_ITER_METHODS:
				return lambda *others: self._set_op(obj, name, others)
			if name in _SET_KEY_METHODS:
				return lambda value: getattr(obj, name)(self.key(value))
		elif isinstance(obj, dict):
			if name in _DICT_KEY_METHODS:
				return lambda k, *args: getattr(obj, name)(self.key(k), *args)
			if name == "update":
				return lambda *args, **kwargs: self._dict_update(obj, args, kwargs)
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
		items = self.materialize(iterable, "This extend")
		_check_items(len(lst) + len(items), "This extend")
		lst.extend(items)
		self.invalidate(lst)
		self._after(lst)

	def _sort(self, lst: list, key, reverse: bool):
		ordered = self.sorted(lst, key=key, reverse=reverse)
		lst.clear()
		lst.extend(ordered)

	def _search(self, lst: list, name: str, value, args):
		self.check_cost(lst, f"This {name}()")
		self.check_cost(value, f"This {name}()")
		return getattr(lst, name)(value, *args)

	def _set_op(self, obj, name: str, others):
		lists = [self.materialize(o, f"set.{name}()") for o in others]
		for items in lists:
			check_hash_keys(items, f"set.{name}()")
			_check_items(len(obj) + len(items), f"set.{name}()")
		self.invalidate(obj)
		return getattr(obj, name)(*lists)

	def _dict_update(self, obj: dict, args, kwargs):
		if args:
			source = args[0]
			pairs = (
				list(source.items())
				if isinstance(source, dict)
				else self.materialize(source, "dict.update()")
			)
			check_hash_keys((p[0] for p in pairs if isinstance(p, _SEQ) and p), "dict.update()")
			_check_items(len(obj) + len(pairs), "dict.update()")
			obj.update(pairs, **kwargs)
		else:
			obj.update(**kwargs)
		self.invalidate(obj)

	def _to_bytes(self, n: int, args, kwargs):
		length = args[0] if args else kwargs.get("length", 1)
		if isinstance(length, int) and length > MAX_TEXT_CHARS:
			_check_text(length, "This to_bytes")
		return n.to_bytes(*args, **kwargs)


def _arg(args, kwargs, index: int, name: str, default=None):
	return args[index] if len(args) > index else kwargs.get(name, default)


def _pad(guards, obj, name, args, kwargs):
	width = _arg(args, kwargs, 0, "width", 0)
	if isinstance(width, int):
		_check_text(width, f"This {name}")
	return getattr(obj, name)(*args, **kwargs)


def _expandtabs(guards, obj, name, args, kwargs):
	size = _arg(args, kwargs, 0, "tabsize", 8)
	tab = "\t" if isinstance(obj, str) else b"\t"
	if isinstance(size, int):
		_check_text(len(obj) + obj.count(tab) * max(size, 0), "This expandtabs")
	return obj.expandtabs(*args, **kwargs)


def _replace(guards, obj, name, args, kwargs):
	old, new = _arg(args, kwargs, 0, "old"), _arg(args, kwargs, 1, "new")
	count = _arg(args, kwargs, 2, "count", -1)
	if isinstance(old, _TEXT) and isinstance(new, _TEXT) and type(old) is type(obj):
		hits = obj.count(old) if old else len(obj) + 1
		if isinstance(count, int) and count >= 0:
			hits = min(hits, count)
		_check_text(len(obj) + hits * (len(new) - len(old)), "This replace")
	return obj.replace(*args, **kwargs)


def _join(guards, obj, name, args, kwargs):
	items = guards.materialize(_arg(args, kwargs, 0, "iterable", ()), "This join")
	total = sum(len(x) for x in items if isinstance(x, _TEXT)) + len(obj) * max(len(items) - 1, 0)
	_check_text(total, "This join")
	return obj.join(items)


def _translate(guards, obj, name, args, kwargs):
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
