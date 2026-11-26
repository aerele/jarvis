"""Tests for the ``compute`` tool (jarvis.tools.compute / _compute_sandbox).

Fixture data is inserted raw (``db_insert``, no controller) so the tests exercise the
reads, not ERPNext's invoice logic: three customers, two companies, invoices in INR
and USD. Everything is rolled back with the test class.
"""

from __future__ import annotations

import sys
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
		_user(ACC_USER, ["Accounts User"])
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
			f"rows = get_list('Sales Invoice', filters={OURS!r}, "
			"fields=['customer', {'SUM': 'base_grand_total', 'as': 'total'}, {'COUNT': '*', 'as': 'n'}], "
			"group_by='customer', order_by='customer asc')\n"
			"result = {r.customer: [r.total, r.n] for r in rows}"
		)
		self.assertEqual(
			out["result"],
			{CUST_A: [1500.0, 2], CUST_B: [8600.0, 2], CUST_C: [50.0, 1]},
		)
		self.assertEqual(out["rows_read"], 3)
		self.assertEqual(out["printed"], "")
		self.assertIsInstance(out["elapsed_ms"], int)

	def test_the_sql_string_aggregate_form_is_accepted_too(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['sum(base_grand_total) as total'])\n"
			"result = rows[0].total"
		)
		self.assertEqual(out["result"], 10150.0)

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
			"result = {'date': from_doc, 'dec': Decimal('1.25'), 'dt': datetime.datetime(2026, 1, 2, 3, 4, 5), "
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
		self.assertRefused("result = len('x' * 10 ** 9)", fragment="repeating a sequence")
		self.assertRefused("s = 'x'\ns *= 10 ** 9\nresult = 1", fragment="repeating a sequence")
		self.assertRefused("result = 2 ** (10 ** 9)", fragment="integer powers")
		self.assertRefused("result = pow(7, 10 ** 9)", fragment="integer powers")
		self.assertRefused("result = 1 << (10 ** 9)", fragment="shifts")


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
					f"result = get_list('Sales Invoice', filters={OURS!r}, fields=[{{'SUM': 'base_grand_total', 'as': 't'}}])[0].t"
				)
				self.assertEqual(readable["result"], 10150.0)
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
				"def forever():\n    while True:\n        yield 1\nresult = max(forever())",
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
				f"rows = get_list('Sales Invoice', filters={ALL!r}, fields=['currency', 'grand_total'])\n"
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
		with _count_list_calls() as calls:
			out = _run(
				f"rows = get_list('Sales Invoice', filters={ALL!r}, "
				"fields=[{'SUM': 'base_grand_total', 'as': 't'}, {'COUNT': '*', 'as': 'n'}])\n"
				"result = [rows[0].t, rows[0].n]"
			)
		self.assertNotIn("warnings", out)
		self.assertEqual(out["result"], [14670.0, 6])
		self.assertEqual(len(calls), 1)
		self.assertNotIn("currency", out["reads"][0])

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
	def test_receipts_describe_each_read(self):
		out = _run(
			f"rows = get_list('Sales Invoice', filters={OURS!r}, fields=['customer', "
			"{'SUM': 'base_grand_total', 'as': 't'}], group_by='customer')\n"
			"n = count('Customer', {'name': ['like', 'CT Cust%']})\n"
			"d = get_doc('Sales Invoice', 'CT-SINV-0003')\n"
			"v = get_value('Sales Invoice', 'CT-SINV-0001', 'grand_total')\n"
			"result = [len(rows), n]"
		)
		first, second, third, fourth = out["reads"]
		self.assertEqual(
			first,
			{
				"source": "get_list",
				"doctype": "Sales Invoice",
				"filters": OURS,
				"fields": ["customer", {"SUM": "base_grand_total", "as": "t"}],
				"group_by": "customer",
				"rows": 3,
			},
		)
		self.assertEqual(
			(second["source"], second["doctype"], second["rows"], second["count"]),
			("count", "Customer", 1, 3),
		)
		self.assertEqual(
			(third["source"], third["name"], third["currency"]), ("get_doc", "CT-SINV-0003", "USD")
		)
		self.assertEqual(
			(fourth["source"], fourth["fields"], fourth["rows"]), ("get_value", ["grand_total"], 1)
		)

	def test_receipts_are_capped(self):
		out = _run("for i in range(40):\n    count('Customer')\nresult = 1")
		self.assertEqual(len(out["reads"]), sandbox.MAX_READ_RECEIPTS + 1)
		self.assertEqual(out["reads"][-1], {"more": 10})

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
				"the limit to read every matching row (or keep it for a top-N on purpose)."
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
		self.assertEqual(out["reads"], [{"source": "run_report", "report": name, "filters": {}, "rows": 5}])

	def test_none_result_is_refused(self):
		with self.assertRaisesRegex(ComputeCodeError, "result is None"):
			_run("result = None")
		with self.assertRaisesRegex(ComputeCodeError, "NaN or Infinity"):
			_run("result = {'ratio': float('inf')}")
