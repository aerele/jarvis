"""Coverage guard: every ``@frappe.whitelist`` endpoint under ``jarvis/chat/``
must be access-gated (security review PART 1 TASK 8).

The requirement ("Jarvis + chat accessible ONLY if the Jarvis User role is
given") only holds if NO whitelisted chat endpoint is reachable without an
access check. Historically the role gate lived only in ``chat/api.py`` and was
applied ad-hoc, so ~60 SPA endpoints across the other ``chat/*_api.py`` modules
were reachable with just a session cookie + CSRF token. This test enumerates
every whitelisted function under ``jarvis/chat/`` (AST-derived, never a
hand-kept list) and asserts each one is gated, so the "new endpoint forgot the
gate" regression class fails CI permanently.

An endpoint counts as gated when it:
  * carries the ``@require_jarvis_user`` decorator (the preferred forward form,
    ``jarvis.permissions``), OR
  * calls a recognized access guard in its body — the Jarvis-User gate
    (``require_jarvis_access``), a stricter role gate
    (``frappe.only_for`` / the module reviewer/admin ``_guard`` helpers), a
    System-User gate (``_require_system_user``), or a per-doc ownership gate
    (``check_permission`` / ``_may_act_on`` / ``_get_owned_conversation``) that
    the security review blesses as "already correct", OR
  * is listed in :data:`_OWNER_GATED_ALLOWLIST` with a one-line justification.

Any whitelisted chat endpoint that is none of these fails the test.
"""

from __future__ import annotations

import ast
import os

from frappe.tests.utils import FrappeTestCase

_CHAT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chat")

# The gate decorator (marker the wrapper stamps + the source-level name).
_GATE_DECORATOR = "require_jarvis_user"

# Substrings identifying an access-guard CALL inside an endpoint body. Kept in
# sync with the guards the security review recognizes; a new module-local guard
# helper just needs a recognizable name (``_guard`` / ``_require_*`` / ``*only_for*``).
_GUARD_CALL_SUBSTRS = (
	"require_jarvis_access",
	"require_jarvis_user",
	# Tenant-admin gate. STRICTER than require_jarvis_user: it demands the Jarvis
	# Admin role (or System Manager / Administrator), not merely Jarvis User. The
	# admin_* usage endpoints (user_settings_api) call it, so without this entry the
	# sweep reported them as ungated -- a false positive on endpoints that are in
	# fact gated harder than the sweep's own baseline.
	"require_jarvis_admin",
	"require_skill_reviewer",  # PART 2 TASK 12: skill-reviewer/admin gate (org-wide apply)
	# Admin-level access CHECK (bool), the analogue of has_jarvis_access below. The
	# reviewer-or-admin endpoints (agents_api.promote_installation /
	# demote_installation) gate in-body on `me != doc.reviewer and not
	# has_jarvis_admin_access(me)` -> frappe.throw(PermissionError) — a gate STRICTER
	# than require_jarvis_user (the caller must be the named reviewer or a Jarvis
	# Admin), so without this entry the sweep false-positived them as ungated.
	"has_jarvis_admin_access",
	"_require_system_user",
	"only_for",  # frappe.only_for(...) — an SM/role gate, stricter than Jarvis User
	"_guard",  # learned_api._guard / _admin_guard (reviewer/admin role gate)
	"_reviewer_roles",
	"_admin_roles",
	"check_permission",  # per-doc owner/role gate (Frappe native)
	"_may_act_on",  # approvals ownership gate
	"_get_owned_conversation",  # chat/api ownership gate
	"has_jarvis_access",
)

# Endpoints intentionally gate-free (with a one-line justification). Empty by
# design: every current chat endpoint is gated by decorator or an in-body guard.
# Add an entry ONLY with a reviewer-approved reason (e.g. a deliberately public
# health probe); the test then permits it and documents why.
_OWNER_GATED_ALLOWLIST: dict[str, str] = {}


def _is_whitelisted(node: ast.FunctionDef) -> bool:
	return any("whitelist" in ast.unparse(d) for d in node.decorator_list)


def _has_gate_decorator(node: ast.FunctionDef) -> bool:
	return any(_GATE_DECORATOR in ast.unparse(d) for d in node.decorator_list)


def _calls_a_guard(node: ast.FunctionDef) -> bool:
	for sub in ast.walk(node):
		if isinstance(sub, ast.Call):
			try:
				name = ast.unparse(sub.func)
			except Exception:
				name = ""
			if any(g in name for g in _GUARD_CALL_SUBSTRS):
				return True
	return False


def _iter_chat_endpoints():
	"""Yield ``(module, funcname, node)`` for every whitelisted function under
	jarvis/chat/ — AST-derived so a new endpoint is swept automatically."""
	for fname in sorted(os.listdir(_CHAT_DIR)):
		if not fname.endswith(".py"):
			continue
		path = os.path.join(_CHAT_DIR, fname)
		with open(path, encoding="utf-8") as fh:
			tree = ast.parse(fh.read(), filename=path)
		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_whitelisted(node):
				yield fname[:-3], node.name, node


class TestChatEndpointGating(FrappeTestCase):
	def test_every_chat_endpoint_is_access_gated(self):
		found_any = False
		offenders = []
		for module, name, node in _iter_chat_endpoints():
			found_any = True
			key = f"{module}.{name}"
			if key in _OWNER_GATED_ALLOWLIST:
				continue
			if _has_gate_decorator(node) or _calls_a_guard(node):
				continue
			offenders.append(key)

		self.assertTrue(
			found_any,
			"sweep found no whitelisted chat endpoints - the AST walk broke",
		)
		self.assertFalse(
			offenders,
			"ungated @frappe.whitelist chat endpoints (reachable with just a "
			"session cookie + CSRF - add @require_jarvis_user, call an access "
			"guard, or allowlist with a justification): " + ", ".join(sorted(offenders)),
		)
