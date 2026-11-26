"""Tests for the ``compute`` tool (jarvis.tools.compute / _compute_sandbox).

Fixture data is inserted raw (``db_insert``, no controller) so the tests exercise the
reads, not ERPNext's invoice logic: three customers, two companies, invoices in INR
and USD. Everything is rolled back with the test class.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _failure_kind
from jarvis.exceptions import (
	ComputeCodeError,
	FeatureDisabledError,
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools import _compute_sandbox as sandbox
from jarvis.tools import registry
from jarvis.tools.compute import compute

CO_INR = "CT Rupee Co"
CO_USD = "CT Dollar Co"
CUST_A, CUST_B, CUST_C = "CT Cust A", "CT Cust B", "CT Cust C"
ACC_USER = "ct-compute-acc@example.com"
LIMITED_USER = "ct-compute-limited@example.com"
NO_ROLE_USER = "ct-compute-none@example.com"
# A user who sees only CT Cust A, whose invoices are all in INR.
INR_ONLY_USER = "ct-compute-inr-only@example.com"
OURS = [["name", "like", "CT-SINV-%"]]
# OURS within the INR company: base_* totals over it are one currency.
INR_CO = [["name", "like", "CT-SINV-%"], ["company", "=", "CT Rupee Co"]]
SYS_USER = "ct-compute-sysmgr@example.com"
DRAFT = "CT-DRAFT-0007"
# OURS plus one AED invoice (outside OURS, so the totals above stay as they are).
ALL = [["name", "like", "CT-%INV-%"]]

# (name, customer, company, currency, rate, grand_total, posting_date, outstanding)
INVOICES = (
	("CT-SINV-0001", CUST_A, CO_INR, "INR", 1, 1000, "2026-10-05", 1000),
	("CT-SINV-0002", CUST_A, CO_INR, "INR", 1, 500, "2026-10-20", 0),
	("CT-SINV-0003", CUST_B, CO_INR, "USD", 83, 100, "2026-10-11", 100),
	("CT-SINV-0004", CUST_B, CO_USD, "USD", 1, 300, "2026-11-02", 300),
	("CT-SINV-0005", CUST_C, CO_USD, "USD", 1, 50, "2026-11-15", 0),
	("CT-XINV-0006", CUST_C, CO_INR, "AED", 22.6, 200, "2026-11-20", 200),
)


def _raw(doc: dict):
	d = frappe.get_doc(doc)
	d.db_insert()
	return d


def _user(email: str, roles: list[str]):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": r} for r in roles],
		}
	)
	user.insert(ignore_permissions=True)
	return user


@contextmanager
def _as(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


def _run(code: str, description: str = "Works out a test figure.") -> dict:
	return compute(code, description)


class ComputeTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for name, abbr, currency in ((CO_INR, "CTR", "INR"), (CO_USD, "CTD", "USD")):
			if not frappe.db.exists("Company", name):
				_raw(
					{
						"doctype": "Company",
						"name": name,
						"company_name": name,
						"abbr": abbr,
						"default_currency": currency,
						"country": "India",
					}
				)
		for name in (CUST_A, CUST_B, CUST_C):
			if not frappe.db.exists("Customer", name):
				_raw({"doctype": "Customer", "name": name, "customer_name": name, "customer_type": "Company"})
		for name, customer, company, currency, rate, total, posting, outstanding in INVOICES:
			_raw(
				{
					"doctype": "Sales Invoice",
					"name": name,
					"customer": customer,
					"company": company,
					"currency": currency,
					"party_account_currency": currency,
					"conversion_rate": rate,
					"grand_total": total,
					"base_grand_total": total * rate,
					"net_total": total,
					"base_net_total": total * rate,
					"outstanding_amount": outstanding,
					"posting_date": posting,
					"due_date": posting,
					"docstatus": 1,
				}
			)
		# Items of two invoices in different currencies (their amounts are in the parent's).
		for parent, amount in (("CT-SINV-0001", 1000), ("CT-SINV-0003", 100)):
			_raw(
				{
					"doctype": "Sales Invoice Item",
					"name": f"{parent}-ITEM",
					"parent": parent,
					"parenttype": "Sales Invoice",
					"parentfield": "items",
					"item_code": "CT-ITEM",
					"qty": 1,
					"rate": amount,
					"amount": amount,
					"base_amount": amount,
					"docstatus": 1,
				}
			)
		# One draft, outside OURS / ALL: only the docstatus tests read it.
		_raw(
			{
				"doctype": "Sales Invoice",
				"name": DRAFT,
				"customer": CUST_A,
				"company": CO_INR,
				"currency": "INR",
				"party_account_currency": "INR",
				"conversion_rate": 1,
				"grand_total": 70,
				"base_grand_total": 70,
				"outstanding_amount": 70,
				"posting_date": "2026-11-21",
				"docstatus": 0,
			}
		)
		_user(ACC_USER, ["Accounts User"])
		_user(SYS_USER, ["System Manager"])
		_user(LIMITED_USER, ["Accounts User"])
		_user(NO_ROLE_USER, [])
		_user(INR_ONLY_USER, ["Accounts User"])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": INR_ONLY_USER,
				"allow": "Customer",
				"for_value": CUST_A,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=INR_ONLY_USER)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": LIMITED_USER,
				"allow": "Company",
				"for_value": CO_INR,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=LIMITED_USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


class TestComputeWorksAsWorktreeCode(FrappeTestCase):
	def test_the_code_under_test_is_the_one_imported(self):
		import jarvis

		# The tests are run with the worktree first on PYTHONPATH; print where from.
		print(f"\n[test_compute] jarvis imported from {jarvis.__file__}")
		self.assertTrue(sandbox.__file__.endswith("_compute_sandbox.py"))


class TestHappyPaths(ComputeTestCase):
	def test_base_total_grouped_by_customer(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={INR_CO!r}, "
			"fields=['customer', {'SUM': 'base_grand_total', 'as': 'total'}, {'COUNT': '*', 'as': 'n'}], "
			"group_by='customer', order_by='customer asc')\n"
			"result = {r.customer: [r.total, r.n] for r in rows}"
		)
		self.assertEqual(out["result"], {CUST_A: [1500.0, 2], CUST_B: [8300.0, 1]})
		self.assertEqual(out["rows_read"], 2)
		self.assertNotIn("printed", out)  # only when something was printed
		self.assertNotIn("warnings", out)
		self.assertIsInstance(out["elapsed_ms"], int)

	def test_the_sql_string_aggregate_form_is_accepted_too(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={INR_CO!r}, fields=['sum(base_grand_total) as total'])\n"
			"result = rows[0].total"
		)
		self.assertEqual(out["result"], 9800.0)

	def test_multi_currency_base_totals_differ_from_document_currency_totals(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, "
			"fields=['currency', 'grand_total', 'base_grand_total'])\n"
			"result = {\n"
			"    'base': sum(r.base_grand_total for r in rows),\n"
			"    'naive': sum(r.grand_total for r in rows),\n"
			"    'currencies': sorted(set(r.currency for r in rows)),\n"
			"    'count': len(rows),\n"
			"}"
		)
		self.assertEqual(out["result"]["base"], 10150.0)
		self.assertEqual(out["result"]["naive"], 1950.0)
		self.assertEqual(out["result"]["currencies"], ["INR", "USD"])
		self.assertEqual(out["result"]["count"], 5)

	def test_top_n(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['name', 'base_grand_total'])\n"
			"top = sorted(rows, key=lambda r: r.base_grand_total, reverse=True)[:2]\n"
			"result = [r.name for r in top]"
		)
		self.assertEqual(out["result"], ["CT-SINV-0003", "CT-SINV-0001"])

	def test_monthly_totals_with_date_helpers(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['posting_date', 'base_grand_total'])\n"
			"months = {}\n"
			"for r in rows:\n"
			"    key = str(get_first_day(r.posting_date))\n"
			"    months[key] = months.get(key, 0) + flt(r.base_grand_total)\n"
			"start = getdate('2026-10-15')\n"
			"result = {\n"
			"    'months': months,\n"
			"    'next_month': str(add_months(start, 1)),\n"
			"    'last_day': str(get_last_day(start)),\n"
			"    'days': date_diff('2026-11-01', start),\n"
			"    'add_days': str(add_days(start, 3)),\n"
			"    'today_is_text': isinstance(today(), str),\n"
			"}"
		)
		r = out["result"]
		self.assertEqual(r["months"], {"2026-10-01": 9800.0, "2026-11-01": 350.0})
		self.assertEqual(r["next_month"], "2026-11-15")
		self.assertEqual(r["last_day"], "2026-10-31")
		self.assertEqual(r["days"], 17)
		self.assertEqual(r["add_days"], "2026-10-18")
		self.assertTrue(r["today_is_text"])

	def test_outstanding_by_party_account_currency(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r} + [['outstanding_amount', '>', 0]], "
			"fields=['party_account_currency', {'SUM': 'outstanding_amount', 'as': 'due'}], "
			"group_by='party_account_currency')\n"
			"result = {r.party_account_currency: r.due for r in rows}"
		)
		self.assertEqual(out["result"], {"INR": 1000.0, "USD": 400.0})

	def test_frappe_prefixed_and_bare_helpers_agree(self):
		out = _run(
			"a = frappe.get_list('Customer', filters={'name': ['like', 'CT Cust%']}, pluck='name')\n"
			"b = get_list('Customer', filters={'name': ['like', 'CT Cust%']}, pluck='name')\n"
			f"c = frappe.db.count('Sales Invoice', {OURS!r})\n"
			f"d = count('Sales Invoice', {OURS!r})\n"
			"e = frappe.db.get_value('Sales Invoice', 'CT-SINV-0003', 'base_grand_total')\n"
			"f = get_value('Sales Invoice', {'name': 'CT-SINV-0003'}, ['currency', 'conversion_rate'])\n"
			"g = frappe.db.exists('Customer', 'CT Cust A')\n"
			"h = exists('Customer', 'CT Nobody')\n"
			"result = [sorted(a) == sorted(b), c, d, e, f, g, h]"
		)
		self.assertEqual(out["result"], [True, 5, 5, 8300.0, ["USD", 83.0], True, False])

	def test_get_doc_returns_a_dict_with_attribute_access(self):
		out = _run(
			"doc = frappe.get_doc('Sales Invoice', 'CT-SINV-0003')\n"
			"result = [doc.customer, doc['currency'], doc.get('no_such_field'), isinstance(doc, dict)]"
		)
		self.assertEqual(out["result"], [CUST_B, "USD", None, True])

	def test_get_meta_lists_fields(self):
		out = _run(
			"meta = get_meta('Sales Invoice')\n"
			"names = [f.fieldname for f in meta.fields]\n"
			"result = ['base_grand_total' in names, meta.is_submittable]"
		)
		self.assertEqual(out["result"], [True, True])

	def test_session_user_and_dict(self):
		with _as(ACC_USER):
			out = _run("d = frappe._dict(a=1)\ne = _dict(b=2)\nresult = [frappe.session.user, d.a, e.b]")
		self.assertEqual(out["result"], [ACC_USER, 1, 2])

	def test_default_company_is_a_permitted_company_or_none(self):
		out = _run("result = {'company': default_company()}")
		company = out["result"]["company"]
		self.assertTrue(company is None or frappe.db.exists("Company", company))

	def test_registry_dispatch_runs_it(self):
		out = registry.dispatch(
			"compute",
			{"code": f"result = count('Sales Invoice', {OURS!r})", "description": "Counts test invoices."},
		)
		self.assertEqual(out["result"], 5)

	def test_print_is_captured_and_capped(self):
		out = _run("print('hello', 42)\nresult = 1")
		self.assertEqual(out["printed"], "hello 42\n")
		out = _run("for i in range(500):\n    print('x' * 10)\nresult = 1")
		self.assertLessEqual(len(out["printed"]), sandbox.MAX_PRINT_CHARS + 60)
		self.assertIn("cut at 2,000", out["printed"])


class TestOrdinaryPython(ComputeTestCase):
	def test_constructs(self):
		code = """
d = {}
for k in ['a', 'b', 'a']:
    d[k] = d.get(k, 0)
    d[k] += 1
n = 0
n += 5
lst = [1]
lst += [2]
s = {1}
s |= {2}
a, b = 1, 2
(c, e), f = (3, 4), 5
pairs = [f'{k}={v}' for k, v in sorted(d.items())]
idx = [(i, k, v) for i, (k, v) in enumerate(sorted(d.items()))]
sq = {x: x * x for x in range(4)}
evens = {x for x in range(6) if x % 2 == 0}
gen = sum(x for x in range(5))
def double(x):
    return x * 2
nested = {'k': {'n': 1}}
nested['k']['n'] += 9
result = {
    'd': d, 'n': n, 'lst': lst, 's': sorted(s), 'abc': [a, b, c, e, f], 'pairs': pairs,
    'idx': idx, 'sq': sq, 'evens': sorted(evens), 'gen': gen, 'double': double(21),
    'nested': nested['k']['n'],
    'builtins': [
        sum([1, 2]), min(3, 1), max([4, 9]), len('abc'), round(2.567, 2), abs(-3),
        any([0, 1]), all([1, 1]), list(zip([1, 2], 'ab')), list(reversed([1, 2])),
        sorted([3, 1], key=lambda x: -x), dict(a=1), list((1, 2)), tuple([1]), str(5),
        int('7'), float('1.5'), bool(0), list(enumerate('x')), set([1, 1]) == {1},
    ],
    'fstr': f"{1234.5:,.2f}",
    'percent': '%s-%d' % ('a', 3),
}
"""
		r = _run(code)["result"]
		self.assertEqual(r["d"], {"a": 2, "b": 1})
		self.assertEqual(r["n"], 5)
		self.assertEqual(r["lst"], [1, 2])
		self.assertEqual(r["s"], [1, 2])
		self.assertEqual(r["abc"], [1, 2, 3, 4, 5])
		self.assertEqual(r["pairs"], ["a=2", "b=1"])
		self.assertEqual(r["idx"], [[0, "a", 2], [1, "b", 1]])
		self.assertEqual(r["sq"], {"0": 0, "1": 1, "2": 4, "3": 9})
		self.assertEqual(r["evens"], [0, 2, 4])
		self.assertEqual(r["gen"], 10)
		self.assertEqual(r["double"], 42)
		self.assertEqual(r["nested"], 10)
		self.assertEqual(
			r["builtins"],
			[
				3,
				1,
				9,
				3,
				2.57,
				3,
				True,
				True,
				[[1, "a"], [2, "b"]],
				[2, 1],
				[3, 1],
				{"a": 1},
				[1, 2],
				[1],
				"5",
				7,
				1.5,
				False,
				[[0, "x"]],
				True,
			],
		)
		self.assertEqual(r["fstr"], "1,234.50")
		self.assertEqual(r["percent"], "a-3")

	def test_json_helpers(self):
		r = _run("result = json.loads(json.dumps({'a': [1, 2]}))")["result"]
		self.assertEqual(r, {"a": [1, 2]})

	def test_result_is_json_safe(self):
		r = _run(
			"from_doc = get_value('Sales Invoice', 'CT-SINV-0001', 'posting_date')\n"
			"result = {'date': from_doc, 'dec': 1.25, 'dt': datetime.datetime(2026, 1, 2, 3, 4, 5), "
			"'d2': datetime.date(2026, 1, 2), 'tuple': (1, 2)}"
		)["result"]
		self.assertEqual(r["date"], "2026-10-05")
		self.assertEqual(r["dec"], 1.25)
		self.assertEqual(r["dt"], "2026-01-02 03:04:05")
		self.assertEqual(r["d2"], "2026-01-02")
		self.assertEqual(r["tuple"], [1, 2])

	def test_nan_and_objects_are_refused_with_a_reason(self):
		with self.assertRaisesRegex(ComputeCodeError, "NaN or Infinity"):
			_run("result = float('nan')")
		with self.assertRaisesRegex(ComputeCodeError, "plain data"):
			_run("result = {'f': len}")

	def test_missing_result(self):
		with self.assertRaisesRegex(ComputeCodeError, "variable named result"):
			_run("x = 1")


class TestTeachingErrors(ComputeTestCase):
	def assertTeaches(self, code: str, *fragments: str, exc=ComputeCodeError):
		with self.assertRaises(exc) as ctx:
			_run(code)
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))
		return ctx.exception

	def test_errors_are_fixable(self):
		e = self.assertTeaches("import os\nresult = 1", "No imports")
		self.assertEqual(_failure_kind.kind_of(e), _failure_kind.FIXABLE)

	def test_imports(self):
		self.assertTeaches("from frappe import db\nresult = 1", "No imports", "already in scope")
		self.assertTeaches("result = __import__('os')", "imports are not available")

	def test_sql_and_get_all(self):
		self.assertTeaches("result = frappe.db.sql('select 1')", "frappe.db.sql is not available", "get_list")
		self.assertTeaches("result = frappe.get_all('Customer')", "ignores permissions", "get_list")
		self.assertTeaches("result = frappe.db.get_all('Customer')", "ignores permissions")
		self.assertTeaches("result = frappe.qb.DocType('Customer')", "query builder")

	def test_writes_jobs_mail_network(self):
		self.assertTeaches("frappe.db.set_value('Customer', 'x', 'y', 1)\nresult = 1", "read-only")
		self.assertTeaches("d = frappe.new_doc('ToDo')\nresult = 1", "read-only")
		self.assertTeaches("frappe.enqueue('x')\nresult = 1", "background jobs")
		self.assertTeaches("frappe.sendmail(recipients=['a@b.c'])\nresult = 1", "cannot send email")
		self.assertTeaches("frappe.make_get_request('http://x')\nresult = 1", "network")
		self.assertTeaches("result = frappe.call('frappe.client.get_list')", "frappe.call is not available")

	def test_doc_write_methods_do_not_exist(self):
		self.assertTeaches(
			"d = get_doc('Sales Invoice', 'CT-SINV-0001')\nd.save()\nresult = 1", "no field 'save'"
		)

	def test_unknown_names_list_what_is_available(self):
		e = self.assertTeaches("result = frappe_get_list('Customer')", "is not defined here", "get_list")
		self.assertIn("run_report", str(e))
		self.assertTeaches(
			"result = frappe.get_lst('Customer')", "frappe.get_lst is not available", "get_list"
		)
		self.assertTeaches("result = frappe.utils.nope()", "frappe.utils.nope is not available", "getdate")

	def test_dynamic_access_is_taught_at_run_time_too(self):
		self.assertTeaches("f = frappe\nresult = f.db.sql('select 1')", "frappe.db.sql is not available")

	def test_syntax_error_names_the_line(self):
		self.assertTeaches("x = 1\nresult = (", "line 2", "SyntaxError")

	def test_runtime_error_names_the_line_and_hints(self):
		self.assertTeaches("x = {}\ny = 2\nresult = x['missing']", "line 3", "KeyError", "d.get(k, 0)")
		self.assertTeaches("result = None + 1", "line 1", "flt(x)")

	def test_misspelled_field_fails_instead_of_totalling_zero(self):
		self.assertTeaches(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['base_grand_totl'])\nresult = 1",
			"has no field(s) base_grand_totl",
		)
		self.assertTeaches(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['name'])\n"
			"result = sum(r.base_grand_total for r in rows)",
			"no field 'base_grand_total'",
		)
		self.assertTeaches(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=[{{'SUM': 'grand_totl', 'as': 't'}}])\n"
			"result = 1",
			"has no field(s) grand_totl",
		)

	def test_get_list_keyword_typos(self):
		self.assertTeaches("result = get_list('Customer', filter={'name': 'x'})", "does not take filter")

	def test_unknown_doctype(self):
		self.assertTeaches("result = get_list('No Such Doctype')", "unknown doctype")

	def test_class_definitions(self):
		self.assertTeaches("class A:\n    pass\nresult = 1", "class definitions")

	def test_format_method(self):
		self.assertTeaches("result = '{}'.format(1)", "f-string")

	def test_code_and_description_limits(self):
		with self.assertRaisesRegex(ComputeCodeError, "limit is 20,000"):
			compute("result = 1\n" + "#" * sandbox.MAX_CODE_CHARS, "Too long.")
		with self.assertRaisesRegex(ComputeCodeError, "description is required"):
			compute("result = 1", "  ")
		with self.assertRaisesRegex(ComputeCodeError, "one plain sentence"):
			compute("result = 1", "x" * 400)
		with self.assertRaisesRegex(ComputeCodeError, "non-empty"):
			compute("   ", "Empty.")


class TestEscapes(ComputeTestCase):
	def assertRefused(self, code: str, exc=ComputeCodeError, fragment: str | None = None):
		with self.assertRaises(exc) as ctx:
			_run(code)
		if fragment:
			self.assertIn(fragment, str(ctx.exception))

	def test_dunder_and_class_walks(self):
		for code in (
			"result = ().__class__.__bases__[0].__subclasses__()",
			"result = [].__class__",
			"result = frappe.get_list.__globals__",
			"result = get_list.__closure__",
			"result = (lambda: 1).__code__",
			"result = frappe.__dict__",
		):
			with self.subTest(code=code):
				self.assertRefused(code)

	def test_format_string_attribute_access(self):
		self.assertRefused("result = '{0.__class__}'.format(1)")
		self.assertRefused("f = '{0.__class__}'.format\nresult = f(1)")
		self.assertRefused("result = '{a.__class__}'.format_map({'a': 1})")

	def test_generator_frames(self):
		self.assertRefused("g = (x for x in [1])\nresult = str(g.gi_frame)")
		self.assertRefused("g = (x for x in [1])\nh = [g]\nresult = str(h[0].gi_code)")

	def test_builtins_that_reach_outside(self):
		for name, arg in (
			("open", "'/etc/passwd'"),
			("eval", "'1'"),
			("exec", "'x=1'"),
			("compile", "'1', 'x', 'eval'"),
			("getattr", "frappe, 'db'"),
			("globals", ""),
			("vars", ""),
			("type", "1"),
			("dir", ""),
		):
			with self.subTest(name=name):
				self.assertRefused(f"result = {name}({arg})", fragment="not available")

	def test_frappe_local_and_site_state(self):
		for attr in ("local", "flags", "conf", "cache", "form_dict"):
			with self.subTest(attr=attr):
				self.assertRefused(f"result = frappe.{attr}", fragment="not available")

	def test_namespace_cannot_be_changed(self):
		self.assertRefused("frappe.get_list = None\nresult = 1", fragment="cannot be changed")
		self.assertRefused("frappe.session.user = 'Administrator'\nresult = 1", fragment="cannot be changed")

	def test_permission_escape_keywords(self):
		for kw in (
			"ignore_permissions=True",
			"user='Administrator'",
			"ignore_user_permissions=True",
			"as_list=1",
		):
			with self.subTest(kw=kw):
				self.assertRefused(
					f"result = get_list('Sales Invoice', {kw})",
					exc=PermissionDeniedError,
					fragment="own permissions",
				)

	def test_jarvis_private_doctypes(self):
		for doctype in ("Jarvis Settings", "Jarvis User Memory", "jarvis custom skill"):
			with self.subTest(doctype=doctype):
				self.assertRefused(
					f"result = get_list('{doctype}')", exc=PermissionDeniedError, fragment="Jarvis's own data"
				)
		self.assertRefused(
			"result = get_doc('Jarvis Settings')", exc=PermissionDeniedError, fragment="Jarvis's own data"
		)

	def test_big_c_level_operations(self):
		self.assertRefused("result = sum(range(10 ** 12))", fragment="range() over")
		self.assertRefused("result = len('x' * 10 ** 9)", fragment="Repeating a sequence")
		self.assertRefused("s = 'x'\ns *= 10 ** 9\nresult = 1", fragment="Repeating a sequence")
		self.assertRefused("result = 2 ** (10 ** 9)", fragment="This power would make an integer")
		self.assertRefused("result = pow(7, 10 ** 9)", fragment="This power would make an integer")
		self.assertRefused("result = 1 << (10 ** 9)", fragment="This shift would make an integer")


class TestPermissions(ComputeTestCase):
	TOTALS = (
		f"rows = get_list('Sales Invoice', filters={OURS!r}, "
		"fields=['company', {'SUM': 'base_grand_total', 'as': 'total'}, {'COUNT': '*', 'as': 'n'}], "
		"group_by='company')\n"
		"result = {r.company: [r.total, r.n] for r in rows}"
	)

	def test_no_read_on_the_doctype(self):
		with _as(NO_ROLE_USER), self.assertRaises(PermissionDeniedError):
			_run("result = count('Sales Invoice')")
		with _as(NO_ROLE_USER), self.assertRaises(PermissionDeniedError):
			_run("result = get_doc('Sales Invoice', 'CT-SINV-0001')")

	def test_a_caught_permission_error_gives_no_data(self):
		with _as(NO_ROLE_USER):
			out = _run(
				"try:\n    rows = get_list('Sales Invoice')\nexcept Exception:\n    rows = []\nresult = len(rows)"
			)
		self.assertEqual(out["result"], 0)

	def test_user_permission_scopes_the_aggregate(self):
		with _as(ACC_USER):
			everything = _run(self.TOTALS)["result"]
		with _as(LIMITED_USER):
			scoped = _run(self.TOTALS)["result"]
		self.assertEqual(everything, {CO_INR: [9800.0, 3], CO_USD: [350.0, 2]})
		self.assertEqual(scoped, {CO_INR: [9800.0, 3]})

	def test_user_permission_scopes_counts_and_docs(self):
		with _as(LIMITED_USER):
			self.assertEqual(_run(f"result = count('Sales Invoice', {OURS!r})")["result"], 3)
			self.assertFalse(_run("result = exists('Sales Invoice', 'CT-SINV-0004')")["result"])
			self.assertIsNone(
				_run("result = {'v': get_value('Sales Invoice', 'CT-SINV-0004', 'grand_total')}")["result"][
					"v"
				]
			)
			with self.assertRaises(PermissionDeniedError):
				_run("result = get_doc('Sales Invoice', 'CT-SINV-0004')")

	def test_permlevel_hidden_field(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		# Level 7: no role reads it (Sales Invoice gives level 1 to All).
		make_property_setter("Sales Invoice", "base_net_total", "permlevel", 7, "Int")
		frappe.clear_cache(doctype="Sales Invoice")
		try:
			with _as(ACC_USER):
				for code in (
					f"result = get_list('Sales Invoice', filters={OURS!r}, fields=[{{'SUM': 'base_net_total', 'as': 't'}}])",
					f"result = get_list('Sales Invoice', filters={OURS!r}, fields=['name', 'base_net_total'])",
					"result = get_list('Sales Invoice', filters={'base_net_total': ['>', 100]})",
					"result = get_list('Sales Invoice', order_by='base_net_total desc')",
				):
					with (
						self.subTest(code=code),
						self.assertRaisesRegex(PermissionDeniedError, "base_net_total"),
					):
						_run(code)
				doc = _run("result = get_doc('Sales Invoice', 'CT-SINV-0003')")["result"]
				self.assertIn(doc.get("base_net_total"), (None, 0, 0.0))
				meta_names = _run("result = [f.fieldname for f in get_meta('Sales Invoice').fields]")[
					"result"
				]
				self.assertNotIn("base_net_total", meta_names)
				# The fields the user can read still total as before.
				readable = _run(
					f"result = get_list('Sales Invoice', filters={INR_CO!r}, fields=[{{'SUM': 'base_grand_total', 'as': 't'}}])[0].t"
				)
				self.assertEqual(readable["result"], 9800.0)
		finally:
			frappe.db.delete("Property Setter", {"doc_type": "Sales Invoice", "field_name": "base_net_total"})
			frappe.clear_cache(doctype="Sales Invoice")

	def test_masked_field_cannot_be_aggregated_or_filtered(self):
		from types import SimpleNamespace

		fake = [SimpleNamespace(fieldname="customer_name")]
		with patch("jarvis.compat.masked_fields_of", return_value=fake), _as(ACC_USER):
			with self.assertRaisesRegex(PermissionDeniedError, "masked"):
				_run("result = get_list('Customer', fields=[{'MAX': 'customer_name', 'as': 'x'}])")
			with self.assertRaisesRegex(PermissionDeniedError, "masked"):
				_run("result = get_list('Customer', filters={'customer_name': ['like', 'CT%']})")


class TestLimits(ComputeTestCase):
	def test_row_cap_per_read_fails_the_call_even_when_caught(self):
		with patch.object(sandbox, "MAX_ROWS_PER_READ", 3):
			with self.assertRaisesRegex(ComputeCodeError, "more than 3 rows"):
				_run(f"result = len(get_list('Sales Invoice', filters={OURS!r}))")
			with self.assertRaisesRegex(ComputeCodeError, "more than 3 rows"):
				_run(
					f"try:\n    rows = get_list('Sales Invoice', filters={OURS!r})\n"
					"except Exception:\n    rows = []\nresult = len(rows)"
				)
			with self.assertRaisesRegex(ComputeCodeError, "more than 3 rows"):
				_run(
					f"try:\n    rows = get_list('Sales Invoice', filters={OURS!r})\nexcept:\n    rows = []\nresult = 0"
				)
			# An explicit limit under the cap is a top-N, not a failure.
			self.assertEqual(
				_run(f"result = len(get_list('Sales Invoice', filters={OURS!r}, limit=2))")["result"], 2
			)

	def test_total_row_budget_across_reads(self):
		with patch.object(sandbox, "MAX_ROWS_TOTAL", 7):
			with self.assertRaisesRegex(ComputeCodeError, "more than 7 rows in total"):
				_run(
					f"a = get_list('Sales Invoice', filters={OURS!r})\n"
					f"b = get_list('Sales Invoice', filters={OURS!r})\nresult = len(a) + len(b)"
				)

	def test_wall_clock_limit(self):
		tracer_before = sys.gettrace()
		with patch.object(sandbox, "WALL_CLOCK_S", 1):
			with self.assertRaisesRegex(ComputeCodeError, "more than 1 seconds"):
				_run("x = 0\nwhile True:\n    x += 1")
			# Catching the stop cannot keep a loop alive: the check is compiled into the top
			# of every loop body, outside any try in it, and the failure is recorded.
			for code in (
				"x = 0\nwhile True:\n    try:\n        x += 1\n    except:\n        continue",
				"while True:\n    try:\n        while True:\n            pass\n    except:\n        pass",
				"try:\n    while True:\n        pass\nexcept:\n    pass\nresult = 'swallowed'",
				"def forever():\n    while True:\n        yield 1\nresult = sum(forever())",
				"def forever():\n    while True:\n        yield 1\nresult = [x for x in forever()]",
			):
				with self.subTest(code=code), self.assertRaisesRegex(ComputeCodeError, "more than 1 seconds"):
					_run(code)
		# The previous trace function (none, or a coverage tool's) is put back.
		self.assertIs(sys.gettrace(), tracer_before)

	def test_memory_limit(self):
		run = sandbox._Run()
		run.rss_at_start = 0
		with patch.object(sandbox, "_rss_bytes", return_value=sandbox.MAX_MEMORY_GROWTH_BYTES + 1):
			with self.assertRaises(sandbox._Stop):
				run.check_memory()
		self.assertIn("too much memory", run.failure)

	def test_result_size(self):
		with self.assertRaisesRegex(ComputeCodeError, "top-N"):
			_run("result = ['x' * 1000 for i in range(40)]")

	def test_kill_switch(self):
		for value in (1, "1", True, "yes"):
			with self.subTest(value=value), patch.dict(frappe.local.conf, {"jarvis_disable_compute": value}):
				with self.assertRaises(FeatureDisabledError):
					_run("result = 1")
		with patch.dict(frappe.local.conf, {"jarvis_disable_compute": 0}):
			self.assertEqual(_run("result = 1")["result"], 1)

	def test_statement_time_limit_is_set_and_restored(self):
		seen = []
		before = frappe.db.sql("select @@max_statement_time")[0][0]

		real = frappe.get_list

		def spy(*args, **kwargs):
			seen.append(float(frappe.db.sql("select @@max_statement_time")[0][0]))
			return real(*args, **kwargs)

		with patch("frappe.get_list", spy):
			_run("result = count('Customer')")
		self.assertEqual(seen, [float(sandbox.STATEMENT_TIME_S)])
		self.assertEqual(frappe.db.sql("select @@max_statement_time")[0][0], before)


class TestReadOnly(ComputeTestCase):
	def _todo_count(self):
		return frappe.db.count("ToDo", {"description": "compute-write-probe"})

	def _fake_report(self, body):
		"""A Query Report whose run is replaced by ``body`` (a report or hook that does
		something when it runs)."""
		name = "CT Compute Probe Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tabSales Invoice`",
				}
			).insert(ignore_permissions=True)

		def run(report_name, filters=None, **kwargs):
			body()
			return {"columns": [{"fieldname": "n", "label": "N", "fieldtype": "Int"}], "result": [{"n": 1}]}

		return name, patch("jarvis.tools.run_report.frappe_run_report", run)

	def test_a_report_that_inserts_is_refused_and_leaves_nothing(self):
		before = self._todo_count()

		def write():
			frappe.get_doc({"doctype": "ToDo", "description": "compute-write-probe"}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()

		name, patched = self._fake_report(write)
		with patched, self.assertRaises(InvalidArgumentError) as ctx:
			_run(f"result = run_report('{name}')")
		self.assertNotIsInstance(ctx.exception, ComputeCodeError)
		self.assertIn("read-only", str(ctx.exception))
		self.assertEqual(_failure_kind.kind_of(ctx.exception), _failure_kind.NOT_FIXABLE)
		self.assertEqual(self._todo_count(), before)

	def test_a_report_that_catches_the_refusal_still_fails_the_call(self):
		before = self._todo_count()

		def write():
			try:
				frappe.db.sql("update `tabToDo` set description = 'x' where 1 = 0")
			except Exception:
				pass

		name, patched = self._fake_report(write)
		with patched, self.assertRaisesRegex(InvalidArgumentError, "read-only"):
			_run(f"result = run_report('{name}')")
		self.assertEqual(self._todo_count(), before)

	def test_raw_ddl_and_commit_are_refused(self):
		for statement in ("create table `tabCT Nope` (x int)", "commit", "rollback", "set autocommit = 1"):
			with self.subTest(statement=statement):

				def write(statement=statement):
					frappe.db.sql(statement)

				name, patched = self._fake_report(write)
				with patched, self.assertRaisesRegex(InvalidArgumentError, "read-only"):
					_run(f"result = run_report('{name}')")

	def test_a_report_that_enqueues_sends_no_job(self):
		from rq import Queue

		def enqueue():
			frappe.enqueue("frappe.ping", queue="short")

		name, patched = self._fake_report(enqueue)
		with patched, patch.object(Queue, "enqueue_call") as sent:
			out = _run(f"r = run_report('{name}')\nresult = r['rows'][0]['n']")
		self.assertEqual(out["result"], 1)
		sent.assert_not_called()

	def test_run_report_inside_a_script(self):
		name = "CT Compute Invoice Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name as invoice, customer, base_grand_total as amount "
					"from `tabSales Invoice` where name like 'CT-SINV-%%' order by name",
				}
			).insert(ignore_permissions=True)
		out = _run(
			f"r = run_report('{name}')\n"
			"by = {}\n"
			"for row in r.rows:\n"
			"    by[row.customer] = by.get(row.customer, 0) + flt(row.amount)\n"
			"result = {'by': by, 'columns': [c.fieldname for c in r.columns], 'n': len(r.rows)}"
		)
		self.assertEqual(out["result"]["by"], {CUST_A: 1500.0, CUST_B: 8600.0, CUST_C: 50.0})
		self.assertEqual(out["result"]["columns"], ["invoice", "customer", "amount"])
		self.assertEqual(out["rows_read"], 5)

	def test_the_reads_leave_the_transaction_as_they_found_it(self):
		frappe.get_doc({"doctype": "ToDo", "description": "compute-before"}).insert(ignore_permissions=True)
		_run("result = count('ToDo')")
		# The request's own write before the call is still there (savepoint, not a rollback).
		self.assertTrue(frappe.db.exists("ToDo", {"description": "compute-before"}))
		# The write guard is gone again: frappe.db.sql is the class's own method.
		self.assertNotIn("sql", vars(frappe.db))


class TestRegistration(FrappeTestCase):
	"""compute is a plain read everywhere tools are classified."""

	def test_not_a_write_or_gated_anywhere(self):
		from jarvis import api

		for name in ("_WRITE_TOOLS", "_GATED_WRITES", "_BRAKE", "_PREVIEWABLE", "_DRY_RUN_ON_PARK"):
			with self.subTest(set=name):
				self.assertNotIn("compute", getattr(api, name))
		self.assertIn("compute", registry.list_tools())

	def test_shared_sessions_classify_it_as_a_read(self):
		from jarvis.chat import collaboration_tools

		self.assertEqual(collaboration_tools.tool_kind("compute"), "read")

	def test_standard_tier_gets_it(self):
		from jarvis.chat import role_profiles

		self.assertIn("jarvis__compute", role_profiles.standard_tools_allow())

	def test_snapshot_config_delegate_is_refused(self):
		from jarvis.tools import _delegate_capability

		cap = {"tools_allow": ["jarvis__get_engagement_config", "jarvis__compute"]}
		self.assertIsNotNone(_delegate_capability.snapshot_config_read_denial(cap, "jarvis__compute", {}))
		self.assertIsNone(
			_delegate_capability.snapshot_config_read_denial({"tools_allow": []}, "compute", {})
		)

	def test_step_label_is_the_description(self):
		from jarvis.chat.agent_run_steps import humanize_tool_call

		label, detail = humanize_tool_call(
			"jarvis__compute",
			{"code": "result = 1", "description": "Totals invoices by customer."},
			{"result": 1, "rows_read": 12, "elapsed_ms": 3, "printed": ""},
		)
		self.assertEqual(label, "Totals invoices by customer.")
		self.assertEqual(detail, "Read 12 rows")
		self.assertEqual(humanize_tool_call("compute", {}, None), ("Worked out a result", ""))


@contextmanager
def _count_list_calls(doctype: str = "Sales Invoice"):
	"""Count frappe.get_list calls on ``doctype`` (the script's reads plus any currency
	probe). Other doctypes are left out: Frappe loads a cold meta through get_list."""
	calls = []
	real = frappe.get_list

	def spy(*args, **kwargs):
		if (args[0] if args else kwargs.get("doctype")) == doctype:
			calls.append((args, kwargs))
		return real(*args, **kwargs)

	with patch("frappe.get_list", spy):
		yield calls


class TestCurrencyGuard(ComputeTestCase):
	def test_db_sum_of_party_currency_amount_across_currencies_is_refused(self):
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(
				f"rows = get_list('Sales Invoice', filters={ALL!r}, "
				"fields=[{'SUM': 'outstanding_amount', 'as': 'due'}])\nresult = rows[0].due"
			)
		message = str(ctx.exception)
		self.assertIn("Sales Invoice rows here are in AED, INR and USD.", message)
		self.assertIn("outstanding_amount is in each row's party_account_currency, so one SUM mixes", message)
		self.assertIn("Group by party_account_currency", message)
		self.assertIn("conversion_rate", message)
		self.assertNotIn("base_outstanding_amount", message)
		self.assertEqual(_failure_kind.kind_of(ctx.exception), _failure_kind.FIXABLE)

	def test_db_sum_of_document_currency_amount_names_the_base_field(self):
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(
				f"rows = get_list('Sales Invoice', filters={ALL!r}, "
				"fields=['customer', {'SUM': 'grand_total', 'as': 't'}], group_by='customer')\nresult = len(rows)"
			)
		message = str(ctx.exception)
		self.assertIn("AED, INR and USD", message)
		self.assertIn("grand_total is in each row's currency", message)
		self.assertIn(
			"Group by currency, or sum the company-currency field base_grand_total instead.", message
		)

	def test_the_sql_string_aggregate_form_is_caught_too(self):
		with self.assertRaisesRegex(ComputeCodeError, "one SUM mixes currencies"):
			_run(
				f"result = get_list('Sales Invoice', filters={ALL!r}, fields=['sum(grand_total) as t'])[0].t"
			)

	def test_catching_the_refusal_does_not_help(self):
		with self.assertRaisesRegex(ComputeCodeError, "one SUM mixes currencies"):
			_run(
				"try:\n"
				f"    rows = get_list('Sales Invoice', filters={ALL!r}, fields=[{{'SUM': 'grand_total', 'as': 't'}}])\n"
				"except:\n    rows = []\nresult = 0"
			)

	def test_grouped_by_the_currency_field_passes_clean(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={ALL!r}, "
			"fields=['party_account_currency', {'SUM': 'outstanding_amount', 'as': 'due'}], "
			"group_by='party_account_currency')\n"
			"result = {r.party_account_currency: r.due for r in rows}"
		)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["result"], {"AED": 200.0, "INR": 1000.0, "USD": 400.0})
		self.assertEqual(out["reads"][0]["currencies"], ["AED", "INR", "USD"])

	def test_rows_in_several_currencies_warn_first_and_once(self):
		code = (
			f"a = get_list('Sales Invoice', filters={ALL!r}, fields=['name', 'grand_total'])\n"
			f"b = get_list('Sales Invoice', filters={ALL!r}, fields=['grand_total'])\n"
			"result = sum(r.grand_total for r in a)"
		)
		with _count_list_calls() as calls:
			out = _run(code)
		self.assertEqual(next(iter(out)), "warnings")
		self.assertEqual(len(out["warnings"]), 1)
		warning = out["warnings"][0]
		self.assertIn("Sales Invoice rows here are in AED, INR and USD.", warning)
		self.assertIn(
			"grand_total is in each row's currency, so adding it across rows mixes currencies.", warning
		)
		self.assertIn("Total it per currency, or use the company-currency field base_grand_total.", warning)
		self.assertEqual(out["result"], 2150.0)
		# Two reads, each with one grouped currency probe (currency was not selected).
		self.assertEqual(len(calls), 4)

	def test_currency_read_from_the_rows_needs_no_probe(self):
		with _count_list_calls() as calls:
			out = _run(
				f"rows = get_list('Sales Invoice', filters={ALL!r} + [['docstatus', '=', 1]], fields=['currency', 'grand_total'])\n"
				"by = {}\nfor r in rows:\n    by[r.currency] = by.get(r.currency, 0) + r.grand_total\nresult = by"
			)
		self.assertEqual(len(calls), 1)
		self.assertIn("warnings", out)  # still told: the amounts are in several currencies
		self.assertEqual(out["result"], {"AED": 200.0, "INR": 1500.0, "USD": 450.0})

	def test_single_currency_is_clean_and_named_on_the_receipt(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r} + [['currency', '=', 'INR']], "
			"fields=[{'SUM': 'grand_total', 'as': 't'}])\nresult = rows[0].t"
		)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["result"], 1500.0)
		self.assertEqual(out["reads"][0]["currency"], "INR")

	def test_company_currency_sum_across_currencies_is_clean(self):
		# Invoices in INR, USD and AED of ONE company: base_grand_total is all INR. The
		# company and docstatus are named, so no probe runs.
		with _count_list_calls() as calls:
			out = _run(
				f"rows = get_list('Sales Invoice', filters={ALL!r} + [['docstatus', '=', 1]], "
				"fields=['company', {'SUM': 'base_grand_total', 'as': 't'}, {'COUNT': '*', 'as': 'n'}], "
				"group_by='company')\n"
				"result = {r.company: [r.t, r.n] for r in rows}"
			)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["result"], {CO_INR: [14320.0, 4], CO_USD: [350.0, 2]})
		self.assertEqual(len(calls), 1)
		self.assertEqual(out["reads"][0]["currencies"], ["INR", "USD"])

	def test_a_doctype_without_money_fields_costs_no_extra_query(self):
		with _count_list_calls("ToDo") as calls:
			_run("result = len(get_list('ToDo', fields=['*']))")
		self.assertEqual(len(calls), 1)

	def test_the_probe_reads_with_the_users_permissions(self):
		code = (
			f"rows = get_list('Sales Invoice', filters={ALL!r}, fields=[{{'SUM': 'grand_total', 'as': 't'}}])\n"
			"result = rows[0].t"
		)
		with _as(INR_ONLY_USER):
			out = _run(code)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["result"], 1500.0)
		self.assertEqual(out["reads"][0]["currency"], "INR")
		with self.assertRaisesRegex(ComputeCodeError, "AED, INR and USD"):
			_run(code)


class TestReceiptsAndPartials(ComputeTestCase):
	def test_receipts_are_compact(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={INR_CO!r}, fields=['customer', "
			"{'SUM': 'base_grand_total', 'as': 't'}], group_by='customer')\n"
			"n = count('Customer', {'name': ['like', 'CT Cust%']})\n"
			"d = get_doc('Sales Invoice', 'CT-SINV-0003')\n"
			"v = get_value('Sales Invoice', 'CT-SINV-0001', 'grand_total')\n"
			"result = [len(rows), n]"
		)
		self.assertEqual(
			out["reads"],
			[
				{"doctype": "Sales Invoice", "rows": 2, "filters": INR_CO, "currency": "INR"},
				{"doctype": "Customer", "rows": 3, "filters": {"name": ["like", "CT Cust%"]}},
				# CT-SINV-0003: amounts in USD, base_* amounts in its company's INR.
				{"doctype": "Sales Invoice", "rows": 2, "currencies": ["INR", "USD"]},
				{
					"doctype": "Sales Invoice",
					"rows": 1,
					"filters": {"name": "CT-SINV-0001"},
					"currency": "INR",
				},
			],
		)
		# get_doc read a USD invoice and get_value an INR one: told before the numbers.
		self.assertEqual(list(out), ["warnings", "result", "reads", "rows_read", "elapsed_ms"])
		self.assertIn("Sales Invoice records in INR and USD", out["warnings"][0])

	def test_receipts_are_capped(self):
		out = _run("for i in range(40):\n    count('Customer')\nresult = 1")
		self.assertEqual(len(out["reads"]), sandbox.MAX_READ_RECEIPTS + 1)
		self.assertEqual(out["reads"][-1], {"more": 25})

	def test_long_filters_are_cut_on_the_receipt(self):
		names = [f"CT-NOPE-{i:04d}" for i in range(80)]
		out = _run(f"result = len(get_list('Customer', filters={{'name': ['in', {names!r}]}}))")
		self.assertIsInstance(out["reads"][0]["filters"], str)
		self.assertTrue(out["reads"][0]["filters"].endswith("...(cut)"))

	def test_an_explicit_limit_that_is_reached_warns(self):
		out = _run(f"result = len(get_list('Sales Invoice', filters={OURS!r}, fields=['name'], limit=2))")
		self.assertEqual(out["result"], 2)
		self.assertEqual(
			out["warnings"],
			[
				"get_list('Sales Invoice') stopped at its limit of 2 rows; totals from it are partial. Drop "
				"the limit to read every matching row (or add an order_by for a top-N)."
			],
		)
		self.assertEqual((out["reads"][0]["limited"], out["reads"][0]["limit"]), (True, 2))
		roomy = _run(
			f"result = len(get_list('Sales Invoice', filters={OURS!r}, fields=['name'], limit_page_length=50))"
		)
		self.assertNotIn("warnings", roomy)
		self.assertEqual((roomy["reads"][0]["limited"], roomy["reads"][0]["limit"]), (True, 50))

	def test_no_read_returns_fewer_rows_than_match_without_raising(self):
		with patch.object(sandbox, "MAX_ROWS_PER_READ", 4):
			for code in (
				f"result = len(get_list('Sales Invoice', filters={OURS!r}))",
				f"result = len(get_list('Sales Invoice', filters={OURS!r}, limit=100))",
				f"result = len(get_list('Sales Invoice', filters={OURS!r}, pluck='name'))",
			):
				with self.subTest(code=code), self.assertRaisesRegex(ComputeCodeError, "more than 4 rows"):
					_run(code)
			# Exactly at the cap is complete, not cut.
			self.assertEqual(
				_run(
					f"result = len(get_list('Sales Invoice', filters={OURS!r}[:1] + [['name', '!=', 'CT-SINV-0005']]))"
				)["result"],
				4,
			)

	def test_a_report_over_the_read_cap_raises(self):
		name = "CT Compute Cap Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tabSales Invoice` where name like 'CT-SINV-%%'",
				}
			).insert(ignore_permissions=True)
		with patch.object(sandbox, "MAX_ROWS_PER_READ", 2):
			with self.assertRaisesRegex(ComputeCodeError, "returned more than 2 rows"):
				_run(f"result = len(run_report('{name}').rows)")
		out = _run(f"result = len(run_report('{name}').rows)")
		self.assertEqual(out["result"], 5)
		self.assertEqual(out["reads"], [{"report": name, "rows": 5}])

	def test_none_result_is_refused(self):
		with self.assertRaisesRegex(ComputeCodeError, "result is None"):
			_run("result = None")
		with self.assertRaisesRegex(ComputeCodeError, "NaN or Infinity"):
			_run("result = {'ratio': float('inf')}")


class TestSingleOperations(ComputeTestCase):
	"""Review (must fix): one C-level operation must not run long or allocate much."""

	def assertFast(self, code: str, fragment: str):
		started = time.monotonic()
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(code)
		self.assertLess(time.monotonic() - started, 1.0, code)
		self.assertIn(fragment, str(ctx.exception))

	def test_the_reviewers_repros_fail_fast(self):
		self.assertFast("x = 7**9999\ny = x ** 9999\nresult = 1", "This power would make an integer")
		self.assertFast("s = 'x'\n" + "s = s + s\n" * 29 + "result = len(s)", "This + would make text")

	def test_arithmetic_is_sized_before_it_runs(self):
		for code, fragment in (
			("x = 2**60000\nresult = (x * x) > 0", "This * would make an integer"),
			("result = 1 << (10**9)", "This shift would make an integer"),
			("l = [0]\n" + "l = l + l\n" * 21 + "result = len(l)", "This + would make a list"),
			("t = (0,) * 150000\nresult = len(t + t)", "This + would make a list"),
			("s = 'x'\n" + "s += s\n" * 21 + "result = 1", "This + would make text"),
			("b = b'x' * 60000\nresult = len(b + b)", "This + would make text"),
			("result = [0] * 10**9", "Repeating a sequence"),
		):
			with self.subTest(code=code):
				self.assertFast(code, fragment)

	def test_builtins_that_build_big_values(self):
		self.assertFast("result = pow(7, 10**9)", "This power would make an integer")
		self.assertFast("result = pow(7, 10**3000, 10**3000)", "modular pow")
		self.assertEqual(_run("result = pow(7, 2**64, 2**61 - 1) > 0")["result"], True)
		self.assertFast("result = round(1, -10**9)", "round() to more than")
		self.assertFast("result = sum([[0]] * 1000, [])", "use a comprehension")
		self.assertEqual(_run("result = sum([1, 2], 0.5)")["result"], 3.5)
		self.assertFast("r = list(range(100000))\nresult = len(list(zip(*[r] * 20000)))", "through *")

	def test_formatting_widths(self):
		for code in (
			"result = '%1000000000d' % 1",
			"result = '%.1000000000f' % 1.5",
			"result = '%*d' % (10**9, 1)",
			"result = f'{1:>{10**9}}'",
			"x = 1\nw = 10**9\nresult = f'{x:{w}}'",
			"result = f'{1.5:.1000000000f}'",
		):
			with self.subTest(code=code):
				self.assertFast(code, "not available")
		self.assertFast("s = 'x' * 60000\nresult = '%s%s' % (s, s)", "This % format would make text")
		self.assertEqual(_run("result = f'{1234.5:,.2f}|{7:>5}|{0.25:.0%}'")["result"], "1,234.50|    7|25%")

	def test_methods_that_grow_text_or_lists(self):
		for code, fragment in (
			("result = 'x'.ljust(10**9)", "This ljust"),
			("result = 'x'.rjust(10**9)", "This rjust"),
			("result = 'x'.center(10**9)", "This center"),
			("result = '1'.zfill(10**9)", "This zfill"),
			("result = ('\\t' * 1000).expandtabs(10**7)", "This expandtabs"),
			("result = ('a' * 1000).replace('a', 'b' * 99000)", "This replace"),
			("result = ''.join(['x' * 99000] * 1000)", "This join"),
			("result = str.join('', ['x' * 99000] * 1000)", "This join"),
			("result = ('a' * 1000).translate({97: 'x' * 99000})", "This translate"),
			("result = (1).to_bytes(10**9, 'big')", "This to_bytes"),
			("l = [0]\n" + "l.extend(l)\n" * 21 + "result = len(l)", "This extend"),
		):
			with self.subTest(code=code):
				self.assertFast(code, fragment)
		self.assertEqual(_run("result = '-'.join(['a', 'b']) + 'x'.ljust(3, '.')")["result"], "a-bx..")

	def test_writing_out_a_shared_structure(self):
		grow = "x = [1]\nfor i in range(5):\n    x = [x] * 40\n"
		for tail in (
			"result = len(str(x))",
			"result = len(repr(x))",
			"result = len(f'{x}')",
			"result = len(json.dumps(x))",
			"result = len('%s' % (x,))",
			"print(x)\nresult = 1",
			"result = x",
		):
			with self.subTest(tail=tail):
				self.assertFast(grow + tail, "would write more than")
		self.assertTrue(
			_run("result = isinstance('a', str) and str(12) == '12' and str.upper('a') == 'A'")["result"]
		)

	def test_slice_assignment_and_helpers(self):
		self.assertFast("l = [0]\nl[1:] = l\nresult = len(l)", "Slice assignment is not available")
		self.assertFast("result = flt(1.5, 10**9)", "precision above")
		self.assertFast("result = fmt_money(1, 10**9)", "precision above")
		self.assertFast("result = money_in_words(10**30)", "below a quadrillion")
		self.assertFast("result = json.dumps([1], indent=10**8)", "indent must be")
		self.assertFast("result = datetime.datetime.now().strftime('%Y' * 1000)", "strftime pattern")

	def test_int_to_text_stays_limited(self):
		# Python's own 4,300-digit limit for int <-> str is never lifted.
		self.assertFast("result = str(10**5000)", "Exceeds the limit")
		self.assertFast("result = int('9' * 4400)", "Exceeds the limit")
		self.assertFast("result = int('9' * 90000)", "This int() would make an integer")

	def test_dates_format_and_parse(self):
		out = _run(
			"d = getdate('2026-10-05')\n"
			"result = [d.strftime('%Y-%m'), str(datetime.datetime.strptime('05/10/2026', '%d/%m/%Y').date())]"
		)
		self.assertEqual(out["result"], ["2026-10", "2026-10-05"])

	def test_memory_is_checked_between_statements(self):
		growth = iter(range(0, 10**12, 400 * 1024 * 1024))
		with (
			patch.object(sandbox, "_rss_bytes", side_effect=lambda: next(growth)),
			patch.object(sandbox, "MEMORY_CHECK_INTERVAL_S", 0),
		):
			with self.assertRaisesRegex(ComputeCodeError, "too much memory"):
				_run("a = 1\nb = 2\nc = 3\nresult = a + b + c")

	def test_memory_is_checked_after_a_big_operation(self):
		calls = []

		def rss():
			calls.append(1)
			return 0 if len(calls) == 1 else sandbox.MAX_MEMORY_GROWTH_BYTES + 1

		with (
			patch.object(sandbox, "_rss_bytes", side_effect=rss),
			patch.object(sandbox, "MEMORY_CHECK_INTERVAL_S", 3600),
		):
			with self.assertRaisesRegex(ComputeCodeError, "too much memory"):
				_run("l = [0] * 150000\nresult = len(l)")


class TestQualifiedNames(ComputeTestCase):
	"""Review (must fix): the currency guard must see a table-qualified field."""

	def test_the_reviewers_repro_is_refused(self):
		with self.assertRaisesRegex(ComputeCodeError, "one SUM mixes currencies"):
			_run(
				f"rows = get_list('Sales Invoice', filters={OURS!r}, "
				"fields=[{'SUM': '`tabSales Invoice`.grand_total', 'as': 't'}])\nresult = rows[0].t"
			)

	def test_every_spelling_is_resolved(self):
		for field in (
			# The spellings Frappe 16 accepts (it refuses an unquoted qualifier with a space).
			"{'sum': '`tabSales Invoice`.`grand_total`', 'as': 't'}",
			"{'SUM': '`tabSales Invoice`.grand_total'}",
			"'sum(`tabSales Invoice`.grand_total) as t'",
			"'SUM(grand_total) AS t'",
		):
			with (
				self.subTest(field=field),
				self.assertRaisesRegex(ComputeCodeError, "one SUM mixes currencies"),
			):
				_run(f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=[{field}])\nresult = 1")

	def test_a_qualified_selected_field_warns(self):
		for field in ("'`tabSales Invoice`.grand_total as gt'", "'`tabSales Invoice`.`grand_total` AS gt'"):
			with self.subTest(field=field):
				out = _run(
					f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['name', {field}])\nresult = len(rows)"
				)
				self.assertIn("grand_total is in each row's currency", out["warnings"][0])

	def test_a_child_table_amount_on_a_parent_read_uses_the_parents_currency(self):
		out = _run(
			"rows = get_list('Sales Invoice', filters=[['name', 'in', ['CT-SINV-0001', 'CT-SINV-0003']]], "
			"fields=['name', '`tabSales Invoice Item`.amount'])\nresult = len(rows)"
		)
		self.assertIn("amount is in each row's Sales Invoice currency", out["warnings"][0])
		self.assertIn("INR and USD", out["warnings"][0])


class TestChildTableReads(ComputeTestCase):
	"""Review: a child-table read's amounts are in the parent's currency."""

	CODE = (
		"rows = get_list('Sales Invoice Item', parent_doctype='Sales Invoice', filters={filters}, "
		"fields=[{{'SUM': 'amount', 'as': 't'}}])\nresult = rows[0].t"
	)

	def test_a_sum_across_parent_currencies_is_refused(self):
		with self.assertRaisesRegex(ComputeCodeError, "INR and USD") as ctx:
			_run(self.CODE.format(filters="{'item_code': 'CT-ITEM'}"))
		self.assertIn("Sales Invoice currency", str(ctx.exception))

	def test_one_parent_currency_is_clean(self):
		out = _run(self.CODE.format(filters="{'item_code': 'CT-ITEM', 'parent': 'CT-SINV-0001'}"))
		self.assertEqual(out["result"], 1000.0)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["reads"][0]["currency"], "INR")

	def test_the_probe_uses_the_users_permissions(self):
		# This user sees CT Cust A only, whose invoices (and so items) are all in INR.
		with _as(INR_ONLY_USER):
			out = _run(self.CODE.format(filters="{'item_code': 'CT-ITEM'}"))
		self.assertEqual(out["result"], 1000.0)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["reads"][0]["currency"], "INR")

	def test_child_rows_warn(self):
		out = _run(
			"rows = get_list('Sales Invoice Item', parent_doctype='Sales Invoice', "
			"filters=[['item_code', '=', 'CT-ITEM']], fields=['parent', 'amount'])\nresult = len(rows)"
		)
		self.assertEqual(next(iter(out)), "warnings")
		self.assertIn("amount is in each row's Sales Invoice currency", out["warnings"][0])


class TestDraftRows(ComputeTestCase):
	"""Review: a submittable read that does not name docstatus also totals drafts."""

	PAIR = "[['name', 'in', ['CT-SINV-0001', 'CT-DRAFT-0007']]]"

	def test_a_sum_including_drafts_is_refused(self):
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(
				f"rows = get_list('Sales Invoice', filters={self.PAIR}, fields=[{{'SUM': 'grand_total', 'as': 't'}}])\nresult = 1"
			)
		self.assertIn(
			"1 draft or cancelled Sales Invoice rows match these filters; add docstatus: 1 for submitted, "
			"0 for drafts, or ['in', [0, 1]] for both.",
			str(ctx.exception),
		)

	def test_naming_docstatus_is_clean(self):
		for condition, total in ((["docstatus", "=", 1], 1000.0), (["docstatus", "in", [0, 1]], 1070.0)):
			with self.subTest(condition=condition):
				out = _run(
					f"rows = get_list('Sales Invoice', filters={self.PAIR} + [{condition!r}], "
					"fields=[{'SUM': 'grand_total', 'as': 't'}])\nresult = rows[0].t"
				)
				self.assertNotIn("warnings", out)
				self.assertEqual(out["result"], total)
		grouped = _run(
			f"rows = get_list('Sales Invoice', filters={self.PAIR}, fields=['docstatus', "
			"{'SUM': 'grand_total', 'as': 't'}], group_by='docstatus')\nresult = {r.docstatus: r.t for r in rows}"
		)
		self.assertEqual(grouped["result"], {"0": 70.0, "1": 1000.0})

	def test_rows_including_drafts_warn(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={self.PAIR}, fields=['name', 'grand_total'])\nresult = len(rows)"
		)
		self.assertIn("1 draft or cancelled Sales Invoice rows match", out["warnings"][0])

	def test_no_message_when_no_draft_matches(self):
		out = _run(
			f"result = get_list('Sales Invoice', filters={INR_CO!r} + [['currency', '=', 'INR']], "
			"fields=[{'SUM': 'grand_total', 'as': 't'}])[0].t"
		)
		self.assertNotIn("warnings", out)


class TestCompanies(ComputeTestCase):
	"""Review: base_* totals across companies with different currencies."""

	def test_a_base_sum_across_company_currencies_is_refused(self):
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(
				f"result = get_list('Sales Invoice', filters={OURS!r}, fields=[{{'SUM': 'base_grand_total', 'as': 't'}}])[0].t"
			)
		self.assertIn(
			"Sales Invoice rows here span companies in INR and USD. base_grand_total is in each row's company "
			"currency, so one SUM mixes currencies. Filter by company, or group by company.",
			str(ctx.exception),
		)

	def test_filtered_or_grouped_by_company_is_clean(self):
		out = _run(
			f"result = get_list('Sales Invoice', filters={INR_CO!r}, fields=[{{'SUM': 'base_grand_total', 'as': 't'}}])[0].t"
		)
		self.assertEqual((out["result"], out["reads"][0]["currency"]), (9800.0, "INR"))
		self.assertNotIn("warnings", out)

	def test_base_rows_across_companies_warn(self):
		out = _run(
			f"result = len(get_list('Sales Invoice', filters={OURS!r}, fields=['name', 'base_grand_total']))"
		)
		self.assertIn("span companies in INR and USD", out["warnings"][0])

	def test_one_probe_per_read(self):
		with _count_list_calls() as calls:
			_run(
				f"result = len(get_list('Sales Invoice', filters={OURS!r}, fields=['name', 'grand_total', 'base_grand_total']))"
			)
		self.assertEqual(len(calls), 2)  # the read and one grouped probe for currency, company and docstatus


class TestReports(ComputeTestCase):
	"""Review: a prepared copy says how old it is; with no copy the report runs inline."""

	NAME = "CT Compute Prepared Report"

	def _report(self, prepared: int):
		if not frappe.db.exists("Report", self.NAME):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": self.NAME,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name as invoice from `tabSales Invoice` where name like 'CT-SINV-%%'",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("Report", self.NAME, "prepared_report", prepared)

	def test_no_finished_copy_runs_inline_and_starts_nothing(self):
		from rq import Queue

		self._report(1)
		before = frappe.db.count("Prepared Report")
		with patch.object(Queue, "enqueue_call") as sent:
			out = _run(f"r = run_report('{self.NAME}')\nresult = [len(r.rows), r.prepared, r.as_of]")
		self.assertEqual(out["result"], [5, False, None])
		self.assertEqual(out["reads"], [{"report": self.NAME, "rows": 5}])
		self.assertEqual(frappe.db.count("Prepared Report"), before)
		sent.assert_not_called()

	def test_a_finished_copy_reports_its_age(self):
		self._report(1)
		copy = {
			"columns": [{"fieldname": "invoice", "label": "Invoice", "fieldtype": "Data"}],
			"result": [{"invoice": "CT-SINV-0001"}],
			"as_of": "2026-11-01 09:30:00",
			"run": "PR-1",
		}
		with patch("jarvis.tools._prepared_reports.peek", return_value=("ready", copy)):
			out = _run(f"r = run_report('{self.NAME}')\nresult = [len(r.rows), r.prepared, r.as_of]")
		self.assertEqual(out["result"], [1, True, "2026-11-01 09:30:00"])
		self.assertEqual(
			out["reads"][0],
			{"report": self.NAME, "rows": 1, "prepared": True, "as_of": "2026-11-01 09:30:00"},
		)

	def test_a_report_past_the_deadline_says_narrow_the_filters(self):
		self._report(0)

		def slow(*args, **kwargs):
			end = time.monotonic() + 5
			while time.monotonic() < end:
				dict()  # Python calls, so the deadline can stop it
			return {"columns": [], "result": []}

		with (
			patch.object(sandbox, "WALL_CLOCK_S", 0.3),
			patch.object(sandbox, "GRACE_S", 0.1),
			patch("jarvis.tools.run_report.frappe_run_report", slow),
		):
			with self.assertRaisesRegex(ComputeCodeError, "did not finish within .* Narrow its filters"):
				_run(f"result = len(run_report('{self.NAME}').rows)")


class TestReviewSmallItems(ComputeTestCase):
	def test_a_limit_with_order_by_is_a_top_n(self):
		out = _run(
			f"result = len(get_list('Sales Invoice', filters={OURS!r}, fields=['name'], order_by='name desc', limit=2))"
		)
		self.assertNotIn("warnings", out)
		self.assertEqual((out["reads"][0]["limited"], out["reads"][0]["limit"]), (True, 2))

	def test_did_you_mean(self):
		with self.assertRaisesRegex(ComputeCodeError, "Did you mean base_grand_total"):
			_run(f"result = get_list('Sales Invoice', filters={OURS!r}, fields=['base_grand_totl'])")
		with self.assertRaisesRegex(ComputeCodeError, "Did you mean Sales Invoice"):
			_run("result = get_list('Sales Invoce')")

	def test_private_doctypes_by_any_spelling(self):
		with _as(SYS_USER):
			for doctype in (
				"jarvis conversation",
				"  Jarvis   Conversation ",
				"jarvis_settings",
				"MCP OAuth Client",
				"MCP OAuth Token",
				"Jarvis Connector",
				"OAuth Bearer Token",
				"Token Cache",
			):
				for code in (f"result = get_meta('{doctype}')", f"result = count('{doctype}')"):
					with (
						self.subTest(code=code),
						self.assertRaisesRegex(PermissionDeniedError, "not available here"),
					):
						_run(code)

	def test_kill_switch_values(self):
		for value, off in (
			(True, True),
			(1, True),
			("1", True),
			("true", True),
			("Yes", True),
			("on", True),
			("maybe", True),
			(2, True),
			(False, False),
			(0, False),
			("0", False),
			("false", False),
			("No", False),
			("off", False),
			("", False),
			(None, False),
		):
			with self.subTest(value=value), patch.dict(frappe.local.conf, {"jarvis_disable_compute": value}):
				if off:
					with self.assertRaises(FeatureDisabledError):
						_run("result = 1")
				else:
					self.assertEqual(_run("result = 1")["result"], 1)

	def test_floats_are_tidied(self):
		out = _run("result = [0.1 + 0.2, 12345.67 + 0.000000000002, 83.123456789, 1e-20, 3]")
		self.assertEqual(out["result"], [0.3, 12345.67, 83.123456789, 1e-20, 3])

	def test_json_safe_keeps_decimals_as_numbers(self):
		import decimal

		from jarvis.tools.compute import _json_safe

		self.assertEqual(_json_safe({"d": decimal.Decimal("1.25")}), {"d": 1.25})


class TestIteration2Lambdas(ComputeTestCase):
	"""Review 2 (high): lambdas and generators driven from C must stop at the deadline."""

	def assertStops(self, code: str):
		started = time.monotonic()
		with patch.object(sandbox, "WALL_CLOCK_S", 1), self.assertRaises(ComputeCodeError):
			_run(code)
		self.assertLess(time.monotonic() - started, 3.0, code)

	def test_the_reviewers_repros(self):
		self.assertStops("r = range(1000000)\nresult = max(map(lambda a: max(map(abs, r)), r))")
		self.assertStops("r = list(range(100000))\nresult = len(sorted(r, key=lambda a: max(r)))")

	def test_callables_called_from_c(self):
		r = "r = list(range(100000))\n"
		for code in (
			r + "result = len(list(map(lambda a: sum(r), r)))",
			r + "result = len(list(filter(lambda a: sum(r) < 0, r)))",
			r + "result = min(r, key=lambda a: sum(r))",
			r + "result = any(map(lambda a: sum(r) < 0, r))",
			r + "result = sum(sum(r) for a in r)",
			r + "def slow(a):\n    return sum(r)\nresult = len(list(map(slow, r)))",
			r + "result = sum(map(sum, [r] * 100000))",
			"f = lambda n: f(n - 1) + f(n - 1) if n else 1\nresult = f(40)",
		):
			with self.subTest(code=code):
				self.assertStops(code)


class TestIteration2Reports(ComputeTestCase):
	"""Review 2: a failing report gives its own error, never a write refusal."""

	def test_a_report_with_a_bad_column(self):
		name = "CT Compute Broken Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select no_such_column from `tabSales Invoice`",
				}
			).insert(ignore_permissions=True)
		before = frappe.db.count("Error Log")
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(f"result = len(run_report('{name}').rows)")
		message = str(ctx.exception)
		self.assertIn(f"run_report('{name}') failed", message)
		self.assertIn("no_such_column", message)
		self.assertNotIn("change the database", message)
		self.assertEqual(frappe.db.count("Error Log"), before)

	def test_a_report_that_throws_after_logging(self):
		name = "CT Compute Probe Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tabSales Invoice`",
				}
			).insert(ignore_permissions=True)

		def run(report_name, filters=None, **kwargs):
			frappe.log_error(title="report failed", message="details")
			frappe.throw("Please set the Company filter")

		before = frappe.db.count("Error Log")
		with (
			patch("jarvis.tools.run_report.frappe_run_report", run),
			self.assertRaises(ComputeCodeError) as ctx,
		):
			_run(f"result = len(run_report('{name}').rows)")
		self.assertIn("Please set the Company filter", str(ctx.exception))
		self.assertIn("get_report_filters", str(ctx.exception))
		self.assertEqual(frappe.db.count("Error Log"), before)

	def test_other_writes_are_still_refused(self):
		def run(report_name, filters=None, **kwargs):
			frappe.db.sql("insert into `tabError Log Copy` (name) values ('x')")

		with (
			patch("jarvis.tools.run_report.frappe_run_report", run),
			self.assertRaisesRegex(InvalidArgumentError, "read-only"),
		):
			_run("result = len(run_report('CT Compute Probe Report').rows)")


class TestIteration2SingleOperations(ComputeTestCase):
	"""Review 2 (medium): comparisons, hashing and big ints in one C call."""

	DEEP = "a = [1]\nb = [1]\nfor i in range(5):\n    a = [a] * 40\n    b = [b] * 40\n"
	DEEP_T = "a = (1,)\nb = (1,)\nfor i in range(5):\n    a = (a,) * 40\n    b = (b,) * 40\n"
	COLLIDE = "M = 2**61 - 1\nkeys = [M * i for i in range(1, 60001)]\n"
	LONG = "s = 'x' * 100000\nt = 'x' * 99999 + 'y'\nl = [s] * 200000\n"

	def assertFast(self, code: str, fragment: str):
		started = time.monotonic()
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(code)
		self.assertLess(time.monotonic() - started, 1.0, code)
		self.assertIn(fragment, str(ctx.exception))

	def test_deep_shared_structures(self):
		# Review 3: 60 doubling levels are refused when built (TestIteration3); a wide
		# structure under the nesting cap still reaches the cost checks.
		for tail in (
			"result = a == b",
			"result = a < b",
			"result = len(sorted([a, b]))",
			"result = len(max([a, b]))",
			"result = len(min(a, b))",
			"result = [a].count(b)",
			"result = [a, b].index(b)",
			"result = b in [a]",
			"l = [a, b]\nl.sort()\nresult = 1",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.DEEP + tail, "could take a very long time")
		for tail in ("d = {a: 1}\nresult = d.get(b)", "d = {}\nd[a] = 1\nresult = 1", "result = {a: 1}[b]"):
			with self.subTest(tail=tail):
				self.assertFast(self.DEEP_T + tail, "could take a very long time")

	def test_keys_that_all_hash_alike(self):
		for tail in (
			"result = len(set(keys))",
			"result = len(dict.fromkeys(keys))",
			"result = len(dict(zip(keys, keys)))",
			"s = set()\ns.update(keys)\nresult = len(s)",
			"result = len({1} | set(keys))",
			"result = len(set([(k,) for k in keys]))",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.COLLIDE + tail, "hash alike")

	def test_long_text_repeated(self):
		for tail in (
			"result = l.count(t)",
			"result = t in l",
			"result = l == [t] * 200000",
			"l.sort()\nresult = 1",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.LONG + tail, "could take a very long time")

	def test_big_ints(self):
		self.assertFast("result = int('f' * 1000000, 16) % 3", "Repeating a sequence")
		self.assertFast("result = int('f' * 90000, 16) > 0", "This int() would make an integer")
		self.assertFast("result = int.from_bytes(b'x' * 10000, 'big') > 0", "This from_bytes")
		out = _run(
			"a = int('f' * 16000, 16)\nb = int('e' * 8000, 16)\n"
			"result = [a % b > 0, a // b > 0, divmod(a, b)[1] > 0]"
		)
		self.assertEqual(out["result"], [True, True, True])  # no "No imports: _pylong"

	def test_ordinary_work_stays_fast(self):
		out = _run(
			"names = [f'C{i}' for i in range(100000)]\n"
			"seen = set(names)\n"
			"hits = sum(1 for n in names[:2000] if n in names[:2000])\n"
			"rows = [{'c': n, 't': i} for i, n in enumerate(names)]\n"
			"top = sorted(rows, key=lambda r: -r['t'])[:3]\n"
			"result = [len(seen), hits, top[0]['c'], max(r['t'] for r in rows), min(names)]"
		)
		self.assertEqual(out["result"], [100000, 2000, "C99999", 99999, "C0"])


class TestIteration2Totals(ComputeTestCase):
	"""Review 2 (low): silent wrong totals."""

	PAIR = "[['name', 'in', ['CT-SINV-0001', 'CT-DRAFT-0007']]]"

	def test_or_filters_on_docstatus_still_probe(self):
		with self.assertRaisesRegex(ComputeCodeError, "draft or cancelled"):
			_run(
				f"rows = get_list('Sales Invoice', filters={self.PAIR}, "
				"or_filters=[['docstatus', '=', 1], ['customer', '=', 'CT Cust A']], "
				"fields=[{'SUM': 'grand_total', 'as': 't'}])\nresult = rows[0].t"
			)

	def test_record_by_record_amounts_in_several_currencies_warn(self):
		out = _run(
			"a = get_doc('Sales Invoice', 'CT-SINV-0001')\n"
			"b = get_value('Sales Invoice', 'CT-SINV-0003', 'grand_total')\n"
			"result = a.grand_total + b"
		)
		self.assertEqual(next(iter(out)), "warnings")
		self.assertIn("Sales Invoice records in INR and USD", out["warnings"][0])
		self.assertEqual(out["reads"][1]["currency"], "USD")
		clean = _run(
			"result = get_value('Sales Invoice', 'CT-SINV-0001', 'grand_total') + "
			"get_value('Sales Invoice', 'CT-SINV-0002', 'grand_total')"
		)
		self.assertNotIn("warnings", clean)

	def test_json_text_filters(self):
		self.assertEqual(
			_run(
				f"result = count('Sales Invoice', {json.dumps(json.dumps({'name': ['like', 'CT-SINV-%']}))})"
			)["result"],
			5,
		)
		self.assertEqual(
			_run(
				'result = len(get_list(\'Sales Invoice\', filters=\'[["name", "like", "CT-SINV-%"]]\', '
				"fields='[\"name\"]'))"
			)["result"],
			5,
		)
		with self.assertRaisesRegex(ComputeCodeError, "filters must be a dict or a list"):
			_run("result = count('Sales Invoice', 'CT-SINV-0001')")


class TestIteration2Access(ComputeTestCase):
	"""Review 2: Password fields do not make a doctype private; their values never come back."""

	SECRET = "Ct-compute-Secr3t-value!"

	def test_password_values_never_come_back(self):
		from frappe.utils.password import update_password

		update_password(ACC_USER, self.SECRET)
		name = "CT Compute User Report"
		if not frappe.db.exists("Report", name):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "User",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name, new_password from `tabUser`",
				}
			).insert(ignore_permissions=True)
		out = _run(
			f"a = get_list('User', filters={{'name': '{ACC_USER}'}}, fields=['*'])\n"
			f"b = get_doc('User', '{ACC_USER}')\n"
			f"d = run_report('{name}')\n"
			"result = [a, b, d.rows, [c.fieldname for c in d.columns]]"
		)
		self.assertNotIn(self.SECRET, json.dumps(out, default=str))
		self.assertNotIn("new_password", json.dumps(out, default=str))
		# Naming a Password field is refused outright (review 3).
		for code in (
			f"result = get_list('User', filters={{'name': '{ACC_USER}'}}, fields=['name', 'new_password'])",
			f"result = get_value('User', '{ACC_USER}', 'new_password')",
		):
			with self.subTest(code=code), self.assertRaisesRegex(PermissionDeniedError, "hold credentials"):
				_run(code)
		self.assertEqual(_run("result = count('User') > 0")["result"], True)

	def test_get_list_on_a_single_doctype_teaches(self):
		with self.assertRaisesRegex(ComputeCodeError, "single doctype .* get_single_value"):
			_run("result = get_list('System Settings')")


class TestIteration2Floats(ComputeTestCase):
	def test_noise_goes_digits_stay(self):
		from jarvis.tools.compute import _tidy_float

		for value, expected in (
			(12345.670000000002, 12345.67),
			(0.1 + 0.2, 0.3),
			(66987822.72000001, 66987822.72),
			(2.9999999999999996, 3.0),
			(12345678901234.56, 12345678901234.56),
			(123456789012345.67, 123456789012345.67),
			(999999999999999.9, 999999999999999.9),
			(0.012345678, 0.012345678),
			(83.123456789, 83.123456789),
			(100.00001, 100.00001),
			(1e-20, 1e-20),
		):
			with self.subTest(value=value):
				self.assertEqual(repr(_tidy_float(value)), repr(expected))
		out = _run("result = [0.1 + 0.2, 12345678901234.56, 0.012345678]")
		self.assertEqual(out["result"], [0.3, 12345678901234.56, 0.012345678])


class TestIteration3(ComputeTestCase):
	"""Review 3: stale comparison costs, starred unpacking, nesting, credential fields."""

	# Under the nesting cap, but comparing two copies visits 40**5 (about 10**8) values.
	WIDE = "a = [0]\nb = [0]\nfor i in range(5):\n    a = [a] * 40\n    b = [b] * 40\n"
	WIDE_T = "a = (0,)\nb = (0,)\nfor i in range(5):\n    a = (a,) * 40\n    b = (b,) * 40\n"
	DEEP = "a = [1]\nfor i in range(60):\n    a = [a, a]\n"
	COLLIDE = "M = 2**61 - 1\nkeys = [1 + M * i for i in range(5000)]\n"

	def assertFast(self, code: str, fragment: str):
		started = time.monotonic()
		with self.assertRaises(ComputeCodeError) as ctx:
			_run(code)
		self.assertLess(time.monotonic() - started, 1.0, code)
		self.assertIn(fragment, str(ctx.exception))

	# N1 -----------------------------------------------------------------------

	def test_a_mutated_list_is_measured_again(self):
		base = "l = [1, 2, 3]\nm = [1, 2, 3]\nfirst = l == m\n" + self.WIDE
		for mutate in (
			"l.pop()\nl.append(a)\nm.pop()\nm.append(b)\n",
			"l.insert(0, a)\nl.pop()\nm.insert(0, b)\nm.pop()\n",
			"l.remove(3)\nl.append(a)\nm.remove(3)\nm.append(b)\n",
			"l.reverse()\nl[0] = a\nm.reverse()\nm[0] = b\n",
			"del l[0]\nl.append(a)\ndel m[0]\nm.append(b)\n",
			"l.clear()\nl.extend([a, 1, 2])\nm.clear()\nm.extend([b, 1, 2])\n",
		):
			with self.subTest(mutate=mutate):
				self.assertFast(base + mutate + "result = l == m", "could take a very long time")

	def test_a_mutated_dict_or_set_is_measured_again(self):
		dicts = "d = {1: 1}\ne = {1: 1}\nfirst = d == e\n" + self.WIDE
		for mutate in (
			"d.popitem()\nd.setdefault(1, a)\ne.popitem()\ne.setdefault(1, b)\n",
			"d.update({1: a})\ne.update({1: b})\n",
			"d.pop(1)\nd[1] = a\ne.pop(1)\ne[1] = b\n",
		):
			with self.subTest(mutate=mutate):
				self.assertFast(dicts + mutate + "result = d == e", "could take a very long time")
		sets = "s = {1}\nt = {1}\nfirst = s == t\n" + self.WIDE_T
		for mutate in (
			"s.discard(1)\ns.add(a)\nt.discard(1)\nt.add(b)\n",
			"s.clear()\ns.update([a])\nt.clear()\nt.update([b])\n",
		):
			with self.subTest(mutate=mutate):
				self.assertFast(sets + mutate + "result = s == t", "could take a very long time")

	def test_a_parent_is_measured_again_when_a_child_changes(self):
		code = (
			"x = [1, 2]\ny = [1, 2]\np = [x]\nq = [y]\nfirst = p == q\n"
			+ self.WIDE
			+ "x.append(a)\ny.append(b)\nresult = p == q"
		)
		self.assertFast(code, "could take a very long time")

	# N2 -----------------------------------------------------------------------

	def test_starred_displays_are_capped_before_they_are_built(self):
		for code in (
			"r = range(1000000)\nresult = len([*r, *r])",
			"r = range(150000)\nresult = len([*r, *r])",
			"r = range(150000)\nresult = len((*r, *r))",
			"r = range(150000)\nresult = len({*r, *r, -1, *range(150000, 300000)})",
			"r = list(range(150000))\nresult = len([0, *r, *r])",
		):
			with self.subTest(code=code):
				self.assertFast(code, "the limit is 200,000")
		self.assertFast(self.COLLIDE + "result = len({*keys})", "hash alike")

	def test_starred_calls_are_capped_before_the_call(self):
		for code in (
			"r = range(1000000)\nresult = max(*r)",
			"r = range(20000)\nprint(*r)\nresult = 1",
			"x = [(i, i) for i in range(20000)]\nresult = len(list(zip(*x)))",
			"d = {f'k{i}': i for i in range(20000)}\nresult = dict(**d)",
		):
			with self.subTest(code=code):
				self.assertFast(code, "arguments")

	def test_dict_unpacking_is_capped(self):
		self.assertFast(
			"d = dict.fromkeys(range(150000))\ne = dict.fromkeys(range(150000, 300000))\nresult = len({**d, **e})",
			"the limit is 200,000",
		)
		self.assertFast(
			self.COLLIDE
			+ "d = dict.fromkeys(keys[:900])\ne = dict.fromkeys(keys[900:1800])\nf = dict.fromkeys(keys[1800:2700])\n"
			"result = len({**d, **e, **f})",
			"This ** unpacking would put",
		)

	def test_collisions_add_up_across_calls(self):
		halves = "a = keys[:900]\nb = keys[900:1800]\n"
		for tail in (
			"d = dict.fromkeys(a)\nd.update(dict.fromkeys(b))\nresult = len(d)",
			"s = set(a)\ns.update(b)\nresult = len(s)",
			"result = len(set(a).union(b))",
			"result = len(set(a) | set(b))",
			"result = len(set(a) ^ set(b))",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.COLLIDE + halves + tail, "hash alike")

	def test_starred_still_works(self):
		out = _run(
			"a = [1, 2]\nb = (3,)\nd = {'x': 1}\n"
			"def f(*args, **kwargs):\n    return [len(args), sorted(kwargs)]\n"
			"result = [[*a, *b], (*a, 0), sorted({*a, *b}), {**d, 'y': 2}, f(*a, 9, *b, k=1, **d),"
			" max(*a), list(zip(*[(1, 2), (3, 4)]))]"
		)
		self.assertEqual(
			out["result"],
			[[1, 2, 3], [1, 2, 0], [1, 2, 3], {"x": 1, "y": 2}, [4, ["k", "x"]], 2, [[1, 3], [2, 4]]],
		)

	# N3 -----------------------------------------------------------------------

	def test_deep_containers_cannot_be_built(self):
		for code in (
			self.DEEP + "result = 1",
			"a = (1,)\nfor i in range(60):\n    a = (a, a)\nresult = 1",
			"a = {}\nfor i in range(60):\n    a = {'k': a}\nresult = 1",
			"a = [1]\nfor i in range(60):\n    a = [a for _ in range(2)]\nresult = 1",
			"a = [1]\nfor i in range(60):\n    b = []\n    b.append(a)\n    a = b\nresult = 1",
			"a = [1]\nfor i in range(60):\n    b = [0]\n    b[0] = a\n    a = b\nresult = 1",
			"a = [1]\nfor i in range(60):\n    a = list([a])\nresult = 1",
			"a = [1]\nfor i in range(60):\n    a = sorted([a])\nresult = 1",
			"a = {}\nfor i in range(60):\n    b = {}\n    b.setdefault('k', a)\n    a = b\nresult = 1",
			"a = {}\nfor i in range(60):\n    b = {}\n    b.update(k=a)\n    a = b\nresult = 1",
			"a = 1\nfor i in range(60):\n    a = frappe._dict(k=a)\nresult = 1",
			"result = json.loads('[' * 50 + ']' * 50)",
		):
			with self.subTest(code=code):
				self.assertFast(code, "levels deep")

	def test_comparisons_through_methods_and_views(self):
		for tail in (
			"l = [a]\nl.remove(b)\nresult = 1",
			"result = (a, 1).count(b)",
			"result = (a, 1).index(b)",
			"result = {1: a}.items() == {1: b}.items()",
			"result = {1: a}.items() <= {1: b}.items()",
			"result = [a] == [b]",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.WIDE_T + tail, "could take a very long time")
		for tail in (
			"result = {a}.issubset({b})",
			"result = len({a} - {b})",
			"result = len({a: 1}.keys() & {b: 1}.keys())",
			"result = len(set([a, b]))",
		):
			with self.subTest(tail=tail):
				self.assertFast(self.WIDE_T + tail, "could take a very long time")

	def test_keys_nesting_a_colliding_int_deeply(self):
		code = (
			"M = 2**61 - 1\nkeys = []\nfor i in range(2500):\n    k = 1 + M * i\n"
			"    for j in range(7):\n        k = (k,)\n    keys.append(k)\nresult = len(set(keys))"
		)
		self.assertFast(code, "alike")

	def test_large_distinct_ints_are_fine(self):
		out = _run("s = set(range(2**62, 2**62 + 3000))\nd = dict.fromkeys(s)\nresult = [len(s), len(d)]")
		self.assertEqual(out["result"], [3000, 3000])

	def test_loop_targets_cannot_store_into_containers(self):
		for code in (
			"d = {}\nfor d['k'] in [1]:\n    pass",
			"d = {}\nresult = [0 for d['k'] in [1]]",
			"l = [0, 0]\nl[0], l[1] = 1, 2",
		):
			with self.subTest(code=code), self.assertRaisesRegex(ComputeCodeError, "cannot store into"):
				_run(code)

	def test_ordinary_nested_results_still_work(self):
		out = _run(
			"top = [{'customer': f'C{i}', 'aging': {'0-30': i, '31-60': [i, {'n': i}]}} for i in range(5)]\n"
			"rows = get_list('Sales Invoice', filters=" + repr(OURS) + ", fields=['name', 'customer'])\n"
			"by = {}\nfor r in rows:\n    by.setdefault(r.customer, []).append(r)\n"
			"again = json.loads(json.dumps({'top5': top}))\n"
			"t = {}\nt[('a', 1)] = [1, 2]\nt[('a', 1)].append({'x': [1]})\n"
			"result = {'top5': top[:2], 'groups': len(by), 'same': again == {'top5': top}, 't': t[('a', 1)]}"
		)
		self.assertEqual(out["result"]["groups"], 3)
		self.assertTrue(out["result"]["same"])
		self.assertEqual(
			out["result"]["top5"][1], {"customer": "C1", "aging": {"0-30": 1, "31-60": [1, {"n": 1}]}}
		)
		self.assertEqual(out["result"]["t"], [1, 2, {"x": [1]}])

	# N4 -----------------------------------------------------------------------

	def test_credential_fields_are_never_read(self):
		frappe.db.set_value(
			"User", ACC_USER, "reset_password_key", "ct-secret-reset-key", update_modified=False
		)
		for user in (SYS_USER, "Administrator"):
			with _as(user):
				doc = _run(f"result = get_doc('User', '{ACC_USER}')")["result"]
				self.assertNotIn("reset_password_key", doc)
				self.assertNotIn("api_key", doc)
				self.assertEqual(doc["name"], ACC_USER)
				star = _run(f"result = get_list('User', filters={{'name': '{ACC_USER}'}}, fields=['*'])")[
					"result"
				]
				self.assertNotIn("reset_password_key", star[0])
				for code in (
					f"result = get_value('User', '{ACC_USER}', 'reset_password_key')",
					f"result = frappe.db.get_value('User', '{ACC_USER}', ['name', 'api_key'])",
					"result = get_list('User', filters={'reset_password_key': ['like', 'ct-%']})",
					"result = get_list('User', or_filters=[['reset_password_key', 'like', 'ct-%']])",
					"result = get_list('User', fields=['name', 'reset_password_key'])",
					"result = get_list('User', fields=['name'], order_by='reset_password_key desc')",
					"result = get_list('User', fields=['api_key', {'COUNT': '*', 'as': 'n'}], group_by='api_key')",
					"result = count('User', {'api_secret': ['is', 'set']})",
					"result = exists('User', {'reset_password_key': 'ct-secret-reset-key'})",
				):
					with self.subTest(user=user, code=code):
						with self.assertRaisesRegex(PermissionDeniedError, "hold credentials"):
							_run(code)

	def test_secret_named_fields_on_any_doctype(self):
		self.assertIn("plaid_access_token", sandbox._secret_fields("Bank"))
		self.assertIn("client_secret", sandbox._secret_fields("Google Settings"))
		self.assertNotIn("otp_issuer_name", sandbox._secret_fields("System Settings"))
		with self.assertRaisesRegex(PermissionDeniedError, "plaid_access_token"):
			_run("result = get_list('Bank', filters={'plaid_access_token': ['is', 'set']})")


class TestIteration4(ComputeTestCase):
	"""Review 4 (low): the cost cache's memory, credential fields the patterns miss."""

	def test_repeated_builtins_over_one_list_keep_memory_flat(self):
		for code in (
			"l = list(range(100000))\nt = 0\nfor i in range(900):\n    t += max(l)\nresult = t",
			"l = list(range(100000))\nt = 0\nfor i in range(300):\n    t += max(x for x in l)\nresult = t",
			"l = list(range(100000))\nt = 0\nfor i in range(300):\n    t += sorted(l)[-1]\nresult = t",
		):
			with self.subTest(code=code):
				self.assertGreater(_run(code)["result"], 0)

	def test_max_with_a_key_many_times(self):
		out = _run(
			"rows = [{'a': i % 997, 'b': i} for i in range(10000)]\nt = 0\nfor i in range(300):\n"
			"    t += max(rows, key=lambda r: r['a'])['b']\nresult = t"
		)
		self.assertEqual(out["result"], 300 * 996)

	def test_the_cost_cache_is_bounded(self):
		from jarvis.tools import _compute_guards

		guards = _compute_guards.Guards(lambda: None)
		for _ in range(200):
			guards.compare(list(range(1000)))
		self.assertLessEqual(len(guards._costs), _compute_guards.CACHE_MAX_ENTRIES)
		guards._keys(list(range(1000)), None, "x")  # a copy the guards made: never cached
		self.assertLessEqual(guards._cached_cost, _compute_guards.CACHE_MAX_COST)

	def test_more_credential_fields(self):
		for doctype, field in (
			("Website Settings", "indexing_authorization_code"),
			("Currency Exchange Settings Details", "value"),
			("Integration Request", "data"),
			("Integration Request", "output"),
			("Integration Request", "error"),
			("Integration Request", "request_headers"),
		):
			with self.subTest(field=field):
				self.assertIn(field, sandbox._secret_fields(doctype))
		self.assertIn("value", sandbox._secret_fields_reachable("Currency Exchange Settings"))
		with _as(SYS_USER):
			for code in (
				"result = get_list('Integration Request', fields=['name', 'output'])",
				"result = get_list('Integration Request', filters={'data': ['like', '%key%']})",
				"result = get_list('Currency Exchange Settings Details', parent_doctype='Currency Exchange Settings',"
				" fields=['key', 'value'])",
				"result = get_value('Website Settings', None, 'indexing_authorization_code')",
			):
				with (
					self.subTest(code=code),
					self.assertRaisesRegex(PermissionDeniedError, "hold credentials"),
				):
					_run(code)
