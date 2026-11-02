"""The proposal key's numbers (``jarvis.chat.one_approval.create_key``).

Two proposals are the same only when their original values are exactly equal, so a
number must never be rounded into another one. ``Decimal.normalize`` rounds to the
decimal context (28 significant digits by default), which made two long references
that differ in their last digits read as one. The canonical form is exact and does
not depend on the context. Pure tests: no site, no database."""

from __future__ import annotations

import decimal
import unittest

from jarvis.chat.one_approval import _decimal, create_key

LONG_A = "1234567890123456789012345678901234"  # 34 digits, past the default context
LONG_B = "1234567890123456789012345678901299"


def key(**values):
	return create_key("Payment Entry", values)


class TestNumbersAreExact(unittest.TestCase):
	def test_long_integers_that_differ_in_the_last_digits_are_different(self):
		self.assertNotEqual(key(reference_no=LONG_A), key(reference_no=LONG_B))

	def test_long_fractions_that_differ_past_the_context_are_different(self):
		self.assertNotEqual(
			key(amount="1.234567890123456789012345678901234"),
			key(amount="1.234567890123456789012345678901299"),
		)
		self.assertNotEqual(
			key(amount="0.00000000000000000000000000000001"), key(amount="0.00000000000000000000000000000002")
		)

	def test_the_result_does_not_depend_on_the_decimal_context(self):
		for prec in (1, 3, 5, 28, 60):
			with decimal.localcontext() as ctx:
				ctx.prec = prec
				self.assertEqual(_decimal(LONG_A), LONG_A, prec)
				self.assertEqual(_decimal("12345.6789"), "12345.6789", prec)
				self.assertNotEqual(key(reference_no=LONG_A), key(reference_no=LONG_B), prec)

	def test_the_same_value_gives_the_same_key_in_any_context(self):
		with decimal.localcontext() as ctx:
			ctx.prec = 3
			low = key(reference_no=LONG_A, amount="2546.50")
		self.assertEqual(low, key(reference_no=LONG_A, amount="2546.50"))


class TestOrdinaryEquivalencesStay(unittest.TestCase):
	def test_numeric_text_is_the_number(self):
		self.assertEqual(key(amount="2546"), key(amount=2546))
		self.assertEqual(key(amount="2546.5"), key(amount=2546.5))

	def test_trailing_zeros_after_the_point_are_not_a_difference(self):
		self.assertEqual(key(amount="2546.50"), key(amount="2546.5"))
		self.assertEqual(key(amount="2546.00"), key(amount=2546))
		self.assertEqual(key(amount=2546.0), key(amount="2546"))

	def test_zeros_before_the_point_are_never_stripped(self):
		self.assertEqual(_decimal("100"), "100")
		self.assertEqual(_decimal("100.00"), "100")
		self.assertEqual(_decimal("10.10"), "10.1")
		self.assertNotEqual(key(amount="100"), key(amount="10"))

	def test_a_leading_zero_reference_stays_text(self):
		self.assertNotEqual(key(reference_no="0436"), key(reference_no=436))
		self.assertNotEqual(key(reference_no="0436"), key(reference_no="436"))

	def test_text_is_not_a_number_or_a_list(self):
		self.assertNotEqual(key(a="1"), key(a=["1"]))
		self.assertNotEqual(key(a="abc"), key(a="ABC"))

	def test_blanks_are_dropped_and_a_different_doctype_is_a_different_key(self):
		self.assertEqual(key(a="x", b=""), key(a="x"))
		self.assertNotEqual(create_key("Payment Entry", {"a": 1}), create_key("Journal Entry", {"a": 1}))


class TestSignAndFloatForms(unittest.TestCase):
	def test_negative_zero_keeps_its_sign_as_before(self):
		self.assertEqual(_decimal("-0"), "-0")
		self.assertEqual(_decimal("-0.0"), "-0")
		self.assertEqual(_decimal("0.0"), "0")
		self.assertNotEqual(key(a="-0"), key(a="0"))
		self.assertEqual(key(a="-0.0"), key(a=-0.0))

	def test_negatives_are_exact_too(self):
		self.assertEqual(_decimal("-" + LONG_A), "-" + LONG_A)
		self.assertNotEqual(key(a="-" + LONG_A), key(a="-" + LONG_B))

	def test_float_scientific_forms_normalise_to_the_plain_number(self):
		self.assertEqual(_decimal(repr(1e16)), "10000000000000000")
		self.assertEqual(key(a=1e16), key(a="10000000000000000"))
		self.assertEqual(key(a=1.5e-7), key(a="0.00000015"))
		self.assertEqual(key(a=0.1), key(a="0.1"))


if __name__ == "__main__":
	unittest.main()
