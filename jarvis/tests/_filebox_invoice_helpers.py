"""Minimal native Purchase Invoice fixture; only for disposable test sites."""

from uuid import uuid4

import frappe


class InvoiceFixture:
	def __init__(self):
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		self.created = []
		self.prefix = "zz-j768 " + uuid4().hex[:8]
		self.party_type = frappe.db.get_value("Party Type", "Supplier", "account_type")

	def insert(self, doctype, **values):
		doc = frappe.get_doc({"doctype": doctype, **values})
		if doctype == "Cost Center" and values.get("cost_center_name") == values.get("company"):
			doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		self.created.append((doctype, doc.name))
		return doc

	def setup(self):
		if frappe.db.exists("Party Type", "Supplier"):
			frappe.db.set_value("Party Type", "Supplier", "account_type", "Payable")
		else:
			self.insert("Party Type", party_type="Supplier", account_type="Payable")
		frappe.local.party_account_types = {}
		self.company = self.prefix + " Company"
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": self.company,
				"abbr": "Q" + uuid4().hex[:6],
				"default_currency": "USD",
				"country": "United States",
			}
		)
		company.name = self.company
		# Avoid provisioning unrelated warehouses/defaults; invoice controllers run normally.
		company.db_insert()
		self.created.append(("Company", company.name))

		def account(label, root=None, **values):
			doc = frappe.get_doc(
				{"doctype": "Account", "account_name": self.prefix + label, "company": self.company, **values}
			)
			if root:
				doc.root_type = root
				doc.flags.ignore_mandatory = True
			doc.insert(ignore_permissions=True)
			self.created.append(("Account", doc.name))
			return doc.name

		expenses = account(" Expenses", "Expense", is_group=1)
		liabilities = account(" Liabilities", "Liability", is_group=1)
		self.expense = account(" Expense", parent_account=expenses, account_type="Expense Account")
		self.payable = account(" Payable", parent_account=liabilities, account_type="Payable")
		root = self.insert("Cost Center", cost_center_name=self.company, company=self.company, is_group=1)
		self.cost_center = self.insert(
			"Cost Center", cost_center_name=self.prefix, company=self.company, parent_cost_center=root.name
		).name
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"default_expense_account": self.expense,
				"default_payable_account": self.payable,
				"cost_center": self.cost_center,
			},
		)
		group = self.insert("Supplier Group", supplier_group_name=self.prefix, is_group=1)
		self.supplier = self.insert(
			"Supplier",
			supplier_name=self.prefix,
			supplier_group=group.name,
			supplier_type="Company",
			country="United States",
			accounts=[{"company": self.company, "account": self.payable}],
		).name
		uom = self.insert("UOM", uom_name=self.prefix, must_be_whole_number=1)
		group = self.insert("Item Group", item_group_name=self.prefix, is_group=1)
		self.item = self.insert(
			"Item",
			item_code=self.prefix,
			item_group=group.name,
			stock_uom=uom.name,
			is_stock_item=0,
			is_purchase_item=1,
			is_sales_item=0,
			item_defaults=[
				{
					"company": self.company,
					"expense_account": self.expense,
					"buying_cost_center": self.cost_center,
				}
			],
		).name
		from erpnext.accounts.utils import get_fiscal_year

		today = frappe.utils.today()
		try:
			get_fiscal_year(today, company=self.company)
		except frappe.ValidationError:
			year = frappe.utils.getdate(today).year
			self.insert(
				"Fiscal Year",
				year=self.prefix,
				year_start_date=f"{year}-01-01",
				year_end_date=f"{year}-12-31",
				companies=[{"company": self.company}],
			)
		self.values = {
			"company": self.company,
			"supplier": self.supplier,
			"currency": "USD",
			"conversion_rate": 1,
			"posting_date": today,
			"due_date": today,
			"credit_to": self.payable,
			"update_stock": 0,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": self.item,
					"qty": 1,
					"rate": 100,
					"expense_account": self.expense,
					"cost_center": self.cost_center,
					"uom": uom.name,
					"stock_uom": uom.name,
					"conversion_factor": 1,
				}
			],
		}
		frappe.db.commit()
		return self

	def cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all(
			"Purchase Invoice", filters={"company": getattr(self, "company", "")}, pluck="name"
		):
			frappe.delete_doc("Purchase Invoice", name, ignore_permissions=True, force=True)
		for doctype, name in reversed(self.created):
			# Fixtures may have committed through the card CAS; remove owned children too.
			for field in frappe.get_meta(doctype).get_table_fields():
				frappe.db.delete(field.options, {"parent": name, "parenttype": doctype})
			frappe.db.delete(doctype, {"name": name})
			frappe.clear_document_cache(doctype, name)
		if self.party_type is not None:
			frappe.db.set_value("Party Type", "Supplier", "account_type", self.party_type)
		frappe.local.party_account_types = {}
		frappe.db.commit()
