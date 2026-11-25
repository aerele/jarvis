"""Pure policy decisions for a connector call: the allowed-actions GATE and the
argument check against the cached ``inputSchema``.

Extracted from the broker so the two security-critical decisions are frappe-free
and unit-testable without a bench. The broker feeds a row-like object (anything
with ``.get(field)`` and, for actions, a list of child dicts) and turns a
returned ``(code, message)`` into its ``{ok: False}`` envelope; ``None`` means
allowed. No frappe import here.
"""

from __future__ import annotations

import json

from jarvis.connectors import schema

# The egress host match lives in its own module; these names stay importable here
# because the broker and older callers read them from ``policy``.
from jarvis.connectors.egress import (
	canonical_ip,
	egress_address_match,
	egress_match,
	normalise_host,
	rule_problems,
)


def action_decision(row, action: str) -> tuple[str, str] | None:
	"""Return ``(code, message)`` if ``action`` is NOT permitted on ``row``, else
	``None``.

	Rules (reading only the STORED child-row flags - never a tool's
	``annotations``, which the MCP spec says are untrusted):

	  * no child row for the action  -> deny (``action_unknown``)
	  * child ``allowed`` is truthy  -> allow
	  * otherwise -> deny (``action_denied``)

	``allowed`` is the ONLY allow bit. ``read_only`` never grants on its own: a
	new read-only tool is pre-checked (``allowed=1``) when it is first stored, so
	an ``allowed=0`` read-only row is one a user switched off, and must stay off.

	The child rows live on the ``allowed_actions`` table of ``Jarvis Connector``
	(a ``Jarvis Connector Action`` each: ``action``, ``allowed``, ``read_only``,
	``destructive``, ``description``)."""
	for child in row.get("allowed_actions") or []:
		if child.get("action") != action:
			continue
		if child.get("allowed"):
			return None
		return ("action_denied", f"The action {action!r} is not enabled for this connector.")
	return ("action_unknown", f"The action {action!r} is not configured on this connector.")


def argument_error(row, action: str, args, schema_obj=None) -> tuple[str, str] | None:
	"""Return ``("invalid_arguments", msg)`` if ``args`` violates the cached
	``inputSchema`` for ``action``, else ``None``. Missing/unparsable cache or an
	unknown tool means "cannot validate locally" -> allow (the server validates
	authoritatively). Pass ``schema_obj`` when the caller already parsed it so the
	cache is not read twice. See the ``tools_cache`` contract in ``broker.py``."""
	if schema_obj is None:
		schema_obj = input_schema(row, action)
	if schema_obj is None:
		return None
	err = schema.validate_arguments(schema_obj, args or {})
	if err:
		return ("invalid_arguments", err)
	return None


def input_schema(row, action: str):
	"""The cached MCP ``inputSchema`` for ``action`` on ``row`` (see the
	``tools_cache`` contract in ``broker.py``), or ``None`` when the cache is
	missing, unparsable, or does not list the action."""
	cache = row.get("tools_cache")
	if not cache:
		return None
	try:
		data = json.loads(cache) if isinstance(cache, str) else cache
	except (ValueError, TypeError):
		return None
	tools = data.get("tools") if isinstance(data, dict) else data
	for tool in tools or []:
		if isinstance(tool, dict) and tool.get("name") == action:
			return tool.get("inputSchema")
	return None
