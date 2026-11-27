"""Users, User Permissions and documents that reach each branch of the batch permission check.

Everything is a raw insert inside the test transaction (nothing is committed). The class-level
fixture is built once per test class; the caller rolls it back to the savepoint.
"""

import time
from contextlib import ExitStack, contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import batch_permission, read_log
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.payment_calendar_majors import IS_V15
from jarvis.tests.test_batch_permission_pins import clean_bindings

SWITCH = batch_permission.SWITCH
SI, PI, PE, JE = "Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"
HOOK_STATE: dict = {}
# A busy shared site ends a build with a lock wait: a few short retries, then the error is raised.
BUILD_ATTEMPTS = 4
# The child doctypes that ERPNext 15 does not have. `row()` inserts nothing for them there; any other name still fails.
ABSENT_ON_V15 = frozenset(
	{
		"Tax Withholding Entry",
		"Company Restriction",
		"Customer Number At Supplier",
		"Supplier Number At Customer",
	}
)


@contextmanager
def switch_on():
	with patch.dict(frappe.local.conf, {SWITCH: 1}):
		yield


@contextmanager
def strict(on: bool):
	frappe.get_system_settings("apply_strict_user_permissions")
	with patch.dict(frappe.local.system_settings.__dict__, {"apply_strict_user_permissions": int(on)}):
		yield


@contextmanager
def meta_patched(doctype: str, **attributes):
	"""Patch attributes of the cached meta of `doctype` and keep that object in place for the block.

	Another process that clears the meta cache would otherwise swap the patched object for a fresh one.
	"""
	real_get_meta = frappe.get_meta
	meta = real_get_meta(doctype)

	def get_meta(name, *args, **kwargs):
		return meta if name == doctype else real_get_meta(name, *args, **kwargs)

	with ExitStack() as stack:
		for name, value in attributes.items():
			stack.enter_context(patch.object(meta, name, value))
		stack.enter_context(patch.object(frappe, "get_meta", get_meta))
		yield meta


@contextmanager
def role_rights(doctype: str, *rows: dict):
	"""Add role permission rows to the meta of `doctype` for one block."""
	extra = [frappe._dict(permlevel=0, **row) for row in rows]
	frappe.local.role_permissions = {}
	with meta_patched(doctype, permissions=[*frappe.get_meta(doctype).permissions, *extra]):
		try:
			yield
		finally:
			frappe.local.role_permissions = {}


@contextmanager
def meta_with_field(doctype: str, **field):
	"""The meta of `doctype` with one more field for one block."""
	with meta_patched(doctype, fields=[*frappe.get_meta(doctype).fields, frappe._dict(field)]):
		yield


@contextmanager
def hooks(extra: dict[str, list[str]]):
	"""Register more `has_permission` hooks (keyed by doctype or `*`) on top of the real ones."""
	real = frappe.get_hooks

	def fake(hook=None, default=None, app_name=None):
		out = real(hook, default=default, app_name=app_name)
		if hook != "has_permission":
			return out
		merged = frappe._dict({key: list(value) for key, value in out.items()})
		for key, methods in extra.items():
			merged[key] = merged.get(key, []) + methods
		return merged

	with patch.object(frappe, "get_hooks", fake):
		yield


def clear_plan_memo():
	setattr(frappe.local, batch_permission.PLAN_MEMO, None)


class BatchFixtures:
	def __init__(self):
		self.fx = CalendarFixtures()
		fx = self.fx
		self.a = fx.company
		self.b = fx.insert(
			"Company",
			"CompanyB",
			company_name=fx.prefix + "B",
			abbr=fx.prefix[-4:] + "B",
			default_currency="INR",
			country="India",
		)
		self.a1 = fx.insert(
			"Company",
			"CompanyA1",
			company_name=fx.prefix + "A1",
			abbr=fx.prefix[-4:] + "1",
			default_currency="INR",
			country="India",
			parent_company=self.a,
		)
		# a nested-set range that no real company uses, so A1 is the only descendant of A
		frappe.db.set_value("Company", self.a, {"lft": 910001, "rgt": 910004}, update_modified=False)
		frappe.db.set_value("Company", self.a1, {"lft": 910002, "rgt": 910003}, update_modified=False)
		self.if_owner_role = self.role("IfOwner")
		self.select_role = self.role("SelectOnly")
		self.u1 = self.user_with_company_up(self.a)
		self.u2 = self.user_with_company_up(self.a, hide_descendants=1)
		self.u3 = fx.user()
		self.u4 = self.user_with_company_up(self.a, role=self.if_owner_role)
		self.u5 = self.user_with_company_up(self.a, role=self.select_role)
		self.u6 = self.user_with_company_up(self.a, applicable_for=SI)
		self.u7 = self.user_with_company_up(self.a)
		self.up(self.u7, "Customer", fx.customer)
		self.u0 = self.user_without_roles()
		self.si_names: list[str] = []
		self.build_invoices()
		self.build_parties()
		self.build_payments()

	# --- users --------------------------------------------------------------------------------
	def role(self, suffix: str) -> str:
		name = self.fx.prefix + suffix
		frappe.get_doc({"doctype": "Role", "role_name": name}).insert(ignore_permissions=True)
		return name

	def user_with_company_up(
		self, company, *, role="Accounts Manager", applicable_for=None, hide_descendants=0
	) -> str:
		user = self.fx.user(role=role)
		self.up(user, "Company", company, applicable_for=applicable_for, hide_descendants=hide_descendants)
		return user

	def user_without_roles(self) -> str:
		user = self.fx.user()
		frappe.db.delete("Has Role", {"parent": user})
		frappe.clear_cache(user=user)
		return user

	def up(self, user, allow, value, *, applicable_for=None, hide_descendants=0):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": allow,
				"for_value": value,
				"apply_to_all_doctypes": 0 if applicable_for else 1,
				"applicable_for": applicable_for,
				"hide_descendants": hide_descendants,
			}
		).insert(ignore_permissions=True)

	# --- documents ----------------------------------------------------------------------------
	def insert(self, doctype, suffix, **fields) -> str:
		return self.fx.insert(doctype, suffix, **fields)

	def invoice(self, suffix, **fields) -> str:
		owner = fields.pop("owner", None)
		values = {"company": self.a, "customer": self.fx.customer, "docstatus": 1, **fields}
		name = self.insert(SI, suffix, **values)
		if owner:  # an insert sets the owner to the session user
			frappe.db.set_value(SI, name, "owner", owner, update_modified=False)
		self.si_names.append(name)
		return name

	def row(self, doctype, suffix, parent, table, **fields):
		"""Insert one child row and return its name; None when Frappe 15 lacks the child doctype."""
		if IS_V15 and doctype in ABSENT_ON_V15 and not frappe.db.exists("DocType", doctype):
			# one of the four child doctypes that ERPNext 15 does not have: no row, so a test that needs it
			# is gated. On any other major, or for any other doctype, a missing doctype stays a loud failure.
			return None
		parenttype = fields.pop("parenttype", SI)
		return self.insert(
			doctype, suffix, parent=parent, parenttype=parenttype, parentfield=table, idx=1, **fields
		)

	def twe(self, suffix, parent, company, **fields):
		return self.row(
			"Tax Withholding Entry",
			suffix,
			parent,
			fields.pop("parentfield", "tax_withholding_entries"),
			company=company,
			**fields,
		)

	def share(self, suffix, doctype, name, user=None, **fields):
		return self.insert(
			"DocShare",
			suffix,
			user=user,
			share_doctype=doctype,
			share_name=name,
			**{"read": 1, "everyone": 0, **fields},
		)

	def build_invoices(self):
		a, b, u1, u4 = self.a, self.b, self.u1, self.u4
		self.si_plain = self.invoice("SIPlain")
		self.si_b = self.invoice("SIB", company=b)
		self.si_twe_b = self.invoice("SITweB")
		self.twe("Twe1", self.si_twe_b, b)
		self.si_wrong_parentfield = self.invoice("SIWrongPf")
		self.twe("Twe2", self.si_wrong_parentfield, b, parentfield="taxes")
		self.si_wrong_parenttype = self.invoice("SIWrongPt")
		self.twe("Twe3", self.si_wrong_parenttype, b, parenttype=PI)
		self.si_twe_empty = self.invoice("SITweEmpty")
		self.twe("Twe4", self.si_twe_empty, "")
		self.si_twe_a = self.invoice("SITweA")
		self.twe("Twe5", self.si_twe_a, a)
		self.si_case = self.invoice("SICase")
		# the row's parent differs in case from the invoice name
		self.twe("Twe7", self.si_case.swapcase(), b)
		self.si_a1 = self.invoice("SIA1", company=self.a1)
		# the company value differs in case or by a trailing space from the allowed one
		self.si_company_lower = self.invoice("SICompanyLower", company=a.lower())
		self.si_company_space = self.invoice("SICompanySpace", company=a + " ")
		self.si_represents_b = self.invoice("SIRepresents", represents_company=b)
		self.si_owner_b = self.invoice("SIOwnerB", company=b, owner=u4)
		self.si_owner_upper_b = self.invoice("SIOwnerUpperB", company=b, owner=u4.upper())
		self.si_other_owner_b = self.invoice("SIOtherOwnerB", company=b, owner=u1)
		self.si_shared = self.invoice("SIShared", company=b)
		self.share("Share1", SI, self.si_shared, u1)
		self.si_shared_upper = self.invoice("SISharedUpper", company=b)
		self.share("Share2", SI, self.si_shared_upper, u1.upper())
		self.si_everyone = self.invoice("SIEveryone", company=b)
		self.share("Share3", SI, self.si_everyone, None, everyone=1)
		self.si_share_no_read = self.invoice("SIShareNoRead", company=b)
		self.share("Share4", SI, self.si_share_no_read, u1, read=0)
		self.si_hook_deny = self.invoice("SIHookDeny")
		self.si_hook_order = self.invoice("SIHookOrder", company=b)
		self.share("Share5", SI, self.si_hook_order, u1)
		self.si_hook_raise_shared = self.invoice("SIHookRaise", company=b)
		self.share("Share6", SI, self.si_hook_raise_shared, u1)
		self.si_hook_value_error = self.invoice("SIHookValue")
		self.si_draft = self.invoice("SIDraft", docstatus=0)
		self.si_cancelled = self.invoice("SICancelled", docstatus=2)
		self.si_missing = self.fx.prefix + "SIMissing"

	def build_parties(self):
		a, b, u5 = self.a, self.b, self.u5
		fx = self.fx
		self.cust_plain = self.insert("Customer", "CustPlain", customer_name="plain", customer_type="Company")
		self.cust_credit_b = self.customer("CustCreditB")
		self.row(
			"Customer Credit Limit",
			"Credit1",
			self.cust_credit_b,
			"credit_limits",
			company=b,
			parenttype="Customer",
		)
		self.cust_party_account_b = self.customer("CustAccB")
		self.row(
			"Party Account", "PAcc1", self.cust_party_account_b, "accounts", company=b, parenttype="Customer"
		)
		self.cust_supnum_b = self.customer("CustSupNum")
		self.row(
			"Supplier Number At Customer",
			"SupNum1",
			self.cust_supnum_b,
			"supplier_numbers",
			company=b,
			parenttype="Customer",
		)
		self.cust_shared = self.customer("CustShared")
		self.row(
			"Customer Credit Limit",
			"Credit2",
			self.cust_shared,
			"credit_limits",
			company=b,
			parenttype="Customer",
		)
		self.share("Share7", "Customer", self.cust_shared, u5)
		self.cust_select_hook = self.customer("CustSelectHook")
		self.row(
			"Customer Credit Limit",
			"Credit3",
			self.cust_select_hook,
			"credit_limits",
			company=b,
			parenttype="Customer",
		)
		self.share("Share8", "Customer", self.cust_select_hook, u5)
		self.cust_restricted_b = self.customer("CustRestrB", restrict_to_companies=1)
		self.row(
			"Company Restriction",
			"Restr1",
			self.cust_restricted_b,
			"allowed_companies",
			company=b,
			parenttype="Customer",
		)
		self.cust_restricted_a = self.customer("CustRestrA", restrict_to_companies=1)
		self.row(
			"Company Restriction",
			"Restr2",
			self.cust_restricted_a,
			"allowed_companies",
			company=a,
			parenttype="Customer",
		)
		self.cust_restricted_none = self.customer("CustRestrNone", restrict_to_companies=1)
		self.sup_plain = fx.supplier
		self.sup_restricted_b = self.insert(
			"Supplier", "SupRestrB", supplier_name="restr", supplier_type="Company", restrict_to_companies=1
		)
		self.row(
			"Company Restriction",
			"Restr3",
			self.sup_restricted_b,
			"allowed_companies",
			company=b,
			parenttype="Supplier",
		)
		self.sup_customer_number_b = self.insert(
			"Supplier", "SupNumB", supplier_name="num", supplier_type="Company"
		)
		self.row(
			"Customer Number At Supplier",
			"CustNum1",
			self.sup_customer_number_b,
			"customer_numbers",
			company=b,
			parenttype="Supplier",
		)
		# a Table MultiSelect row for company B on a supplier that nobody restricts: only the child row hides it
		self.sup_multiselect_b = self.insert(
			"Supplier", "SupMultiB", supplier_name="multi", supplier_type="Company"
		)
		self.row(
			"Company Restriction",
			"Restr4",
			self.sup_multiselect_b,
			"allowed_companies",
			company=b,
			parenttype="Supplier",
		)
		# invoices that point at the restricted masters
		self.si_restr_b = self.invoice("SIRestrB", customer=self.cust_restricted_b)
		self.si_restr_a = self.invoice("SIRestrA", customer=self.cust_restricted_a)
		self.si_restr_none = self.invoice("SIRestrNone", customer=self.cust_restricted_none)
		self.si_restr_case = self.invoice("SIRestrCase", customer=self.cust_restricted_b.swapcase())
		self.customer_names = [
			self.cust_plain,
			fx.customer,
			self.cust_credit_b,
			self.cust_party_account_b,
			self.cust_supnum_b,
			self.cust_shared,
			self.cust_select_hook,
			self.cust_restricted_b,
			self.cust_restricted_a,
			self.cust_restricted_none,
		]
		self.supplier_names = [
			self.sup_plain,
			self.sup_restricted_b,
			self.sup_customer_number_b,
			self.sup_multiselect_b,
		]

	def customer(self, suffix, **fields):
		return self.insert("Customer", suffix, customer_name=suffix, customer_type="Company", **fields)

	def build_payments(self):
		a = self.a
		self.pe_restricted_customer = self.insert(
			PE, "PERestrCust", company=a, party_type="Customer", party=self.cust_restricted_b, docstatus=1
		)
		# the same name is a Supplier that nobody restricted
		self.same_name_supplier = self.insert_named(
			"Supplier", self.cust_restricted_b, supplier_name="same", supplier_type="Company"
		)
		self.pe_same_name_supplier = self.insert(
			PE, "PESameName", company=a, party_type="Supplier", party=self.cust_restricted_b, docstatus=1
		)
		self.pe_plain = self.insert(
			PE, "PEPlain", company=a, party_type="Customer", party=self.fx.customer, docstatus=1
		)
		self.pe_other_company = self.insert(
			PE, "PEB", company=self.b, party_type="Customer", party=self.fx.customer, docstatus=1
		)
		self.build_purchase_and_journal()
		self.pe_names = [
			self.pe_restricted_customer,
			self.pe_same_name_supplier,
			self.pe_plain,
			self.pe_other_company,
		]

	def build_purchase_and_journal(self):
		a, b, fx = self.a, self.b, self.fx
		pi = {"company": a, "supplier": fx.supplier, "docstatus": 1}
		self.pi_plain = self.insert(PI, "PIPlain", **pi)
		self.pi_b = self.insert(PI, "PIB", **{**pi, "company": b})
		self.pi_twe_b = self.insert(PI, "PITweB", **pi)
		self.twe("Twe8", self.pi_twe_b, b, parenttype=PI)
		self.pi_restr_b = self.insert(PI, "PIRestrB", **{**pi, "supplier": self.sup_restricted_b})
		self.pi_names = [self.pi_plain, self.pi_b, self.pi_twe_b, self.pi_restr_b]
		je = {"company": a, "docstatus": 1}
		self.je_plain = self.insert(JE, "JEPlain", **je)
		self.je_b = self.insert(JE, "JEB", **{**je, "company": b})
		self.je_twe_b = self.insert(JE, "JETweB", **je)
		self.twe("Twe9", self.je_twe_b, b, parenttype=JE)
		self.je_names = [self.je_plain, self.je_b, self.je_twe_b]

	def insert_named(self, doctype, name, **fields) -> str:
		frappe.get_doc({"doctype": doctype, "name": name, **fields}).db_insert()
		self.fx.created.append((doctype, name))
		return name

	def names(self, doctype: str) -> list[str]:
		return {
			SI: self.si_names,
			"Customer": self.customer_names,
			"Supplier": self.supplier_names,
			PE: self.pe_names,
			PI: self.pi_names,
			JE: self.je_names,
		}[doctype]


def oracle(doctype, names, ptype):
	"""What `visible_names` has always done per name; written here so the batch is not its own judge."""
	visible = set()
	for name in names:
		try:
			if frappe.has_permission(doctype, ptype, doc=name):
				visible.add(name)
		except (frappe.DoesNotExistError, frappe.PermissionError):
			continue
	return visible


@contextmanager
def session_as(user):
	frappe.set_user(user)
	clear_plan_memo()
	try:
		yield
	finally:
		frappe.set_user("Administrator")
		clear_plan_memo()


def build_fixtures() -> BatchFixtures:
	"""The fixtures, built again when a lock wait or a deadlock with another session ends the transaction.

	The test site is shared. A deadlock rolls back the whole transaction, so nothing of a failed attempt stays.
	"""
	for attempt in range(BUILD_ATTEMPTS):
		frappe.db.rollback()
		frappe.db.savepoint("batch_permission_eq")
		try:
			return BatchFixtures()
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.db.rollback()
			if attempt == BUILD_ATTEMPTS - 1:
				raise
			time.sleep(2 + attempt * 2)


class FixtureCase(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		require_calendar_test_site()
		frappe.set_user("Administrator")
		cls.f = build_fixtures()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="batch_permission_eq")
		for doctype in (SI, PE, "Customer", "Supplier"):
			frappe.clear_cache(doctype=doctype)
		super().tearDownClass()

	def evaluate(self, user, doctype, ptype, names=None):
		with session_as(user):
			plan = batch_permission.batch_plan(doctype)
			return plan, batch_permission._evaluate_batch(
				doctype, set(names or self.f.names(doctype)), ptype, plan
			)


# What `batch_permission` keeps for one request or job on `frappe.local`. `frappe.destroy` drops all of it
# between requests; in a test process only a test can, so every `BatchCase` starts and ends clean.
# `pcal_permission_plan_noted` (`_note_plan_reason`) has no constant.
BATCH_LOCALS = (
	batch_permission.TRIPPED,
	batch_permission.FREEZE,
	batch_permission.EVENTS,
	batch_permission.QUIET,
	batch_permission.STATS,
	batch_permission.SAMPLE_SPENT,
	batch_permission.SAMPLE_RUNS,
	batch_permission.REFUSAL,
	batch_permission.CONFIG_PENDING,
	"pcal_permission_plan_noted",
)


def forget_local() -> None:
	"""What `frappe.destroy` does to the state of the batch: nothing of one job reaches the next."""
	for name in BATCH_LOCALS:
		if hasattr(frappe.local, name):
			delattr(frappe.local, name)
	setattr(frappe.local, batch_permission.PLAN_MEMO, None)


class BatchCase(FixtureCase):
	"""The genuine Frappe functions are bound for the class: apps of this bench wrap some of them."""

	def setUp(self):
		super().setUp()
		forget_local()
		self.addCleanup(forget_local)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		manager = clean_bindings()
		manager.__enter__()
		cls.addClassCleanup(manager.__exit__, None, None, None)
		# An event outside a snapshot view is written at once, and the write commits. No test of the batch
		# leaves an Error Log row: a test that asserts an event patches `error_log_once` again for itself.
		writer = patch.object(read_log, "error_log_once")
		cls.error_log_once = writer.start()
		cls.addClassCleanup(writer.stop)


# Hook functions the tests register through `hooks(...)`. They read HOOK_STATE.
def deny_listed(doc, ptype=None, user=None):
	return doc.name not in HOOK_STATE.get("deny", ())


def raise_permission_listed(doc, ptype=None, user=None):
	if doc.name in HOOK_STATE.get("raise", ()):
		raise frappe.PermissionError("hook")
	return True


def raise_value_error_listed(doc, ptype=None, user=None):
	if doc.name in HOOK_STATE.get("value_error", ()):
		raise ValueError("hook")
	return True


def raise_on_select(doc, ptype=None, user=None):
	if ptype == "select" and doc.name in HOOK_STATE.get("select_raise", ()):
		raise frappe.PermissionError("hook")
	return True


HOOK_PATH = "jarvis.tests.batch_permission_fixtures."
