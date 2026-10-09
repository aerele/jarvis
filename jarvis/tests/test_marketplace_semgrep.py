"""The Marketplace Semgrep gate fails on exactly what fails the Frappe Marketplace audit."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from jarvis.ci import marketplace_semgrep as gate


def _result(severity, *, blocking=None, rule="frappe-x", path="jarvis/a.py", line=3, message="msg"):
	metadata = {} if blocking is None else {"is_blocking": blocking}
	return {
		"check_id": f"home.press.semgrep-rules.{rule}",
		"path": path,
		"start": {"line": line},
		"extra": {"severity": severity, "message": message, "metadata": metadata},
	}


class TestMarketplaceSemgrepGate(unittest.TestCase):
	def run_gate(self, results, errors=()):
		report = io.StringIO(json.dumps({"results": list(results), "errors": list(errors)}))
		out = io.StringIO()
		with patch("sys.stdin", report), redirect_stdout(out):
			code = gate.main([])
		return code, out.getvalue()

	def test_critical_major_minor_and_blocking_fail(self):
		for result in (
			_result("ERROR"),
			_result("CRITICAL"),
			_result("HIGH"),
			_result("WARNING"),
			_result("MEDIUM"),
			_result("LOW", blocking=True),
		):
			with self.subTest(result=result["extra"]):
				self.assertTrue(gate.fails_audit(result))

	def test_info_passes(self):
		for severity in ("LOW", "INFO", "unknown"):
			with self.subTest(severity=severity):
				self.assertFalse(gate.fails_audit(_result(severity, blocking=False)))
		self.assertFalse(gate.fails_audit(_result("INFO", blocking="true")))  # only an explicit True

	def test_an_internal_only_minor_fails(self):
		# The audit hides its occurrences, but its category still reads "Needs Improvement".
		result = _result("WARNING")
		result["extra"]["metadata"]["is_internal_only"] = True
		self.assertTrue(gate.fails_audit(result))

	def test_exit_code_and_annotations(self):
		code, out = self.run_gate(
			[_result("INFO"), _result("WARNING", rule="frappe-open", path="a,b.py", line=7)]
		)
		self.assertEqual(code, 1)
		self.assertIn("::error file=a%2Cb.py,line=7,title=frappe-open (Minor)::msg", out)
		self.assertIn("1 finding(s) fail the Marketplace audit; 1 info", out)
		code, out = self.run_gate([_result("INFO")], errors=[{"message": "Timeout on x.py"}])
		self.assertEqual(code, 0)
		self.assertIn("::warning title=semgrep::Timeout on x.py", out)

	def test_message_newlines_are_escaped(self):
		line = gate.annotation(_result("ERROR", blocking=True, message="a\n100%"))
		self.assertTrue(line.endswith("::a 100%25"), line)
		self.assertIn("(blocking%2C Critical)", line)
