"""Static pre-check for attribute reads that the Frappe server-script sandbox blocks.

This is a UX pre-check that gives a clear message BEFORE save. It is NOT a security
control: the Frappe sandbox (``frappe.utils.safe_exec``) enforces the rule at run time,
and nothing here changes it. The helper therefore fails open: any input it cannot parse
or walk returns no errors, and the compile pass and the sandbox stay the backstop.

Frappe refuses to READ any attribute whose name is in ``UNSAFE_ATTRIBUTES`` (for example
``"x".format(..)``). Store and Del are not checked by name, so only ``ast.Load`` is
flagged. Messages hold line numbers, attribute names and fixed hints, never source text.
"""

from __future__ import annotations

import ast
from collections.abc import Set as AbstractSet

import frappe

# Used only when frappe.utils.safe_exec cannot be imported (a Frappe rename).
_FALLBACK_UNSAFE_ATTRIBUTES = frozenset({"format", "format_map"})
_PARSE_FAILURES = (SyntaxError, ValueError, RecursionError, MemoryError)
# The one source for "which names get the format hint": used here for the save-time
# hints and by triggers.engine for the run-time hint.
FORMAT_NAMES = frozenset({"format", "format_map"})

_HINT_FORMAT_CALL = (
	'Use an f-string (f"PO {doc.name}") or % formatting ("PO %s" % doc.name). '
	'For translated text: _("PO %s") % doc.name'
)
_HINT_FRAPPE_FORMAT = "Use frappe.format_value(value, df)"
_HINT_FORMAT_READ = 'To read a field with this name, use doc.get("format")'
_HINT_OTHER = "This attribute is not available in the sandbox"

_fallback_logged = False


def dotted(node: ast.Attribute) -> str | None:
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


def _log_fallback_once(exc: BaseException) -> None:
	"""Error Log row (kept on production) the first time the fallback set is used.
	Logging must never raise out of the check."""
	global _fallback_logged
	if _fallback_logged:
		return
	_fallback_logged = True
	try:
		frappe.log_error(
			title="Jarvis: UNSAFE_ATTRIBUTES not importable",
			message=(
				"frappe.utils.safe_exec.UNSAFE_ATTRIBUTES could not be imported "
				f"({type(exc).__name__}); the unsafe-attribute check uses its fallback set "
				"(format, format_map). Frappe may have renamed it."
			),
			defer_insert=True,
		)
	except Exception:
		pass


def _unsafe_attributes() -> AbstractSet[str]:
	"""Frappe's own blocked-attribute set, read at call time so a Frappe upgrade is
	picked up without a code change. Falls back (and logs once) if it is gone."""
	try:
		from frappe.utils.safe_exec import UNSAFE_ATTRIBUTES
	except ImportError as e:
		_log_fallback_once(e)
		return _FALLBACK_UNSAFE_ATTRIBUTES
	return UNSAFE_ATTRIBUTES


def runtime_hint(name: str) -> str | None:
	"""Short hint appended to a Failed-run summary when the sandbox blocked ``.name``.
	Only the format names have one; any other name returns None."""
	if name in FORMAT_NAMES:
		return f" - .{name} is blocked in the sandbox; use an f-string or % formatting"
	return None


def _hint(node: ast.Attribute, is_call_func: bool) -> str:
	"""Save-time hint; ``is_call_func`` is True when the attribute is being called
	(``x.format(..)``) rather than only read (``f = x.format``)."""
	if node.attr in FORMAT_NAMES:
		if dotted(node) == "frappe.format":
			return _HINT_FRAPPE_FORMAT
		return _HINT_FORMAT_CALL if is_call_func else _HINT_FORMAT_READ
	return _HINT_OTHER


def unsafe_attribute_errors(code_or_tree, *, mode: str = "exec") -> list[str]:
	"""One error per sandbox-blocked attribute READ, ordered by line.

	``code_or_tree`` is source text or an already parsed ``ast.AST``. ``mode`` is the
	parse mode for text (``"exec"`` for a script, ``"eval"`` for a condition). Never
	raises: any failure returns ``[]``."""
	try:
		tree = code_or_tree
		if isinstance(code_or_tree, str):
			tree = ast.parse(code_or_tree, mode=mode)
		if not isinstance(tree, ast.AST):
			return []
		blocked = _unsafe_attributes()
		call_funcs: set[int] = set()
		found: list[tuple[int, int, str]] = []
		# ast.walk is iterative and visits a parent before its children, so a Call is
		# seen before its func Attribute.
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				call_funcs.add(id(node.func))
			elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in blocked:
				# end_lineno is the line of the attribute name itself, which differs from
				# lineno when the chain spans lines.
				line = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
				hint = _hint(node, id(node) in call_funcs)
				found.append(
					(
						line,
						getattr(node, "end_col_offset", None) or getattr(node, "col_offset", 0),
						f"line {line}: .{node.attr} is blocked in the server-script sandbox "
						f"and fails when the script runs. {hint}",
					)
				)
		found.sort(key=lambda item: item[:2])
		return [message for _line, _col, message in found]
	except _PARSE_FAILURES:
		return []
	except Exception:
		# Fail open (a UX pre-check must never turn a save into an HTTP 500): an
		# unexpected import error or a reshaped UNSAFE_ATTRIBUTES lands here.
		return []


def capped(errors: list[str], cap: int = 10) -> list[str]:
	"""At most ``cap`` errors, plus a ``(+N more)`` tail line when some were cut."""
	shown = list(errors[:cap])
	extra = len(errors) - len(shown)
	if extra > 0:
		shown.append(f"(+{extra} more)")
	return shown


def error_text(errors: list[str], cap: int = 10) -> str:
	"""``capped(errors, cap)`` joined with newlines."""
	return "\n".join(capped(errors, cap))
