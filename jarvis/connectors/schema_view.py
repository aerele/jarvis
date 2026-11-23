"""Shorten an over-large tool ``inputSchema`` without losing its field types.

The agent needs a schema to shape the arguments of a call. A schema over the
size budget used to be replaced by a list of property names, which left the
agent guessing at types. Here it is shortened in steps, and the first step
that fits wins:

  1. drop examples, defaults, ``$comment`` and ``title``; clip every
     ``description`` to 160 characters;
  2. clip descriptions to 60; keep the first 20 enum values plus ``enum_more``;
  3. drop descriptions below the top-level arguments;
  4. below depth 3, an object becomes its property names and an array keeps
     only its item type;
  5. last resort: ``{required, properties: {name: type}}``.

``$defs`` are kept and shortened by the same steps, never inlined. A shortened
schema is marked ``truncated`` with a note; the bench still validates calls
against the FULL cached schema, so this view only guides the agent.

The schema is untrusted server data: nothing here assumes its shape.
Frappe-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SCHEMA_MAX_CHARS = 24_000
NOTE = "Schema shortened to fit; the full schema still checks your arguments."

_MAX_NAMES = 100
_NAME_MAX = 128
_TYPE_MAX = 60
_NOISE = frozenset({"examples", "example", "default", "$comment", "title"})
# Keywords whose value is a map of NAME -> schema (names are not keywords).
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_ONE = frozenset({"items", "additionalProperties", "not", "if", "then", "else", "contains"})
_SCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})


@dataclass(frozen=True)
class _Step:
	description_max: int
	enum_max: int | None = None
	# Deepest level that keeps its description (0 = the root, 1 = its arguments).
	describe_depth: int | None = None
	# Objects and arrays deeper than this are collapsed.
	collapse_below: int | None = None


_STEPS = (
	_Step(160),
	_Step(60, enum_max=20),
	_Step(60, enum_max=20, describe_depth=1),
	_Step(60, enum_max=20, describe_depth=1, collapse_below=3),
)


def compact(schema, budget: int = SCHEMA_MAX_CHARS):
	"""``schema`` unchanged when it serialises within ``budget`` characters,
	else the first shortened form that does. Raises ``TypeError`` or
	``ValueError`` when ``schema`` cannot be serialised at all."""
	if _size(schema) <= budget:
		return schema
	if isinstance(schema, dict):
		for step in _STEPS:
			shrunk = _marked(_shorten(schema, step))
			if _size(shrunk) <= budget:
				return shrunk
	return last_resort(schema)


def last_resort(schema) -> dict:
	"""Required names and ``{name: type}`` for the top-level arguments, capped
	by count and, for absurdly long names, by total size."""
	properties = schema.get("properties") if isinstance(schema, dict) else None
	properties = properties if isinstance(properties, dict) else {}
	names = [name for name in properties if isinstance(name, str)]
	required = _names(schema.get("required") if isinstance(schema, dict) else None)
	keep = min(len(names), _MAX_NAMES)
	while True:
		out = _last_resort_dict(properties, names, required[:keep], keep)
		if keep == 0 or _size(out) <= SCHEMA_MAX_CHARS:
			return out
		keep //= 2


def _last_resort_dict(properties: dict, names: list[str], required: list[str], keep: int) -> dict:
	out: dict = {
		"truncated": True,
		"note": NOTE,
		"required": required,
		"properties": {name[:_NAME_MAX]: _type_of(properties[name]) for name in names[:keep]},
	}
	if len(names) > keep:
		out["properties_more"] = len(names) - keep
	return out


def _size(schema) -> int:
	return len(json.dumps(schema))


def _marked(shrunk: dict) -> dict:
	return {**shrunk, "truncated": True, "note": NOTE}


def _shorten(schema: dict, step: _Step) -> dict:
	return _node(schema, step, 0)


def _node(node, step: _Step, depth: int):
	if not isinstance(node, dict):
		return node
	if step.collapse_below is not None and depth > step.collapse_below:
		collapsed = _collapsed(node)
		if collapsed is not None:
			return collapsed
	out: dict = {}
	for key, value in node.items():
		if key in _NOISE:
			continue
		if key == "description":
			if step.describe_depth is None or depth <= step.describe_depth:
				out[key] = value[: step.description_max] if isinstance(value, str) else value
		elif key == "enum" and step.enum_max is not None and isinstance(value, list):
			out[key] = value[: step.enum_max]
			if len(value) > step.enum_max:
				out["enum_more"] = len(value) - step.enum_max
		else:
			out[key] = _child(key, value, step, depth)
	return out


def _child(key: str, value, step: _Step, depth: int):
	if key in _SCHEMA_MAPS and isinstance(value, dict):
		return {name: _node(sub, step, depth + 1) for name, sub in value.items()}
	if key in _SCHEMA_ONE:
		if isinstance(value, list):
			return [_node(member, step, depth + 1) for member in value]
		return _node(value, step, depth + 1)
	if key in _SCHEMA_LISTS and isinstance(value, list):
		return [_node(member, step, depth + 1) for member in value]
	return value


def _collapsed(node: dict) -> dict | None:
	"""The names-only form of an object, or the item-type form of an array;
	``None`` for anything else (it is kept and shortened normally)."""
	kind = node.get("type")
	properties = node.get("properties")
	if kind == "object" and isinstance(properties, dict):
		return {"type": "object", "properties": _names(list(properties))}
	if kind == "array":
		items = node.get("items")
		item_type = items.get("type") if isinstance(items, dict) else None
		return {"type": "array", "items": {"type": item_type}} if item_type else {"type": "array"}
	return None


def _names(raw) -> list[str]:
	if not isinstance(raw, list):
		return []
	return [name[:_NAME_MAX] for name in raw if isinstance(name, str)][:_MAX_NAMES]


def _type_of(prop) -> str:
	"""A short type label for one property schema."""
	if not isinstance(prop, dict):
		return "any"
	kind = prop.get("type")
	if isinstance(kind, list):
		kind = "|".join(item for item in kind if isinstance(item, str))
	if not isinstance(kind, str) or not kind:
		kind = _union_type(prop) or prop.get("$ref")
	return kind[:_TYPE_MAX] if isinstance(kind, str) and kind else "any"


def _union_type(prop: dict) -> str:
	members = prop.get("anyOf") or prop.get("oneOf")
	if not isinstance(members, list):
		return ""
	kinds = [m.get("type") for m in members if isinstance(m, dict) and isinstance(m.get("type"), str)]
	return "|".join(dict.fromkeys(kinds))
