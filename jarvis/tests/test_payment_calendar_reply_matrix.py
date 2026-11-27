"""Payment Calendar S2 T6: what each kind of user gets from a routed read, against today's read.

Every user is a non-Administrator. Mode `on` with the company on the served list is patched, never saved.
"Above the guard" is `sources.MAX_VOUCHERS = 0`. The oracle is the same user's read with the mode `off` and
the guards lifted (`shadow.lifted_guards`: today's read, ids included), and, for a user the reader must not
serve, the same user's read with the mode `off` above the guard. Each reply is compared as `collect` returned
it and as the stored parts give it back (`store.split_result` then `store.assemble_result`), through
`shadow.canonical_reply`.

Committed fixtures (`consistent_read` rolls back first); users, permissions and keys are removed in `tearDown`.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_reply_matrix
"""

from contextlib import ExitStack, contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import eligibility, read_log, routing, shadow, snapshot, sources, store
from jarvis.payment_calendar import permissions as calendar_permissions
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)
from jarvis.tests.payment_calendar_majors import GATE_NO_MASKED_FIELDS, v16_only

SIDES = ("Payable", "Receivable")
KEY = "k" * 44
HOOKS = "jarvis.tests.test_payment_calendar_reply_matrix"
_hidden: set[str] = set()


def hide_voucher(doc=None, ptype=None, user=None, debug=False):
	"""A has_permission hook that hides the vouchers in `_hidden` (Frappe reads a falsy answer as a denial)."""
	return not (doc is not None and doc.name in _hidden)


def no_narrowing(user=None, doctype=None):
	"""A permission_query_conditions hook that, for the test, narrows nothing."""
	return ""


class MatrixCase(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.fx = CalendarFixtures()
		self.payable = [
			self.fx.invoice((100, 50), side="Payable", paid=30),
			self.fx.invoice((70,), side="Payable"),
		]
		self.receivable = [
			self.fx.invoice((200,), side="Receivable", paid=20),
			self.fx.invoice((90,), side="Receivable"),
		]
		self.fx.fiscal_year()
		self.fx.gl(1000)
		sync_outstanding(self.fx.company)
		self.users = []
		self.settings_patch = patch.object(
			routing,
			"reader_settings",
			side_effect=lambda **_: routing.ReaderSettings(self.mode, (self.fx.company,), ()),
		)
		self.mode = "on"
		self.settings_patch.start()
		self.addCleanup(self.settings_patch.stop)
		# The field gate must not depend on the live permlevels and masks of the shared site.
		self.masked = {}
		for target, kwargs in (
			("jarvis.payment_calendar.eligibility.get_permitted_fields", {"side_effect": permitted_all}),
			(
				"frappe.model.meta.Meta.get_masked_fields",
				{"autospec": True, "side_effect": lambda meta, *a, **k: self.masked.get(meta.name, [])},
			),
		):
			patcher = patch(target, **kwargs)
			patcher.start()
			self.addCleanup(patcher.stop)
		conf = patch.dict(frappe.conf, {"encryption_key": KEY})
		conf.start()
		self.addCleanup(conf.stop)
		self.clean_keys()
		self.addCleanup(self.clean_keys)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		_hidden.clear()
		for user in self.users:
			frappe.db.delete("User Permission", {"user": user})
			frappe.db.delete("Has Role", {"parent": user})
			frappe.db.delete("User", {"name": user})
			frappe.clear_cache(user=user)
		for doctype, name in reversed(self.fx.created):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.cache.hdel("fiscal_years", self.fx.company)
		frappe.clear_cache(doctype="Sales Person")
		frappe.clear_document_cache("Company", self.fx.company)

	def clean_keys(self):
		for number in range(routing.READER_SLOTS):
			frappe.cache.execute_command("DEL", routing.slot_key(number))
		for side in SIDES:
			frappe.cache.execute_command("DEL", routing.memo_key(self.fx.company, side))
		# The day counters the collect `finally` writes (a key per company digest and date; 35 days to live).
		frappe.cache.delete_keys(f"jarvis:pcal:reader:day:{read_log.company_digest(self.fx.company)}")

	# ---------------------------------------------------------------------------------- users

	def new_user(self, *, company_permission=False, permit=None):
		"""A committed Accounts Manager; optionally with a Company permission or one permission on `permit`."""
		user = self.fx.user()
		self.users.append(user)
		frappe.get_doc("User", user).add_roles("Jarvis User")
		if company_permission:
			self.permit(user, "Company", self.fx.company)
		if permit:
			self.permit(user, *permit)
		frappe.db.commit()
		return user

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

	def sales_person(self):
		person = frappe.get_doc(
			{"doctype": "Sales Person", "sales_person_name": self.fx.prefix + " rep", "is_group": 0}
		).insert(ignore_permissions=True)
		self.fx.created.append(("Sales Person", person.name))
		return person.name

	# ------------------------------------------------------------------------------- reads

	def collect(self, user, *, mode, guard, restrictions=()):
		"""`(scope, collect result)` for `user`. `guard`: "above" (MAX_VOUCHERS 0), "lifted" or "none"."""
		frappe.set_user(user)
		try:
			self.mode = mode
			with ExitStack() as stack:
				# The fingerprint covers the hooks, so the restrictions are in force before it is taken.
				for restriction in restrictions:
					stack.enter_context(restriction)
				scope = normalize_scope(self.fx.company, [])
				fingerprint = authorize_scope(scope)["fingerprint"]
				if guard == "above":
					stack.enter_context(patch.object(sources, "MAX_VOUCHERS", 0))
				elif guard == "lifted":
					stack.enter_context(shadow.lifted_guards())
				return scope, snapshot.collect(scope, fingerprint)
		finally:
			frappe.set_user("Administrator")

	def replies(self, scope, result):
		"""The two forms of one reply, canonical."""
		whole = shadow.canonical_reply(result)
		split = shadow.canonical_reply(store.assemble_result(store.split_result(result, scope)))
		return whole, split

	def assert_replies_equal(self, actual, expected, label):
		(actual_scope, actual_result), (expected_scope, expected_result) = actual, expected
		for form, got, want in zip(
			("collect", "split"),
			self.replies(actual_scope, actual_result),
			self.replies(expected_scope, expected_result),
			strict=True,
		):
			self.assertEqual(got, want, f"{label}: the {form} reply differs")

	def rows(self, result, side):
		return result["sources"][side]["rows"]

	@contextmanager
	def spies(self):
		"""The reader, today's permission filter and the permission check, counted."""
		real_reader = routing.builtin_reader.read_obligations
		served = []

		def reader_spy(*args, **kwargs):
			served.append(real_reader(*args, **kwargs))
			return served[-1]

		with (
			patch.object(routing.builtin_reader, "read_obligations", side_effect=reader_spy) as reader,
			patch.object(sources, "_assemble", wraps=sources._assemble) as assemble,
			patch.object(frappe, "has_permission", wraps=frappe.has_permission) as has_permission,
		):
			yield SpyBox(reader, assemble, has_permission, served)

	# -------------------------------------------------------------------------- assertions

	def assert_served_equal(self, user, restrictions=(), expect_rows=True):
		"""The reader serves both sides and the replies equal today's lifted read, ids included."""
		with self.spies() as spy:
			routed = self.collect(user, mode="on", guard="above", restrictions=restrictions)
		self.assertEqual(sorted(call.args[1] for call in spy.reader.call_args_list), list(SIDES))
		self.assertEqual(routed[1]["unavailable"], {})
		today = self.collect(user, mode="off", guard="lifted", restrictions=restrictions)
		self.assertEqual(today[1]["unavailable"], {})
		self.assert_replies_equal(routed, today, "routed vs today unguarded")
		if expect_rows:
			for side in SIDES:
				self.assertTrue(self.rows(routed[1], side), side)
		return routed

	def assert_refused_like_today(self, user, restrictions=(), expect_too_large=True, empty_by_today=()):
		"""`too_large` as today gives it above the guard; the reader returns no row.

		`empty_by_today`: a side the user sees no voucher of, so today's own guard (a count of the user's
		vouchers) does not trip and today serves it, empty, with no reader involved.
		"""
		with self.spies() as spy:
			routed = self.collect(user, mode="on", guard="above", restrictions=restrictions)
		today = self.collect(user, mode="off", guard="above", restrictions=restrictions)
		self.assert_replies_equal(routed, today, "routed vs today above the guard")
		self.assertEqual(spy.served, [], "the reader returned rows")
		for side in SIDES:
			if side in empty_by_today:
				self.assertEqual(self.rows(routed[1], side), [], side)
				continue
			self.assertIsNone(routed[1]["sources"][side], side)
			if expect_too_large:
				self.assertEqual(routed[1]["unavailable"][side], "too_large", side)
		if not empty_by_today:
			spy.assemble.assert_not_called()
		return routed


def permitted_all(doctype, *args, **kwargs):
	return sorted(eligibility.READER_FIELDS[doctype])


class SpyBox:
	def __init__(self, reader, assemble, has_permission, served):
		self.reader, self.assemble, self.has_permission = reader, assemble, has_permission
		self.served = served  # what the reader returned: rows it served


@v16_only(GATE_NO_MASKED_FIELDS)
class TestRoutedReplyMatchesToday(MatrixCase):
	def test_an_unrestricted_user_is_served_and_equal(self):
		self.assert_served_equal(self.new_user())

	def test_a_company_permission_that_allows_the_company_is_refused_like_today(self):
		"""By design in S2: an allowing Company permission adds match filters on the report's Account and
		Project columns, so eligibility refuses (`column_filter`) and today's `too_large` stands."""
		self.assert_refused_like_today(self.new_user(company_permission=True))

	def test_scoped_company_permissions_refuse_before_the_ledger_read(self):
		user = self.new_user()
		self.assertNotEqual(user, "Administrator")
		# Company access stays valid, while invoice details have a separate Company permission.
		for applicable_for in ("Company", "Sales Invoice"):
			self.permit(user, "Company", self.fx.company, applicable_for=applicable_for)
		frappe.db.commit()
		with (
			self.spies() as spy,
			patch.object(
				routing.builtin_reader, "ledger_control", wraps=routing.builtin_reader.ledger_control
			) as ledger,
			patch.object(read_log, "reader_outcomes", wraps=read_log.reader_outcomes) as outcomes,
		):
			routed = self.collect(user, mode="on", guard="above")
		for side in SIDES:
			self.assertIsNone(routed[1]["sources"][side], side)
			self.assertEqual(routed[1]["unavailable"][side], "too_large", side)
		self.assertEqual(spy.served, [])
		spy.assemble.assert_not_called()
		ledger.assert_not_called()
		self.assertEqual(
			{record["side"]: record.get("fallback_detail") for record in outcomes.call_args.args[0]},
			{side: ["scoped_company_permission"] for side in SIDES},
		)

	def test_the_two_company_permissions_leak_case_is_refused_and_the_reader_never_runs(self):
		"""AC-S1 (the S2b plan-check BLOCKER): Company=A for the Company and Company=B for Payment Ledger Entry.

		The user may open company A, but today's ledger read narrows A's lines by the Payment Ledger Entry
		permission (only B), so today's lifted read returns NOTHING of A. The reader must refuse this user up
		front, with the reason `scoped_company_permission`, and read no ledger line. (Without that refusal the
		reader's own control total happens to stop this exact read today, as `control_total`; the explicit
		refusal does not depend on that, and the recorded reason pins it.)
		"""
		other = CalendarFixtures()
		self.fx.created.extend(other.created)
		user = self.new_user()
		self.permit(user, "Company", self.fx.company, applicable_for="Company")
		self.permit(user, "Company", other.company, applicable_for="Payment Ledger Entry")
		frappe.db.commit()

		frappe.set_user(user)
		try:
			scope = normalize_scope(self.fx.company, [])
			for side in SIDES:
				args = (self.fx.company, side, scope["anchor"], scope["window_start"], scope["window_end"])
				self.assertEqual(
					shadow.today_unguarded(*args)["rows"], [], f"today's read of {side} is empty"
				)
		finally:
			frappe.set_user("Administrator")

		with (
			self.spies() as spy,
			patch.object(
				routing.builtin_reader, "ledger_control", wraps=routing.builtin_reader.ledger_control
			) as ledger,
			patch.object(read_log, "reader_outcomes", wraps=read_log.reader_outcomes) as outcomes,
			# Above today's LEDGER-ROW guard: that probe counts every line of the company, not the user's, so
			# it refuses (`too_large`) even though this user sees no voucher (a voucher-count guard would not).
			patch.object(sources, "MAX_LEDGER_ROWS", 0),
		):
			routed = self.collect(user, mode="on", guard="none")
		for side in SIDES:
			self.assertIsNone(routed[1]["sources"][side], side)
			self.assertEqual(routed[1]["unavailable"][side], "too_large", side)
		self.assertEqual(spy.served, [], "the reader returned rows")
		spy.assemble.assert_not_called()
		ledger.assert_not_called()  # eligibility refused before any ledger statement
		records = {record["side"]: record for record in outcomes.call_args.args[0]}
		for side in SIDES:
			self.assertEqual(records[side]["fallback"], "ineligible", side)
			self.assertIn("scoped_company_permission", records[side]["fallback_detail"], side)

	def test_a_company_permission_for_another_company_cannot_open_the_scope(self):
		other = CalendarFixtures()
		self.fx.created.extend(other.created)
		user = self.new_user(permit=("Company", other.company))
		frappe.set_user(user)
		try:
			with self.spies() as spy, self.assertRaises(frappe.PermissionError):
				normalize_scope(self.fx.company, [])
		finally:
			frappe.set_user("Administrator")
		spy.reader.assert_not_called()

	def test_a_customer_permission_is_refused_like_today(self):
		self.assert_refused_like_today(self.new_user(permit=("Customer", self.fx.customer)))

	def test_a_supplier_permission_is_refused_like_today(self):
		self.assert_refused_like_today(self.new_user(permit=("Supplier", self.fx.supplier)))

	def test_a_sales_person_permission_is_refused_like_today(self):
		self.assert_refused_like_today(
			self.new_user(permit=("Sales Person", self.sales_person())), empty_by_today=("Receivable",)
		)

	def test_an_owner_only_user_is_refused_like_today(self):
		real = calendar_permissions.get_role_permissions

		def owner_only(meta, *args, **kwargs):
			return {**real(meta, *args, **kwargs), "if_owner": {"read": 1}}

		self.assert_refused_like_today(
			self.new_user(),
			restrictions=[patch.object(calendar_permissions, "get_role_permissions", owner_only)],
		)

	def test_a_permission_query_hook_is_refused_like_today(self):
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			if kind == "permission_query_conditions":
				return {
					"Sales Invoice": [f"{HOOKS}.no_narrowing"],
					"Purchase Invoice": [f"{HOOKS}.no_narrowing"],
				}
			return real(kind, *args, **kwargs)

		self.assert_refused_like_today(self.new_user(), restrictions=[patch("frappe.get_hooks", get_hooks)])

	def test_a_server_script_permission_query_is_refused_like_today(self):
		self.assert_refused_like_today(
			self.new_user(),
			restrictions=[
				patch.object(
					eligibility,
					"get_server_script_map",
					return_value={"permission_query": {"Sales Invoice": "narrow sales"}},
				)
			],
		)

	def test_a_column_filter_is_refused_like_today(self):
		self.assert_refused_like_today(
			self.new_user(),
			restrictions=[
				patch.object(eligibility, "_column_match_filters", return_value={"Territory": ["India"]})
			],
		)

	def test_a_masked_field_is_refused_like_today(self):
		self.masked = {"Sales Invoice": [frappe._dict(fieldname="outstanding_amount")]}
		self.assert_refused_like_today(self.new_user(), expect_too_large=False)

	def test_a_has_permission_hook_that_hides_an_invoice_is_served_without_it_and_equal(self):
		hidden = self.payable[1]
		_hidden.add(hidden)
		real = frappe.get_hooks

		def get_hooks(kind=None, *args, **kwargs):
			if kind == "has_permission":
				return {"Purchase Invoice": [f"{HOOKS}.hide_voucher"]}
			return real(kind, *args, **kwargs)

		user = self.new_user()
		with self.spies() as spy:
			routed = self.collect(
				user, mode="on", guard="above", restrictions=[patch("frappe.get_hooks", get_hooks)]
			)
		self.assertEqual(sorted(call.args[1] for call in spy.reader.call_args_list), list(SIDES))
		today = self.collect(
			user, mode="off", guard="lifted", restrictions=[patch("frappe.get_hooks", get_hooks)]
		)
		self.assert_replies_equal(routed, today, "routed vs today unguarded")
		self.assertNotIn(hidden, {row["voucher_no"] for row in self.rows(routed[1], "Payable")})
		self.assertIn(self.payable[0], {row["voucher_no"] for row in self.rows(routed[1], "Payable")})
		# The hook runs once per voucher, in this user's session, through the document-level check.
		checked = {
			call.kwargs["doc"]
			for call in spy.has_permission.call_args_list
			if call.args[:1] == ("Purchase Invoice",) and "doc" in call.kwargs
		}
		self.assertEqual(checked, set(self.payable))

	def test_a_user_without_the_receivable_report_gets_forbidden_and_no_receivable_read(self):
		user = self.new_user()
		real = calendar_access_require_report()

		def require_report(company, account_type):
			if account_type == "Receivable":
				raise frappe.PermissionError("no report")
			return real(company, account_type)

		with self.spies() as spy, patch("jarvis.payment_calendar.access.require_report", require_report):
			routed = self.collect(user, mode="on", guard="above")
		self.assertEqual(routed[1]["unavailable"]["Receivable"], "forbidden")
		self.assertIsNone(routed[1]["sources"]["Receivable"])
		self.assertEqual([call.args[1] for call in spy.reader.call_args_list], ["Payable"])

	def test_a_user_without_the_payable_report_is_refused_and_the_reader_is_not_called(self):
		user = self.new_user()

		def require_report(company, account_type):
			raise frappe.PermissionError("no report")

		with self.spies() as spy, patch("jarvis.payment_calendar.access.require_report", require_report):
			with self.assertRaises(frappe.PermissionError):
				self.collect(user, mode="on", guard="above")
		spy.reader.assert_not_called()

	def test_a_permission_revoked_after_the_scope_was_proved_never_reads_through_the_reader(self):
		user = self.new_user()
		frappe.set_user(user)
		scope = normalize_scope(self.fx.company, [])
		fingerprint = authorize_scope(scope)["fingerprint"]
		frappe.set_user("Administrator")
		self.permit(user, "Customer", self.fx.customer)
		frappe.db.commit()
		self.mode = "on"
		frappe.set_user(user)
		try:
			with self.spies() as spy, patch.object(sources, "MAX_VOUCHERS", 0):
				try:
					result = snapshot.collect(scope, fingerprint)
				except ScopeChanged as refused:
					self.assertEqual(refused.code, "scope_changed")
				else:
					for side in SIDES:
						self.assertEqual(result["unavailable"].get(side), "too_large", side)
						self.assertIsNone(result["sources"][side])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(spy.served, [], "the reader returned rows")
		spy.assemble.assert_not_called()


def calendar_access_require_report():
	from jarvis.payment_calendar import access

	return access.require_report
