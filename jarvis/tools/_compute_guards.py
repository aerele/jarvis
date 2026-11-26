"""Size and cost guards for the compute sandbox: no single operation may run long or allocate much.

The sandbox checks time and memory between statements, at every lambda call and for
every loop item (``_compute_sandbox._Run.tick``). One C-level operation runs to its end
without returning to Python, so a check can only come before it: every operation that
can build a huge value, or do a huge amount of work, in one C call has its result size
or its cost worked out from its operands first and is refused above a cap.

Two structural bounds make the rest tractable:

- **Nesting depth.** No container the script builds may be nested more than
  MAX_NESTING levels deep: every construction point checks it (displays, comprehension
  items, item and attribute assignment, append / insert / extend / setdefault / update /
  add, list() / tuple() / set() / dict() of nested input, json.loads). A deep shared
  structure (``a = [a, a]`` sixty times) cannot be built at all.
- **Cost of comparing or hashing.** Every comparison operand, dict or set key, hash(),
  sort, min / max and every comparing method (list.count / index / remove, tuple.count /
  index, the set methods and dict views) is checked first: the values it may visit,
  shared references counted each time, text in TEXT_COST_CHARS chunks. A container that
  holds no container is measured in C (the set of its item types) and that result is
  cached until the container is mutated in any way (every mutator method, item / attribute
  assignment, del, an in-place operator invalidates it); anything nested is always walked,
  with an id-memoised walk, never cached, so a mutated child can never leave a stale parent.

Covered besides (each rewritten by the compile policy or wrapped in the namespace):

- ``+ * ** << % //`` and the set operators, and their augmented forms: int results by
  bit length, str/bytes by length, list/tuple by item count, ``str % args`` by widths and
  an estimate of its length, int division by operand size.
- starred displays, starred calls and ``**`` unpacking (``[*r, *r]``, ``f(*r)``,
  ``{**d}``): each part read with the item cap, a running total across the parts, before
  anything is allocated.
- builtins ``pow round sum divmod sorted min max repr hash`` and the types ``str int list
  tuple set dict`` (``isinstance`` with them still works).
- f-string fields, %-format widths, writing out (str / repr / print / json / the result).
- methods that grow text or lists (``ljust rjust center zfill expandtabs replace join
  translate``, ``int.to_bytes`` / ``from_bytes``, ``list.extend``, ``strftime``), also when
  reached through the class (``str.join(sep, items)``).
- sets and dict keys: distinct integer keys that share a hash value (they can be chosen to
  fall into one bucket, which is quadratic in C) are counted; more than
  MAX_HASH_COLLISIONS is refused. Distinct large ints with distinct hashes (snowflake ids)
  are fine.

The caps are module constants, each with its reason.
"""

from __future__ import annotations

import datetime
import decimal
import math
import re
from itertools import chain, compress, islice, repeat
from operator import is_, is_not, itemgetter

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

# Deepest nesting a container the script builds may have. Results like
# {"top5": [{"customer": ..., "aging": {"0-30": ...}}]} are 4 levels; report rows 2;
# a document with child tables 3. Deep shared structures (each level doubling) need far
# more, and are what makes one comparison or hash exponential.
MAX_NESTING = 10

# Most values one comparison, sort, min / max, count / index or hash may visit (shared
# references counted each time, text in TEXT_COST_CHARS chunks). Comparing two
# 200,000-row lists of scalars stays inside; a large shared structure does not.
MAX_COMPARE_COST = 2_000_000
TEXT_COST_CHARS = 64

# Python hashes an int as int % (2**61 - 1): distinct ints at or above that can share one
# hash value and fall into one bucket, which makes building a set or dict of them
# quadratic in C. Distinct keys with a shared hash are counted; more than this is refused.
HASH_SAFE_INT = 2**61 - 1
MAX_HASH_COLLISIONS = 1_000

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
# shared references as often as they appear: writing it out is one C call.
MAX_WRITTEN_VALUES = 1_000_000

# Items an unsized iterable is read in, between time and memory checks: each chunk is
# one C call (a few hundred microseconds), a running total keeps it under the cap.
MATERIALIZE_CHUNK = 4_096

# Containers shorter than this are measured on every comparison (a few microseconds in
# C) rather than cached: the cache holds what it measures alive until the run ends.
CACHE_MIN_ITEMS = 256

# The cache is emptied when it would pass this many entries or this total cost (values
# visited), so even script objects the code has dropped stay alive only within a bound.
CACHE_MAX_ENTRIES = 32
CACHE_MAX_COST = 1_000_000

# Most arguments one call may receive (positional, *args and keywords together): zip(*x)
# of a few thousand pairs stays well inside.
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
_VIEWS = (type({}.keys()), type({}.values()), type({}.items()))
_CONTAINERS = (list, tuple, dict, set, frozenset, *_VIEWS)
# Right-hand sides of ``in`` that are a hash lookup, comparing only the left side.
_LOOKUPS = (dict, set, frozenset, type({}.keys()), type({}.items()))
_SCALARS = frozenset(
	{
		int,
		float,
		str,
		bytes,
		bool,
		type(None),
		datetime.date,
		datetime.datetime,
		datetime.time,
		datetime.timedelta,
		decimal.Decimal,
	}
)
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
# Structure: flatness, nesting, expanded size                                  #
# --------------------------------------------------------------------------- #


def _values(node):
	"""The items of a container as one iterable (a dict's keys and values)."""
	if isinstance(node, dict):
		return [*node.keys(), *node.values()] if len(node) < 64 else _dict_parts(node)
	return node


def _dict_parts(node):
	yield from node.keys()
	yield from node.values()


def _is_flat(node) -> bool:
	"""Whether a container holds no container, checked in C (stops at the first one)."""
	if isinstance(node, dict):
		return _SCALARS.issuperset(map(type, node.values())) and _SCALARS.issuperset(map(type, node))
	return _SCALARS.issuperset(map(type, node))


def _of_type(values: list, t: type) -> list:
	"""The items of ``values`` whose type is exactly ``t``, selected in C."""
	return list(compress(values, map(is_, map(type, values), repeat(t))))


def _scalar_extra(values: list, types: set) -> int:
	"""The cost of ``values`` (all scalars) beyond one per value: ints by size, text by
	length, worked out in C."""
	extra = 0
	if int in types:
		ints = values if types <= {int, bool} else _of_type(values, int)
		extra += sum(map(int.bit_length, ints)) // 64
	for t in (str, bytes):
		if t in types:
			texts = values if len(types) == 1 else _of_type(values, t)
			extra += sum(map(len, texts)) // TEXT_COST_CHARS
	return extra


def _quick_cost(node) -> int | None:
	"""Cost of comparing a container of scalars, or of scalars and tuples of scalars (sort
	keys, grouping keys), worked out in C. None for anything nested deeper. The cost of
	such a container changes only when the container itself is mutated (tuples and
	scalars are immutable), so it may be cached until then."""
	if isinstance(node, dict):
		values = [*node.keys(), *node.values()]
	elif isinstance(node, list | tuple | set | frozenset):
		values = node
	else:
		values = list(node)
	types = set(map(type, values))
	if _SCALARS.issuperset(types):
		return len(values) + _scalar_extra(values, types)
	if types - _SCALARS != {tuple}:
		return None
	tuples = values if len(types) == 1 else _of_type(values, tuple)
	inner = list(chain.from_iterable(tuples))
	inner_types = set(map(type, inner))
	if not _SCALARS.issuperset(inner_types):
		return None
	cost = len(values) + len(inner) + _scalar_extra(inner, inner_types)
	if len(types) > 1:
		rest = list(compress(values, map(is_not, map(type, values), repeat(tuple))))
		cost += _scalar_extra(rest, types - {tuple})
	return cost


def _extra_cost(value) -> int:
	if isinstance(value, _TEXT):
		return len(value) // TEXT_COST_CHARS
	if isinstance(value, int) and not isinstance(value, bool):
		return value.bit_length() // 64
	return 0


def _unit_cost(value) -> int:
	return 1 + _extra_cost(value)


def _unit_one(value) -> int:
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
			for part in _values(node):
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
		for part in _values(node):
			if isinstance(part, _CONTAINERS) and id(part) not in weights:
				stack.append((part, False))
	return weights[id(value)]


def nesting_depth(value, limit: int) -> int:
	"""How deeply ``value`` nests containers (1 for a container of scalars), stopping as
	soon as it passes ``limit``. Each distinct container is visited once; a container of
	scalars is recognised in C without walking its items. ``open_`` counts the
	containers entered and not yet finished, which in this walk are exactly the path
	from ``value`` down to the current one."""
	if not isinstance(value, _CONTAINERS):
		return 0
	depths: dict[int, int] = {}
	stack = [(value, False)]
	open_ = 0
	while stack:
		node, done = stack.pop()
		key = id(node)
		if done:
			open_ -= 1
			deepest = 0
			for part in _values(node):
				if isinstance(part, _CONTAINERS):
					deepest = max(deepest, depths.get(id(part), 1))
			depths[key] = deepest + 1
			if depths[key] > limit:
				return depths[key]
			continue
		if key in depths:
			continue
		if _is_flat(node):
			depths[key] = 1
			continue
		depths[key] = 1  # in progress: a cycle counts once
		open_ += 1
		if open_ >= limit:  # this path, plus the container inside the current node
			return limit + 1
		stack.append((node, True))
		for part in _values(node):
			if isinstance(part, _CONTAINERS) and id(part) not in depths:
				stack.append((part, False))
	return depths[id(value)]


def check_nesting(value, what: str = "This value"):
	"""Refuse a container nested more than MAX_NESTING levels deep."""
	if type(value) in _SCALARS or not isinstance(value, _CONTAINERS):
		return value
	if nesting_depth(value, MAX_NESTING) > MAX_NESTING:
		_refuse(
			f"{what} would nest containers more than {MAX_NESTING} levels deep. Keep results "
			"shallow: a list of dicts, or a dict of lists."
		)
	return value


def check_written_size(value, what: str = "Writing this value out"):
	"""Refuse to write out a value with more than MAX_WRITTEN_VALUES values once shared
	references are expanded."""
	if isinstance(value, _CONTAINERS) and expanded_size(value, _unit_one, MAX_WRITTEN_VALUES) is None:
		_refuse(
			f"{what} would write more than {MAX_WRITTEN_VALUES:,} values (shared references count "
			"each time they appear). Return a summary instead."
		)


# --------------------------------------------------------------------------- #
# Hash collisions                                                              #
# --------------------------------------------------------------------------- #


def _heavy_key(value, memo: dict, depth: int = 0) -> bool:
	"""Whether ``value`` hashes through an int that can share its hash with others. A
	tuple nested past MAX_NESTING counts as one (it can only come from a path the nesting
	checks do not see, so it is treated as the worst case). ``memo`` holds each tuple
	visited, so a shared structure costs its distinct tuples."""
	if isinstance(value, int) and not isinstance(value, bool):
		return abs(value) >= HASH_SAFE_INT
	if not isinstance(value, tuple):
		return False
	if depth > MAX_NESTING:
		return True
	hit = memo.get(id(value))
	if hit is None:
		memo[id(value)] = False  # in progress
		hit = any(_heavy_key(v, memo, depth + 1) for v in value)
		memo[id(value)] = hit
	return hit


def _canonical(value, memo: dict, depth: int = 0):
	"""A stand-in for a key whose hash never collides by accident: ints by their hex
	digits, tuples item by item (one stand-in per distinct tuple), anything nested past
	MAX_NESTING by its identity (counted as distinct, so it can only add to the count)."""
	if isinstance(value, int) and not isinstance(value, bool):
		return hex(value)
	if not isinstance(value, tuple):
		return value
	if depth > MAX_NESTING:
		return ("deep", id(value))
	hit = memo.get(id(value))
	if hit is None:
		hit = tuple(_canonical(v, memo, depth + 1) for v in value)
		memo[id(value)] = hit
	return hit


def check_hash_keys(keys, what: str):
	"""Refuse to hash many distinct keys that share hash values (int keys of 2**61 or more
	can be chosen so). Strings, floats and small ints cannot collide this way and are
	skipped in C; the rest are counted: distinct keys minus distinct hashes. Call after
	the keys' comparison cost was checked (a tuple key is walked here)."""
	keys = keys if isinstance(keys, list | tuple) else list(keys)
	types = set(map(type, keys))
	if not types & {int, tuple}:
		return
	if types <= {int, bool}:
		heavy = list(compress(keys, map(HASH_SAFE_INT.__le__, map(abs, keys))))
		if len(heavy) <= MAX_HASH_COLLISIONS:
			return
		collisions = len(set(map(hex, heavy))) - len(set(map(hash, heavy)))
	else:
		memo: dict = {}
		heavy = [k for k in keys if _heavy_key(k, memo)]
		if len(heavy) <= MAX_HASH_COLLISIONS:
			return
		memo = {}
		collisions = len({_canonical(k, memo) for k in heavy}) - len(set(map(hash, heavy)))
	if collisions > MAX_HASH_COLLISIONS:
		_refuse(
			f"{what} would put {collisions:,} distinct keys that hash alike in one table (large "
			"integers chosen to collide), which is very slow. Use text or smaller numbers as keys."
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


def guarded_type(name: str, real: type, call, attrs: dict):
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
# Methods, by type                                                             #
# --------------------------------------------------------------------------- #

_TEXT_METHOD_NAMES = frozenset(
	{"ljust", "rjust", "center", "zfill", "expandtabs", "replace", "join", "translate"}
)
# Every method that changes a list / dict / set in place: calling one drops the cached
# comparison cost of that container.
_LIST_MUTATORS = frozenset({"append", "insert", "extend", "pop", "remove", "clear", "sort", "reverse"})
_DICT_MUTATORS = frozenset({"setdefault", "update", "pop", "popitem", "clear"})
_SET_MUTATORS = frozenset(
	{
		"add",
		"discard",
		"remove",
		"pop",
		"clear",
		"update",
		"intersection_update",
		"difference_update",
		"symmetric_difference_update",
	}
)
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
# Set methods whose result (or the set itself) takes the other side's keys in.
_SET_GROWING = frozenset({"update", "union", "symmetric_difference", "symmetric_difference_update"})
_GUARDED_LIST_METHODS = _LIST_MUTATORS | {"count", "index"}
_GUARDED_DICT_METHODS = _DICT_MUTATORS | {"get"}
_GUARDED_SET_METHODS = _SET_MUTATORS | _SET_ITER_METHODS


# --------------------------------------------------------------------------- #
# The guards, bound to one run                                                 #
# --------------------------------------------------------------------------- #


class Guards:
	"""The guards one compute run calls. ``check_memory`` and ``tick`` are the run's own."""

	def __init__(self, check_memory, tick=None):
		self._check_memory = check_memory
		self._tick = tick or (lambda: None)
		# id -> (object, len, cost) for flat containers (no container inside); the object
		# is held so its id cannot be reused. Dropped on any mutation (``invalidate``).
		self._costs: dict[int, tuple] = {}
		self._cached_cost = 0  # sum of the cached costs: bounds what the cache keeps alive

	def _after(self, result):
		if _size_bytes(result) > BIG_RESULT_BYTES:
			self._check_memory()
		return result

	# Cost of comparing / hashing, and nesting -------------------------------------

	def invalidate(self, obj):
		self._costs.pop(id(obj), None)

	def check_cost(self, value, what: str, cache: bool = False):
		"""Refuse when comparing or hashing ``value`` could visit more than MAX_COMPARE_COST.
		``cache``: ``value`` is the script's own object (an operand, a key, the list a
		method is called on), so its measured cost may be remembered until it changes. A
		copy the guards made (a materialised iterable, sort keys) is never cached: the
		cache holds what it stores alive until the run ends."""
		if type(value) in _SCALARS or not isinstance(value, _CONTAINERS):
			return
		hit = self._costs.get(id(value))
		if hit is not None and hit[0] is value and hit[1] == len(value):
			return
		cost = _quick_cost(value)
		flat = cost is not None
		if not flat:
			cost = expanded_size(value, _unit_cost, MAX_COMPARE_COST)
		if cost is None or cost > MAX_COMPARE_COST:
			_refuse(
				f"{what} could take a very long time: the values involved are too large or nested "
				"too deeply (shared references count each time). Compare totals or keys instead."
			)
		if cache and flat and len(value) >= CACHE_MIN_ITEMS and not isinstance(value, _VIEWS):
			if len(self._costs) >= CACHE_MAX_ENTRIES or self._cached_cost + cost > CACHE_MAX_COST:
				self._costs.clear()
				self._cached_cost = 0
			self._costs[id(value)] = (value, len(value), cost)
			self._cached_cost += cost

	def compare(self, value, membership: int = 0):
		"""One operand of a comparison. ``membership`` 1: the right side of ``in``, where a
		dict or set is a hash lookup and only the left side is compared; 2: ``is``."""
		if type(value) in _SCALARS or membership == 2:
			return value
		if membership == 1 and isinstance(value, _LOOKUPS):
			return value
		self.check_cost(value, "This comparison", cache=True)
		return value

	def key(self, value):
		"""A key used to index, store into or look up in a dict or set (hashed and compared)."""
		if type(value) not in _SCALARS:
			self.check_cost(value, "Using this as a key", cache=True)
		return value

	def check_keys(self, keys: list, what: str, into=()):
		"""Keys about to be hashed into a set or dict in one C call: their comparison cost
		first, then hash collisions. ``into``: the keys already in the table they join
		(collisions add up across calls, so they are counted together)."""
		self.check_cost(keys, what)
		if into and set(map(type, keys)) & {int, tuple}:
			check_hash_keys([*into, *keys], what)
		else:
			check_hash_keys(keys, what)

	def nest(self, value):
		"""A value about to be put inside a container, or a container just built: refused
		if it nests too deeply. A container of scalars is recognised in C."""
		if type(value) in _SCALARS or not isinstance(value, _CONTAINERS) or _is_flat(value):
			return value
		return check_nesting(value, "Putting this value in a container")

	def hash(self, value):
		self.key(value)
		return hash(value)

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
			setlike = (set, frozenset, dict, *_VIEWS)
			if isinstance(a, setlike) and isinstance(b, setlike):
				_check_items(len(a) + len(b), "This set operation")
				self.check_cost(a, "This set operation", cache=True)
				self.check_keys(list(b), "This set operation", into=list(a))

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
		result = apply(op, x, y)
		if isinstance(result, _CONTAINERS):
			check_nesting(result, "This in-place operation")
		return self._after(result)

	# Materialising iterables ----------------------------------------------------

	def materialize(self, iterable, what: str, cap: int = MAX_SEQUENCE_ITEMS) -> list:
		"""``iterable`` as a list, refused past ``cap`` items without building more. A sized
		iterable is checked by its length first; anything else is read in chunks of
		MATERIALIZE_CHUNK items (each read in C), with the run's time and memory checks
		between chunks."""
		if hasattr(iterable, "__len__") and not isinstance(iterable, dict):
			n = len(iterable)
			if n > cap:
				_refuse(f"{what} would make a list of {n:,} items; the limit is {cap:,}.")
			if isinstance(iterable, (list, tuple, range, str, bytes, set, frozenset, *_VIEWS)):
				return list(iterable)
		it = iter(iterable)
		out: list = []
		while True:
			self._tick()
			chunk = list(islice(it, MATERIALIZE_CHUNK))
			out += chunk
			if len(out) > cap:
				_refuse(f"{what} would make a list of more than {cap:,} items; the limit is {cap:,}.")
			if len(chunk) < MATERIALIZE_CHUNK:
				return out
			self._check_memory()

	# Starred displays and calls -------------------------------------------------

	def build(self, kind: str, *parts):
		"""A list / tuple / set display with starred parts: each part is ``(starred,
		value)``; every starred part is read with the item cap, a running total across the
		display, before the result is built."""
		self._tick()
		items: list = []
		for starred, value in parts:
			if starred:
				items.extend(self._starred(value, len(items)))
			else:
				items.append(value)
				_check_items(len(items), "This display")
		check_nesting(items, "This display")
		if kind == "set":
			self.check_keys(items, "This set")
			return self._after(set(items))
		return self._after(tuple(items) if kind == "tuple" else items)

	def _starred(self, value, so_far: int) -> list:
		try:
			return self.materialize(value, "This starred display", MAX_SEQUENCE_ITEMS - so_far)
		except GuardRefused:
			_refuse(
				f"This starred display would make a list of more than {MAX_SEQUENCE_ITEMS:,} items; "
				f"the limit is {MAX_SEQUENCE_ITEMS:,}."
			)

	def build_dict(self, *parts):
		"""A dict display with ``**`` parts: each part is ``(0, key, value)``, or
		``(1, mapping)`` for ``**mapping``. The ``**`` keys are counted and checked before
		they are merged."""
		self._tick()
		out: dict = {}
		for part in parts:
			if part[0]:
				value = part[1]
				pairs = (
					list(value.items())
					if isinstance(value, dict)
					else self.materialize(value, "This ** unpacking")
				)
				_check_items(len(out) + len(pairs), "This ** unpacking")
				self.check_keys(_pair_keys(pairs), "This ** unpacking", into=out)
				out.update(pairs)
			else:
				out[self.key(part[1])] = part[2]
		check_nesting(out, "This dict")
		return self._after(out)

	def call(self, func, *parts):
		"""A call with ``*args`` / ``**kwargs``: each part is ``(0, value)`` positional,
		``(1, iterable)`` starred, ``(2, name, value)`` keyword or ``(3, mapping)`` ``**``.
		Arguments are read with a running total, before the call."""
		args: list = []
		kwargs: dict = {}
		for part in parts:
			flag = part[0]
			if flag == 0:
				args.append(part[1])
			elif flag == 1:
				args.extend(
					self.materialize(part[1], "The arguments of a call through *", MAX_STAR_ARGS - len(args))
				)
			elif flag == 2:
				_add_keyword(kwargs, part[1], part[2])
			else:
				mapping = part[1]
				if len(mapping) + len(args) + len(kwargs) > MAX_STAR_ARGS:
					_refuse(f"a call with more than {MAX_STAR_ARGS:,} arguments is not available.")
				for name in mapping:
					_add_keyword(kwargs, name, mapping[name])
			if len(args) + len(kwargs) > MAX_STAR_ARGS:
				_refuse(f"a call with more than {MAX_STAR_ARGS:,} arguments is not available.")
		return func(*args, **kwargs)

	def apply(self, func, *args, **kwargs):
		"""RestrictedPython's ``_apply_`` (kept for any call the policy did not rewrite)."""
		if len(args) + len(kwargs) > MAX_STAR_ARGS:
			_refuse(f"a call with more than {MAX_STAR_ARGS:,} arguments through * is not available.")
		return func(*args, **kwargs)

	# Builtins and types -----------------------------------------------------------

	def pow(self, base, exp, mod=None):
		self._tick()
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
		self._tick()
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
		keys = items if key is None else list(map(key, items))
		self.check_cost(keys, what)
		return keys

	def sorted(self, iterable, /, *, key=None, reverse=False):
		self._tick()
		items = self.materialize(iterable, "sorted()")
		check_nesting(items, "sorted()")
		keys = self._keys(items, key, "This sort")
		if key is None:
			return sorted(items, reverse=reverse)
		order = sorted(range(len(items)), key=keys.__getitem__, reverse=reverse)
		return [items[i] for i in order]

	def _extreme(self, pick, name: str, args, key, default):
		self._tick()
		if len(args) == 1:
			items = self.materialize(args[0], f"{name}()")
		else:
			items = list(args)
		if not items:
			if default is _MISSING:
				raise ValueError(f"{name}() arg is an empty sequence")
			return default
		keys = self._keys(items, key, f"This {name}()")
		if key is None:
			return pick(items)
		return items[pick(range(len(items)), key=keys.__getitem__)]

	def min(self, *args, key=None, default=_MISSING):
		return self._extreme(min, "min", args, key, default)

	def max(self, *args, key=None, default=_MISSING):
		return self._extreme(max, "max", args, key, default)

	def repr(self, value):
		self._tick()
		check_written_size(value, "repr()")
		return self._after(repr(value))

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
			self._tick()
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
			self._tick()
			items = self.materialize(iterable, "list()")
			check_nesting(items, "list()")
			return self._after(items)

		def make_tuple(iterable=()):
			self._tick()
			items = self.materialize(iterable, "tuple()")
			check_nesting(items, "tuple()")
			return self._after(tuple(items))

		def make_set(iterable=()):
			self._tick()
			items = self.materialize(iterable, "set()")
			check_nesting(items, "set()")
			self.check_keys(items, "set()")
			return self._after(set(items))

		def make_dict(*args, **kwargs):
			self._tick()
			if args:
				source = args[0]
				pairs = (
					list(source.items()) if isinstance(source, dict) else self.materialize(source, "dict()")
				)
				keys = _pair_keys(pairs)
				self.check_keys(keys, "dict()")
				_check_items(len(pairs) + len(kwargs), "dict()")
				out = dict(pairs, **kwargs)
			else:
				out = dict(**kwargs)
			check_nesting(out, "dict()")
			return self._after(out)

		def fromkeys(iterable, value=None):
			self._tick()
			items = self.materialize(iterable, "dict.fromkeys()")
			self.check_keys(items, "dict.fromkeys()")
			check_nesting(value, "dict.fromkeys()")
			return self._after(dict.fromkeys(items, value))

		return {
			"str": guarded_type("str", str, make_str, self._class_methods(str, _TEXT_METHOD_NAMES)),
			"int": guarded_type(
				"int", int, make_int, {"from_bytes": from_bytes, **self._class_methods(int, ("to_bytes",))}
			),
			"list": guarded_type("list", list, make_list, self._class_methods(list, _GUARDED_LIST_METHODS)),
			"tuple": guarded_type("tuple", tuple, make_tuple, self._class_methods(tuple, ("count", "index"))),
			"set": guarded_type("set", set, make_set, self._class_methods(set, _GUARDED_SET_METHODS)),
			"dict": guarded_type(
				"dict",
				dict,
				make_dict,
				{"fromkeys": fromkeys, **self._class_methods(dict, _GUARDED_DICT_METHODS)},
			),
		}

	# f-strings ------------------------------------------------------------------

	def format_value(self, value, conversion: int, spec):
		"""One f-string replacement field: conversion, spec check, then the piece's size."""
		if type(value) not in _SCALARS:
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
			if name in _GUARDED_LIST_METHODS:
				return self._list_method(obj, name)
		elif isinstance(obj, tuple):
			if name in ("count", "index"):
				return lambda value, *args: self._search(obj, name, value, args)
		elif isinstance(obj, set | frozenset):
			if name in _GUARDED_SET_METHODS:
				return self._set_method(obj, name)
		elif isinstance(obj, dict):
			if name in _GUARDED_DICT_METHODS:
				return self._dict_method(obj, name)
		elif isinstance(obj, _VIEWS):
			if name == "isdisjoint":
				return lambda other: self._view_isdisjoint(obj, other)
		elif isinstance(obj, int) and name == "to_bytes":
			return lambda *args, **kwargs: self._to_bytes(obj, args, kwargs)
		elif isinstance(obj, datetime.date | datetime.time) and name == "strftime":
			return lambda fmt: self._strftime(obj, fmt)
		return None

	def _list_method(self, lst: list, name: str):
		real = getattr(lst, name)
		if name == "extend":
			return lambda iterable: self._extend(lst, iterable)
		if name == "sort":
			return lambda *, key=None, reverse=False: self._sort(lst, key, reverse)
		if name in ("count", "index"):
			return lambda value, *args: self._search(lst, name, value, args)

		def call(*args, **kwargs):
			self.invalidate(lst)
			if name == "append":
				_check_items(len(lst) + 1, "This append")
				self.nest(args[0] if args else None)
			elif name == "insert":
				_check_items(len(lst) + 1, "This insert")
				self.nest(args[1] if len(args) > 1 else None)
			elif name == "remove":
				self.check_cost(lst, "This remove()", cache=True)
				self.check_cost(args[0] if args else None, "This remove()")
			return real(*args, **kwargs)

		return call

	def _dict_method(self, d: dict, name: str):
		real = getattr(d, name)
		if name == "update":
			return lambda *args, **kwargs: self._dict_update(d, args, kwargs)

		def call(*args, **kwargs):
			if name in ("get", "pop", "setdefault") and args:
				self.key(args[0])
			if name == "setdefault" and len(args) > 1:
				self.nest(args[1])
			if name != "get":
				self.invalidate(d)
			return real(*args, **kwargs)

		return call

	def _set_method(self, s, name: str):
		real = getattr(s, name)

		def call(*args, **kwargs):
			if name in _SET_MUTATORS:
				self.invalidate(s)
			if name in _SET_ITER_METHODS:
				lists = [self.materialize(o, f"set.{name}()") for o in args]
				self.check_cost(s, f"set.{name}()", cache=True)
				for items in lists:
					_check_items(len(s) + len(items), f"set.{name}()")
					self.check_keys(items, f"set.{name}()", into=s if name in _SET_GROWING else ())
					check_nesting(items, f"set.{name}()")
				return real(*lists)
			if name in ("add", "discard", "remove") and args:
				self.key(args[0])
				self.nest(args[0])
			return real(*args, **kwargs)

		return call

	def _view_isdisjoint(self, view, other):
		items = self.materialize(other, "isdisjoint()")
		self.check_cost(view, "isdisjoint()")
		self.check_cost(items, "isdisjoint()")
		return view.isdisjoint(items)

	def _strftime(self, value, fmt):
		if isinstance(fmt, str) and len(fmt) > MAX_STRFTIME_CHARS:
			_refuse(f"a strftime pattern longer than {MAX_STRFTIME_CHARS} characters is not available.")
		return value.strftime(fmt)

	def _extend(self, lst: list, iterable):
		items = self.materialize(iterable, "This extend")
		_check_items(len(lst) + len(items), "This extend")
		check_nesting(items, "This extend")
		self.invalidate(lst)
		lst.extend(items)
		self._after(lst)

	def _sort(self, lst: list, key, reverse: bool):
		ordered = self.sorted(lst, key=key, reverse=reverse)
		self.invalidate(lst)
		lst.clear()
		lst.extend(ordered)

	def _search(self, seq, name: str, value, args):
		self.check_cost(seq, f"This {name}()", cache=True)
		self.check_cost(value, f"This {name}()")
		return getattr(seq, name)(value, *args)

	def _dict_update(self, obj: dict, args, kwargs):
		self.invalidate(obj)
		if args:
			source = args[0]
			pairs = (
				list(source.items())
				if isinstance(source, dict)
				else self.materialize(source, "dict.update()")
			)
			keys = _pair_keys(pairs)
			self.check_keys(keys, "dict.update()", into=obj)
			check_nesting(pairs, "dict.update()")
			_check_items(len(obj) + len(pairs), "dict.update()")
			obj.update(pairs, **kwargs)
		else:
			check_nesting(kwargs, "dict.update()")
			obj.update(**kwargs)

	def _to_bytes(self, n: int, args, kwargs):
		length = args[0] if args else kwargs.get("length", 1)
		if isinstance(length, int) and length > MAX_TEXT_CHARS:
			_check_text(length, "This to_bytes")
		return n.to_bytes(*args, **kwargs)


def _add_keyword(kwargs: dict, name, value):
	if not isinstance(name, str):
		raise TypeError("keywords must be strings")
	if name in kwargs:
		raise TypeError(f"got multiple values for keyword argument '{name}'")
	kwargs[name] = value


def _pair_keys(pairs: list) -> list:
	"""The keys dict(pairs) would hash: each pair's first item (in C for the usual
	tuples and lists)."""
	try:
		return list(map(itemgetter(0), pairs))
	except (TypeError, IndexError, KeyError):
		return [next(iter(p), None) for p in pairs if isinstance(p, _TEXT + _SEQ) or hasattr(p, "__iter__")]


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
