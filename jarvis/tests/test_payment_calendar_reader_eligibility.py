"""OC2: the built-in reader serves only users for whom the report narrows nothing; unknown means no."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import compatibility, eligibility
from jarvis.payment_calendar.errors import ReaderFallback
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

COMPANY = "Acme"
RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."
AUDITED_QUERY = RESTRICTION + "get_inherited_permission_query_conditions"
AUDITED_PERMISSION = RESTRICTION + "has_inherited_permission"


def inputs(**overrides):
	"""The inputs of an unrestricted Administrator; each test row changes one."""
	base = {
		"company": COMPANY,
		"scope_reasons": [],
		"user_permissions": {},
		"allowed_companies": None,
		"restriction_configured": False,
		"hooks": {"permission_query_conditions": {}, "has_permission": {}},
		"server_script_queries": frozenset(),
		"audited_sources_ok": True,
		"linked_doctypes": frozenset({"Territory", "Customer Group", "Warehouse", "Cost Center"}),
		"column_match_filters": {},
		"masked_report_fields": False,
	}
	base.update(overrides)
	return base


def permitted_all(doctype, *args, **kwargs):
	"""Every field the reader reads is permitted: a stand-in for the live meta, which parallel
	lanes on the shared site may change."""
	return sorted(eligibility.READER_FIELDS[doctype])


def unrestricted_fields(test):
	"""Make the field gate independent of live site meta (permlevels, masks) for this test case.

	`test.masked` maps a doctype to the fields the patched Meta.get_masked_fields reports.
	"""
	test.masked = {}
	for target, kwargs in (
		("jarvis.payment_calendar.eligibility.get_permitted_fields", {"side_effect": permitted_all}),
		(
			"frappe.model.meta.Meta.get_masked_fields",
			{"autospec": True, "side_effect": lambda meta, *a, **k: test.masked.get(meta.name, [])},
		),
	):
		patcher = patch(target, **kwargs)
		patcher.start()
		test.addCleanup(patcher.stop)


class TestIneligibilityReasons(TestCase):
	"""The pure rule table, one subtest per row."""

	def test_rule_table(self):
		rows = [
			("unrestricted administrator", {}, []),
			("incomplete scope", {"scope_reasons": ["restricted_obligations"]}, ["incomplete_scope"]),
			(
				"permission on a linked doctype",
				{"user_permissions": {"Territory": [{}]}},
				["user_permission"],
			),
			(
				"permission on Warehouse, linked from Sales Invoice",
				{"user_permissions": {"Warehouse": [{}]}},
				["user_permission"],
			),
			("permission on an unlinked doctype", {"user_permissions": {"Brand": [{}]}}, []),
			(
				"Company permission allowing the company",
				{"user_permissions": {"Company": [{}]}, "allowed_companies": {COMPANY, "Other"}},
				[],
			),
			(
				"Company permission allowing only another company",
				{"user_permissions": {"Company": [{}]}, "allowed_companies": {"Other"}},
				["company_permission"],
			),
			(
				"Company permission with no allowed company",
				{"user_permissions": {"Company": [{}]}, "allowed_companies": set()},
				["company_permission"],
			),
			(
				"Company permission allowing the company and a restriction",
				{
					"user_permissions": {"Company": [{}]},
					"allowed_companies": {COMPANY},
					"restriction_configured": True,
				},
				["company_restriction"],
			),
			("restriction with no Company permission", {"restriction_configured": True}, []),
			(
				"server script permission query on a guarded doctype",
				{"server_script_queries": frozenset({"Sales Invoice"})},
				["permission_hook"],
			),
			(
				"server script permission query on the wildcard guard",
				{"server_script_queries": frozenset({"*"})},
				["permission_hook"],
			),
			(
				"server script permission query on an unrelated doctype",
				{"server_script_queries": frozenset({"ToDo"})},
				[],
			),
			(
				"unaudited query hook on Sales Invoice",
				{"hooks": {"permission_query_conditions": {"Sales Invoice": ["app.hooks.narrow"]}}},
				["permission_hook"],
			),
			(
				"unaudited query hook given as a string",
				{"hooks": {"permission_query_conditions": {"Sales Invoice": "app.hooks.narrow"}}},
				["permission_hook"],
			),
			(
				"unaudited has_permission hook on Employee (I1: rows pass visible_names)",
				{"hooks": {"has_permission": {"Employee": ["app.hooks.narrow"]}}},
				[],
			),
			(
				"unaudited wildcard has_permission hook (I1: rows pass visible_names)",
				{"hooks": {"has_permission": {"*": ["app.hooks.narrow"]}}},
				[],
			),
			(
				"unaudited wildcard query hook",
				{"hooks": {"permission_query_conditions": {"*": ["app.hooks.narrow"]}}},
				["permission_hook"],
			),
			(
				"unaudited hook next to the audited one",
				{
					"hooks": {
						"permission_query_conditions": {"*": [AUDITED_QUERY, "app.hooks.narrow"]},
					}
				},
				["permission_hook"],
			),
			(
				"audited wildcard hooks with audited sources",
				{
					"hooks": {
						"permission_query_conditions": {"*": [AUDITED_QUERY]},
						"has_permission": {"*": [AUDITED_PERMISSION]},
					}
				},
				[],
			),
			(
				"audited hooks with unaudited sources",
				{
					"hooks": {"permission_query_conditions": {"*": [AUDITED_QUERY]}},
					"audited_sources_ok": False,
				},
				["permission_hook"],
			),
			(
				"audited query function registered as the has_permission kind",
				{"hooks": {"has_permission": {"*": [AUDITED_QUERY]}}},
				[],
			),
			(
				"audited has_permission hooks with unaudited sources",
				{"hooks": {"has_permission": {"*": [AUDITED_PERMISSION]}}, "audited_sources_ok": False},
				[],
			),
			(
				"audited has_permission function registered as the query kind",
				{"hooks": {"permission_query_conditions": {"*": [AUDITED_PERMISSION]}}},
				["permission_hook"],
			),
			(
				"unaudited hook on an unguarded doctype",
				{"hooks": {"has_permission": {"ToDo": ["app.hooks.narrow"]}}},
				[],
			),
			(
				"has_permission hook of an unknown shape",
				{"hooks": {"has_permission": {"Customer": [1]}}},
				[],
			),
			(
				"query hook of an unknown shape",
				{"hooks": {"permission_query_conditions": {"Customer": [1]}}},
				["permission_hook"],
			),
			("hooks of an unknown shape", {"hooks": None}, ["permission_hook"]),
			("column match filters", {"column_match_filters": {"Territory": ["x"]}}, ["column_filter"]),
			("masked or restricted reader fields", {"masked_report_fields": True}, ["column_filter"]),
			(
				"several reasons are sorted",
				{"masked_report_fields": True, "scope_reasons": ["x"], "allowed_companies": set()},
				["column_filter", "company_permission", "incomplete_scope"],
			),
		]
		for label, overrides, expected in rows:
			with self.subTest(label):
				self.assertEqual(eligibility.ineligibility_reasons(**inputs(**overrides)), expected)

	def test_guarded_doctypes_are_each_checked_for_query_hooks(self):
		for doctype in eligibility.GUARDED_DOCTYPES:
			with self.subTest(doctype=doctype):
				hooks = {"permission_query_conditions": {doctype: ["app.hooks.narrow"]}}
				self.assertEqual(
					eligibility.ineligibility_reasons(**inputs(hooks=hooks)), ["permission_hook"]
				)

	def test_no_has_permission_hook_makes_a_user_ineligible(self):
		"""I1: the reader's rows reach a user only through `sources._assemble` -> `visible_names`, which
		runs every `has_permission` hook in the user's own session. A hook on any doctype is therefore
		not an eligibility reason (O1 = A)."""
		for doctype in eligibility.GUARDED_DOCTYPES:
			with self.subTest(doctype=doctype):
				hooks = {"has_permission": {doctype: ["app.hooks.narrow"]}}
				self.assertEqual(eligibility.ineligibility_reasons(**inputs(hooks=hooks)), [])

	def test_the_audited_source_names_are_derived_from_the_audited_hooks(self):
		every_path = {path for paths in eligibility.AUDITED_HOOKS.values() for path in paths}
		self.assertEqual(set(eligibility._AUDITED_SOURCE_NAMES), every_path)
		self.assertEqual(len(eligibility._AUDITED_SOURCE_NAMES), 4)
		self.assertEqual(list(eligibility._AUDITED_SOURCE_NAMES), sorted(every_path))

	def test_audited_hooks_are_the_company_restriction_functions(self):
		self.assertEqual(
			eligibility.AUDITED_HOOKS,
			{
				"permission_query_conditions": {
					RESTRICTION + "get_permission_query_conditions",
					AUDITED_QUERY,
				},
				"has_permission": {RESTRICTION + "has_permission", AUDITED_PERMISSION},
			},
		)


class TestLinkedDoctypes(TestCase):
	def setUp(self):
		require_calendar_test_site()

	def test_header_link_targets_columns_party_types_and_dimensions_are_linked(self):
		columns = [
			{"fieldtype": "Link", "options": "Territory"},
			{"fieldtype": "Dynamic Link", "options": "party_type"},
			{"fieldtype": "Currency"},
		]
		linked = eligibility.linked_doctypes(columns, ["Customer"])
		for doctype in (
			"Warehouse",
			"Territory",
			"Customer",
			"Cost Center",
			"Project",
			"Company",
			"Payment Ledger Entry",
			"Sales Person",
		):
			with self.subTest(doctype):
				self.assertIn(doctype, linked)
		self.assertNotIn("party_type", linked)
		self.assertNotIn("Brand", linked)

	def test_every_reader_field_is_a_real_column(self):
		for doctype, fields in eligibility.READER_FIELDS.items():
			with self.subTest(doctype):
				self.assertLessEqual(fields, set(frappe.get_meta(doctype).get_valid_columns()))


class TestReaderIneligibility(TestCase):
	"""The wrapper, with live permissions on a fixture company."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_eligibility")
		self.fx = CalendarFixtures()
		unrestricted_fields(self)
		pins = patch.object(compatibility, "reader_pin_drift", return_value=[])
		self.pin_drift = pins.start()
		self.addCleanup(pins.stop)
		restriction = patch("jarvis.permission_hooks._restriction_configured", return_value=False)
		restriction.start()
		self.addCleanup(restriction.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_eligibility")

	def reasons(self, columns=()):
		return eligibility.reader_ineligibility(self.fx.company, "Receivable", list(columns))

	def permit(self, user, allow, value):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": allow,
				"for_value": value,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)

	def test_administrator_on_a_fixture_company_is_eligible(self):
		self.assertEqual(self.reasons(), [])

	def test_the_four_audited_sources_are_pin_checked(self):
		self.reasons()
		(names,) = self.pin_drift.call_args.args
		self.assertEqual(len(names), 4)
		self.assertTrue(all(name.startswith(RESTRICTION) for name in names))

	def test_a_pin_drift_of_the_audited_sources_is_ineligible_only_when_a_hook_is_registered(self):
		self.pin_drift.return_value = list(eligibility._AUDITED_SOURCE_NAMES)
		hooks = {"permission_query_conditions": {"*": [AUDITED_QUERY]}, "has_permission": {}}
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			return hooks[kind] if kind in hooks else real(kind, *args, **kwargs)

		with patch("frappe.get_hooks", get_hooks):
			self.assertEqual(self.reasons(), ["permission_hook"])

	def test_a_customer_permission_is_ineligible_with_incomplete_scope(self):
		user = self.fx.user()
		self.permit(user, "Customer", self.fx.customer)
		frappe.set_user(user)
		self.assertEqual(self.reasons(), ["incomplete_scope", "user_permission"])

	def test_a_sales_person_permission_is_ineligible(self):
		user = self.fx.user()
		sales_person = frappe.get_doc(
			{"doctype": "Sales Person", "sales_person_name": self.fx.prefix + " rep", "is_group": 0}
		).insert(ignore_permissions=True)
		self.permit(user, "Sales Person", sales_person.name)
		frappe.set_user(user)
		self.assertEqual(self.reasons(), ["incomplete_scope", "user_permission"])

	def test_a_permission_on_a_linked_header_doctype_is_ineligible(self):
		user = self.fx.user()
		warehouse = frappe.get_all("Warehouse", pluck="name", limit=1)
		if not warehouse:
			self.skipTest("the site has no Warehouse")
		self.permit(user, "Warehouse", warehouse[0])
		frappe.set_user(user)
		self.assertIn("user_permission", self.reasons())

	def test_a_company_permission_allowing_the_company_is_eligible_with_no_report_columns(self):
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company)
		frappe.set_user(user)
		self.assertEqual(self.reasons(), [])

	def test_a_company_permission_allowing_the_company_is_ineligible_with_the_reports_columns(self):
		"""By design in S2: the report's Account and Project columns get a Company match filter."""
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company)
		frappe.set_user(user)
		columns = [
			{"fieldtype": "Link", "options": "Account"},
			{"fieldtype": "Link", "options": "Project"},
		]
		self.assertEqual(self.reasons(columns), ["column_filter"])

	def test_a_company_permission_for_another_company_is_ineligible(self):
		user = self.fx.user()
		self.permit(user, "Company", CalendarFixtures().company)
		frappe.set_user(user)
		self.assertIn("company_permission", self.reasons())

	def test_a_company_permission_with_a_restriction_is_ineligible(self):
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company)
		frappe.set_user(user)
		with patch("jarvis.permission_hooks._restriction_configured", return_value=True):
			self.assertIn("company_restriction", self.reasons())

	def test_match_filters_of_a_column_doctype_are_ineligible(self):
		columns = [{"fieldtype": "Link", "options": "Territory"}]
		with patch(
			"frappe.desk.query_report.get_user_match_filters", return_value={"Territory": ["India"]}
		) as match:
			self.assertEqual(self.reasons(columns), ["column_filter"])
		self.assertEqual(match.call_args.args[0], ["Territory"])

	def test_a_server_script_permission_query_on_a_guarded_doctype_is_ineligible(self):
		with patch.object(
			eligibility,
			"get_server_script_map",
			return_value={"permission_query": {"Sales Invoice": "narrow sales"}},
		):
			self.assertEqual(self.reasons(), ["permission_hook"])
		with patch.object(
			eligibility, "get_server_script_map", return_value={"permission_query": {"ToDo": "x"}}
		):
			self.assertEqual(self.reasons(), [])

	def test_the_live_server_script_map_is_read(self):
		"""A real Server Script with a permission query on a guarded doctype makes the user ineligible."""
		frappe.get_doc(
			{
				"doctype": "Server Script",
				"__newname": self.fx.prefix + "perm",
				"name": self.fx.prefix + "perm",
				"script_type": "Permission Query",
				"reference_doctype": "Sales Invoice",
				"script": "conditions = ''",
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
		frappe.client_cache.delete_value("server_script_map")
		self.addCleanup(frappe.client_cache.delete_value, "server_script_map")
		self.assertEqual(self.reasons(), ["permission_hook"])

	def test_pins_proved_by_the_caller_are_not_checked_again(self):
		for proved in (True, False):
			with self.subTest(proved=proved):
				self.pin_drift.reset_mock()
				self.pin_drift.return_value = list(eligibility._AUDITED_SOURCE_NAMES)
				hooks = {"permission_query_conditions": {"*": [AUDITED_QUERY]}, "has_permission": {}}
				real = frappe.get_hooks

				def get_hooks(kind=None, *args, **kwargs):
					return hooks[kind] if kind in hooks else real(kind, *args, **kwargs)

				with patch("frappe.get_hooks", get_hooks):
					found = eligibility.reader_ineligibility(
						self.fx.company, "Receivable", [], audited_sources_ok=proved
					)
				self.pin_drift.assert_not_called()
				self.assertEqual(found, [] if proved else ["permission_hook"])

	def test_any_unreadable_input_is_unknown(self):
		for target in (
			"frappe.get_hooks",
			"jarvis.payment_calendar.eligibility.get_user_permissions",
			"jarvis.payment_calendar.permissions.incomplete_scope_reasons",
			"jarvis.payment_calendar.eligibility.reader_fields_restricted",
			"jarvis.payment_calendar.eligibility.linked_doctypes",
			"jarvis.payment_calendar.eligibility._column_match_filters",
			"jarvis.permission_hooks._restriction_configured",
		):
			with self.subTest(target), patch(target, side_effect=RuntimeError("boom")):
				self.assert_unknown(RuntimeError)
		with patch.object(eligibility, "get_server_script_map", side_effect=RuntimeError("boom")):
			self.assert_unknown(RuntimeError)
		with patch.object(eligibility, "get_server_script_map", return_value={}):
			self.assert_unknown(KeyError)

	def assert_unknown(self, cause):
		"""The user is ineligible as unknown, and the class of the failure is recoverable."""
		with self.assertRaises(ReaderFallback) as raised:
			self.reasons()
		self.assertEqual(raised.exception.reason, "eligibility_unknown")
		self.assertIsInstance(raised.exception.__cause__, cause)

	def test_an_unreadable_pin_check_is_unknown(self):
		self.pin_drift.side_effect = RuntimeError("boom")
		self.assert_unknown(RuntimeError)

	def test_an_error_in_the_rule_table_is_unknown(self):
		with patch.object(eligibility, "ineligibility_reasons", side_effect=TypeError("boom")):
			self.assert_unknown(TypeError)

	def hooks_patch(self, **registered):
		"""`frappe.get_hooks` with the given hook kinds registered, every other call real."""
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			return registered[kind] if kind in registered else real(kind, *args, **kwargs)

		return patch("frappe.get_hooks", get_hooks)

	def eligible_user(self):
		"""A non-Administrator whose scope is complete: a Company permission that allows the fixture company."""
		user = self.fx.user()
		self.permit(user, "Company", self.fx.company)
		frappe.set_user(user)
		self.assertNotEqual(frappe.session.user, "Administrator")
		self.assertEqual(self.reasons(), [])
		return user

	def test_an_unaudited_wildcard_has_permission_hook_alone_leaves_a_user_eligible(self):
		self.eligible_user()
		with self.hooks_patch(has_permission={"*": ["app.hooks.narrow"]}):
			self.assertEqual(self.reasons(), [])

	def test_an_unaudited_query_hook_makes_a_user_ineligible(self):
		self.eligible_user()
		# The scope rule sees a query hook too, so `incomplete_scope` comes with it.
		for registered in ({"*": ["app.hooks.narrow"]}, {"Sales Invoice": ["app.hooks.narrow"]}):
			with self.subTest(registered), self.hooks_patch(permission_query_conditions=registered):
				self.assertEqual(self.reasons(), ["incomplete_scope", "permission_hook"])

	def test_a_server_script_query_makes_a_non_administrator_ineligible(self):
		self.eligible_user()
		with patch.object(
			eligibility,
			"get_server_script_map",
			return_value={"permission_query": {"Sales Invoice": "narrow sales"}},
		):
			self.assertEqual(self.reasons(), ["permission_hook"])

	def test_any_failed_statement_in_the_eligibility_reads_is_unknown(self):
		for failed in (
			frappe.db.OperationalError(1054, "Unknown column"),
			frappe.db.OperationalError(2013, "lost"),
		):
			with self.subTest(failed.args[0]):
				with patch("jarvis.payment_calendar.eligibility.get_user_permissions", side_effect=failed):
					self.assert_unknown(frappe.db.OperationalError)
				self.pin_drift.side_effect = failed
				self.assert_unknown(frappe.db.OperationalError)
				self.pin_drift.side_effect = None

	def test_a_job_deadline_is_not_an_unknown_state(self):
		from rq.timeouts import JobTimeoutException

		for error in (TimeoutError, JobTimeoutException):
			with self.subTest(error.__name__):
				self.pin_drift.side_effect = error()
				with self.assertRaises(error):
					self.reasons()


class TestReaderFieldsRestricted(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		unrestricted_fields(self)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_unrestricted_administrator(self):
		self.assertFalse(eligibility.reader_fields_restricted())

	def test_a_masked_field_the_reader_reads_is_restricted(self):
		for doctype, fieldname in (
			("Sales Invoice", "outstanding_amount"),
			("Purchase Invoice", "bill_no"),
			("Journal Entry", "due_date"),
			("Payment Entry", "party"),
		):
			with self.subTest(doctype):
				self.masked = {doctype: [frappe._dict(fieldname=fieldname)]}
				self.assertTrue(eligibility.reader_fields_restricted())

	def test_a_masked_field_the_reader_does_not_read_is_not_restricted(self):
		self.masked = {"Sales Invoice": [frappe._dict(fieldname="remarks")]}
		self.assertFalse(eligibility.reader_fields_restricted())

	def test_a_permlevel_restricted_field_the_reader_reads_is_restricted(self):
		for doctype, fieldname in (
			("Sales Invoice", "outstanding_amount"),
			("Purchase Invoice", "credit_to"),
			("Journal Entry", "bill_date"),
			("Payment Entry", "reference_no"),
		):

			def permitted(dt, *args, d=doctype, f=fieldname, **kwargs):
				return [x for x in permitted_all(dt) if not (dt == d and x == f)]

			with self.subTest(doctype), patch.object(eligibility, "get_permitted_fields", permitted):
				self.assertTrue(eligibility.reader_fields_restricted())


class TestReaderFieldsRestrictedThroughFrappe(TestCase):
	"""The real get_permitted_fields path; the permlevel grant is patched, so no shared state changes."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_permlevel")
		self.addCleanup(frappe.db.rollback, save_point="calendar_permlevel")
		self.addCleanup(frappe.set_user, "Administrator")
		patcher = patch("frappe.model.meta.Meta.get_masked_fields", return_value=[])
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_user_without_the_permlevel_of_a_reader_field_is_restricted(self):
		user = CalendarFixtures().user()
		frappe.set_user(user)
		with patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[1]):
			self.assertTrue(eligibility.reader_fields_restricted())

	def test_a_user_with_every_permlevel_is_not_restricted(self):
		user = CalendarFixtures().user()
		frappe.set_user(user)
		with patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0, 1, 2]):
			self.assertFalse(eligibility.reader_fields_restricted())
