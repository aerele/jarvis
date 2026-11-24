"""AC-B8: the reply of a read is the same with the switch on as with the switch off.

The cases are those of `test_payment_calendar_reply_matrix` (its fixtures, its `collect`, its canonical
comparison of the whole and the split reply), run twice for each user: the switch off, then the switch on.
Two reads per user: the routed read (mode `on`, the reader serves a side it may serve) and today's read with
the guards lifted (mode `off`). A `has_permission` hook hides one invoice of each side, so the permission
check has work to do. `SMALL_INPUT` is 0, so a user without permissions takes the batch on two names.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_batch_permission_replies
"""

from unittest.mock import patch

import frappe

from jarvis.payment_calendar import batch_permission, read_log
from jarvis.tests import test_payment_calendar_reply_matrix as matrix
from jarvis.tests.test_batch_permission_pins import clean_bindings

PURCHASE_INVOICE, SALES_INVOICE = "Purchase Invoice", "Sales Invoice"
READS = (("on", "above"), ("off", "lifted"))


class SwitchPairCase(matrix.MatrixCase):
	"""The genuine Frappe functions are bound for the class; no test leaves an Error Log row."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		manager = clean_bindings()
		manager.__enter__()
		cls.addClassCleanup(manager.__exit__, None, None, None)
		writer = patch.object(read_log, "error_log_once")
		writer.start()
		cls.addClassCleanup(writer.stop)

	def setUp(self):
		super().setUp()
		self.evaluated = []
		real = batch_permission._evaluate_batch

		def evaluate(doctype, *args, **kwargs):
			self.evaluated.append(doctype)
			return real(doctype, *args, **kwargs)

		for patcher in (
			patch.object(batch_permission, "_evaluate_batch", evaluate),
			patch.object(batch_permission, "SMALL_INPUT", 0),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		matrix._hidden.update({self.payable[1], self.receivable[1]})
		frappe.db.commit()  # `consistent_read` rolls back first: the fixtures must be committed

	def restrictions(self, switch: bool):
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			if kind == "has_permission":
				hook = f"{matrix.HOOKS}.hide_voucher"
				return {PURCHASE_INVOICE: [hook], SALES_INVOICE: [hook]}
			return real(kind, *args, **kwargs)

		return [
			patch("frappe.get_hooks", get_hooks),
			patch.dict(frappe.local.conf, {batch_permission.SWITCH: int(switch)}),
		]

	def read(self, user, switch, mode, guard):
		"""`(scope, result)` of one read, or the class of the error that ended it."""
		self.evaluated.clear()
		try:
			return self.collect(user, mode=mode, guard=guard, restrictions=self.restrictions(switch))
		except Exception as error:
			return type(error)

	def assert_same_reply(self, user, label):
		"""The reads of `user` give the same reply with the switch on and off; returns both."""
		outcomes = {}
		for mode, guard in READS:
			off = self.read(user, False, mode, guard)
			self.assertEqual(self.evaluated, [], f"{label}: the batch ran with the switch off")
			on = self.read(user, True, mode, guard)
			outcomes[mode, guard] = (off, on, list(self.evaluated))
			if isinstance(off, type) or isinstance(on, type):
				self.assertIs(on, off, f"{label} {mode}/{guard}: the error differs")
			else:
				self.assert_replies_equal(on, off, f"{label} {mode}/{guard}: switch on vs off")
		return outcomes

	def served_rows(self, result, side):
		"""The rows of a side, or none when the side is refused (`too_large`)."""
		return [] if result["sources"][side] is None else self.rows(result, side)

	def hidden_in(self, result, side):
		return {row["voucher_no"] for row in self.served_rows(result, side)} & matrix._hidden


class TestReplyWithTheSwitchOn(SwitchPairCase):
	def test_a_user_without_permissions_gets_the_same_reply_and_the_batch_ran(self):
		outcomes = self.assert_same_reply(self.new_user(), "no permissions")
		for (mode, guard), (off, on, evaluated) in outcomes.items():
			with self.subTest(mode=mode, guard=guard):
				self.assertIsInstance(on, tuple, "the read was served")
				self.assertEqual(set(evaluated), {PURCHASE_INVOICE, SALES_INVOICE})
				for side in matrix.SIDES:
					self.assertEqual(self.hidden_in(on[1], side), set(), side)
					self.assertTrue(self.served_rows(on[1], side), side)

	def test_a_user_with_a_company_permission_gets_the_same_reply(self):
		outcomes = self.assert_same_reply(self.new_user(company_permission=True), "company permission")
		for (mode, guard), (off, on, _) in outcomes.items():
			with self.subTest(mode=mode, guard=guard):
				self.assertIsInstance(on, tuple)
				for side in matrix.SIDES:
					self.assertEqual(self.hidden_in(on[1], side), set(), side)

	def test_the_administrator_gets_the_same_reply(self):
		outcomes = self.assert_same_reply("Administrator", "administrator")
		for (mode, guard), (off, on, _) in outcomes.items():
			with self.subTest(mode=mode, guard=guard):
				self.assertIsInstance(on, tuple)
				for side in matrix.SIDES:
					# Frappe's own check passes the Administrator before any hook: the hidden invoices stay
					self.assertTrue(self.served_rows(on[1], side), side)

	def test_a_visitor_is_refused_alike(self):
		visitor = self.fx.user()
		self.users.append(visitor)
		frappe.db.delete("Has Role", {"parent": visitor})
		frappe.db.commit()
		frappe.clear_cache(user=visitor)
		outcomes = self.assert_same_reply(visitor, "visitor")
		for (mode, guard), (off, on, _) in outcomes.items():
			with self.subTest(mode=mode, guard=guard):
				self.assertIsInstance(on, type, "the visitor gets no reply")
				self.assertIsInstance(off, type)
