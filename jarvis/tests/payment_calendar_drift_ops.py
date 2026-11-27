"""The 15 real-controller operations of the drift generator (port of the T8 spike generator).

Every operation saves, submits or cancels real ERPNext documents. `Skip` means the current state has no
candidate for the operation. Documents get deterministic names from the seed, so a run is repeatable.
"""

from __future__ import annotations

from collections import Counter

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate

from jarvis.tests import _erpnext_masters as masters

COMPANY = masters.COMPANY
ABBR = masters.ABBR
DEBTORS, CREDITORS, CASH = f"Debtors - {ABBR}", f"Creditors - {ABBR}", f"Cash - {ABBR}"
INCOME, EXPENSE, COST_CENTER = f"Sales - {ABBR}", f"Cost of Goods Sold - {ABBR}", f"Main - {ABBR}"
SI, PI, JE, PE = "Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry"
ITEM = "PCALZ-ITEM"
N_CUSTOMERS, N_SUPPLIERS = 8, 5
TEMPLATE_2X, TEMPLATE_3X = "_PCALZ 2x", "_PCALZ 3x"


class Skip(Exception):
	"""No candidate for this operation in the current state."""


def customer_name(index: int) -> str:
	return f"PCALZ-C{index:02d}"


def supplier_name(index: int) -> str:
	return f"PCALZ-S{index:02d}"


class DriftOperations:
	"""Mixin: needs `self.rng`, `self.anchor`, `self.seed`, `self.counts` (Counter), `self.big_payment`,
	`self.last_advance` (side, party of the latest unallocated payment, or None) and `self.forcing`
	(the kind the generator forces right now, or None).
	"""

	def name(self, prefix):
		self.counts[prefix] += 1
		return f"PCALZ-{prefix}-{self.seed}-{self.counts[prefix]:04d}"

	def party(self, side):
		rng = self.rng
		if side == "c":
			return customer_name(0 if rng.random() < 0.6 else rng.randint(1, N_CUSTOMERS - 1))
		return supplier_name(0 if rng.random() < 0.6 else rng.randint(1, N_SUPPLIERS - 1))

	def pick_side(self):
		return "c" if self.rng.random() < 0.6 else "s"

	def doc_date(self):
		rng, anchor = self.rng, self.anchor
		x = rng.random()
		if x < 0.65:
			return getdate(add_days(anchor, -rng.randint(0, 90)))
		if x < 0.80:
			return getdate(add_days(anchor, -rng.randint(91, 365)))
		if x < 0.92:
			return getdate(add_days(anchor, -rng.randint(366, 590)))
		return getdate(add_days(anchor, rng.randint(1, 25)))

	def pay_date(self):
		rng, anchor = self.rng, self.anchor
		x = rng.random()
		if x < 0.70:
			return getdate(add_days(anchor, -rng.randint(0, 30)))
		if x < 0.87:
			return getdate(add_days(anchor, -rng.randint(31, 500)))
		return getdate(add_days(anchor, rng.randint(1, 25)))

	def window_start(self):
		"""First day of the calendar window the drift test reads (12 months back): older due dates are outside it."""
		return get_first_day(add_months(self.anchor, -12))

	def open_invoices(self, doctype, party=None, min_n=1):
		party_field = "customer" if doctype == SI else "supplier"
		filters = {
			"company": COMPANY,
			"docstatus": 1,
			"is_return": 0,
			"outstanding_amount": [">", 0],
		}
		if party:
			filters[party_field] = party
		rows = frappe.get_all(
			doctype, filters=filters, fields=["name", "outstanding_amount"], order_by="name asc"
		)
		if len(rows) < min_n:
			raise Skip
		return rows

	def submitted_names(self, doctype, **extra):
		filters = {"company": COMPANY, "docstatus": 1, **extra}
		return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc")

	# ---------------------------------------------------------------- invoices
	def op_si(self):
		return self._invoice(SI)

	def op_pi(self):
		return self._invoice(PI)

	def _invoice(self, doctype):
		rng = self.rng
		posting = self.doc_date()
		is_sales = doctype == SI
		party = self.party("c" if is_sales else "s")
		x = rng.random()
		template = TEMPLATE_2X if x < 0.35 else TEMPLATE_3X if x < 0.5 else None
		account_field = "income_account" if is_sales else "expense_account"
		doc = {
			"doctype": doctype,
			"company": COMPANY,
			"posting_date": posting,
			"set_posting_time": 1,
			"currency": "INR",
			"conversion_rate": 1,
			"update_stock": 0,
			"price_list_currency": "INR",
			"plc_conversion_rate": 1,
			"items": [
				{
					"item_code": ITEM,
					"qty": rng.randint(1, 5),
					"rate": rng.randint(1000, 500000) / 100,
					"cost_center": COST_CENTER,
					account_field: INCOME if is_sales else EXPENSE,
				}
			],
		}
		if is_sales:
			doc.update(customer=party, debit_to=DEBTORS, selling_price_list=masters.SELLING_PRICE_LIST)
		else:
			doc.update(
				supplier=party,
				credit_to=CREDITORS,
				buying_price_list="_PCALZ Buying",
				bill_no=f"B{rng.randint(1, 10**6)}",
				bill_date=posting,
			)
		if template:
			doc["payment_terms_template"] = template
		else:
			doc["due_date"] = add_days(posting, rng.randint(0, 60))
		self._save_and_submit(doc, self.name("SI" if is_sales else "PI"))

	def _save_and_submit(self, doc, name):
		document = frappe.get_doc(doc).insert(ignore_permissions=True, set_name=name)
		document.submit()
		return document

	# ---------------------------------------------------------------- payments
	def op_pe_single(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		rng = self.rng
		doctype = SI if self.pick_side() == "c" else PI
		invoice = rng.choice(self.open_invoices(doctype, self.party("c" if doctype == SI else "s")))
		amount = None
		if rng.random() < 0.5:
			amount = round(flt(invoice.outstanding_amount) * rng.uniform(0.2, 0.8), 2)
		payment = get_payment_entry(doctype, invoice.name, party_amount=amount, bank_account=CASH)
		posting = self.pay_date()
		payment.posting_date, payment.reference_no, payment.reference_date = posting, "T8", posting
		payment.insert(ignore_permissions=True, set_name=self.name("PE"))
		payment.submit()

	def _payment(self, doctype, party, references=(), amount=0.0):
		"""A submitted Payment Entry for `party`; `references` are (name, term, amount) rows."""
		receive = doctype == SI
		posting = self.pay_date()
		payment = frappe.new_doc(PE)
		payment.update(
			{
				"payment_type": "Receive" if receive else "Pay",
				"company": COMPANY,
				"posting_date": posting,
				"party_type": "Customer" if receive else "Supplier",
				"party": party,
				"paid_from": DEBTORS if receive else CASH,
				"paid_to": CASH if receive else CREDITORS,
				"paid_from_account_currency": "INR",
				"paid_to_account_currency": "INR",
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": "T8",
				"reference_date": posting,
			}
		)
		for name, term, allocated in references:
			payment.append(
				"references",
				{
					"reference_doctype": doctype,
					"reference_name": name,
					"allocated_amount": allocated,
					"payment_term": term,
				},
			)
		amount = round(amount, 2)
		payment.paid_amount = payment.received_amount = amount
		payment.base_paid_amount = payment.base_received_amount = amount
		payment.insert(ignore_permissions=True, set_name=self.name("PE"))
		payment.submit()
		return payment

	def _term_rows(self, doctype, invoice):
		"""One (name, term, outstanding) row per open term: ERPNext needs one reference per term."""
		terms = frappe.db.sql(
			"""select ps.payment_term, ps.outstanding from `tabPayment Schedule` ps
			join `tabPayment Terms Template` t on t.name = %s
			where ps.parent = %s and ps.parenttype = %s and ps.outstanding > 0
			and t.allocate_payment_based_on_payment_terms = 1 order by ps.idx""",
			(
				frappe.db.get_value(doctype, invoice.name, "payment_terms_template") or "",
				invoice.name,
				doctype,
			),
		)
		rows = [(term, flt(outstanding)) for term, outstanding in terms]
		return [
			(invoice.name, term, amount) for term, amount in rows or [(None, flt(invoice.outstanding_amount))]
		]

	def _multi_payment(self, doctype, party, invoices, extra=0.0):
		references = []
		for invoice in invoices:
			references += self._term_rows(doctype, invoice)
		if self.rng.random() < 0.4:
			name, term, amount = references[-1]
			references[-1] = (name, term, round(amount * self.rng.uniform(0.2, 0.9), 2))
		total = sum(amount for _, _, amount in references) + extra
		return self._payment(doctype, party, references, total)

	def op_pe_multi(self):
		doctype = SI if self.pick_side() == "c" else PI
		party = self.party("c" if doctype == SI else "s")
		rows = self.open_invoices(doctype, party, min_n=2)
		chosen = self.rng.sample(rows, min(len(rows), self.rng.randint(2, 8)))
		extra = round(self.rng.uniform(10, 500), 2) if self.rng.random() < 0.25 else 0.0
		self._multi_payment(doctype, party, chosen, extra)

	def op_pe_full(self):
		"""Pay one open invoice in full (forced tail only): the paid invoice that `return_paid` then returns."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		doctype = SI if self.pick_side() == "c" else PI
		rows = self.open_invoices(doctype)
		# Prefer an invoice due before the window: once paid it is outside the reader's discovery.
		old = [row for row in rows if self.is_old(doctype, row.name)]
		invoice = self.rng.choice(old or rows)
		payment = get_payment_entry(doctype, invoice.name, bank_account=CASH)
		posting = self.pay_date()
		payment.posting_date, payment.reference_no, payment.reference_date = posting, "T8", posting
		payment.insert(ignore_permissions=True, set_name=self.name("PE"))
		payment.submit()

	def op_pe_big(self):
		"""One payment over many invoices (the spike used 20+; scaled down for the CI size)."""
		for doctype, party in ((SI, customer_name(0)), (PI, supplier_name(0))):
			try:
				rows = self.open_invoices(doctype, party, min_n=8)
			except Skip:
				continue
			chosen = self.rng.sample(rows, min(len(rows), 12))
			self.big_payment = self._multi_payment(doctype, party, chosen).name
			return
		raise Skip

	def op_pe_advance(self):
		side = self.pick_side()
		doctype = SI if side == "c" else PI
		party = self.party(side)
		self._payment(doctype, party, amount=self.rng.randint(5000, 2000000) / 100)
		self.last_advance = (side, party)

	def op_je_multi(self):
		rng = self.rng
		doctype = SI if self.pick_side() == "c" else PI
		party = self.party("c" if doctype == SI else "s")
		rows = self.open_invoices(doctype, party)
		chosen = rng.sample(rows, min(len(rows), rng.randint(2, 4)))
		accounts, total = [], 0.0
		for invoice in chosen:
			outstanding = flt(invoice.outstanding_amount)
			amount = outstanding if rng.random() < 0.6 else round(outstanding * rng.uniform(0.2, 0.9), 2)
			total += amount
			row = {
				"account": DEBTORS if doctype == SI else CREDITORS,
				"party_type": "Customer" if doctype == SI else "Supplier",
				"party": party,
				"reference_type": doctype,
				"reference_name": invoice.name,
				"cost_center": COST_CENTER,
			}
			row["credit_in_account_currency" if doctype == SI else "debit_in_account_currency"] = amount
			accounts.append(row)
		cash = {"account": CASH, "cost_center": COST_CENTER}
		cash["debit_in_account_currency" if doctype == SI else "credit_in_account_currency"] = round(total, 2)
		accounts.append(cash)
		self._save_and_submit(
			{
				"doctype": JE,
				"voucher_type": "Journal Entry",
				"company": COMPANY,
				"posting_date": self.pay_date(),
				"user_remark": "T8",
				"accounts": [*accounts],
			},
			self.name("JE"),
		)

	# ---------------------------------------------------------------- reconciliation
	def op_reconcile(self):
		"""Reconcile one party. When forced, try the party of the latest advance first, then every other party."""
		side = self.pick_side()
		candidates = [(side, self.party(side))]
		if self.forcing == "reconcile":
			everyone = [("c", customer_name(i)) for i in range(N_CUSTOMERS)]
			everyone += [("s", supplier_name(i)) for i in range(N_SUPPLIERS)]
			candidates = [self.last_advance, *everyone] if self.last_advance else everyone
		for candidate_side, party in candidates:
			try:
				return self._reconcile(candidate_side, party)
			except Skip:
				continue
		raise Skip

	def _reconcile(self, side, party):
		rng = self.rng
		reconciliation = frappe.new_doc("Payment Reconciliation")
		reconciliation.company = COMPANY
		reconciliation.party_type = "Customer" if side == "c" else "Supplier"
		reconciliation.party = party
		reconciliation.receivable_payable_account = DEBTORS if side == "c" else CREDITORS
		reconciliation.get_unreconciled_entries()
		if not reconciliation.get("payments") or not reconciliation.get("invoices"):
			raise Skip
		payments = rng.sample(
			list(reconciliation.payments), min(len(reconciliation.payments), rng.randint(1, 2))
		)
		invoices = rng.sample(
			list(reconciliation.invoices), min(len(reconciliation.invoices), rng.randint(1, 5))
		)
		reconciliation.allocate_entries(
			frappe._dict(
				invoices=[row.as_dict() for row in invoices], payments=[row.as_dict() for row in payments]
			)
		)
		if not reconciliation.get("allocation"):
			raise Skip
		reconciliation.reconcile()

	def op_unreconcile(self):
		rows = frappe.db.sql(
			"""select distinct ref.parent from `tabPayment Entry Reference` ref
			join `tabPayment Entry` pe on pe.name = ref.parent
			where pe.company = %s and pe.docstatus = 1 and ref.allocated_amount > 0
			and ref.reference_doctype in ('Sales Invoice', 'Purchase Invoice') order by ref.parent""",
			COMPANY,
		)
		if not rows:
			raise Skip
		unreconcile = frappe.new_doc("Unreconcile Payment")
		unreconcile.company, unreconcile.voucher_type = COMPANY, PE
		unreconcile.voucher_no = self.rng.choice(rows)[0]
		unreconcile.add_references()
		if not unreconcile.allocations:
			raise Skip
		if self.rng.random() < 0.5:
			unreconcile.allocations = [self.rng.choice(unreconcile.allocations)]
		unreconcile.save(ignore_permissions=True)
		unreconcile.submit()

	# ---------------------------------------------------------------- returns
	def _return(self, doctype, paid_only=False, old_paid=False):
		"""Submit a return of a random invoice and give it back."""
		rng = self.rng
		extra = {"outstanding_amount": 0} if paid_only or old_paid else {}
		names = self.submitted_names(doctype, is_return=0, **extra)
		if old_paid:
			# A refund needs a paid invoice that has no return yet and is due before the window, so that
			# the original is outside the reader's discovery while its return (dated late) is inside.
			returned_already = set(
				frappe.get_all(
					doctype,
					filters={"company": COMPANY, "docstatus": 1, "is_return": 1},
					pluck="return_against",
				)
			)
			names = [name for name in names if name not in returned_already and self.is_old(doctype, name)]
		if not names:
			raise Skip
		return self._submit_return(doctype, rng.choice(names), late=old_paid)

	def is_old(self, doctype, name):
		"""True when every due date of the invoice is before the window (its header due date is the latest)."""
		return getdate(frappe.db.get_value(doctype, name, "due_date")) < self.window_start()

	def _submit_return(self, doctype, source, *, late=False, own_outstanding=None, one_unit=False):
		"""Return of `source`. `late` dates it within 60 days of the anchor. `own_outstanding` fixes
		`update_outstanding_for_self` (ERPNext still forces 1 when the return exceeds the outstanding).
		"""
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import make_debit_note
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		rng = self.rng
		returned = (make_sales_return if doctype == SI else make_debit_note)(source)
		for item in returned.items:
			if one_unit:
				item.qty = -1
			else:
				item.qty = -min(abs(item.qty), rng.randint(1, max(1, int(abs(item.qty)))))
		returned.update_outstanding_for_self = (
			rng.choice([0, 1]) if own_outstanding is None else own_outstanding
		)
		source_date = getdate(frappe.db.get_value(doctype, source, "posting_date"))
		if late:
			returned.posting_date = max(getdate(add_days(self.anchor, -rng.randint(0, 60))), source_date)
		else:
			returned.posting_date = max(self.doc_date(), source_date)
		returned.set_posting_time = 1
		if doctype == PI:
			returned.bill_no, returned.bill_date = f"DN{rng.randint(1, 10**6)}", returned.posting_date
		returned.payment_schedule = []
		if late:
			# The copy keeps the original's old due date, which would hide the return from the window.
			returned.due_date = returned.posting_date
		returned.insert(ignore_permissions=True, set_name=self.name("CN" if doctype == SI else "DN"))
		returned.submit()
		return returned

	def _pay(self, doctype, name, posting):
		"""A Payment Entry for the whole outstanding of `name` (an invoice, or a return that has its own)."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		payment = get_payment_entry(doctype, name, bank_account=CASH)
		payment.posting_date = payment.reference_date = posting
		payment.reference_no = "T8"
		payment.insert(ignore_permissions=True, set_name=self.name("PE"))
		payment.submit()
		return payment

	def op_credit_note(self):
		self._return(SI)

	def op_debit_note(self):
		self._return(PI)

	def op_refund(self):
		"""A late return of an old, fully paid invoice, then a payment that refunds the return.

		ERPNext gives such a return its own outstanding, so the refund lines are against the return.
		"""
		doctype = SI if self.pick_side() == "c" else PI
		returned = self._return(doctype, old_paid=True)
		self._pay(doctype, returned.name, returned.posting_date)

	def op_fold_return(self):
		"""A late one-unit return of an old open invoice that folds onto it, then payment of the rest.

		The return does not update its own outstanding: its ledger lines sit on the original's key. The
		original ends closed, outside the window, while the return is due inside it: the shape where the
		reader needs the return closure.
		"""
		doctype = SI if self.pick_side() == "c" else PI
		rows = [row for row in self.open_invoices(doctype) if self.is_old(doctype, row.name)]
		if not rows:
			raise Skip
		source = self.rng.choice(rows).name
		returned = self._submit_return(doctype, source, late=True, own_outstanding=0, one_unit=True)
		if returned.update_outstanding_for_self:
			raise Skip
		self._pay(doctype, source, self.pay_date())

	def op_return_paid(self):
		self._return(SI if self.pick_side() == "c" else PI, paid_only=True)

	# ---------------------------------------------------------------- cancellations
	def _cancel(self, doctype, name, unlink):
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", unlink)
		frappe.clear_document_cache("Accounts Settings")
		document = frappe.get_doc(doctype, name)
		document.cancel()
		return document

	def op_cancel(self):
		doctype = self.rng.choices([SI, PI, PE, JE], weights=[3, 2, 4, 1])[0]
		names = self.submitted_names(doctype)
		if not names:
			raise Skip
		self._cancel(doctype, self.rng.choice(names), self.rng.choice([0, 1]))

	def op_cancel_multiref(self):
		rows = frappe.db.sql(
			"""select ref.parent, ref.reference_doctype, ref.reference_name
			from `tabPayment Entry Reference` ref join `tabPayment Entry` pe on pe.name = ref.parent
			where pe.company = %s and pe.docstatus = 1 and ref.allocated_amount > 0 and ref.parent in (
				select parent from `tabPayment Entry Reference` group by parent having count(*) >= 2)
			order by ref.parent, ref.idx""",
			COMPANY,
		)
		big = [row for row in rows if row[0] == self.big_payment]
		rows = big or rows
		if not rows:
			raise Skip
		payment, doctype, reference = self.rng.choice(rows)
		if self.rng.random() < 0.3:
			self._cancel(PE, payment, 1)
		else:
			self._cancel(doctype, reference, 1)

	def op_cancel_amend(self):
		rng = self.rng
		doctype = SI if self.pick_side() == "c" else PI
		names = self.submitted_names(doctype, is_return=0)
		if not names:
			raise Skip
		old = self._cancel(doctype, rng.choice(names), 1)
		new = frappe.copy_doc(old)
		new.amended_from, new.docstatus = old.name, 0
		new.payment_schedule = []
		new.set("advances", [])
		if rng.random() < 0.5:
			self._change_party(new, old, doctype)
		if not new.payment_terms_template:
			new.due_date = max(getdate(new.due_date), getdate(new.posting_date))
		new.insert(ignore_permissions=True)
		new.submit()

	def _change_party(self, new, old, doctype):
		is_sales = doctype == SI
		field = "customer" if is_sales else "supplier"
		names = (
			[customer_name(i) for i in range(N_CUSTOMERS)]
			if is_sales
			else [supplier_name(i) for i in range(N_SUPPLIERS)]
		)
		new.set(field, self.rng.choice([n for n in names if n != old.get(field)]))
		new.set(
			f"{field}_name",
			frappe.db.get_value("Customer" if is_sales else "Supplier", new.get(field), f"{field}_name"),
		)


OPERATIONS = Counter(
	{
		"si": 13,
		"pi": 9,
		"pe_single": 10,
		"pe_multi": 6,
		"pe_advance": 4,
		"reconcile": 5,
		"unreconcile": 4,
		"je_multi": 4,
		"credit_note": 4,
		"debit_note": 3,
		"return_paid": 2,
		"cancel": 7,
		"cancel_multiref": 2,
		"cancel_amend": 4,
	}
)
WARMUP_OPERATIONS = {"si": 8, "pi": 3, "pe_single": 1}
