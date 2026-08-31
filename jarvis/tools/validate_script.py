"""Static validation of a Server Script / Script Report body against the
safe_exec sandbox - WITHOUT running it.

Three passes, all side-effect free:
1. An AST scan for the constructs safe_exec forbids, with a clear message each
   (imports, `frappe.get_all` / `frappe.db.sql` which bypass User Permissions,
   `open`/`eval`/`exec`/`__import__`, dunder access), plus a warning on SQL
   functions passed as `get_list` string fields.
2. A namespace check: `frappe.<attr>` chains whose first level is absent from the
   real `get_safe_globals()` (e.g. `frappe.defaults`, `frappe.get_user_default`) -
   they compile but are None at runtime.
3. `frappe.utils.safe_exec._compile_code` - the EXACT RestrictedPython compile
   safe_exec uses (FrappeTransformer policy), so a compile the agent's draft
   would fail is caught here instead of at runtime.

Read-only: parses/compiles, never executes, so nothing runs and no doc is touched.
"""

from __future__ import annotations

import ast
import re

# frappe attribute chains that compile fine but are BANNED for Jarvis-authored
# scripts because they read past User Permissions (cross-company/branch leak).
_PERMISSION_BYPASS = {"frappe.get_all", "frappe.db.get_all", "frappe.db.sql"}
# names that simply don't exist in the safe_exec namespace.
_BANNED_NAMES = {"open", "eval", "exec", "compile", "__import__", "input", "globals", "locals"}
# read calls whose `fields=` list Frappe screens for SQL functions.
_FIELD_READ_METHODS = {"get_list", "get_all", "get_value", "get_values"}
# SQL aggregate/functions Frappe v16 rejects when passed as a string field.
_SQL_FUNC_FIELD = re.compile(
	r"\b(sum|count|avg|min|max|group_concat|std|variance|stddev)\s*\(", re.IGNORECASE
)


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


def _agg_field_warnings(call: ast.Call) -> list[str]:
	"""Flag SQL functions passed as string `fields` to a read call (e.g.
	`get_list(fields=["sum(grand_total) as t"])`). safe_exec compiles them fine,
	but Frappe rejects SQL functions in SELECT at query time - so they pass the
	compile pass yet throw when the report runs. Aggregate in Python instead."""
	func = _dotted(call.func) if isinstance(call.func, ast.Attribute) else None
	if not func or func.split(".")[-1] not in _FIELD_READ_METHODS:
		return []
	fields = next((kw.value for kw in call.keywords if kw.arg == "fields"), None)
	if not isinstance(fields, ast.List):
		return []
	out = []
	for elt in fields.elts:
		if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and _SQL_FUNC_FIELD.search(elt.value):
			out.append(
				f"line {call.lineno}: `{func.split('.')[-1]}` field {elt.value!r} uses an SQL "
				"function as a string - Frappe rejects SQL functions in SELECT at query time. "
				"Fetch plain fields (limit_page_length=0) and aggregate/sort/slice in Python."
			)
	return out


def _namespace_errors(tree: ast.AST, frappe_keys: set[str]) -> list[str]:
	"""Flag `frappe.<attr>` chains whose first level is NOT in the real safe_exec
	namespace (e.g. `frappe.defaults.*`, `frappe.get_user_default`). They compile
	fine but are None at runtime -> `TypeError: 'NoneType' object is not callable`.
	First-level only, so valid deeper access (frappe.db.x, frappe.utils.x) is left
	to the compile/runtime; one error per missing attribute."""
	errors: list[str] = []
	seen: set[str] = set()
	for node in ast.walk(tree):
		if not isinstance(node, ast.Attribute):
			continue
		dotted = _dotted(node)
		if not dotted or not dotted.startswith("frappe."):
			continue
		first = dotted.split(".")[1]
		if first in frappe_keys or first in seen:
			continue
		seen.add(first)
		errors.append(
			f"line {node.lineno}: `frappe.{first}` is not in the safe_exec namespace "
			"(compiles but is None at runtime). Use only the curated frappe namespace - "
			"a default value is `frappe.db.get_default(key)`, not `frappe.defaults` / `frappe.get_user_default`."
		)
	return errors


def _result_shape_warning(assign: ast.Assign) -> list[str]:
	"""Flag `result = [columns, ...]` - a Script Report's columns/rows pair must go
	in `data` (`data = [columns, rows]`), not `result`, or Frappe uses the report's
	own (empty) columns and renders blank rows."""
	for tgt in assign.targets:
		if isinstance(tgt, ast.Name) and tgt.id == "result":
			val = assign.value
			if isinstance(val, ast.List) and val.elts:
				first = val.elts[0]
				if isinstance(first, ast.Name) and first.id == "columns":
					return [
						f"line {assign.lineno}: `result = [columns, ...]` renders a Script Report "
						"blank - the columns/rows pair goes in `data`: `data = [columns, rows]`."
					]
	return []


def _ast_scan(tree: ast.AST) -> tuple[list[str], list[str]]:
	errors: list[str] = []
	warnings: list[str] = []
	qb_flagged = False
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
			elif dotted and (dotted == "frappe.qb" or dotted.startswith("frappe.qb.")):
				if not qb_flagged:
					qb_flagged = True
					errors.append(
						f"line {node.lineno}: `frappe.qb` runs raw SQL that bypasses User "
						"Permissions, and its `Sum`/`Count` helpers aren't in the sandbox "
						"(NameError). Read via `frappe.get_list` and aggregate in Python."
					)
			elif node.attr.startswith("__") and node.attr.endswith("__"):
				errors.append(f"line {node.lineno}: dunder access `{node.attr}` is blocked in safe_exec.")
		elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
			errors.append(f"line {node.lineno}: `{node.id}` is not available in safe_exec.")
		elif isinstance(node, ast.Call):
			warnings.extend(_agg_field_warnings(node))
		elif isinstance(node, ast.Assign):
			warnings.extend(_result_shape_warning(node))
	return errors, warnings


def _restricted_compile(code: str) -> None:
	"""Run the exact RestrictedPython compile safe_exec uses, across Frappe
	versions. v16 exposes ``_compile_code``; older Frappe (v15) does not, so fall
	back to the ``compile_restricted`` + ``FrappeTransformer`` it wraps - same
	compile. Raises the compile's ``SyntaxError``; ``ImportError`` only if neither
	entrypoint is importable (then the caller degrades to the AST scan)."""
	try:
		from frappe.utils.safe_exec import _compile_code
	except ImportError:
		from frappe.utils.safe_exec import FrappeTransformer
		from RestrictedPython import compile_restricted

		compile_restricted(code, filename="<validate_script>", policy=FrappeTransformer, mode="exec")
	else:
		_compile_code(code, filename="<validate_script>")


def validate_script(code, script_type=None) -> dict:
	"""Static-check a Server Script or a Script Report's ``report_script`` against
	Frappe's safe_exec sandbox WITHOUT running it. Call this before staging a
	Server Script / Script Report create, and fix every error before drafting.

	Catches: any `import` / third-party / app-function use, `frappe.get_all`,
	`frappe.db.sql`, and `frappe.qb` (all bypass User Permissions - use
	`frappe.get_list`), `open`/`eval`/`exec`/`__import__`, dunder access, and any
	RestrictedPython compile error (the exact compile safe_exec runs). It never
	executes the code. Also flags `frappe.<attr>` calls outside the safe_exec
	namespace (e.g. `frappe.defaults` / `frappe.get_user_default` - use
	`frappe.db.get_default`). Warns on SQL functions passed as `get_list` string
	fields (`sum(...) as x`), and on `result = [columns, ...]` (a Script Report's
	columns/rows pair goes in `data`, not `result`, or it renders blank).

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
		tree = ast.parse(code)
	except SyntaxError as e:
		return {"ok": False, "errors": [f"SyntaxError: {e.msg} (line {e.lineno})"], "warnings": warnings}

	scan_errors, scan_warnings = _ast_scan(tree)
	errors.extend(scan_errors)
	warnings.extend(scan_warnings)

	# Flag frappe.* attributes absent from the real safe_exec namespace (best-effort:
	# needs a frappe context, so guard it; the AST scan + compile pass stand without it).
	try:
		from frappe.utils.safe_exec import get_safe_globals

		frappe_ns = get_safe_globals().get("frappe")
		if frappe_ns is not None:
			errors.extend(_namespace_errors(tree, set(frappe_ns.keys())))
	except Exception:
		pass

	# The authoritative RestrictedPython compile safe_exec itself uses. Guarded:
	# if neither entrypoint exists, the AST scan above still stands.
	try:
		_restricted_compile(code)
	except SyntaxError as e:
		loc = f" (line {e.lineno})" if getattr(e, "lineno", None) else ""
		errors.append(f"RestrictedPython rejected the script: {e.msg}{loc}")
	except ImportError:
		warnings.append("Could not run the RestrictedPython compile pass; relied on the static scan only.")
	except Exception as e:  # any compile failure is a real error to surface
		errors.append(f"RestrictedPython compile failed: {e}")

	return {"ok": not errors, "errors": errors, "warnings": warnings}
