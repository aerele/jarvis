"""jarvis.tools._sandbox_attrs flags attribute reads that the Frappe server-script
sandbox blocks (``.format`` and friends) before they are saved.

The parity tests run the same samples through the real sandbox
(``frappe.utils.safe_exec.safe_exec`` / ``frappe.safe_eval``) so the static check and
the run-time rule cannot drift apart unnoticed.
"""

import ast
import sys
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.classes.context_managers import enable_safe_exec
from frappe.tests.utils import FrappeTestCase
from frappe.utils.safe_exec import safe_exec

from jarvis.exceptions import InvalidFieldValueError, SandboxCodeError
from jarvis.tools import _sandbox_attrs
from jarvis.tools._sandbox_attrs import (
	FORMAT_NAMES,
	SANDBOX_CODE_FIELDS,
	capped,
	check_sandbox_code,
	dotted,
	error_text,
	runtime_hint,
	unsafe_attribute_errors,
)

# Samples run in the real sandbox with fresh locals from _doc() (the sandbox forbids
# `frappe._dict` inside a script, because of the leading underscore).
_FLAGGED_EXEC = {
	"format_call": 'x = "PO {}".format(1)\n',
	"translated_format_call": 'x = _("PO {}").format(1)\n',
	"frappe_format_call": "x = frappe.format(1)\n",
	"alias": 's = "a"\nf = s.format\n',
	"format_map": 'x = "{a}".format_map({"a": 1})\n',
	"field_read": "x = doc.format\n",
}
_CLEAN_EXEC = {
	"store": "doc.format = 1\n",
	"delete": "del doc.format\n",
	"f_string": 'x = f"PO {doc.name}"\n',
	"percent": 'x = "PO %s" % doc.name\n',
	"format_value": 'x = frappe.format_value(5, {"fieldtype": "Int"})\n',
	"literal_text": 'x = "a.format(b)"\n',
	"doc_get": 'x = doc.get("format")\n',
}
_FLAGGED_EVAL = {
	"format_call": '"PO {}".format(1)',
	"short_circuit": '(doc.name == "B") or "{}".format(1)',
	"field_read": "doc.format",
}
_CLEAN_EVAL = {
	"doc_get": 'doc.get("format")',
	"compare": 'doc.name == "A"',
}


def _doc():
	return frappe._dict(name="A", format="x")


class TestUnsafeAttributeErrors(unittest.TestCase):
	def test_format_call_is_flagged_with_line_and_fstring_hint(self):
		errors = unsafe_attribute_errors('x = 1\ny = "PO {}".format(x)\n')
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].startswith("line 2: .format is blocked in the server-script sandbox"))
		self.assertIn("fails when the script runs. Use an f-string", errors[0])
		self.assertIn('_("PO %s") % doc.name', errors[0])

	def test_hints_have_no_final_full_stop(self):
		for code in (
			'"a".format(1)\n',
			"frappe.format(1)\n",
			"f = s.format\n",
			"x = g.gi_frame\n",
		):
			with self.subTest(code=code):
				errors = unsafe_attribute_errors(code)
				self.assertEqual(len(errors), 1)
				self.assertFalse(errors[0].endswith("."), errors[0])

	def test_translated_format_call_is_flagged(self):
		errors = unsafe_attribute_errors('x = _("PO {}").format(1)\n')
		self.assertEqual(len(errors), 1)
		self.assertIn("Use an f-string", errors[0])

	def test_frappe_format_gets_format_value_hint(self):
		errors = unsafe_attribute_errors("x = frappe.format(1)\n")
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].endswith("Use frappe.format_value(value, df)"), errors[0])

	def test_alias_is_flagged_with_field_read_hint(self):
		errors = unsafe_attribute_errors('s = "a"\nf = s.format\n')
		self.assertEqual(len(errors), 1)
		self.assertIn('doc.get("format")', errors[0])
		self.assertTrue(errors[0].startswith("line 2:"))

	def test_format_map_call_is_flagged(self):
		errors = unsafe_attribute_errors('x = "{a}".format_map({"a": 1})\n')
		self.assertEqual(len(errors), 1)
		self.assertIn(".format_map is blocked", errors[0])
		self.assertIn("Use an f-string", errors[0])

	def test_doc_format_read_gets_field_hint(self):
		errors = unsafe_attribute_errors("x = doc.format\n")
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].endswith('To read a field with this name, use doc.get("format")'))

	def test_other_member_gets_generic_hint(self):
		errors = unsafe_attribute_errors("x = g.gi_frame\n")
		self.assertEqual(len(errors), 1)
		self.assertIn(".gi_frame is blocked", errors[0])
		self.assertTrue(errors[0].endswith("This attribute is not available in the sandbox"))

	def test_every_occurrence_is_reported_in_line_order(self):
		code = 'a = "1".format(1)\nb = 2\nc = "3".format(3)\nd = "4".format(4)\n'
		errors = unsafe_attribute_errors(code)
		self.assertEqual([e.split(":")[0] for e in errors], ["line 1", "line 3", "line 4"])

	def test_errors_are_sorted_by_line_not_walk_order(self):
		# ast.walk is breadth-first: the shallow line-2 match is visited before the
		# deeper line-1 match.
		errors = unsafe_attribute_errors('x = f(g("a".format(1)))\ny = s.format\n')
		self.assertEqual([e.split(":")[0] for e in errors], ["line 1", "line 2"])

	def test_store_is_not_flagged(self):
		self.assertEqual(unsafe_attribute_errors("doc.format = 1\n"), [])

	def test_del_is_not_flagged(self):
		self.assertEqual(unsafe_attribute_errors("del doc.format\n"), [])

	def test_clean_code_is_not_flagged(self):
		for code in (
			'x = f"PO {doc.name}"\n',
			'x = "PO %s" % doc.name\n',
			"x = frappe.format_value(v, df)\n",
			'x = ".format("\n',
			'x = "a.format(b)"\n',
			'x = doc.get("format")\n',
		):
			with self.subTest(code=code):
				self.assertEqual(unsafe_attribute_errors(code), [])

	def test_message_never_contains_source_text(self):
		errors = unsafe_attribute_errors('SECRET_VALUE = "TOPSECRET-123 {}".format(1)\n')
		self.assertEqual(len(errors), 1)
		self.assertNotIn("TOPSECRET", errors[0])
		self.assertNotIn("SECRET_VALUE", errors[0])

	def test_eval_mode(self):
		errors = unsafe_attribute_errors('(doc.a == 1) or "{}".format(1)', mode="eval")
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].startswith("line 1:"))

	def test_mode_selects_the_parser(self):
		# An assignment is not a valid eval expression, so eval mode fails open;
		# exec mode flags the same text.
		self.assertEqual(unsafe_attribute_errors("x = a.format", mode="eval"), [])
		self.assertEqual(len(unsafe_attribute_errors("x = a.format", mode="exec")), 1)

	def test_multiline_chain_reports_the_attribute_line(self):
		errors = unsafe_attribute_errors('x = ("a"\n\t.format(1))\n')
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].startswith("line 2:"), errors[0])

	def test_tree_input(self):
		tree = ast.parse('x = "a".format(1)\n')
		errors = unsafe_attribute_errors(tree)
		self.assertEqual(len(errors), 1)
		self.assertTrue(errors[0].startswith("line 1:"))

	def test_non_code_input_fails_open(self):
		for value in (None, 5, b"x = 1", ["a"]):
			with self.subTest(value=value):
				self.assertEqual(unsafe_attribute_errors(value), [])


class TestConditionSubject(unittest.TestCase):
	"""FX-6: a workflow condition gets condition wording and no f-string hint."""

	def test_condition_wording_has_no_fstring_hint(self):
		[call] = unsafe_attribute_errors('"{}".format(1)', mode="eval", subject="condition")
		self.assertIn("line 1: .format is blocked in Frappe's restricted expression evaluator", call)
		self.assertNotIn("f-string", call)
		self.assertNotIn("%", call)
		self.assertIn('Compare fields directly (for example doc.status == "Open")', call)
		self.assertNotIn("server-script sandbox", call)
		[read] = unsafe_attribute_errors("doc.format == 1", mode="eval", subject="condition")
		self.assertIn("line 1: .format is blocked in Frappe's restricted expression evaluator", read)
		self.assertIn('doc.get("format")', read)
		self.assertNotIn("f-string", read)

	def test_default_subject_keeps_the_script_wording(self):
		[call] = unsafe_attribute_errors('x = "{}".format(1)')
		self.assertIn("is blocked in the server-script sandbox and fails when the script runs.", call)
		self.assertIn('Use an f-string (f"PO {doc.name}")', call)
		self.assertEqual(call, unsafe_attribute_errors('x = "{}".format(1)', subject="script")[0])


class TestFailOpen(unittest.TestCase):
	def test_syntax_error(self):
		self.assertEqual(unsafe_attribute_errors("def (:\n"), [])

	def test_null_byte(self):
		self.assertEqual(unsafe_attribute_errors("\x00"), [])

	def test_empty(self):
		self.assertEqual(unsafe_attribute_errors(""), [])

	def test_very_long_attribute_chain(self):
		self.assertEqual(unsafe_attribute_errors("x" + ".a" * 50000), [])

	def test_very_deep_unary_nesting(self):
		self.assertEqual(unsafe_attribute_errors("-" * 200000 + "1"), [])

	def test_walk_failure_returns_empty(self):
		with patch.object(_sandbox_attrs.ast, "walk", side_effect=RecursionError):
			self.assertEqual(unsafe_attribute_errors('"a".format(1)\n'), [])
		with patch.object(_sandbox_attrs.ast, "walk", side_effect=MemoryError):
			self.assertEqual(unsafe_attribute_errors('"a".format(1)\n'), [])


class TestErrorText(unittest.TestCase):
	def test_joins_with_newline(self):
		self.assertEqual(error_text(["a", "b"]), "a\nb")

	def test_empty(self):
		self.assertEqual(error_text([]), "")

	def test_cap_and_more_marker(self):
		errors = [f"e{i}" for i in range(13)]
		lines = error_text(errors).split("\n")
		self.assertEqual(lines[:10], errors[:10])
		self.assertEqual(lines[10:], ["(+3 more)"])

	def test_at_cap_has_no_marker(self):
		errors = [f"e{i}" for i in range(10)]
		self.assertNotIn("more", error_text(errors))

	def test_custom_cap(self):
		self.assertEqual(error_text(["a", "b", "c"], cap=2), "a\nb\n(+1 more)")


class TestCapped(unittest.TestCase):
	def test_under_cap_is_a_copy(self):
		errors = ["a", "b"]
		out = capped(errors)
		self.assertEqual(out, ["a", "b"])
		self.assertIsNot(out, errors)

	def test_empty(self):
		self.assertEqual(capped([]), [])

	def test_over_cap_adds_more_marker(self):
		errors = [f"e{i}" for i in range(13)]
		self.assertEqual(capped(errors), errors[:10] + ["(+3 more)"])

	def test_at_cap_has_no_marker(self):
		errors = [f"e{i}" for i in range(10)]
		self.assertEqual(capped(errors), errors)

	def test_custom_cap(self):
		self.assertEqual(capped(["a", "b", "c"], cap=2), ["a", "b", "(+1 more)"])


class TestDotted(unittest.TestCase):
	def test_plain_chain(self):
		node = ast.parse("frappe.db.sql", mode="eval").body
		self.assertEqual(dotted(node), "frappe.db.sql")

	def test_non_name_base_is_none(self):
		node = ast.parse("a[0].b", mode="eval").body
		self.assertIsNone(dotted(node))


class TestUnsafeAttributeSource(unittest.TestCase):
	def test_frappe_set_is_importable_and_has_format(self):
		# Not mocked: catches a Frappe rename of UNSAFE_ATTRIBUTES.
		from frappe.utils.safe_exec import UNSAFE_ATTRIBUTES

		self.assertIn("format", UNSAFE_ATTRIBUTES)
		self.assertIn("format_map", UNSAFE_ATTRIBUTES)

	def test_import_failure_uses_fallback_and_logs_an_error_once(self):
		with (
			patch.dict(sys.modules, {"frappe.utils.safe_exec": None}),
			patch.object(_sandbox_attrs, "_fallback_logged", False),
			patch.object(frappe, "log_error") as log_error,
		):
			first = unsafe_attribute_errors('"a".format(1)\n')
			second = unsafe_attribute_errors('"a".format_map({})\n')
			generic = unsafe_attribute_errors("x = g.gi_frame\n")
		self.assertEqual(len(first), 1)
		self.assertEqual(len(second), 1)
		# The fallback set holds only format / format_map.
		self.assertEqual(generic, [])
		# An Error Log row (visible on production), once per process.
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["title"], "Jarvis: UNSAFE_ATTRIBUTES not importable")
		self.assertIn("fallback", log_error.call_args.kwargs["message"])
		self.assertIs(log_error.call_args.kwargs["defer_insert"], True)

	def test_logging_failure_in_the_fallback_cannot_raise(self):
		with (
			patch.dict(sys.modules, {"frappe.utils.safe_exec": None}),
			patch.object(_sandbox_attrs, "_fallback_logged", False),
			patch.object(frappe, "log_error", side_effect=RuntimeError("log down")),
		):
			self.assertEqual(len(unsafe_attribute_errors('"a".format(1)\n')), 1)

	def test_a_non_container_unsafe_set_fails_open(self):
		# Frappe reshaping UNSAFE_ATTRIBUTES must not turn a save into an HTTP 500.
		with patch("frappe.utils.safe_exec.UNSAFE_ATTRIBUTES", 5):
			self.assertEqual(unsafe_attribute_errors('"a".format(1)\n'), [])

	def test_an_unexpected_import_error_fails_open(self):
		class _Broken:
			def __getattr__(self, name):
				raise RuntimeError("broken module")

		with patch.dict(sys.modules, {"frappe.utils.safe_exec": _Broken()}):
			self.assertEqual(unsafe_attribute_errors('"a".format(1)\n'), [])


class TestRuntimeHint(unittest.TestCase):
	def test_format_names(self):
		self.assertEqual(FORMAT_NAMES, frozenset({"format", "format_map"}))

	def test_hint_for_format_names(self):
		for name in FORMAT_NAMES:
			self.assertEqual(
				runtime_hint(name),
				f" - .{name} is blocked in the sandbox; use an f-string or % formatting",
			)

	def test_no_hint_for_other_names(self):
		for name in ("__class__", "gi_frame", "format_value", ""):
			self.assertIsNone(runtime_hint(name))


_BAD = 'x = "{}".format(1)\n'
_THREE_BAD = 'a = "{}".format(1)\nb = "{}".format(2)\nc = "{}".format_map({})\n'
_FIX_LINE = "Fix every line, then call validate_script before you write it."


def _raised(*args, **kwargs) -> str:
	with unittest.TestCase().assertRaises(InvalidFieldValueError) as ctx:
		check_sandbox_code(*args, **kwargs)
	return str(ctx.exception)


class TestCheckSandboxCode(unittest.TestCase):
	"""T4: an agent write of a Server Script / Report script is checked."""

	def test_mapped_fields(self):
		self.assertEqual(SANDBOX_CODE_FIELDS, {"Server Script": ("script",), "Report": ("report_script",)})

	def test_create_lists_every_line_in_one_error(self):
		text = _raised("Server Script", {"script": _THREE_BAD})
		self.assertTrue(text.startswith("Server Script script:\n"), text)
		self.assertTrue(text.endswith("\n" + _FIX_LINE), text)
		for line in ("line 1:", "line 2:", "line 3:"):
			self.assertIn(line, text)
		# Plain text: the hint keeps its quotes, nothing is HTML-escaped.
		self.assertIn('f"PO {doc.name}"', text)
		self.assertNotIn("&quot;", text)

	def test_create_report_script(self):
		self.assertTrue(_raised("Report", {"report_script": _BAD}).startswith("Report report_script:\n"))

	def test_create_clean_empty_and_other_shapes_pass(self):
		for doctype, values in (
			("Server Script", {"script": 'x = f"{1}"\n'}),
			("Server Script", {"script": ""}),
			("Server Script", {"script": None}),
			("Server Script", {"script": 5}),
			("Server Script", {"script": ["a.format(1)"]}),
			("Server Script", {"reference_doctype": "ToDo"}),
			("Server Script", {}),
			("Note", {"content": _BAD}),
			("ToDo", {"script": _BAD}),
		):
			with self.subTest(doctype=doctype, values=values):
				self.assertIsNone(check_sandbox_code(doctype, values))

	def test_only_the_mapped_field_of_each_doctype_is_checked(self):
		self.assertIsNone(check_sandbox_code("Server Script", {"report_script": _BAD}))
		self.assertIsNone(check_sandbox_code("Report", {"script": _BAD}))

	def test_cap_notes_the_rest(self):
		text = _raised(
			"Server Script", {"script": "".join(f'a{i} = "{{}}".format({i})\n' for i in range(12))}
		)
		self.assertIn("(+2 more)", text)

	def test_update_changed_field_is_checked(self):
		before = frappe._dict(script="x = 1\n", disabled=0, script_type="API")
		self.assertIn("Server Script script:", _raised("Server Script", {"script": _BAD}, before))

	def test_update_unchanged_field_is_not_checked(self):
		before = frappe._dict(script=_BAD, disabled=0, script_type="API")
		self.assertIsNone(check_sandbox_code("Server Script", {"script": _BAD}, before))
		before = frappe._dict(report_script=_BAD, report_type="Script Report")
		self.assertIsNone(check_sandbox_code("Report", {"report_script": _BAD}, before))

	def test_update_repeated_unchanged_value_beside_disabling_passes(self):
		before = frappe._dict(script=_BAD, disabled=0, script_type="API")
		self.assertIsNone(check_sandbox_code("Server Script", {"disabled": 1, "script": _BAD}, before))

	def test_update_reenabling_a_stored_bad_script_is_refused(self):
		before = frappe._dict(script=_BAD, disabled=1, script_type="API")
		for disabled in (0, "0", False):
			with self.subTest(disabled=disabled):
				self.assertIn(
					"Server Script script (stored code,",
					_raised("Server Script", {"disabled": disabled}, before),
				)
		# Repeating the unchanged stored code beside the re-enable is refused once.
		self.assertIn("line 1:", _raised("Server Script", {"disabled": 0, "script": _BAD}, before))

	def test_update_changes_that_do_not_activate_pass(self):
		before = frappe._dict(script=_BAD, disabled=1, script_type="API")
		self.assertIsNone(check_sandbox_code("Server Script", {"disabled": 1}, before))
		self.assertIsNone(check_sandbox_code("Server Script", {"disabled": "1"}, before))
		active = frappe._dict(script=_BAD, disabled=0, script_type="API")
		self.assertIsNone(check_sandbox_code("Server Script", {"disabled": 0}, active))
		self.assertIsNone(check_sandbox_code("Server Script", {"disabled": 1}, active))

	def test_the_error_is_a_sandbox_code_error_with_the_argument_envelope(self):
		with self.assertRaises(SandboxCodeError) as ctx:
			check_sandbox_code("Server Script", {"script": _BAD})
		self.assertIsInstance(ctx.exception, InvalidFieldValueError)
		self.assertEqual(ctx.exception.envelope_code, "InvalidArgumentError")

	def test_update_script_type_change_checks_the_stored_script(self):
		before = frappe._dict(script=_BAD, disabled=0, script_type="API")
		self.assertIn(
			"Server Script script (stored code,",
			_raised("Server Script", {"script_type": "Permission Query"}, before),
		)
		self.assertIsNone(check_sandbox_code("Server Script", {"script_type": "API"}, before))

	def test_update_script_type_change_on_a_disabled_script_passes(self):
		# A disabled script's code cannot run; enabling it later is still checked.
		before = frappe._dict(script=_BAD, disabled=1, script_type="API")
		self.assertIsNone(check_sandbox_code("Server Script", {"script_type": "Permission Query"}, before))
		self.assertIsNone(
			check_sandbox_code("Server Script", {"script_type": "Permission Query", "disabled": 1}, before)
		)
		self.assertIn(
			"Server Script script (stored code,",
			_raised("Server Script", {"script_type": "Permission Query", "disabled": 0}, before),
		)

	def test_update_script_type_change_with_disabling_in_the_same_update_passes(self):
		before = frappe._dict(script=_BAD, disabled=0, script_type="API")
		self.assertIsNone(
			check_sandbox_code("Server Script", {"script_type": "Permission Query", "disabled": 1}, before)
		)

	def test_update_report_type_to_script_report_checks_the_stored_script(self):
		before = frappe._dict(report_script=_BAD, report_type="Query Report")
		self.assertIn(
			"Report report_script (stored code,", _raised("Report", {"report_type": "Script Report"}, before)
		)
		self.assertIsNone(check_sandbox_code("Report", {"report_type": "Report Builder"}, before))
		already = frappe._dict(report_script=_BAD, report_type="Script Report")
		self.assertIsNone(check_sandbox_code("Report", {"report_type": "Script Report"}, already))

	def test_update_report_reenabling_checks_the_stored_script(self):
		before = frappe._dict(report_script=_BAD, report_type="Script Report", disabled=1, is_standard="No")
		for disabled in (0, "0", False):
			with self.subTest(disabled=disabled):
				self.assertIn(
					"Report report_script (stored code,", _raised("Report", {"disabled": disabled}, before)
				)
		self.assertIsNone(check_sandbox_code("Report", {"disabled": 1}, before))

	def test_update_report_reenabling_a_report_that_is_not_a_script_report_passes(self):
		before = frappe._dict(report_script=_BAD, report_type="Query Report", disabled=1, is_standard="No")
		self.assertIsNone(check_sandbox_code("Report", {"disabled": 0}, before))
		# ... unless the same update makes it one.
		self.assertIn(
			"Report report_script (stored code,",
			_raised("Report", {"disabled": 0, "report_type": "Script Report"}, before),
		)

	def test_update_report_going_non_standard_checks_the_stored_script(self):
		before = frappe._dict(report_script=_BAD, report_type="Script Report", disabled=0, is_standard="Yes")
		self.assertIn("Report report_script (stored code,", _raised("Report", {"is_standard": "No"}, before))
		self.assertIsNone(check_sandbox_code("Report", {"is_standard": "Yes"}, before))
		no_script = frappe._dict(report_script=_BAD, report_type="Report Builder", is_standard="Yes")
		self.assertIsNone(check_sandbox_code("Report", {"is_standard": "No"}, no_script))
		already = frappe._dict(report_script=_BAD, report_type="Script Report", is_standard="No")
		self.assertIsNone(check_sandbox_code("Report", {"is_standard": "No"}, already))

	def test_a_stored_code_refusal_names_the_document_and_says_so(self):
		before = frappe._dict(name="zz-ss", script=_BAD, disabled=1, script_type="API")
		text = _raised("Server Script", {"disabled": 0}, before)
		self.assertTrue(
			text.startswith("Server Script zz-ss script (stored code, which this update makes active):\n"),
			text,
		)
		self.assertIn("line 1:", text)
		self.assertTrue(
			text.endswith("\nSend a corrected script in the same update, or call validate_script first."),
			text,
		)
		self.assertNotIn(_FIX_LINE, text)
		# Repeating the unchanged stored code beside the re-enable is still the stored code.
		self.assertIn("(stored code,", _raised("Server Script", {"disabled": 0, "script": _BAD}, before))
		report = frappe._dict(
			name="zz-rep", report_script=_BAD, report_type="Query Report", is_standard="No", disabled=0
		)
		text = _raised("Report", {"report_type": "Script Report"}, report)
		self.assertTrue(text.startswith("Report zz-rep report_script (stored code,"), text)
		self.assertTrue(
			text.endswith(
				"Send a corrected report_script in the same update, or call validate_script first."
			),
			text,
		)

	def test_a_changed_value_on_update_names_the_document_but_is_not_stored_code(self):
		before = frappe._dict(name="zz-ss", script="x = 1\n", disabled=0, script_type="API")
		text = _raised("Server Script", {"script": _BAD}, before)
		self.assertTrue(text.startswith("Server Script zz-ss script:\n"), text)
		self.assertTrue(text.endswith("\n" + _FIX_LINE), text)
		self.assertNotIn("stored code", text)

	def test_update_clean_stored_code_passes_every_trigger(self):
		before = frappe._dict(script="x = 1\n", disabled=1, script_type="API")
		self.assertIsNone(
			check_sandbox_code("Server Script", {"disabled": 0, "script_type": "Permission Query"}, before)
		)

	def test_deep_input_fails_open(self):
		deep = "x = " + "(" * 100000 + "1" + ")" * 100000
		self.assertIsNone(check_sandbox_code("Server Script", {"script": deep}))
		self.assertIsNone(check_sandbox_code("Report", {"report_script": "-" * 200000 + "1"}))


class TestParityWithSandbox(FrappeTestCase):
	"""Static verdict == real sandbox verdict, for the same sample."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(enable_safe_exec())

	def test_flagged_exec_samples_raise_in_the_sandbox(self):
		for name, code in _FLAGGED_EXEC.items():
			with self.subTest(sample=name):
				self.assertTrue(unsafe_attribute_errors(code), "not flagged statically")
				with self.assertRaisesRegex(SyntaxError, "is an unsafe attribute"):
					safe_exec(code, None, {"doc": _doc()})

	def test_clean_exec_samples_run_in_the_sandbox(self):
		for name, code in _CLEAN_EXEC.items():
			with self.subTest(sample=name):
				self.assertEqual(unsafe_attribute_errors(code), [])
				safe_exec(code, None, {"doc": _doc()})  # must complete with no exception

	def test_flagged_eval_samples_raise_in_the_sandbox(self):
		for name, code in _FLAGGED_EVAL.items():
			with self.subTest(sample=name):
				self.assertTrue(unsafe_attribute_errors(code, mode="eval"), "not flagged statically")
				with self.assertRaisesRegex(SyntaxError, "is an unsafe attribute"):
					frappe.safe_eval(code, None, {"doc": _doc()})

	def test_clean_eval_samples_run_in_the_sandbox(self):
		for name, code in _CLEAN_EVAL.items():
			with self.subTest(sample=name):
				self.assertEqual(unsafe_attribute_errors(code, mode="eval"), [])
				frappe.safe_eval(code, None, {"doc": _doc()})
