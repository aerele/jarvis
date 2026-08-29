"""Static validation of a Server Script / Script Report body against the
safe_exec sandbox - WITHOUT running it.

Two passes, both side-effect free:
1. An AST scan for the constructs safe_exec forbids, with a clear message each
   (imports, `frappe.get_all` / `frappe.db.sql` which bypass User Permissions,
   `open`/`eval`/`exec`/`__import__`, dunder access).
2. `frappe.utils.safe_exec._compile_code` - the EXACT RestrictedPython compile
   safe_exec uses (FrappeTransformer policy), so a compile the agent's draft
   would fail is caught here instead of at runtime.

Read-only: parses/compiles, never executes, so nothing runs and no doc is touched.
"""

from __future__ import annotations

import ast

# frappe attribute chains that compile fine but are BANNED for Jarvis-authored
# scripts because they read past User Permissions (cross-company/branch leak).
_PERMISSION_BYPASS = {"frappe.get_all", "frappe.db.get_all", "frappe.db.sql"}
# names that simply don't exist in the safe_exec namespace.
_BANNED_NAMES = {"open", "eval", "exec", "compile", "__import__", "input", "globals", "locals"}


def _dotted(node: ast.Attribute) -> str | None:
	"""Reconstruct a static attribute chain (frappe.db.sql). None if the base
	isn't a plain name (so aliased/indexed access is left to the compile pass)."""
	parts = []
	cur: ast.AST = node
	while isinstance(cur, ast.Attribute):
		parts.append(cur.attr)
		cur = cur.value
	if not isinstance(cur, ast.Name):
		return None
	parts.append(cur.id)
	return ".".join(reversed(parts))


def _ast_scan(code: str) -> list[str]:
	errors: list[str] = []
	tree = ast.parse(code)  # SyntaxError handled by the caller
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			errors.append(
				f"line {node.lineno}: `import` is not allowed in safe_exec - no imports, "
				"no third-party packages, no app functions. Use only the frappe namespace "
				"(HTTP via frappe.make_post_request / FrappeClient, JSON via the injected json)."
			)
		elif isinstance(node, ast.Attribute):
			dotted = _dotted(node)
			if dotted in _PERMISSION_BYPASS:
				errors.append(
					f"line {node.lineno}: `{dotted}` bypasses User Permissions "
					"(cross-company/branch/dimension leak). Use `frappe.get_list` for every read."
				)
			elif node.attr.startswith("__") and node.attr.endswith("__"):
				errors.append(f"line {node.lineno}: dunder access `{node.attr}` is blocked in safe_exec.")
		elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
			errors.append(f"line {node.lineno}: `{node.id}` is not available in safe_exec.")
	return errors


def validate_script(code, script_type=None) -> dict:
	"""Static-check a Server Script or a Script Report's ``report_script`` against
	Frappe's safe_exec sandbox WITHOUT running it. Call this before staging a
	Server Script / Script Report create, and fix every error before drafting.

	Catches: any `import` / third-party / app-function use, `frappe.get_all` and
	`frappe.db.sql` (both bypass User Permissions - use `frappe.get_list`),
	`open`/`eval`/`exec`/`__import__`, dunder access, and any RestrictedPython
	compile error (the exact compile safe_exec runs). It never executes the code.

	- ``code``: the script body (str).
	- ``script_type``: optional hint ("Script Report", "DocType Event", "API",
	  "Permission Query", "Scheduler Event"); advisory, does not change the checks.

	Result: ``{"ok": bool, "errors": [str], "warnings": [str]}``. ``ok`` is True
	only when there are no errors. See the ``frappe-scripting`` skill for the
	allowed namespace and per-type contract.
	"""
	from jarvis.exceptions import InvalidArgumentError

	if not isinstance(code, str) or not code.strip():
		raise InvalidArgumentError("`code` must be a non-empty script body.")

	errors: list[str] = []
	warnings: list[str] = []

	try:
		errors.extend(_ast_scan(code))
	except SyntaxError as e:
		return {"ok": False, "errors": [f"SyntaxError: {e.msg} (line {e.lineno})"], "warnings": warnings}

	# The authoritative RestrictedPython compile safe_exec itself uses. Private
	# frappe helper, so guard it: if it moves, the AST scan above still stands.
	try:
		from frappe.utils.safe_exec import _compile_code

		_compile_code(code, filename="<validate_script>")
	except SyntaxError as e:
		loc = f" (line {e.lineno})" if getattr(e, "lineno", None) else ""
		errors.append(f"RestrictedPython rejected the script: {e.msg}{loc}")
	except ImportError:
		warnings.append("Could not run the RestrictedPython compile pass; relied on the static scan only.")
	except Exception as e:  # any compile failure is a real error to surface
		errors.append(f"RestrictedPython compile failed: {e}")

	return {"ok": not errors, "errors": errors, "warnings": warnings}
