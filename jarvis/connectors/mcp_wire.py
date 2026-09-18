"""Pure wire-level helpers for the MCP client: SSE frame parsing, JSON-RPC
response matching, header value encoding, modern-era error recognition and the
``x-mcp-header`` parameter mirroring the 2026-07-28 Streamable HTTP transport
requires. No frappe import, no socket - everything here is unit-tested flat.

Spec references (revision 2026-07-28): basic/transports/streamable-http
(Request Metadata, Value Encoding, Custom Headers from Tool Parameters,
Backward Compatibility), basic/versioning (UnsupportedProtocolVersionError),
server/tools (Tool Names).
"""

from __future__ import annotations

import base64
import json
import re

MODERN_PROTOCOL_VERSION = "2026-07-28"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

# JSON-RPC error codes a MODERN server uses (spec: Backward Compatibility).
RPC_HEADER_MISMATCH = -32020
RPC_MISSING_CLIENT_CAPABILITY = -32021
RPC_UNSUPPORTED_PROTOCOL_VERSION = -32022
RPC_METHOD_NOT_FOUND = -32601

# server/tools "Tool Names": letters, digits, underscore, hyphen, dot; 1-128.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
# RFC 9110 field-name token characters, for x-mcp-header values.
_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="


# --------------------------------------------------------------------------- #
# SSE + JSON-RPC matching
# --------------------------------------------------------------------------- #
def iter_sse_messages(raw: bytes):
	"""Yield the parsed JSON value of each complete SSE ``data:`` event in
	``raw``. Events are separated by a blank line; a ``data:`` field may span
	several lines (joined with ``\\n``); ``:`` comment lines and other fields
	(``event:``, ``id:``, ``retry:``) are ignored. A ``data`` payload that is not
	valid JSON is skipped (a keep-alive or non-JSON-RPC event), never raised on."""
	text = raw.decode("utf-8", errors="replace")
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	for block in text.split("\n\n"):
		data_lines = []
		for line in block.split("\n"):
			if not line or line.startswith(":"):
				continue
			field, _, value = line.partition(":")
			if field != "data":
				continue
			data_lines.append(value[1:] if value.startswith(" ") else value)
		if not data_lines:
			continue
		payload = "\n".join(data_lines).strip()
		if not payload:
			continue
		try:
			yield json.loads(payload)
		except (ValueError, TypeError):
			continue


def matches_response(msg, request_id) -> bool:
	"""True when ``msg`` is the JSON-RPC response to our request: same ``id`` and
	carrying either ``result`` or ``error``. Server-to-client requests (they have
	an ``id`` AND a ``method``) and notifications (no ``id``) are NOT our
	response and must be skipped."""
	if not isinstance(msg, dict):
		return False
	if msg.get("method") is not None:
		return False
	if msg.get("id") != request_id:
		return False
	return "result" in msg or "error" in msg


# --------------------------------------------------------------------------- #
# modern era: error recognition + version pick
# --------------------------------------------------------------------------- #
def parse_rpc_error(raw: bytes) -> dict | None:
	"""The JSON-RPC ``error`` object in a 4xx body, or ``None`` when the body is
	empty, not JSON, or not shaped like a JSON-RPC error response."""
	if not raw:
		return None
	try:
		msg = json.loads(raw.decode("utf-8"))
	except (ValueError, UnicodeDecodeError):
		return None
	if not isinstance(msg, dict) or not isinstance(msg.get("error"), dict):
		return None
	err = msg["error"]
	return err if isinstance(err.get("code"), int) else None


def is_modern_probe_error(err: dict | None) -> bool:
	"""On a ``server/discover`` probe, only a version, header or capability error
	proves a modern server: a modern server MUST implement ``server/discover``, so
	"method not found" there is a legacy server's answer, not a modern one's."""
	return bool(err) and err.get("code") in (
		RPC_HEADER_MISMATCH,
		RPC_MISSING_CLIENT_CAPABILITY,
		RPC_UNSUPPORTED_PROTOCOL_VERSION,
	)


def supported_versions(err: dict | None) -> list[str]:
	"""The ``data.supported`` list of an UnsupportedProtocolVersionError."""
	if not err or err.get("code") != RPC_UNSUPPORTED_PROTOCOL_VERSION:
		return []
	data = err.get("data") or {}
	versions = data.get("supported") if isinstance(data, dict) else None
	return [v for v in (versions or []) if isinstance(v, str)]


def pick_legacy_version(versions, accepted) -> str | None:
	"""Newest version in ``versions`` that we also speak, else ``None``."""
	common = [v for v in versions if v in accepted]
	return max(common) if common else None


# --------------------------------------------------------------------------- #
# header values
# --------------------------------------------------------------------------- #
def encode_header_value(value) -> str:
	"""A header-safe string for ``value`` (spec: Value Encoding). Booleans become
	``true``/``false``, integers their decimal form. Anything not plain visible
	ASCII, with leading/trailing whitespace, or that itself looks like the
	sentinel, is carried Base64-encoded as ``=?base64?...?=``."""
	if isinstance(value, bool):
		text = "true" if value else "false"
	elif isinstance(value, int):
		text = str(value)
	else:
		text = str(value)
	if _is_header_safe(text):
		return text
	encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
	return f"{_SENTINEL_PREFIX}{encoded}{_SENTINEL_SUFFIX}"


def _is_header_safe(text: str) -> bool:
	if not text or text != text.strip():
		return False
	if text.startswith(_SENTINEL_PREFIX) and text.endswith(_SENTINEL_SUFFIX):
		return False
	return all(0x21 <= ord(ch) <= 0x7E or ch == " " for ch in text)


# --------------------------------------------------------------------------- #
# x-mcp-header: schema validation + value extraction
# --------------------------------------------------------------------------- #
def header_annotation_error(input_schema) -> str | None:
	"""Why ``input_schema``'s ``x-mcp-header`` annotations are invalid, or
	``None`` when the tool is usable. An invalid annotation makes the WHOLE tool
	definition invalid and the client MUST drop it from ``tools/list`` (spec:
	Schema Extension). Only ``properties`` chains are searched; an annotation
	anywhere else (``items``, ``oneOf``, ``$ref``, ...) is an error."""
	if not isinstance(input_schema, dict):
		return None
	seen: set[str] = set()
	for path, prop in _walk_properties(input_schema):
		name = prop.get("x-mcp-header")
		if name is None:
			continue
		if not isinstance(name, str) or not name or not _HEADER_TOKEN_RE.match(name):
			return f"x-mcp-header on {'.'.join(path)} is not a valid header token"
		if prop.get("type") not in ("string", "integer", "boolean"):
			return f"x-mcp-header on {'.'.join(path)} is not a string, integer or boolean"
		key = name.lower()
		if key in seen:
			return f"x-mcp-header {name!r} is declared twice"
		seen.add(key)
	if _annotation_outside_properties(input_schema):
		return "x-mcp-header used outside a plain properties chain"
	return None


def header_params(input_schema, args) -> dict[str, str]:
	"""``{"Mcp-Param-<name>": encoded value}`` for every annotated property that
	has a value in ``args`` at its exact property path. Missing values are omitted
	(spec: Header extraction). Assumes :func:`header_annotation_error` is None."""
	headers: dict[str, str] = {}
	if not isinstance(input_schema, dict) or not isinstance(args, dict):
		return headers
	for path, prop in _walk_properties(input_schema):
		name = prop.get("x-mcp-header")
		if not isinstance(name, str) or not _HEADER_TOKEN_RE.match(name):
			continue  # never build a header from a name sanitize did not vet
		value = _value_at(args, path)
		if value is None or isinstance(value, (dict, list, float)):
			continue
		headers[f"Mcp-Param-{name}"] = encode_header_value(value)
	return headers


def _walk_properties(schema: dict, path: tuple = ()):
	"""Yield ``(path, property_schema)`` for every property reachable through a
	chain of ``properties`` keys only."""
	props = schema.get("properties")
	if not isinstance(props, dict):
		return
	for key, prop in props.items():
		if not isinstance(prop, dict):
			continue
		here = (*path, str(key))
		yield here, prop
		yield from _walk_properties(prop, here)


def _annotation_outside_properties(node, in_properties_chain: bool = True) -> bool:
	"""True when an ``x-mcp-header`` key sits under a keyword other than a plain
	``properties`` chain from the root."""
	if isinstance(node, dict):
		for key, child in node.items():
			if key == "x-mcp-header" and not in_properties_chain:
				return True
			if key == "properties" and isinstance(child, dict):
				for prop in child.values():
					if _annotation_outside_properties(prop, in_properties_chain):
						return True
			elif key != "x-mcp-header" and _annotation_outside_properties(child, False):
				return True
	elif isinstance(node, list):
		return any(_annotation_outside_properties(item, False) for item in node)
	return False


def _value_at(args: dict, path: tuple):
	node = args
	for key in path:
		if not isinstance(node, dict) or key not in node:
			return None
		node = node[key]
	return node
