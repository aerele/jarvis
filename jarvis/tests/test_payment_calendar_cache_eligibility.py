"""I1-cache and OC2 for shared rows: a cache generation reaches a user only when the user is filter-free.

S4 serves nothing; this module only defines the rule and its matrix (S5 calls it). Every unknown is a no.
"""

import json
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import cache_eligibility, compatibility, eligibility, sources
from jarvis.payment_calendar.errors import TIMEOUTS
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site
from jarvis.tests.test_payment_calendar_reader_eligibility import unrestricted_fields

RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."
AUDITED_QUERY = RESTRICTION + "get_inherited_permission_query_conditions"
AUDITED_PERMISSION = RESTRICTION + "has_inherited_permission"

GENERATION_DOCTYPE = "Jarvis Payment Calendar Cache Generation"
STORED = frozenset({"Sales Invoice", "Journal Entry", "Expense Claim", "Customer", "Employee"})


def inputs(**overrides):
	"""The inputs of a filter-free user; each matrix row changes one."""
	base = {
		"user_permissions": {},
		"scope_reasons": [],
		"reader_reasons": [],
		"hooks": {"permission_query_conditions": {}, "has_permission": {}},
		"server_script_queries": frozenset(),
		"audited_sources_ok": True,
		"doctypes": STORED,
		"masked": frozenset(),
		"owner_only": frozenset(),
		"unreadable": frozenset(),
	}
	base.update(overrides)
	return base


def query(**by_doctype):
	return {"permission_query_conditions": by_doctype, "has_permission": {}}


def permission(**by_doctype):
	return {"permission_query_conditions": {}, "has_permission": by_doctype}


class TestIneligibilityReasons(TestCase):
	"""The pure rule table, one subtest per row."""

	def test_rule_table(self):
		up = {"doc": "x", "applicable_for": ""}
		rows = [
			("filter-free user", {}, []),
			*(
				(f"User Permission on {doctype}", {"user_permissions": {doctype: [up]}}, ["user_permission"])
				for doctype in ("Department", "Branch", "Industry Type", "Customer", "Sales Person", "Brand")
			),
			(
				"all-doctype Company permission (C2-4: refused, whatever it allows)",
				{"user_permissions": {"Company": [up]}},
				["user_permission"],
			),
			(
				"scoped Company permission",
				{"user_permissions": {"Company": [{"doc": "x", "applicable_for": "Sales Invoice"}]}},
				["user_permission"],
			),
			("empty permission list still counts", {"user_permissions": {"Brand": []}}, ["user_permission"]),
			("permissions of an unknown shape", {"user_permissions": None}, ["user_permission"]),
			("incomplete scope", {"scope_reasons": ["restricted_obligations"]}, ["incomplete_scope"]),
			("reader reasons", {"reader_reasons": ["column_filter"]}, ["reader_ineligible"]),
			(
				"query hook on a stored voucher type",
				{"hooks": query(**{"Expense Claim": ["a.b"]})},
				["query_hook"],
			),
			("query hook on a party type", {"hooks": query(Employee=["a.b"])}, ["query_hook"]),
			("query hook given as a string", {"hooks": query(Employee="a.b")}, ["query_hook"]),
			("query hook on an unrelated doctype", {"hooks": query(ToDo=["a.b"])}, []),
			(
				"query hook on a guarded doctype that is not stored",
				{"hooks": query(Account=["a.b"])},
				["query_hook"],
			),
			("unaudited wildcard query hook", {"hooks": query(**{"*": ["a.b"]})}, ["query_hook"]),
			(
				"unaudited hook next to the audited one",
				{"hooks": query(**{"*": [AUDITED_QUERY, "a.b"]})},
				["query_hook"],
			),
			("audited wildcard query hook", {"hooks": query(**{"*": [AUDITED_QUERY]})}, []),
			(
				"audited wildcard query hook, sources drifted",
				{"hooks": query(**{"*": [AUDITED_QUERY]}), "audited_sources_ok": False},
				["query_hook"],
			),
			("query hook of an unknown shape", {"hooks": query(Employee=[1])}, ["query_hook"]),
			("hooks of an unknown shape", {"hooks": None}, ["query_hook", "permission_hook"]),
			(
				"audited function registered under the other kind",
				{"hooks": query(**{"*": [AUDITED_PERMISSION]})},
				["query_hook"],
			),
			(
				"server script query on a stored voucher type",
				{"server_script_queries": frozenset({"Expense Claim"})},
				["server_script_query"],
			),
			(
				"server script query on a party type",
				{"server_script_queries": frozenset({"Employee"})},
				["server_script_query"],
			),
			(
				"server script query on the wildcard",
				{"server_script_queries": frozenset({"*"})},
				["server_script_query"],
			),
			(
				"server script query on an unrelated doctype",
				{"server_script_queries": frozenset({"ToDo"})},
				[],
			),
			(
				"unaudited has_permission hook on a stored voucher type (I1-cache: the rule comes back)",
				{"hooks": permission(**{"Expense Claim": ["a.b"]})},
				["permission_hook"],
			),
			(
				"unaudited has_permission hook on a party type",
				{"hooks": permission(Employee=["a.b"])},
				["permission_hook"],
			),
			(
				"unaudited wildcard has_permission hook",
				{"hooks": permission(**{"*": ["a.b"]})},
				["permission_hook"],
			),
			("has_permission hook on an unrelated doctype", {"hooks": permission(ToDo=["a.b"])}, []),
			("audited has_permission hook", {"hooks": permission(**{"*": [AUDITED_PERMISSION]})}, []),
			(
				"audited has_permission hook, sources drifted",
				{"hooks": permission(**{"*": [AUDITED_PERMISSION]}), "audited_sources_ok": False},
				["permission_hook"],
			),
			(
				"has_permission hook of an unknown shape",
				{"hooks": permission(Employee=[1])},
				["permission_hook"],
			),
			("a masked field", {"masked": frozenset({"Employee"})}, ["masked_field"]),
			("an owner-only role", {"owner_only": frozenset({"Expense Claim"})}, ["owner_only"]),
			("a doctype the user cannot read", {"unreadable": frozenset({"Employee"})}, ["unreadable"]),
			(
				"several reasons come out in a fixed order",
				{
					"unreadable": frozenset({"Employee"}),
					"hooks": query(Employee=["a.b"]),
					"scope_reasons": ["x"],
					"user_permissions": {"Brand": [{}]},
					"masked": frozenset({"Employee"}),
				},
				["user_permission", "incomplete_scope", "query_hook", "masked_field", "unreadable"],
			),
		]
		for label, overrides, expected in rows:
			with self.subTest(label):
				self.assertEqual(cache_eligibility.ineligibility_reasons(**inputs(**overrides)), expected)

	def test_every_guarded_doctype_is_hook_checked_whatever_the_generation_stores(self):
		for doctype in eligibility.GUARDED_DOCTYPES:
			for kind, expected in (
				("permission_query_conditions", ["query_hook"]),
				("has_permission", ["permission_hook"]),
			):
				with self.subTest(doctype=doctype, kind=kind):
					hooks = {"permission_query_conditions": {}, "has_permission": {}}
					hooks[kind] = {doctype: ["a.b"]}
					found = cache_eligibility.ineligibility_reasons(
						**inputs(hooks=hooks, doctypes=frozenset())
					)
					self.assertEqual(found, expected)

	def test_audited_paths_are_those_of_the_reader_rule(self):
		self.assertIs(cache_eligibility.AUDITED_HOOKS, eligibility.AUDITED_HOOKS)

	def test_the_names_taken_from_eligibility_exist(self):
		"""A rename there would make every user `eligibility_unknown` without a sound; fail here instead."""
		hooks = eligibility.AUDITED_HOOKS
		self.assertIsInstance(hooks, dict)
		self.assertEqual(set(hooks), {"permission_query_conditions", "has_permission"})
		self.assertTrue(all(isinstance(paths, frozenset) and paths for paths in hooks.values()))
		self.assertEqual(
			eligibility._AUDITED_SOURCE_NAMES, tuple(sorted(p for paths in hooks.values() for p in paths))
		)
		self.assertIsInstance(eligibility.GUARDED_DOCTYPES, tuple)
		self.assertIn("*", eligibility.GUARDED_DOCTYPES)
		with patch.object(compatibility, "reader_pin_drift", return_value=[]) as drift:
			self.assertTrue(cache_eligibility.audited_sources_ok())
		drift.assert_called_once_with(eligibility._AUDITED_SOURCE_NAMES)


def generation(side="Receivable", company="Acme", **payload):
	body = {
		"voucher_doctypes": ["Sales Invoice", "Journal Entry", "Expense Claim"],
		"party_doctypes": ["Customer", "Employee"],
		"permission_columns": [{"fieldname": "territory", "fieldtype": "Link", "options": "Territory"}],
	}
	body.update(payload)
	return {"name": "17", "company": company, "side": side, "payload": body}


class TestGenerationShape(TestCase):
	"""The stored row is read strictly: a missing or odd field is `generation_unknown`."""

	def test_reads_the_stored_doctype_sets_and_columns(self):
		shape = cache_eligibility.read_generation(generation(), "Acme")
		self.assertEqual(shape.side, "Receivable")
		self.assertEqual(
			shape.doctypes, {"Sales Invoice", "Journal Entry", "Expense Claim", "Customer", "Employee"}
		)
		self.assertEqual(shape.columns[0]["options"], "Territory")

	def test_a_json_text_payload_is_read(self):
		row = generation()
		row["payload"] = json.dumps(row["payload"])
		self.assertEqual(cache_eligibility.read_generation(row, "Acme").side, "Receivable")

	def test_a_bank_generation_needs_no_doctype_sets(self):
		shape = cache_eligibility.read_generation({**generation("Bank"), "payload": {}}, "Acme")
		self.assertEqual(shape.side, "Bank")
		self.assertEqual(shape.doctypes, set(cache_eligibility.BANK_DOCTYPES))

	def test_a_payload_built_as_the_refresh_will_write_it_round_trips(self):
		columns = [
			{"fieldname": "territory", "fieldtype": "Link", "options": "Territory", "label": "Territory"},
			{"fieldname": "against", "fieldtype": "Dynamic Link", "options": "party_type"},
			{"fieldname": "outstanding", "fieldtype": "Currency"},
		]
		payload = {
			"voucher_doctypes": sorted({"Sales Invoice", "Journal Entry"}),
			"party_doctypes": ["Customer"],
			"permission_columns": sources._permission_columns(columns),
		}
		row = frappe._dict(company="Acme", side="Payable", payload=json.dumps(payload))
		shape = cache_eligibility.read_generation(row, "Acme")
		self.assertEqual(shape.side, "Payable")
		self.assertEqual(shape.doctypes, {"Sales Invoice", "Journal Entry", "Customer"})
		self.assertEqual([c["fieldname"] for c in shape.columns], ["territory", "against"])
		self.assertEqual(shape.columns, payload["permission_columns"])

	def test_a_document_row_is_refused_and_its_as_dict_is_read(self):
		"""The real Cache Generation Document is not a mapping: S5 must pass `row.as_dict()`."""
		require_calendar_test_site()
		if not frappe.db.exists("DocType", GENERATION_DOCTYPE):
			self.skipTest("the Cache Generation DocType is not synced on this site")
		row = generation()
		doc = frappe.new_doc(GENERATION_DOCTYPE)
		doc.company, doc.side, doc.payload = row["company"], row["side"], json.dumps(row["payload"])
		with self.assertRaises(cache_eligibility.UnknownGeneration):
			cache_eligibility.read_generation(doc, "Acme")
		shape = cache_eligibility.read_generation(doc.as_dict(), "Acme")
		self.assertEqual(shape.side, "Receivable")
		self.assertIn("Expense Claim", shape.doctypes)

	def test_unknown_shapes_are_refused(self):
		good = generation()
		rows = [
			("no row", None),
			("not a mapping", "17"),
			("another company", {**good, "company": "Other"}),
			("unknown side", {**good, "side": "Stock"}),
			("no side", {k: v for k, v in good.items() if k != "side"}),
			("no payload", {k: v for k, v in good.items() if k != "payload"}),
			("payload not JSON", {**good, "payload": "{"}),
			("payload not an object", {**good, "payload": "[]"}),
			("no voucher set", {**good, "payload": {"party_doctypes": ["Customer"]}}),
			("no party set", {**good, "payload": {"voucher_doctypes": ["Sales Invoice"]}}),
			(
				"voucher set not a list",
				{**good, "payload": {**good["payload"], "voucher_doctypes": "Sales Invoice"}},
			),
			("doctype not a string", {**good, "payload": {**good["payload"], "party_doctypes": [1]}}),
			("columns not a list", {**good, "payload": {**good["payload"], "permission_columns": {}}}),
		]
		for label, row in rows:
			with self.subTest(label), self.assertRaises(cache_eligibility.UnknownGeneration):
				cache_eligibility.read_generation(row, "Acme")


class TestFilterFree(TestCase):
	"""The live wrapper, with real users on a fixture company."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("cache_eligibility")
		self.fx = CalendarFixtures()
		unrestricted_fields(self)
		pins = patch.object(compatibility, "reader_pin_drift", return_value=[])
		pins.start()
		self.addCleanup(pins.stop)
		restriction = patch("jarvis.permission_hooks._restriction_configured", return_value=False)
		restriction.start()
		self.addCleanup(restriction.stop)
		self.generation = generation(company=self.fx.company)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cache_eligibility")

	def check(self, user=None, row=None):
		return cache_eligibility.filter_free(
			user or frappe.session.user, self.fx.company, self.generation if row is None else row
		)

	def permit(self, user, allow, value, *, applicable_for=""):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": allow,
				"for_value": value,
				"apply_to_all_doctypes": not applicable_for,
				"applicable_for": applicable_for,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)

	def hooks_patch(self, **registered):
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			return registered[kind] if kind in registered else real(kind, *args, **kwargs)

		return patch("frappe.get_hooks", get_hooks)

	def test_administrator_with_no_hooks_is_filter_free(self):
		with self.hooks_patch(permission_query_conditions={}, has_permission={}):
			self.assertEqual(self.check(), (True, ""))

	def test_a_user_permission_of_any_doctype_is_not_filter_free(self):
		customer = self.fx.customer
		for allow, value in (("Customer", customer), ("Company", self.fx.company)):
			with self.subTest(allow):
				frappe.set_user("Administrator")
				user = self.fx.user()
				self.permit(user, allow, value)
				frappe.set_user(user)
				self.assertEqual(self.check(), (False, "user_permission"))

	def test_a_company_permission_that_allows_the_company_is_not_filter_free(self):
		"""C2-4: S2b's accepted shape (an all-doctype Company UP) is refused for shared rows."""
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company)
		frappe.set_user(user)
		self.assertEqual(self.check(), (False, "user_permission"))

	def test_a_scoped_company_permission_is_not_filter_free(self):
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company, applicable_for="Sales Invoice")
		frappe.set_user(user)
		self.assertEqual(self.check(), (False, "user_permission"))

	def test_a_user_that_is_not_the_session_user_is_refused(self):
		other = self.fx.user()
		self.assertEqual(self.check(user=other), (False, "user_mismatch"))

	def test_an_unknown_generation_is_refused(self):
		for row in ({}, {**self.generation, "side": "Stock"}, {**self.generation, "company": "Other"}):
			with self.subTest(row):
				self.assertEqual(self.check(row=row), (False, "generation_unknown"))

	def test_a_query_hook_on_a_stored_voucher_type_is_not_filter_free(self):
		with self.hooks_patch(permission_query_conditions={"Expense Claim": ["a.b"]}, has_permission={}):
			self.assertEqual(self.check(), (False, "query_hook"))

	def test_a_query_hook_on_a_party_type_the_generation_did_not_store_is_not_filter_free(self):
		"""Every Party Type counts (D-6), also one that has no row in the generation."""
		self.assertEqual(cache_eligibility._party_types(), set(frappe.get_all("Party Type", pluck="name")))
		with (
			patch.object(cache_eligibility, "_party_types", return_value=frozenset({"Shareholder"})),
			self.hooks_patch(permission_query_conditions={"Shareholder": ["a.b"]}, has_permission={}),
		):
			self.assertEqual(self.check(), (False, "query_hook"))

	def test_an_unaudited_has_permission_hook_on_a_stored_type_is_not_filter_free(self):
		with self.hooks_patch(permission_query_conditions={}, has_permission={"Expense Claim": ["a.b"]}):
			self.assertEqual(self.check(), (False, "permission_hook"))

	def test_audited_wildcard_hooks_are_filter_free(self):
		with self.hooks_patch(
			permission_query_conditions={"*": [AUDITED_QUERY]}, has_permission={"*": [AUDITED_PERMISSION]}
		):
			self.assertEqual(self.check(), (True, ""))

	def test_a_server_script_query_on_a_stored_type_is_not_filter_free(self):
		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={}),
			patch.object(
				cache_eligibility,
				"get_server_script_map",
				return_value={"permission_query": {"Expense Claim": "narrow"}},
			),
		):
			self.assertEqual(self.check(), (False, "server_script_query"))

	def test_a_masked_field_on_a_stored_doctype_is_not_filter_free(self):
		self.masked = {"Employee": [object()]}
		with self.hooks_patch(permission_query_conditions={}, has_permission={}):
			self.assertEqual(self.check(), (False, "masked_field"))

	def test_an_owner_only_role_on_a_stored_doctype_is_not_filter_free(self):
		real = cache_eligibility.get_role_permissions

		def owner_only(meta, *args, **kwargs):
			perms = dict(real(meta, *args, **kwargs))
			return {**perms, "if_owner": {"read": 1}} if meta == "Expense Claim" else perms

		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={}),
			patch.object(cache_eligibility, "get_role_permissions", owner_only),
		):
			self.assertEqual(self.check(), (False, "owner_only"))

	def test_an_owner_limit_on_read_report_or_select_is_owner_only(self):
		real = cache_eligibility.get_role_permissions
		for key, expected in (("read", True), ("report", True), ("select", True), ("write", False)):

			def limited(meta, *args, _key=key, **kwargs):
				perms = dict(real(meta, *args, **kwargs))
				return {**perms, "if_owner": {_key: 1}} if meta == "Expense Claim" else perms

			with self.subTest(key), patch.object(cache_eligibility, "get_role_permissions", limited):
				self.assertEqual(cache_eligibility._owner_only("Expense Claim", "Administrator"), expected)

	def test_a_user_without_read_on_a_stored_doctype_is_not_filter_free(self):
		user = self.fx.user()
		frappe.set_user(user)
		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={}),
			patch.object(cache_eligibility.frappe, "has_permission", side_effect=self.deny("Employee")),
		):
			self.assertEqual(self.check(), (False, "unreadable"))

	def deny(self, denied):
		real = frappe.has_permission

		def has_permission(doctype=None, ptype="read", *args, **kwargs):
			return False if doctype == denied else real(doctype, ptype, *args, **kwargs)

		return has_permission

	def test_reader_reasons_use_the_stored_columns_and_the_side(self):
		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={}),
			patch.object(
				cache_eligibility.eligibility, "reader_ineligibility", return_value=["column_filter"]
			) as reader,
		):
			self.assertEqual(self.check(), (False, "reader_ineligible"))
		company, account_type, columns = reader.call_args.args
		self.assertEqual((company, account_type), (self.fx.company, "Receivable"))
		self.assertEqual(columns, self.generation["payload"]["permission_columns"])
		self.assertTrue(reader.call_args.kwargs["audited_sources_ok"])

	def test_the_payable_side_reads_the_payable_account_type(self):
		row = generation("Payable", company=self.fx.company)
		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={}),
			patch.object(cache_eligibility.eligibility, "reader_ineligibility", return_value=[]) as reader,
		):
			self.assertEqual(self.check(row=row), (True, ""))
		self.assertEqual(reader.call_args.args[1], "Payable")

	def test_a_bank_generation_skips_the_report_check_and_judges_the_account_doctypes(self):
		row = generation("Bank", company=self.fx.company)
		row["payload"] = {}
		with (
			self.hooks_patch(permission_query_conditions={}, has_permission={"GL Entry": ["a.b"]}),
			patch.object(cache_eligibility.eligibility, "reader_ineligibility") as reader,
		):
			self.assertEqual(self.check(row=row), (False, "permission_hook"))
		reader.assert_not_called()

	def test_a_failed_input_is_unknown_never_filter_free(self):
		for target in (
			"jarvis.payment_calendar.cache_eligibility.get_user_permissions",
			"jarvis.payment_calendar.permissions.incomplete_scope_reasons",
			"jarvis.payment_calendar.eligibility.reader_ineligibility",
			"jarvis.payment_calendar.cache_eligibility.get_server_script_map",
			"jarvis.payment_calendar.cache_eligibility.get_role_permissions",
			"frappe.get_hooks",
			"frappe.has_permission",
			"jarvis.payment_calendar.cache_eligibility.audited_sources_ok",
		):
			with self.subTest(target), patch(target, side_effect=RuntimeError("boom")):
				self.assertEqual(self.check(), (False, "eligibility_unknown"))

	def test_a_job_deadline_is_not_swallowed(self):
		for timeout in TIMEOUTS:
			with (
				self.subTest(timeout),
				patch(
					"jarvis.payment_calendar.cache_eligibility.get_user_permissions", side_effect=timeout()
				),
				self.assertRaises(timeout),
			):
				self.check()
