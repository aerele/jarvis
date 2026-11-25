"""Coverage-gap remediation text reads as sentences whether an evaluator sends a
subject ("Stock Settings") or a whole sentence as its detail."""

import unittest

from jarvis.chat.agent_runs import _remediation_text


class TestRemediationText(unittest.TestCase):
	def test_a_subject_fills_the_slot(self):
		self.assertEqual(
			_remediation_text("unsupported_customisation", "Stock Settings"),
			"A customisation on Stock Settings is not supported by this check.",
		)

	def test_no_detail_uses_the_neutral_noun(self):
		self.assertEqual(
			_remediation_text("record_coverage_insufficient", ""),
			"Not enough records in scope this period to evaluate.",
		)

	def test_a_sentence_detail_follows_the_template_instead_of_filling_it(self):
		cases = [
			(
				"unsupported_customisation",
				"The evaluator scope did not match the server-stamped Warehouse boundary.",
				"A customisation on this doctype is not supported by this check. "
				"The evaluator scope did not match the server-stamped Warehouse boundary.",
			),
			(
				"record_coverage_insufficient",
				"The warehouse summary has incomplete constituent checks",
				"Not enough records in scope this period to evaluate. "
				"The warehouse summary has incomplete constituent checks.",
			),
			(
				"unsupported_customisation",
				"A required date is malformed",
				"A customisation on this doctype is not supported by this check. A required date is malformed.",
			),
		]
		for code, detail, expected in cases:
			with self.subTest(detail=detail):
				self.assertEqual(_remediation_text(code, detail), expected)
