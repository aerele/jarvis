"""Voucher-type scenarios: today's read of each ledger shape, and the built-in reader against it.

Most scenarios are raw ledger lines on `test_jarvis`. A real controller is impractical for the HRMS and
lending documents (employees, salary structures, loan products, a chart of accounts per scenario), so the
lines are the ones the cited controllers post, with a minimal submitted header from
`CalendarFixtures.voucher_lines`: the account type, the sign, the party type and the against reference are
the controller's own, and an advance link goes to an Advance Payment Ledger Entry, never to the ledger line.
The Sales Invoice and Journal Entry shapes also run through the real controllers
(`TestRealControllerVoucherTypes`). The reader is called through its public `builtin_reader.read_obligations`
and must serve (no ReaderFallback) unless a scenario declares the fallback.
"""

from unittest import SkipTest

import frappe
from frappe.utils import add_days, nowdate

from jarvis.payment_calendar import builtin_reader, sources
from jarvis.payment_calendar.errors import ReaderFallback
from jarvis.tests import _erpnext_masters as masters
from jarvis.tests.test_payment_calendar_builtin_reader import (
	INVOICE_TYPES,
	PARTY_TYPES,
	SIDES,
	ReaderCase,
)

# The reads of the scenarios go through this name, so a differential family can re-run them with another reader.
read_obligations = sources.read_obligations

REVALUATION_TYPES = ("Exchange Rate Revaluation", "Exchange Gain Or Loss")

# Every shape the combined test builds, with the app that owns its DocTypes (None: ERPNext). The combined test
# builds exactly the shapes whose app is installed, and fails when one is skipped.
COMBINED_SHAPES = {
	"employee_advance": "hrms",
	"expense_claim": "hrms",
	"expense_claim_paid": "hrms",
	"expense_claim_settled_by_advance": "hrms",
	"expense_claim_foreign": "hrms",
	"gratuity": "hrms",
	"leave_encashment": "hrms",
	"payroll_with_employees": "hrms",
	"payroll_without_party": "hrms",
	"loan": "lending",
	"loan_employee": "lending",
	"loan_repayment_on_invoice": "lending",
	"pos_invoice": None,
	"write_off": None,
	"opening": None,
	"unknown_type": None,
	"exchange_rate_revaluation": None,
	"exchange_gain_or_loss_only": None,
}
# `add_gain_loss_on_invoice` changes the currency of the side's own account, so it never joins the combined ledger.


def require_doctypes(*doctypes):
	"""Skip with a reason naming the absent DocTypes (their app is not installed on this site)."""
	missing = [name for name in doctypes if not frappe.db.exists("DocType", name)]
	if missing:
		raise SkipTest(
			f"DocType(s) {', '.join(missing)} are not installed on this site (hrms or lending absent)"
		)


class VoucherCase(ReaderCase):
	def both_sides(self, scenario, window=None):
		"""As `ReaderCase.both_sides`, but a scenario that skips (its app's DocType is absent) skips the TEST.

		`unittest` records a `SkipTest` raised inside a `subTest` as a skipped sub-test and runs on, so the
		test body would assert expected rows over a ledger nothing was built into.
		"""
		for side in SIDES:
			skipped = None
			with self.subTest(side):
				try:
					scenario(side)
				except SkipTest as skip:
					skipped = skip
				else:
					self.assert_reader_equals_today(side, window)
			if skipped:
				raise skipped

	def reader_result(self, side, window=None):
		"""The PUBLIC reader: it must serve (a ReaderFallback propagates and fails the test)."""
		start, end = self.window(window)
		return builtin_reader.read_obligations(self.fx.company, side, self.fx.today, start, end)

	def read(self, side, window=None):
		start, end = self.window(window)
		return read_obligations(self.fx.company, side, self.fx.today, start, end)

	def rows(self, side, *fields):
		"""The chosen fields of every row of today's read of `side`, in row order."""
		return [tuple(row[field] for field in fields) for row in self.read(side)["rows"]]

	def assert_equal_or_fallback(self, side, reason=None, window=None):
		"""The reader equals today's read, or (only when `reason` is given) raises that ReaderFallback.

		A fallback is declared per scenario with its reason; the reader never serves a different figure.
		"""
		if reason is None:
			return self.assert_reader_equals_today(side, window)
		self.sync()
		self.today_result(side, window)
		with self.assertRaises(ReaderFallback) as raised:
			self.reader_result(side, window)
		self.assertEqual(raised.exception.reason, reason)
		return None

	def party_line(self, suffix, side, party_type, party, **extra):
		"""A ledger line of `side`'s account for a party of any party type (default values of `voucher_lines`)."""
		return {
			"suffix": suffix,
			"account_type": side,
			"account": self.parties(side)[1],
			"party_type": party_type,
			"party": party,
			**extra,
		}

	def employee(self, tag="A"):
		name = self.fx.prefix + "Employee" + tag
		if frappe.db.exists("Employee", name):
			return name
		return self.fx.insert("Employee", "Employee" + tag, employee_name="Scenario employee " + tag)

	def usd_payable(self):
		"""A second Payable account of the fixture company, in USD (an Expense Claim in a foreign currency)."""
		name = self.fx.prefix + "PayableUSD"
		if frappe.db.exists("Account", name):
			return name
		return self.fx.insert(
			"Account",
			"PayableUSD",
			account_name=self.fx.prefix + "PayableUSD",
			company=self.fx.company,
			account_type="Payable",
			account_currency="USD",
			is_group=0,
			disabled=0,
			report_type="Balance Sheet",
			root_type="Liability",
			lft=1,
			rgt=2,
		)

	def payment_against(self, suffix, side, party_type, party, doctype, name, amount, **extra):
		"""A Payment Entry line of `amount` that references another document (a payment of an invoice kind)."""
		return self.fx.voucher_lines(
			"Payment Entry",
			self.fx.prefix + "PE" + suffix,
			[
				self.party_line(
					suffix,
					side,
					party_type,
					party,
					amount=amount,
					against_voucher_type=doctype,
					against_voucher_no=name,
					**extra,
				)
			],
		)

	def advance_payment(self, suffix, side, party, advance_type, advance_no, amount):
		"""A Payment Entry line that pays an advance document: against ITSELF, the advance link is not on the line."""
		voucher = self.fx.prefix + "AdvancePE" + suffix
		return self.fx.voucher_lines(
			"Payment Entry",
			voucher,
			[
				self.party_line(
					suffix,
					side,
					"Employee",
					party,
					amount=amount,
					against_voucher_type="Payment Entry",
					against_voucher_no=voucher,
					advance_voucher=(advance_type, advance_no),
				)
			],
		)


class TestVoucherTypeScenarios(VoucherCase):
	"""One test per voucher type. The `add_*` builders put one scenario into the fixture company for a side.

	Each builder adds only the side the controller posts to: Employee payables and payroll are Payable lines
	(an Employee party is kept on that side); the Employee Advance account and the loans are Receivable lines
	(the Receivable report excludes the Employee party type). The other side's read is then empty.
	"""

	def add_expense_claim(self, side):
		require_doctypes("Expense Claim")
		if side != "Payable":
			return
		employee = self.employee()
		claim = self.fx.prefix + "ExpenseClaim"
		self.fx.voucher_lines(
			"Expense Claim", claim, [self.party_line("ClaimOwn", side, "Employee", employee, amount=100)]
		)
		self.payment_against("ClaimPaid", side, "Employee", employee, "Expense Claim", claim, -40)

	def add_expense_claim_paid(self, side):
		require_doctypes("Expense Claim")
		if side != "Payable":
			return
		employee, claim = self.employee(), self.fx.prefix + "ExpenseClaimPaid"
		self.fx.voucher_lines(
			"Expense Claim",
			claim,
			[
				self.party_line("PaidClaimOwn", side, "Employee", employee, amount=100),
				self.party_line("PaidClaimSettled", side, "Employee", employee, amount=-100),
			],
		)

	def add_expense_claim_foreign(self, side):
		require_doctypes("Expense Claim")
		if side != "Payable":
			return
		employee, claim = self.employee(), self.fx.prefix + "ExpenseClaimUSD"
		foreign = {"account": self.usd_payable(), "account_currency": "USD"}
		self.fx.voucher_lines(
			"Expense Claim",
			claim,
			[
				self.party_line(
					"UsdClaimOwn",
					side,
					"Employee",
					employee,
					amount=8300,
					amount_in_account_currency=100,
					**foreign,
				)
			],
		)
		self.payment_against(
			"UsdClaimPaid",
			side,
			"Employee",
			employee,
			"Expense Claim",
			claim,
			-4150,
			amount_in_account_currency=-50,
			**foreign,
		)

	def add_advance_paid_document(self, side, doctype):
		"""The payable posted by `doctype` and an advance Payment Entry that paid it (see `advance_payment`)."""
		require_doctypes(doctype)
		if side != "Payable":
			return
		employee, name = self.employee(), self.fx.prefix + doctype.replace(" ", "")
		tag = doctype.replace(" ", "")
		self.fx.voucher_lines(
			doctype, name, [self.party_line("Own" + tag, side, "Employee", employee, amount=100)]
		)
		self.advance_payment("Paid" + tag, side, employee, doctype, name, -40)

	def add_employee_advance(self, side):
		require_doctypes("Employee Advance")
		if side != "Receivable":
			return
		advance = self.fx.prefix + "EmployeeAdvance"
		if frappe.db.exists("Employee Advance", advance):
			return
		self.fx.voucher_lines("Employee Advance", advance, [])
		self.advance_payment("EmployeeAdvance", side, self.employee(), "Employee Advance", advance, 70)

	def add_expense_claim_settled_by_advance(self, side):
		"""A claim of 130 less the advance of 30 used: payable 100 (Payable), advance consumed -30 (Receivable)."""
		require_doctypes("Expense Claim", "Employee Advance")
		self.add_employee_advance(side)
		employee, claim = self.employee(), self.fx.prefix + "ExpenseClaimAdvance"
		advance = self.fx.prefix + "EmployeeAdvance"
		if side == "Payable":
			line = self.party_line("AdvClaimOwn", side, "Employee", employee, amount=100)
		else:
			line = self.party_line(
				"AdvClaimUsed",
				side,
				"Employee",
				employee,
				amount=-30,
				against_voucher_type="Employee Advance",
				against_voucher_no=advance,
				advance_voucher=("Employee Advance", advance),
			)
		self.fx.voucher_lines("Expense Claim", claim, [line])

	def add_payroll(self, side, with_party=True):
		"""The accrual Journal Entry and the bank Journal Entry, both against the Payroll Entry."""
		require_doctypes("Payroll Entry")
		if side != "Payable":
			return
		kind = "WithEmployees" if with_party else "NoParty"
		payroll = self.fx.prefix + "Payroll" + kind
		self.fx.voucher_lines("Payroll Entry", payroll, [])
		if with_party:
			# Employee-wise payroll payable: one line per employee, the party kept (payroll_entry.py:972-981, :1203-1217).
			one, other = self.employee("A"), self.employee("B")
			accruals = (("AccrualA", one, 500), ("AccrualB", other, 300))
			payments = (("BankA", one, -500), ("BankB", other, -100))
		else:
			# Default payroll payable: one line, no party (payroll_entry.py:972-975, :1220-1233).
			accruals, payments = (("AccrualNoParty", None, 800),), (("BankNoParty", None, -600),)
		for journal, entries in (("AccrualJE", accruals), ("BankJE", payments)):
			self.fx.voucher_lines(
				"Journal Entry",
				self.fx.prefix + journal + kind,
				[
					self.party_line(
						name,
						side,
						"Employee" if party else None,
						party,
						amount=amount,
						against_voucher_type="Payroll Entry",
						against_voucher_no=payroll,
					)
					for name, party, amount in entries
				],
			)

	def add_loan_lines(self, side, tag, party_type, party):
		"""A Loan with a disbursement and a repayment line on the Receivable (loan) account."""
		require_doctypes("Loan", "Loan Disbursement", "Loan Repayment")
		if side != "Receivable":
			return
		loan = self.fx.prefix + "Loan" + tag
		self.fx.voucher_lines("Loan", loan, [])
		for voucher_type, kind, amount in (
			("Loan Disbursement", "Disbursed", 1000),
			("Loan Repayment", "Repaid", -400),
		):
			self.fx.voucher_lines(
				voucher_type,
				self.fx.prefix + kind + tag,
				[
					self.party_line(
						kind + tag,
						side,
						party_type,
						party,
						amount=amount,
						against_voucher_type="Loan",
						against_voucher_no=loan,
					)
				],
			)

	def add_loan(self, side):
		self.add_loan_lines(side, "Customer", "Customer", self.fx.customer)

	def add_loan_employee(self, side):
		self.add_loan_lines(side, "Employee", "Employee", self.employee())

	def add_loan_repayment_on_invoice(self, side):
		"""A Loan Repayment of charges: the credit line is on the Sales Invoice's own account, against the invoice."""
		require_doctypes("Loan Repayment")
		if side != "Receivable":
			return
		invoice = self.invoice(side, (100,))
		self.fx.voucher_lines(
			"Loan Repayment",
			self.fx.prefix + "ChargeRepayment",
			[
				self.party_line(
					"ChargeRepaid",
					side,
					"Customer",
					self.fx.customer,
					amount=-40,
					against_voucher_type="Sales Invoice",
					against_voucher_no=invoice,
				)
			],
		)

	def add_pos_invoice(self, side):
		doctype = INVOICE_TYPES[side]
		invoice = self.invoice(side, (100,))
		frappe.db.set_value(
			doctype, invoice, "is_pos" if side == "Receivable" else "is_paid", 1, update_modified=False
		)
		self.raw("PosPaid" + side, doctype, invoice, doctype, invoice, -60, side)

	def add_write_off(self, side, paid=95):
		doctype = INVOICE_TYPES[side]
		invoice = self.invoice(side, (100,), paid=paid)
		self.raw("WriteOff" + str(paid) + side, doctype, invoice, doctype, invoice, -5, side)

	def add_opening(self, side):
		doctype = INVOICE_TYPES[side]
		party, _ = self.parties(side)
		invoice = self.invoice(side, (100,), offset=-400)
		frappe.db.set_value(doctype, invoice, "is_opening", "Yes", update_modified=False)
		journal = self.fx.prefix + "OpeningJE" + side
		self.fx.voucher_lines(
			"Journal Entry",
			journal,
			[
				self.party_line(
					"OpeningJournal" + side,
					side,
					PARTY_TYPES[side],
					party,
					amount=70,
					posting_date=add_days(self.fx.today, -400),
				)
			],
			header={"voucher_type": "Opening Entry", "is_opening": "Yes"},
		)
		self.payment_against(
			"OpeningPaid" + side, side, PARTY_TYPES[side], party, "Journal Entry", journal, -20
		)

	def add_revaluation_journal(self, side, journal_type):
		party, _ = self.parties(side)
		kind = journal_type.replace(" ", "")
		self.fx.voucher_lines(
			"Journal Entry",
			self.fx.prefix + "ErrJE" + kind + side,
			[
				self.party_line(
					"ErrLine" + kind + side,
					side,
					PARTY_TYPES[side],
					party,
					amount=3,
					amount_in_account_currency=0,
					against_voucher_type="Exchange Rate Revaluation",
					against_voucher_no=self.fx.prefix + "Err" + side,
				)
			],
			header={"voucher_type": journal_type},
		)

	def add_gain_loss_on_invoice(self, side):
		"""A foreign invoice whose company-currency residual (0.01) the Exchange Gain Or Loss journal books off.

		erpnext accounts/utils.py:2575-2580 posts the journal's party line against the invoice (ref1) in the
		company currency only: amount -0.01, amount in account currency 0.
		"""
		invoice = self.foreign_invoice(side, gross=8000.01, paid=8000)
		party, _ = self.parties(side)
		self.fx.voucher_lines(
			"Journal Entry",
			self.fx.prefix + "GainLossJE" + side,
			[
				self.party_line(
					"GainLoss" + side,
					side,
					PARTY_TYPES[side],
					party,
					amount=-0.01,
					amount_in_account_currency=0,
					account_currency="USD",
					against_voucher_type=INVOICE_TYPES[side],
					against_voucher_no=invoice,
				)
			],
			header={"voucher_type": "Exchange Gain Or Loss"},
		)
		return invoice

	def add_unknown_type(self, side):
		self.raw(
			"Unknown" + side,
			"Payment Entry",
			self.fx.prefix + "UnknownPE" + side,
			"_JPC Never Seen DocType",
			self.fx.prefix + "Mystery",
			-30,
			side,
		)

	def test_expense_claim(self):
		"""hrms expense_claim.py:263-271 credits the Employee payable of the claim against itself; a Payment
		Entry that references the claim (payment_entry.py:1616-1622, the generic `else` branch: Expense Claim is
		not an advance doctype) clears it by reference.
		"""
		self.both_sides(self.add_expense_claim)
		self.assertEqual(
			self.rows("Payable", "voucher_type", "party_type", "remaining"),
			[("Expense Claim", "Employee", "60.00")],
		)
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_expense_claim_paid_on_submit(self):
		"""An is_paid claim: the payable credit (expense_claim.py:263-271) and the payable debit against the claim
		itself (:342-353) post in one voucher and net to zero.
		"""
		self.both_sides(self.add_expense_claim_paid)
		self.assertEqual(self.rows("Payable", "voucher_type", "remaining"), [])

	def test_expense_claim_in_a_foreign_currency(self):
		"""A USD claim of 100 at 83: the payable credit has amount 8300 and amount in account currency 100
		(expense_claim.py:263-271, account currency of the claim); a payment of 50 USD (4150 in company
		currency) references it. The row is in company currency: 8300 less 4150 is 4150.
		"""
		self.both_sides(self.add_expense_claim_foreign)
		self.assertEqual(
			self.rows("Payable", "voucher_type", "original", "remaining"),
			[("Expense Claim", "8300.00", "4150.00")],
		)

	def test_gratuity(self):
		"""hrms gratuity.py:69-86 posts the gratuity payable against itself. Gratuity is an advance payable
		(hrms hooks.py:283), so a Payment Entry that pays it writes a line against ITSELF
		(payment_entry.py:1597-1610) and the link to the gratuity goes to an Advance Payment Ledger Entry
		(utils.py:2100-2141): the gratuity row stays whole and the payment is an unallocated adjustment.
		"""
		self.both_sides(lambda side: self.add_advance_paid_document(side, "Gratuity"))
		self.assertEqual(
			self.rows("Payable", "voucher_type", "kind", "remaining"),
			[("Gratuity", "obligation", "100.00"), ("Payment Entry", "adjustment", "-40.00")],
		)
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_leave_encashment(self):
		"""hrms leave_encashment.py:301-312 posts the encashment payable against itself; an advance Payment
		Entry is a line against itself with an Advance Payment Ledger Entry link (as for the gratuity).
		"""
		self.both_sides(lambda side: self.add_advance_paid_document(side, "Leave Encashment"))
		self.assertEqual(
			self.rows("Payable", "voucher_type", "kind", "remaining"),
			[("Leave Encashment", "obligation", "100.00"), ("Payment Entry", "adjustment", "-40.00")],
		)
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_employee_advance(self):
		"""The advance account is Receivable (hrms employee_advance.py:82-91): the Payment Entry that pays an
		advance is a debit of 70 on a Receivable account for an Employee, against itself with an Advance
		Payment Ledger Entry link (payment_entry.py:1597-1610, utils.py:2100-2141).

		The Receivable report excludes the Employee party type and no Payable account is touched: both sides
		are empty, and the reader's control total excludes the same line.
		"""
		self.both_sides(self.add_employee_advance)
		for side in SIDES:
			self.assertEqual(self.rows(side, "voucher_type"), [])

	def test_expense_claim_settled_by_an_employee_advance(self):
		"""hrms expense_claim.py:300-325 credits the advance account (Receivable) against the Employee Advance,
		with the advance link (advance_voucher_*, an Advance Payment Ledger Entry); the claim's own payable
		(:263-271) is the claim total less the advance, here 100. The advance part is an Employee line on a
		Receivable account (excluded); the claim stays an open payable of 100.
		"""
		self.both_sides(self.add_expense_claim_settled_by_advance)
		self.assertEqual(self.rows("Payable", "voucher_type", "remaining"), [("Expense Claim", "100.00")])
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_payroll_entry_accrual_and_bank_journal_per_employee(self):
		"""hrms payroll_entry.py:972-981 and :1203-1217 post the accrual (credit) and the bank Journal Entry
		(debit) on the payroll payable per Employee, both with reference Payroll Entry. The report builds its own
		Payroll Entry row for lines against a Payroll Entry (accounts_receivable.py:239-295): the accrual and the
		bank journal net per employee on the payroll's key.
		"""
		self.both_sides(lambda side: self.add_payroll(side, with_party=True))
		self.assertEqual(
			self.rows("Payable", "voucher_type", "voucher_no", "remaining"),
			[("Payroll Entry", self.fx.prefix + "PayrollWithEmployees", "200.00")],
		)
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_payroll_entry_default_path_without_party(self):
		"""The default payroll payable has no party: accrual line (payroll_entry.py:972-975) and bank journal
		line (:1220-1233) carry reference Payroll Entry and an empty party type.

		The report's party criterion drops lines without a party type: today's read has no row and discloses
		the omission (`filtered_obligations`, not complete for the company), and the reader gives the same.
		"""
		self.both_sides(lambda side: self.add_payroll(side, with_party=False))
		result = self.read("Payable")
		self.assertEqual(result["rows"], [])
		self.assertIn("filtered_obligations", result["scope_reasons"])
		self.assertFalse(result["complete_for_company"])
		self.assertEqual(self.rows("Receivable", "voucher_type"), [])

	def test_a_scenario_that_skips_skips_the_test(self):
		"""An absent app's DocType skips the whole test; it is not a skipped sub-test followed by assertions."""
		built = []

		def scenario(side):
			built.append(side)
			raise SkipTest("app absent")

		with self.assertRaises(SkipTest):
			self.both_sides(scenario)
		self.assertEqual(built, [SIDES[0]])

	def test_loan_disbursement_and_repayment(self):
		"""lending loan_disbursement.py:753-760 (debit of the loan account, applicant as party) and
		loan_repayment.py:2450-2459 (credit of the loan account) post Receivable lines against a Loan.

		The report builds a row for a Loan only from its own lines, and a Loan has none: each line falls
		back to its own key. The disbursement of 1000 stays an obligation and the repayment of 400 is an
		unallocated payment (an adjustment), not a net 600.
		"""
		self.both_sides(self.add_loan)
		self.assertEqual(
			self.rows("Receivable", "voucher_type", "kind", "remaining"),
			[
				("Loan Disbursement", "obligation", "1000.00"),
				("Loan Repayment", "adjustment", "-400.00"),
			],
		)
		self.assertEqual(self.rows("Payable", "voucher_type"), [])

	def test_loan_of_an_employee_applicant_is_excluded_from_receivable(self):
		"""A loan whose applicant is an Employee (loan_disbursement.py:758): the Receivable report excludes it."""
		self.both_sides(self.add_loan_employee)
		for side in SIDES:
			self.assertEqual(self.rows(side, "voucher_type"), [])

	def test_loan_repayment_against_a_sales_invoice(self):
		"""lending loan_repayment.py:2335-2347 repays charges: a credit line on the invoice's own account
		(`debit_to`), against the Sales Invoice, with the applicant as party.
		"""
		self.both_sides(self.add_loan_repayment_on_invoice)
		self.assertEqual(self.rows("Receivable", "voucher_type", "remaining"), [("Sales Invoice", "60.00")])
		self.assertEqual(self.rows("Payable", "voucher_type"), [])

	def test_pos_invoice_with_its_own_payment_line(self):
		"""erpnext sales_invoice.py:2227-2262 (POS payment credit of `debit_to`, against the invoice itself) and
		purchase_invoice.py:1796-1822 (paid Purchase Invoice) post the payment as a negative line of the
		invoice against itself, in the invoice's own voucher.

		The report books that line as paid, not as a credit note (accounts_receivable.py:318-321).
		"""
		self.both_sides(self.add_pos_invoice)
		for side in SIDES:
			self.assertEqual(
				self.rows(side, "original", "paid", "credit", "remaining"),
				[("100.00", "60.00", "0.00", "40.00")],
			)

	def test_write_off_settles_the_rest_of_a_paid_invoice(self):
		"""erpnext sales_invoice.py:2327-2360 (write-off credit of `debit_to`) and purchase_invoice.py:1843-1870:
		a negative line of the invoice against itself.
		"""
		self.both_sides(self.add_write_off)
		for side in SIDES:
			# The invoice's terms fall in the window, so it shows as a settled row with nothing remaining.
			self.assertEqual(
				self.rows(side, "paid", "remaining", "settlement"), [("100.00", "0.00", "Settled")]
			)

	def test_partial_write_off_leaves_the_rest_open(self):
		self.both_sides(lambda side: self.add_write_off(side, paid=50))
		for side in SIDES:
			self.assertEqual(self.rows(side, "paid", "remaining"), [("55.00", "45.00")])

	def test_opening_invoice_and_opening_journal(self):
		"""An opening invoice (is_opening Yes) and an Opening Entry Journal Entry line against itself, partly paid.

		The ledger lines are those of any invoice or journal; the opening flag and the voucher type live on
		the header only (journal_entry.py:129-130).
		"""
		self.both_sides(self.add_opening)
		for side in SIDES:
			self.assertEqual(
				sorted(self.rows(side, "voucher_type", "remaining")),
				[("Journal Entry", "50.00"), (INVOICE_TYPES[side], "100.00")],
			)

	def test_revaluation_journals_count_with_a_zero_account_currency_residual(self):
		"""erpnext exchange_rate_revaluation.py:416 books a Journal Entry of voucher type Exchange Gain Or Loss
		with lines against the revaluation, and accounts_receivable.py:1368-1383 lists the Journal Entries of
		voucher type Exchange Rate Revaluation or Exchange Gain Or Loss (`err_journals`); a company residual with
		a zero account-currency residual counts only for those (build_data, :350-376).
		"""
		self.both_sides(lambda side: self.add_revaluation_journal(side, REVALUATION_TYPES[0]))
		for side in SIDES:
			self.assertEqual(
				self.rows(side, "voucher_type", "kind", "remaining"),
				[("Journal Entry", "adjustment", "3.00")],
			)

	def test_exchange_gain_or_loss_journal_counts_too(self):
		self.both_sides(lambda side: self.add_revaluation_journal(side, REVALUATION_TYPES[1]))
		for side in SIDES:
			self.assertEqual(self.rows(side, "voucher_type", "remaining"), [("Journal Entry", "3.00")])

	def test_plain_journal_with_a_zero_account_currency_residual_is_dropped(self):
		"""The control for the two tests above: the same lines in an ordinary Journal Entry give no row."""
		self.both_sides(lambda side: self.add_revaluation_journal(side, "Journal Entry"))
		for side in SIDES:
			self.assertEqual(self.rows(side, "voucher_type"), [])

	def test_exchange_gain_or_loss_journal_against_an_invoice(self):
		"""The journal of payment reconciliation (utils.py:2575-2580) references the invoice and carries the
		company-currency gain or loss only (amount in account currency 0): it zeroes the 0.01 residual of a
		foreign invoice that was paid in full in its own currency.
		"""
		for side in SIDES:
			with self.subTest(side):
				invoice = self.add_gain_loss_on_invoice(side)
				result = self.assert_reader_equals_today(side)
				self.assertEqual(
					[(row["voucher_no"], row["remaining"]) for row in result["rows"]],
					[(invoice, "0.00")],
				)
				self.assertFalse(result["excluded_rows"], "the journal books off the leftover")

	def test_line_against_an_unknown_document_type(self):
		"""A line against a DocType the report never heard of takes the generic path: its own key."""

		def scenario(side):
			self.invoice(side, (100,))
			self.add_unknown_type(side)

		self.both_sides(scenario)
		for side in SIDES:
			self.assertEqual(
				sorted(self.rows(side, "voucher_type", "remaining")),
				[("Payment Entry", "-30.00"), (INVOICE_TYPES[side], "100.00")],
			)

	def combined_builders(self):
		"""The shape name -> builder of every entry of COMBINED_SHAPES (a missing name fails the test)."""
		builders = {
			"employee_advance": self.add_employee_advance,
			"expense_claim": self.add_expense_claim,
			"expense_claim_paid": self.add_expense_claim_paid,
			"expense_claim_settled_by_advance": self.add_expense_claim_settled_by_advance,
			"expense_claim_foreign": self.add_expense_claim_foreign,
			"gratuity": lambda side: self.add_advance_paid_document(side, "Gratuity"),
			"leave_encashment": lambda side: self.add_advance_paid_document(side, "Leave Encashment"),
			"payroll_with_employees": lambda side: self.add_payroll(side, with_party=True),
			"payroll_without_party": lambda side: self.add_payroll(side, with_party=False),
			"loan": self.add_loan,
			"loan_employee": self.add_loan_employee,
			"loan_repayment_on_invoice": self.add_loan_repayment_on_invoice,
			"pos_invoice": self.add_pos_invoice,
			"write_off": self.add_write_off,
			"opening": self.add_opening,
			"unknown_type": self.add_unknown_type,
			"exchange_rate_revaluation": lambda side: self.add_revaluation_journal(
				side, REVALUATION_TYPES[0]
			),
			"exchange_gain_or_loss_only": lambda side: self.add_revaluation_journal(
				side, REVALUATION_TYPES[1]
			),
		}
		self.assertEqual(sorted(builders), sorted(COMBINED_SHAPES))
		return builders

	def test_every_voucher_type_in_one_company(self):
		"""All the shapes above in one ledger: the reader equals today's read on each side, and serves.

		A shape is built when its app is installed; one that is skipped while its app is installed (a DocType
		missing) fails the test, so the HR and lending shapes can never silently drop out.
		"""
		installed = set(frappe.get_installed_apps())
		expected = {name for name, app in COMBINED_SHAPES.items() if app is None or app in installed}
		builders = self.combined_builders()
		built = set()
		for name in sorted(expected):
			try:
				for side in SIDES:
					builders[name](side)
			except SkipTest as skipped:
				self.fail(f"shape {name} was skipped although its app is installed: {skipped}")
			built.add(name)
		self.assertEqual(built, expected)
		for app in ("hrms", "lending"):
			if app in installed:
				self.assertTrue(
					{name for name, owner in COMBINED_SHAPES.items() if owner == app} <= built,
					f"the {app} shapes are built while the app is installed",
				)
		for side in SIDES:
			with self.subTest(side):
				result = self.assert_reader_equals_today(side)
				self.assertTrue(result["rows"])


class TestRealControllerVoucherTypes(VoucherCase):
	"""Real Sales Invoices from ERPNext's own controller; headers and ledger come from ERPNext."""

	def real_company(self):
		"""The ledger company of the other real-document tests; the fixture company is swapped for it."""
		company = masters.ensure_ledger_company()
		masters.ensure_fiscal_years(nowdate())
		self.fx.company = company
		return company

	def service_item(self):
		code = "_J2A Upkeep Service"
		if frappe.db.exists("Item", code):
			return code
		masters.ensure_item_group_and_uom()
		values = {
			"doctype": "Item",
			"item_code": code,
			"item_group": masters.ITEM_GROUP,
			"stock_uom": masters.UOM,
			"is_stock_item": 0,
		}
		# india_compliance makes the HSN code mandatory on a site that has it installed.
		if frappe.get_meta("Item").has_field("gst_hsn_code"):
			values["gst_hsn_code"] = "01011010"
		return frappe.get_doc(values).insert(ignore_permissions=True).name

	def account(self, company, account_type=None, name=None):
		filters = {"company": company, "is_group": 0}
		if account_type:
			filters["account_type"] = account_type
		if name:
			filters["account_name"] = name
		return frappe.db.get_value("Account", filters, "name")

	def mode_of_payment(self, company):
		name = "_JPC Cash Mode"
		if not frappe.db.exists("Mode of Payment", name):
			frappe.get_doc(
				{
					"doctype": "Mode of Payment",
					"mode_of_payment": name,
					"type": "Cash",
					"accounts": [{"company": company, "default_account": self.account(company, "Cash")}],
				}
			).insert(ignore_permissions=True)
		return name

	def sales_invoice(self, company, *, consolidated=False, **extra):
		doc = {
			"doctype": "Sales Invoice",
			"company": company,
			"customer": masters.ensure_customer("_J2A Upkeep Customer"),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"due_date": add_days(nowdate(), 20),
			"currency": "INR",
			"conversion_rate": 1,
			"selling_price_list": masters.ensure_selling_price_list(),
			"price_list_currency": "INR",
			"plc_conversion_rate": 1,
			"items": [{"item_code": self.service_item(), "qty": 1, "rate": 100}],
			**extra,
		}
		invoice = frappe.get_doc(doc)
		if consolidated:
			# What the POS Invoice Merge Log sets on the Sales Invoice it posts (pos_invoice_merge_log.py:174-175).
			invoice.is_consolidated = 1
			invoice.flags.is_consolidated = True
		invoice.insert(ignore_permissions=True)
		invoice.submit()
		return invoice

	def cash_payment(self, company, amount):
		return {"mode_of_payment": self.mode_of_payment(company), "amount": amount}

	def journal_entry(self, company, voucher_type, rows):
		doc = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": company,
				"voucher_type": voucher_type,
				"posting_date": nowdate(),
				"cost_center": frappe.db.get_value("Company", company, "cost_center"),
				"accounts": rows,
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def receivable_row(self, invoice, side_amount, **extra):
		"""A Journal Entry row on the invoice's own receivable account for its customer, credit `side_amount`."""
		return {
			"account": frappe.db.get_value("Sales Invoice", invoice.name, "debit_to"),
			"party_type": "Customer",
			"party": invoice.customer,
			"credit_in_account_currency": side_amount,
			**extra,
		}

	def test_pos_invoice_with_a_payment_and_a_write_off(self):
		"""A real POS Sales Invoice: grand total 100, paid 60 in cash, 5 written off, 35 remains open.

		ERPNext posts the payment (sales_invoice.py:2224-2262) and the write-off (:2326) itself, so the
		ledger shape here is the real one and not an assumption.
		"""
		company = self.real_company()
		invoice = self.sales_invoice(
			company,
			is_pos=1,
			payments=[{"mode_of_payment": self.mode_of_payment(company), "amount": 60}],
			write_off_amount=5,
			write_off_account=self.account(company, name="Write Off"),
			write_off_cost_center=frappe.db.get_value("Company", company, "cost_center"),
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"), 35)
		result = self.assert_reader_equals_today("Receivable")
		self.assertEqual(
			[(row["voucher_no"], row["paid"], row["remaining"]) for row in result["rows"]],
			[(invoice.name, "65.00", "35.00")],
		)

	def pos_return(self, own_outstanding):
		"""A real POS Sales Invoice paid in full and its real return (make_return_doc) with its refund line."""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		company = self.real_company()
		invoice = self.sales_invoice(company, is_pos=1, payments=[self.cash_payment(company, 100)])
		refund = make_return_doc("Sales Invoice", invoice.name)
		refund.update_outstanding_for_self = own_outstanding
		refund.insert(ignore_permissions=True)
		refund.submit()
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"), 0)
		window = (add_days(nowdate(), -5), add_days(nowdate(), 40))
		return invoice, self.assert_reader_equals_today("Receivable", window)

	def test_pos_return_refunding_against_the_original(self):
		"""A POS return that does not update its own outstanding: its refund payment line is against the original
		invoice (`against_voucher` is the return_against, sales_invoice.py:2227-2262). The refund and the
		credit net the original to nothing.
		"""
		invoice, result = self.pos_return(own_outstanding=0)
		self.assertEqual(
			[(row["voucher_no"], row["remaining"]) for row in result["rows"]], [(invoice.name, "0.00")]
		)

	def test_pos_return_with_its_own_outstanding(self):
		"""A POS return that updates its own outstanding: the refund payment line is against the return itself."""
		_, result = self.pos_return(own_outstanding=1)
		self.assertEqual(
			sorted((row["original"], row["paid"], row["credit"], row["remaining"]) for row in result["rows"]),
			[("100.00", "0.00", "100.00", "0.00"), ("100.00", "100.00", "0.00", "0.00")],
		)

	def test_consolidated_pos_invoice(self):
		"""The Sales Invoice a POS Invoice Merge Log posts (is_consolidated, is_pos, payments): the same ledger
		shape as a POS invoice, its payment a negative line against the invoice itself.
		"""
		company = self.real_company()
		# The merge log copies the amounts of the POS invoices into the items and skips the item arithmetic
		# (pos_invoice_merge_log.py:253-258; taxes_and_totals.py:260).
		priced = {
			field: 100
			for field in (
				"rate",
				"amount",
				"net_rate",
				"net_amount",
				"base_rate",
				"base_amount",
				"base_net_rate",
				"base_net_amount",
			)
		}
		invoice = self.sales_invoice(
			company,
			is_pos=1,
			consolidated=True,
			items=[{"item_code": self.service_item(), "qty": 1, **priced}],
			payments=[self.cash_payment(company, 70)],
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"), 30)
		result = self.assert_reader_equals_today("Receivable")
		self.assertEqual(
			[(row["voucher_no"], row["paid"], row["remaining"]) for row in result["rows"]],
			[(invoice.name, "70.00", "30.00")],
		)

	def test_write_off_entry_journal_against_an_invoice(self):
		"""A real Journal Entry of voucher type Write Off Entry that clears 20 of an open invoice: its receivable
		row references the Sales Invoice (journal_entry.py:1210-1211).
		"""
		company = self.real_company()
		invoice = self.sales_invoice(company)
		self.journal_entry(
			company,
			"Write Off Entry",
			[
				{"account": self.account(company, name="Write Off"), "debit_in_account_currency": 20},
				self.receivable_row(invoice, 20, reference_type="Sales Invoice", reference_name=invoice.name),
			],
		)
		result = self.assert_reader_equals_today("Receivable")
		self.assertEqual(
			[(row["voucher_no"], row["remaining"]) for row in result["rows"]], [(invoice.name, "80.00")]
		)

	def test_opening_entry_journal(self):
		"""A real Journal Entry of voucher type Opening Entry (is_opening Yes, journal_entry.py:129-130): a
		receivable debit of 70 for a customer against the entry itself, balanced on the temporary opening account.
		"""
		company = self.real_company()
		customer = masters.ensure_customer("_J2A Upkeep Customer")
		journal = self.journal_entry(
			company,
			"Opening Entry",
			[
				{
					"account": self.account(company, "Receivable"),
					"party_type": "Customer",
					"party": customer,
					"debit_in_account_currency": 70,
				},
				{
					"account": self.account(company, name="Temporary Opening"),
					"credit_in_account_currency": 70,
				},
			],
		)
		self.assertEqual(journal.is_opening, "Yes")
		result = self.assert_reader_equals_today("Receivable")
		self.assertEqual(
			[(row["voucher_no"], row["remaining"]) for row in result["rows"]], [(journal.name, "70.00")]
		)
