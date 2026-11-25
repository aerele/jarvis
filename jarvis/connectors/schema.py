"""Validate connector-call arguments against a tool's cached MCP ``inputSchema``.

Runs BEFORE the outbound call so a bad argument set is a clean, local
``invalid_arguments`` tool error instead of a wasted round trip (and, for a
consequential connector, a wasted human confirmation). The schema comes from the
server's ``tools/list`` result cached on the row - see the ``tools_cache``
contract note in ``broker.py``.

If the ``jsonschema`` package is available we use it for a full Draft check;
otherwise we fall back to a deliberately LIGHT structural check (top-level
object, ``required`` present, primitive ``type`` match, ``additionalProperties:
false`` rejecting unknown keys). The light path is intentionally lenient on
anything it does not understand (nested schemas, ``anyOf``, formats): the goal
is to catch obvious caller mistakes, not to reimplement JSON Schema, and the
server validates authoritatively anyway. No frappe import - unit-tested without
a bench.
"""

from __future__ import annotations

try:  # pragma: no cover - presence depends on the environment
	import jsonschema

	_HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover
	jsonschema = None
	_HAS_JSONSCHEMA = False


# JSON Schema primitive type -> Python predicate. ``integer`` excludes bool
# (Python bools are ints); ``number`` accepts int/float but not bool.
def _is_number(v) -> bool:
	return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_integer(v) -> bool:
	return isinstance(v, int) and not isinstance(v, bool)


_TYPE_CHECKS = {
	"string": lambda v: isinstance(v, str),
	"number": _is_number,
	"integer": _is_integer,
	"boolean": lambda v: isinstance(v, bool),
	"object": lambda v: isinstance(v, dict),
	"array": lambda v: isinstance(v, list),
	"null": lambda v: v is None,
}


def _type_ok(value, type_spec) -> bool:
	# ``type`` may be a single string or a list of allowed types.
	if isinstance(type_spec, list):
		return any(_type_ok(value, t) for t in type_spec)
	check = _TYPE_CHECKS.get(type_spec)
	return True if check is None else check(value)


def validate_arguments(schema, args) -> str | None:
	"""Return an error string if ``args`` violates ``schema``, else ``None``.

	A schema that is missing, empty, or not an object-type schema is treated as
	"no constraints" (return ``None``): we only validate what we can read."""
	if not isinstance(schema, dict) or not schema:
		return None
	if schema.get("type") not in (None, "object"):
		return None
	if not isinstance(args, dict):
		return "arguments must be an object"

	if _HAS_JSONSCHEMA:
		try:
			jsonschema.validate(instance=args, schema=schema)
			return None
		except jsonschema.ValidationError as exc:  # type: ignore[union-attr]
			# Keep the message short and free of the full instance dump (it can
			# carry caller-supplied values we would rather not splash into logs).
			path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
			return f"argument {path} is invalid: {exc.message}"
		except Exception:
			# A malformed schema should not hard-fail the call; fall through to
			# the light check, which is defensive about what it does not grok.
			pass

	return _light_check(schema, args)


def _light_check(schema: dict, args: dict) -> str | None:
	props = schema.get("properties") or {}
	required = schema.get("required") or []
	for r in required:
		if r not in args:
			return f"missing required argument: {r}"
	if schema.get("additionalProperties") is False:
		extra = sorted(k for k in args if k not in props)
		if extra:
			return f"unexpected argument(s): {', '.join(extra)}"
	for key, value in args.items():
		spec = props.get(key)
		if not isinstance(spec, dict):
			continue
		type_spec = spec.get("type")
		if type_spec and not _type_ok(value, type_spec):
			want = type_spec if isinstance(type_spec, str) else "/".join(type_spec)
			return f"argument {key!r} should be {want}"
	return None
