"""Static pre-check for attribute reads that the Frappe server-script sandbox blocks.

This is a UX pre-check that gives a clear message BEFORE save. It is NOT a security
control: the Frappe sandbox (``frappe.utils.safe_exec``) enforces the rule at run time,
and nothing here changes it. The helper therefore fails open: any input it cannot parse
or walk returns no errors, and the compile pass and the sandbox stay the backstop.

Frappe refuses to READ any attribute whose name is in ``UNSAFE_ATTRIBUTES`` (for example
``"x".format(..)``). Store and Del are not checked by name, so only ``ast.Load`` is
flagged. Messages hold line numbers, attribute names and fixed hints, never source text.

The last section (``check_sandbox_code``) is the agent-write guard: unlike the rest of
this module it raises, so an agent cannot save a Server Script or Report script that the
sandbox would refuse.
"""

from __future__ import annotations

import ast
from collections.abc import Set as AbstractSet

import frappe
from frappe.utils import cint

from jarvis.exceptions import SandboxCodeError

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
# A workflow condition is an expression, not a script: it may not build text at all
# (no f-string, no % formatting), so the hint points at comparing fields directly.
_HINT_CONDITION_CALL = 'Compare fields directly (for example doc.status == "Open")'
_SUBJECT_CONDITION = "condition"

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


def _hint(node: ast.Attribute, is_call_func: bool, subject: str = "script") -> str:
	"""Save-time hint; ``is_call_func`` is True when the attribute is being called
	(``x.format(..)``) rather than only read (``f = x.format``)."""
	if node.attr in FORMAT_NAMES:
		if dotted(node) == "frappe.format":
			return _HINT_FRAPPE_FORMAT
		if not is_call_func:
			return _HINT_FORMAT_READ
		return _HINT_CONDITION_CALL if subject == _SUBJECT_CONDITION else _HINT_FORMAT_CALL
	return _HINT_OTHER


def _message(line: int, attr: str, hint: str, subject: str) -> str:
	if subject == _SUBJECT_CONDITION:
		return (
			f"line {line}: .{attr} is blocked in Frappe's restricted expression evaluator "
			f"and fails when the condition is evaluated. {hint}"
		)
	return (
		f"line {line}: .{attr} is blocked in the server-script sandbox and fails when the script runs. {hint}"
	)


def unsafe_attribute_errors(code_or_tree, *, mode: str = "exec", subject: str = "script") -> list[str]:
	"""One error per sandbox-blocked attribute READ, ordered by line.

	``code_or_tree`` is source text or an already parsed ``ast.AST``. ``mode`` is the
	parse mode for text (``"exec"`` for a script, ``"eval"`` for a condition).
	``subject`` picks the wording: ``"script"`` (default) or ``"condition"``. Never
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
				hint = _hint(node, id(node) in call_funcs, subject)
				found.append(
					(
						line,
						getattr(node, "end_col_offset", None) or getattr(node, "col_offset", 0),
						_message(line, node.attr, hint, subject),
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


# The fields an agent write can fill with code that runs in the Frappe sandbox.
SANDBOX_CODE_FIELDS = {"Server Script": ("script",), "Report": ("report_script",)}
_FIX_LINE = "Fix every line, then call validate_script before you write it."


def _code_fields_to_check(doctype: str, values: dict, before) -> tuple[str, ...]:
	"""The mapped code fields of ``doctype`` that an agent write must check.

	Create (``before`` is None): each field in ``values``. Update: each field whose
	value differs from ``before``, plus every field when the update makes stored code
	active. Server Script: ``disabled`` 1 to 0, or a changed ``script_type`` while the
	script is enabled after the update. Report that is a Script Report after the update:
	``disabled`` 1 to 0, ``is_standard`` "Yes" to "No" (Frappe then runs report_script),
	or ``report_type`` changed to "Script Report"."""
	fields = SANDBOX_CODE_FIELDS.get(doctype, ())
	if before is None:
		return tuple(f for f in fields if f in values)
	changed = tuple(f for f in fields if f in values and values[f] != before.get(f))
	activated = False
	if doctype == "Server Script":
		enabled_now = cint(values.get("disabled", before.get("disabled"))) == 0
		activated = (
			"disabled" in values and cint(before.get("disabled")) == 1 and cint(values["disabled"]) == 0
		) or (enabled_now and "script_type" in values and values["script_type"] != before.get("script_type"))
	elif doctype == "Report":
		if values.get("report_type", before.get("report_type")) == "Script Report":
			activated = (
				("report_type" in values and before.get("report_type") != "Script Report")
				or (
					"disabled" in values
					and cint(before.get("disabled")) == 1
					and cint(values["disabled"]) == 0
				)
				or (
					"is_standard" in values
					and before.get("is_standard") == "Yes"
					and values["is_standard"] == "No"
				)
			)
	return fields if activated else changed


def check_sandbox_code(doctype: str, values: dict, before=None) -> None:
	"""Refuse an agent write of sandbox code that reads a blocked attribute.

	``values`` is what the write sets; ``before`` is the loaded document for an update
	(None for a create). The code checked is the value that applies after the write.
	Raises ``SandboxCodeError`` with every offending line (plain text). On an update the
	message names the document; when the failing code is the stored value (the update
	only makes it active) it says so."""
	for field in _code_fields_to_check(doctype, values, before):
		code = values[field] if field in values else before.get(field)
		if not isinstance(code, str) or not code.strip():
			continue
		errors = unsafe_attribute_errors(code)
		if not errors:
			continue
		who = f"{doctype} {before.get('name')}" if before is not None and before.get("name") else doctype
		if before is not None and (field not in values or values[field] == before.get(field)):
			head = f"{who} {field} (stored code, which this update makes active):\n"
			tail = f"Send a corrected {field} in the same update, or call validate_script first."
		else:
			head, tail = f"{who} {field}:\n", _FIX_LINE
		raise SandboxCodeError(head + error_text(errors) + "\n" + tail)
