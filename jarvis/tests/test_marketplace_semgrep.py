"""The Marketplace Semgrep gate fails on exactly what fails the Frappe Marketplace audit."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

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
		with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
			json.dump({"results": list(results), "errors": list(errors)}, fh)
		self.addCleanup(os.unlink, fh.name)
		out = io.StringIO()
		with redirect_stdout(out):
			code = gate.main([fh.name])
		return code, out.getvalue()

	def test_critical_major_and_blocking_fail(self):
		for result in (
			_result("ERROR"),
			_result("CRITICAL"),
			_result("HIGH"),
			_result("WARNING", blocking=True),
		):
			with self.subTest(result=result["extra"]):
				self.assertTrue(gate.fails_audit(result))

	def test_minor_and_info_pass(self):
		for severity in ("WARNING", "MEDIUM", "LOW", "INFO", "unknown"):
			with self.subTest(severity=severity):
				self.assertFalse(gate.fails_audit(_result(severity, blocking=False)))
		self.assertFalse(gate.fails_audit(_result("WARNING", blocking="true")))  # only an explicit True

	def test_exit_code_and_annotations(self):
		code, out = self.run_gate(
			[_result("WARNING"), _result("HIGH", rule="frappe-sql", path="a,b.py", line=7)]
		)
		self.assertEqual(code, 1)
		self.assertIn("::error file=a%2Cb.py,line=7,title=frappe-sql (Major)::msg", out)
		self.assertIn("1 finding(s) fail the Marketplace audit; 1 minor", out)
		code, out = self.run_gate([_result("WARNING")], errors=[{"message": "Timeout on x.py"}])
		self.assertEqual(code, 0)
		self.assertIn("::warning title=semgrep::Timeout on x.py", out)

	def test_message_newlines_are_escaped(self):
		line = gate.annotation(_result("ERROR", blocking=True, message="a\n100%"))
		self.assertTrue(line.endswith("::a 100%25"), line)
		self.assertIn("(blocking%2C Critical)", line)
