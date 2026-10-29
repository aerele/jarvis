"""Guard: every ``frappe.whitelist`` declares its HTTP methods, and only an
explicit allowlist of endpoints accepts GET.

A pure AST scan of the jarvis package. It needs no site data and imports no
jarvis module, so a new endpoint without ``methods=`` fails here on any Frappe.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

from frappe.tests.utils import FrappeTestCase

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Endpoints that a browser opens as a link, an image source or a provider
# redirect, so they must accept GET. Everything else is POST-only.
GET_ALLOWLIST = frozenset(
	{
		# <a href> and <img src> in the shared transcript view.
		"jarvis.chat.sharing.get_shared_file",
		# <a href> and <img src> for support attachments.
		"jarvis.support.media.download",
		# Download link for an exported conversation.
		"jarvis.chat.export.download_conversation",
<<<<<<< HEAD
		# MCP connectors (and their OAuth callback) are not on this branch.
=======
		# Top-level redirect from the MCP OAuth provider.
		"jarvis.chat.connectors_api.mcp_oauth_callback",
		# Read-only boot payload for the holiday touch; refetched by the client without a reload.
		"jarvis.holiday_touch.api.get_today",
>>>>>>> 1a653b6 (fix(holiday-touches): register get_today with the endpoint audits; satisfy semgrep)
	}
)


def _whitelist_names(tree: ast.AST) -> tuple[set[str], set[str]]:
	"""Names that mean ``frappe.whitelist`` in this module, from its imports.

	Returns (module aliases for frappe, bare names bound to whitelist).
	"""
	modules = {"frappe"}
	bare = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			modules.update(a.asname for a in node.names if a.name == "frappe" and a.asname)
		elif isinstance(node, ast.ImportFrom) and node.module == "frappe" and node.level == 0:
			bare.update(a.asname or a.name for a in node.names if a.name == "whitelist")
	return modules, bare


def _is_whitelist(node: ast.AST, names: tuple[set[str], set[str]]) -> bool:
	modules, bare = names
	if isinstance(node, ast.Name):
		return node.id in bare
	return (
		isinstance(node, ast.Attribute)
		and node.attr == "whitelist"
		and isinstance(node.value, ast.Name)
		and node.value.id in modules
	)


def _methods_of(call: ast.Call) -> list[str] | None:
	for kw in call.keywords:
		if kw.arg == "methods":
			try:
				value = ast.literal_eval(kw.value)
			except ValueError:
				return []
			return [str(v).upper() for v in value]
	return None


@functools.cache
def _scan() -> list[tuple[str, int, str | None, list[str] | None]]:
	"""Return (relative path, line, dotted path or None, methods) per use."""
	found = []
	for path in sorted(PACKAGE_ROOT.rglob("*.py")):
		rel = path.relative_to(PACKAGE_ROOT.parent)
		if rel.parts[:2] == ("jarvis", "tests"):
			continue
		module = ".".join(rel.with_suffix("").parts)
		tree = ast.parse(path.read_text(), filename=str(path))
		names = _whitelist_names(tree)
		decorated = {}
		for node in ast.walk(tree):
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				for deco in node.decorator_list:
					decorated[id(deco)] = f"{module}.{node.name}"
		for node in ast.walk(tree):
			if isinstance(node, ast.Call) and _is_whitelist(node.func, names):
				found.append((str(rel), node.lineno, decorated.get(id(node)), _methods_of(node)))
			elif _is_whitelist(node, names) and id(node) in decorated:
				# Bare ``@frappe.whitelist`` with no call: no methods at all.
				found.append((str(rel), node.lineno, decorated[id(node)], None))
	return found


class TestWhitelistHttpMethods(FrappeTestCase):
	def test_scan_finds_the_endpoints(self):
		self.assertGreater(len(_scan()), 100)

	def test_every_whitelist_declares_methods(self):
		missing = [f"{rel}:{line}" for rel, line, _, methods in _scan() if not methods]
		self.assertEqual(missing, [], "frappe.whitelist without methods=: " + ", ".join(missing))

	def test_only_the_allowlist_accepts_get(self):
		unexpected = [
			f"{rel}:{line} ({dotted})"
			for rel, line, dotted, methods in _scan()
			if methods and "GET" in methods and dotted not in GET_ALLOWLIST
		]
		self.assertEqual(unexpected, [], "GET outside the allowlist: " + ", ".join(unexpected))

	def test_every_allowlisted_endpoint_exists_and_uses_get(self):
		by_path = {dotted: methods for _, _, dotted, methods in _scan() if dotted}
		for dotted in sorted(GET_ALLOWLIST):
			with self.subTest(endpoint=dotted):
				self.assertIn("GET", by_path.get(dotted) or [])
