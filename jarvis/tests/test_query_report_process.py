"""Actual child, bounded IPC, proof-before-SQL ordering and resource failures."""

import unittest
from unittest.mock import patch

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests.test_query_report_data import source
from jarvis.tools import _report_query_process as process


class TestReportQueryProcess(unittest.TestCase):
	def test_real_child_reaches_last_row_and_counts_whole_source(self):
		calls = []

		def authorize(schema, deadline):
			self.assertEqual(schema["source_rows"], 6001)
			calls.append("schema")
			return {"proof_fields": [0, 1], "masked_columns": []}

		def validate(schema, context, proofs, deadline):
			self.assertEqual(len(proofs["rows"]), 6000)
			self.assertEqual(proofs["rows"][-1][1], "CUST-06000")
			calls.append("proofs")

		result = process.analyze_report(
			source(),
			{"add_total_row": True},
			authorize=authorize,
			validate=validate,
			sql="SELECT COUNT(*), decimal_sum(c3), max(_row) FROM report_rows",
		)
		self.assertEqual(calls, ["schema", "proofs"])
		self.assertEqual(result["rows"], [[6000, "600.00", 6000]])
		self.assertTrue(result["coverage"]["complete"])

	def test_access_rejection_precedes_even_invalid_sql(self):
		def reject(*_):
			raise PermissionDeniedError("revoked customer access")

		with self.assertRaisesRegex(PermissionDeniedError, "revoked"):
			process.analyze_report(
				source(10),
				{"add_total_row": True},
				authorize=lambda *_: {"proof_fields": [1]},
				validate=reject,
				sql="THIS IS NOT SQL",
			)

	def test_deadline_kills_and_reaps_child(self):
		children = []
		real = process.subprocess.Popen

		def start(*args, **kwargs):
			child = real(*args, **kwargs)
			children.append(child)
			return child

		with (
			patch.object(process.subprocess, "Popen", side_effect=start),
			self.assertRaisesRegex(InvalidArgumentError, "resource budget"),
		):
			process.analyze_report(
				source(10),
				{"add_total_row": True},
				authorize=lambda *_: {},
				validate=lambda *_: None,
				deadline=process.Deadline(0.001),
			)
		self.assertEqual(len(children), 1)
		self.assertIsNotNone(children[0].poll())

	def test_stdout_cap_refuses_before_parsing_large_frame(self):
		with (
			patch.object(process, "MAX_SCHEMA", 80),
			self.assertRaisesRegex(InvalidArgumentError, "output exceeded"),
		):
			process.analyze_report(
				source(3), {"add_total_row": True}, authorize=lambda *_: {}, validate=lambda *_: None
			)

	def test_invalid_gzip_does_not_reach_access_callback(self):
		called = []
		with self.assertRaisesRegex(InvalidArgumentError, "gzip"):
			process.analyze_report(
				b"not-a-report", {}, authorize=lambda *_: called.append(1), validate=lambda *_: None
			)
		self.assertEqual(called, [])
