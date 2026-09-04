"""Pure policy decisions for a connector call: the allowed-actions GATE and the
argument check against the cached ``inputSchema``.

Extracted from the broker so the two security-critical decisions are frappe-free
and unit-testable without a bench. The broker feeds a row-like object (anything
with ``.get(field)`` and, for actions, a list of child dicts) and turns a
returned ``(code, message)`` into its ``{ok: False}`` envelope; ``None`` means
allowed. No frappe import here.
"""

from __future__ import annotations

import fnmatch
import json

from jarvis.connectors import schema


def action_decision(row, action: str) -> tuple[str, str] | None:
	"""Return ``(code, message)`` if ``action`` is NOT permitted on ``row``, else
	``None``.

	Rules (reading only the STORED child-row flags - never a tool's
	``annotations``, which the MCP spec says are untrusted):

	  * no child row for the action  -> deny (``action_unknown``)
	  * child ``allowed`` is truthy  -> allow
	  * child is ``read_only`` and NOT ``destructive`` -> allow (auto-allowed)
	  * otherwise (destructive, or not marked allowed) -> deny (``action_denied``)

	The child rows live on the ``allowed_actions`` table of ``Jarvis Connector``
	(a ``Jarvis Connector Action`` each: ``action``, ``allowed``, ``read_only``,
	``destructive``, ``description``)."""
	for child in row.get("allowed_actions") or []:
		if child.get("action") != action:
			continue
		if child.get("allowed"):
			return None
		if child.get("read_only") and not child.get("destructive"):
			return None
		return ("action_denied", f"The action {action!r} is not enabled for this connector.")
	return ("action_unknown", f"The action {action!r} is not configured on this connector.")


def argument_error(row, action: str, args) -> tuple[str, str] | None:
	"""Return ``("invalid_arguments", msg)`` if ``args`` violates the cached
	``inputSchema`` for ``action``, else ``None``. Missing/unparsable cache or an
	unknown tool means "cannot validate locally" -> allow (the server validates
	authoritatively). See the ``tools_cache`` contract in ``broker.py``."""
	schema_obj = _input_schema(row, action)
	if schema_obj is None:
		return None
	err = schema.validate_arguments(schema_obj, args or {})
	if err:
		return ("invalid_arguments", err)
	return None


def egress_match(host: str, raw: str | None) -> bool:
	"""Decide whether ``host`` is permitted by the operator's egress rules ``raw``
	(the free-text ``Jarvis Settings.connector_egress_rules``, one rule per line).
	Fail-OPEN: empty/unset rules allow everything (the SSRF guard still blocks
	private/metadata addresses regardless of this).

	Grammar (blank lines and ``#`` comments ignored). Each rule is a host glob
	(``fnmatch``); a bare domain also matches its subdomains. A line beginning
	``!`` is a DENY rule, otherwise an ALLOW rule:

	  * any DENY rule matches           -> deny
	  * ALLOW rules exist but none match -> deny (allow-list mode)
	  * no ALLOW rules (deny-list/empty) -> allow
	"""
	if not raw:
		return True
	host = (host or "").lower()
	allows: list[str] = []
	denies: list[str] = []
	for line in raw.splitlines():
		rule = line.strip().lower()
		if not rule or rule.startswith("#"):
			continue
		if rule.startswith("!"):
			denies.append(rule[1:].strip())
		else:
			allows.append(rule)
	if any(_host_matches(host, r) for r in denies):
		return False
	if allows and not any(_host_matches(host, r) for r in allows):
		return False
	return True


def _host_matches(host: str, rule: str) -> bool:
	if not rule:
		return False
	return fnmatch.fnmatch(host, rule) or host == rule or host.endswith("." + rule)


def _input_schema(row, action: str):
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
