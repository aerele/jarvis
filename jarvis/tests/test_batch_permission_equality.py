"""The batch permission check returns exactly what `frappe.has_permission(doc=name)` returns per name."""

import functools
from collections import Counter
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import batch_permission, permissions
from jarvis.tests.batch_permission_fixtures import (
	HOOK_PATH,
	HOOK_STATE,
	JE,
	PE,
	PI,
	SI,
	BatchCase,
	clear_plan_memo,
	hooks,
	meta_patched,
	meta_with_field,
	oracle,
	role_rights,
	session_as,
	strict,
	switch_on,
)

DOCTYPES = (SI, PI, JE, "Customer", "Supplier", PE)
BRANCHES = (
	"child_deny",
	"child_deny_empty",
	"share_grant",
	"everyone_share",
	"owner_only",
	"select_read",
	"candidate_deny",
	"candidate_allow",
	"shim_miss",
	"hook_deny",
	"hook_raise",
)


class TestBatchPermissionWrappedBinding(BatchCase):
	def test_a_wrapper_on_has_permission_sends_the_doctype_per_document_and_the_result_is_unchanged(self):
		f = self.f
		real = frappe.permissions.has_permission

		@functools.wraps(real)
		def wrapper(*args, **kwargs):
			return real(*args, **kwargs)

		with switch_on(), session_as(f.u1):
			self.assertEqual(batch_permission.batch_plan(SI).mode, "batch")
		with switch_on(), session_as(f.u1), patch.object(frappe.permissions, "has_permission", wrapper):
			plan = batch_permission.batch_plan(SI)
			names = f.names(SI)
			with patch.object(batch_permission, "_evaluate_batch") as batch:
				got = batch_permission.batch_visible(SI, set(names), "read")
			expected = oracle(SI, names, "read")
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "entry_patched"))
		self.assertEqual(got, expected)
		batch.assert_not_called()


class TestBatchPermissionEquality(BatchCase):
	def test_batch_equals_per_document_check_for_every_user_doctype_mode_and_ptype(self):
		f = self.f
		users = (f.u1, f.u2, f.u3, f.u4, f.u5, f.u6, f.u0)
		with switch_on():
			for user in users:
				for doctype in DOCTYPES:
					for ptype in ("read", "select"):
						for strict_on in (False, True):
							with self.subTest(
								user=user[-18:], doctype=doctype, ptype=ptype, strict=strict_on
							):
								self.assert_equal_case(user, doctype, ptype, strict_on)

	def assert_equal_case(self, user, doctype, ptype, strict_on):
		names = self.f.names(doctype)
		with strict(strict_on), session_as(user):
			expected = oracle(doctype, names, ptype)
			plan = batch_permission.batch_plan(doctype)
			self.assertEqual((plan.mode, plan.reason), ("batch", None))
			with patch.object(batch_permission, "_lazy_doc", wraps=batch_permission._lazy_doc) as lazy:
				result = batch_permission._evaluate_batch(doctype, set(names), ptype, plan)
				got = batch_permission.batch_visible(doctype, set(names), ptype)
			self.assertEqual(result.visible, expected)
			self.assertEqual(got, expected)
			self.assertEqual(lazy.call_count, 0)
			if doctype in (SI, PI, JE, PE):
				self.assertEqual(result.shim_miss, 0)

	def test_scoped_users_take_the_per_document_path_and_still_match(self):
		with switch_on(), session_as(self.f.u7):
			plan = batch_permission.batch_plan(SI)
			self.assertEqual((plan.mode, plan.reason), ("per_doc", "scope_users"))
			names = self.f.names(SI)
			self.assertEqual(
				batch_permission.batch_visible(SI, set(names), "read"), oracle(SI, names, "read")
			)

	def test_fixtures_change_the_result_in_the_expected_documents(self):
		f = self.f
		with switch_on(), strict(False):
			_, off = self.evaluate(f.u1, SI, "read")
		with switch_on(), strict(True):
			_, on = self.evaluate(f.u1, SI, "read")
		visible = {
			f.si_plain,
			f.si_wrong_parentfield,
			f.si_wrong_parenttype,
			f.si_represents_b,
			f.si_twe_a,
			f.si_shared,
			f.si_shared_upper,
			f.si_everyone,
			f.si_restr_a,
			f.si_twe_empty,
			f.si_a1,
		}
		hidden = {
			f.si_company_lower,
			f.si_company_space,
			f.si_b,
			f.si_twe_b,
			f.si_case,
			f.si_restr_b,
			f.si_restr_none,
			f.si_restr_case,
			f.si_share_no_read,
			f.si_other_owner_b,
		}
		self.assertTrue(visible <= off.visible, visible - off.visible)
		self.assertFalse(hidden & off.visible, hidden & off.visible)
		self.assertNotIn(f.si_twe_empty, on.visible)
		with switch_on():
			_, hidden_descendants = self.evaluate(f.u2, SI, "read")
		self.assertNotIn(f.si_a1, hidden_descendants.visible)
		self.assertIn(f.si_plain, hidden_descendants.visible)
		self.assertTrue(visible - {f.si_twe_empty} <= on.visible)

	def test_a_represents_company_that_respects_user_permissions_hides_the_invoice(self):
		f = self.f
		with meta_patched(SI) as meta, switch_on():
			with patch.object(meta.get_field("represents_company"), "ignore_user_permissions", 0):
				plan, result = self.evaluate(f.u1, SI, "read")
				with session_as(f.u1):
					expected = oracle(SI, f.names(SI), "read")
		self.assertEqual((plan.mode, plan.reason), ("batch", None))
		self.assertEqual(result.visible, expected)
		self.assertNotIn(f.si_represents_b, result.visible)

	def test_each_branch_changes_a_verdict_when_its_input_is_removed(self):
		"""For each branch: the real run counts it, and a run without that branch's input differs."""
		f = self.f
		run = batch_permission._Run
		original = {name: getattr(run, name) for name in ("_shares", "_permissions", "_verdict")}
		if_owner_rows = ({"role": f.if_owner_role, "read": 1, "if_owner": 1},)
		hook_paths = {SI: [HOOK_PATH + "deny_listed"], "*": [HOOK_PATH + "raise_permission_listed"]}
		HOOK_STATE.clear()
		HOOK_STATE.update({"deny": {f.si_hook_deny}, "raise": {f.si_hook_order}})

		def only_user_shares(self, chunk):
			return {name: every for name, every in original["_shares"](self, chunk).items() if not every}

		def no_if_owner(self, owner):
			return original["_permissions"](self, owner)[0], {}

		def no_read_recheck(self, doc, ptype):
			return False if ptype == "read" else original["_verdict"](self, doc, ptype)

		def patched(name, replacement):
			return lambda: patch.object(run, name, replacement)

		no_hooks = patched("_hooks_allow", lambda self, doc, ptype: True)
		cases = {
			"child_deny": (f.u1, SI, "read", False, (), {}, patched("_child_rows", lambda self, t, c: {})),
			"child_deny_empty": (f.u1, SI, "read", True, (), {}, lambda: strict(False)),
			"share_grant": (f.u1, SI, "read", False, (), {}, patched("_shares", lambda self, c: {})),
			"everyone_share": (f.u1, SI, "read", False, (), {}, patched("_shares", only_user_shares)),
			"owner_only": (
				f.u4, SI, "read", False, (("Sales Invoice", if_owner_rows),), {}, patched("_permissions", no_if_owner),
			),
			"select_read": (f.u1, "Customer", "select", False, (), {}, patched("_verdict", no_read_recheck)),
			"candidate_deny": (f.u1, SI, "read", False, (), {}, patched("_candidates", lambda self, c: set())),
			"hook_deny": (f.u1, SI, "read", False, (), hook_paths, no_hooks),
			"hook_raise": (f.u1, SI, "read", False, (), hook_paths, no_hooks),
			"shim_miss": (
				f.u1, "Customer", "read", False, (), {},
				lambda: patch.object(permissions, "_visible_names_per_doc", lambda *a: set()),
			),
		}  # fmt: skip
		# candidate_allow cannot change a verdict (the real hook returns True for those documents); the
		# exact candidate counts of test_restricted_master_candidates... pin it instead.
		with switch_on():
			for branch, (user, doctype, ptype, strict_on, grants, extra, variant) in cases.items():
				with self.subTest(branch=branch), strict(strict_on), self.granted(grants), hooks(extra):
					_, real = self.evaluate(user, doctype, ptype)
					with variant():
						_, changed = self.evaluate(user, doctype, ptype)
					self.assertGreaterEqual(real.branches[branch], 1)
					self.assertNotEqual(real.visible, changed.visible)
		HOOK_STATE.clear()

	def test_a_table_multiselect_row_for_another_company_hides_a_supplier(self):
		f = self.f
		# ERPNext marks the company link of Company Restriction as ignoring User Permissions; this one does not.
		with meta_patched("Company Restriction") as meta, switch_on():
			with patch.object(meta.get_field("company"), "ignore_user_permissions", 0):
				_, result = self.evaluate(f.u1, "Supplier", "read")
				with session_as(f.u1):
					expected = oracle("Supplier", f.names("Supplier"), "read")
		self.assertEqual(result.visible, expected)
		self.assertNotIn(f.sup_multiselect_b, result.visible)
		self.assertIn(f.sup_plain, result.visible)
		self.assertGreaterEqual(result.branches["child_deny"], 1)

	def test_if_owner_user_reads_an_own_document_hidden_by_a_user_permission(self):
		f = self.f
		rows = ({"role": f.if_owner_role, "read": 1, "if_owner": 1},)
		with switch_on(), role_rights(SI, *rows):
			_, result = self.evaluate(f.u4, SI, "read")
			with session_as(f.u4):
				expected = oracle(SI, f.si_names, "read")
		self.assertEqual(result.visible, expected)
		self.assertIn(f.si_owner_b, result.visible)
		self.assertIn(f.si_owner_upper_b, result.visible)
		self.assertNotIn(f.si_other_owner_b, result.visible)
		self.assertGreaterEqual(result.branches["owner_only"], 2)

	def test_every_branch_runs_at_least_once_and_each_one_changes_a_result(self):
		f = self.f
		total = Counter()
		select_rows = ({"role": f.select_role, "select": 1, "read": 0},)
		if_owner_rows = ({"role": f.if_owner_role, "read": 1, "if_owner": 1},)
		deny = {"Sales Invoice": [HOOK_PATH + "deny_listed"], "*": [HOOK_PATH + "raise_permission_listed"]}
		HOOK_STATE.clear()
		HOOK_STATE.update({"deny": {f.si_hook_deny}, "raise": {f.si_hook_order}})
		scenarios = [
			(f.u1, SI, "read", True, (), {}),
			(f.u1, SI, "read", False, (), {}),
			(f.u1, "Customer", "read", False, (), {}),
			(f.u1, "Customer", "select", False, (), {}),
			(f.u1, PE, "read", False, (), {}),
			(f.u4, SI, "read", False, (("Sales Invoice", if_owner_rows),), {}),
			(f.u5, "Customer", "select", False, (("Customer", select_rows),), {}),
			(f.u1, SI, "read", False, (), deny),
		]
		with switch_on():
			for user, doctype, ptype, strict_on, grants, extra in scenarios:
				with strict(strict_on), self.granted(grants), hooks(extra):
					_, result = self.evaluate(user, doctype, ptype)
					with session_as(user):
						self.assertEqual(result.visible, oracle(doctype, self.f.names(doctype), ptype))
					total.update(result.branches)
		HOOK_STATE.clear()
		for branch in BRANCHES:
			self.assertGreaterEqual(total[branch], 1, branch)

	@contextmanager
	def granted(self, grants):
		if not grants:
			yield
			return
		((doctype, rows),) = grants
		with role_rights(doctype, *rows):
			yield

	def test_restricted_master_candidates_are_exactly_the_documents_that_point_at_one(self):
		f = self.f
		with switch_on():
			_, si = self.evaluate(f.u1, SI, "read")
			_, pe = self.evaluate(f.u1, PE, "read")
		self.assertEqual((si.branches["candidate_deny"], si.branches["candidate_allow"]), (3, 1))
		self.assertEqual((pe.branches["candidate_deny"], pe.branches["candidate_allow"]), (1, 0))
		self.assertIn(f.pe_same_name_supplier, pe.visible)
		self.assertNotIn(f.pe_restricted_customer, pe.visible)


class TestBatchPermissionHooks(BatchCase):
	def run_hooks(self, user, doctype, ptype, extra, **state):
		HOOK_STATE.clear()
		HOOK_STATE.update(state)
		names = self.f.names(doctype)
		with switch_on(), hooks(extra), session_as(user):
			expected = oracle(doctype, names, ptype)
			with patch.object(batch_permission, "_lazy_doc", wraps=batch_permission._lazy_doc) as lazy:
				got = batch_permission.batch_visible(doctype, set(names), ptype)
		self.assertEqual(got, expected)
		self.assertGreater(lazy.call_count, 0)
		return got

	def tearDown(self):
		HOOK_STATE.clear()

	def test_unknown_hook_denies_one_invoice(self):
		f = self.f
		got = self.run_hooks(f.u1, SI, "read", {SI: [HOOK_PATH + "deny_listed"]}, deny={f.si_hook_deny})
		self.assertNotIn(f.si_hook_deny, got)
		self.assertIn(f.si_plain, got)

	def test_hooks_run_in_frappes_order_so_the_raising_star_hook_hides_a_shared_invoice(self):
		f = self.f
		got = self.run_hooks(
			f.u1,
			SI,
			"read",
			{SI: [HOOK_PATH + "deny_listed"], "*": [HOOK_PATH + "raise_permission_listed"]},
			deny={f.si_hook_order},
			**{"raise": {f.si_hook_order, f.si_hook_raise_shared}},
		)
		self.assertNotIn(f.si_hook_order, got)
		self.assertNotIn(f.si_hook_raise_shared, got)
		self.assertIn(f.si_shared, got)

	def test_a_hook_that_raises_only_for_select_hides_a_document_that_read_would_share(self):
		f = self.f
		rows = ({"role": f.select_role, "select": 1, "read": 0},)
		with role_rights("Customer", *rows):
			got = self.run_hooks(
				f.u5,
				"Customer",
				"select",
				{"Customer": [HOOK_PATH + "raise_on_select"]},
				select_raise={f.cust_select_hook},
			)
		self.assertNotIn(f.cust_select_hook, got)
		self.assertIn(f.cust_shared, got)

	def test_other_hook_errors_propagate_from_both_paths(self):
		f = self.f
		HOOK_STATE.clear()
		HOOK_STATE["value_error"] = {f.si_hook_value_error}
		extra = {SI: [HOOK_PATH + "raise_value_error_listed"]}
		names = set(f.names(SI))
		with switch_on(), hooks(extra), session_as(f.u1):
			with self.assertRaises(ValueError):
				oracle(SI, names, "read")
			with self.assertRaises(ValueError):
				batch_permission.batch_visible(SI, names, "read")


class TestBatchPermissionRawHooks(BatchCase):
	def test_a_user_without_a_company_user_permission_still_evaluates_the_raw_audited_hook_for_candidates(
		self,
	):
		"""The batch reads the raw hooks, as Frappe does; `effective_hooks` would drop this `*` hook for U3."""
		f = self.f
		with switch_on():
			_, result = self.evaluate(f.u3, SI, "read")
			with session_as(f.u3):
				expected = oracle(SI, f.names(SI), "read")
		self.assertEqual(result.visible, expected)
		self.assertGreater(result.branches["candidate_allow"], 0)


class TestBatchPermissionInputsAndSwitch(BatchCase):
	def test_input_spelling_is_exact_and_a_deleted_name_is_not_visible(self):
		f = self.f
		names = {f.si_plain, f.si_plain.lower(), f.si_plain + " ", f.si_b.lower(), f.si_missing}
		with switch_on(), session_as(f.u1):
			expected = oracle(SI, names, "read")
			got = batch_permission.batch_visible(SI, set(names), "read")
			plan = batch_permission.batch_plan(SI)
			result = batch_permission._evaluate_batch(SI, set(names), "read", plan)
		self.assertEqual(got, expected)
		self.assertEqual(got, {f.si_plain, f.si_plain.lower(), f.si_plain + " "})
		self.assertEqual(result.batch_evaluated, 1)

	def test_empty_input_gives_an_empty_set_without_a_statement(self):
		with switch_on(), session_as(self.f.u1), patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			self.assertEqual(batch_permission.batch_visible(SI, set(), "read"), set())
		self.assertEqual(sql.call_count, 0)

	def test_switch_off_runs_only_the_per_document_loop(self):
		f = self.f
		names = set(f.names(SI))
		with session_as(f.u1):
			with patch.object(
				permissions, "_visible_names_per_doc", wraps=permissions._visible_names_per_doc
			) as loop:
				with patch.object(batch_permission, "_evaluate_batch") as batch:
					got = batch_permission.batch_visible(SI, names, "read")
			self.assertEqual(got, oracle(SI, names, "read"))
		self.assertEqual(loop.call_count, 1)
		batch.assert_not_called()

	def test_switch_off_leaves_no_stat_event_or_plan(self):
		f = self.f
		clear_plan_memo()
		frappe.local.pcal_permission_stats = []
		frappe.local.pcal_permission_events = []
		with session_as(f.u1):
			batch_permission.batch_visible(SI, set(f.names(SI)), "read")
		self.assertEqual(frappe.local.pcal_permission_stats, [])
		self.assertEqual(frappe.local.pcal_permission_events, [])
		self.assertFalse(getattr(frappe.local, batch_permission.PLAN_MEMO, None))

	def test_a_call_records_name_free_stats(self):
		f = self.f
		frappe.local.pcal_permission_stats = []
		with switch_on(), session_as(f.u1):
			batch_permission.batch_visible(SI, set(f.names(SI)), "read")
		(stats,) = frappe.local.pcal_permission_stats
		self.assertEqual((stats["permission_mode"], stats["reason"]), ("batch", None))
		self.assertEqual(stats["names"], len(f.names(SI)))
		self.assertNotIn(f.si_plain, repr(stats))


class TestBatchPermissionValueFidelity(BatchCase):
	def check(self, expected_mode, expected_reason=None):
		for strict_on in (False, True):
			with strict(strict_on), session_as(self.f.u1):
				names = self.f.names(SI)
				plan = batch_permission.batch_plan(SI)
				self.assertEqual((plan.mode, plan.reason), (expected_mode, expected_reason))
				with switch_on():
					self.assertEqual(
						batch_permission.batch_visible(SI, set(names), "read"), oracle(SI, names, "read")
					)

	def test_a_virtual_link_to_company_goes_per_document(self):
		field = {
			"fieldname": "jpc_virtual_company",
			"fieldtype": "Link",
			"options": "Company",
			"is_virtual": 1,
		}
		with meta_with_field(SI, **field):
			self.check("per_doc", "no_column")

	def test_a_link_field_without_a_column_that_no_user_permission_names_is_read_as_empty(self):
		field = {
			"fieldname": "jpc_virtual_customer",
			"fieldtype": "Link",
			"options": "Customer",
			"is_virtual": 1,
		}
		with meta_with_field(SI, **field):
			self.check("batch")

	def test_the_real_erpnext_controllers_stay_on_the_batch_path(self):
		with session_as(self.f.u1):
			for doctype in (SI, PI, PE, JE, "Customer", "Supplier"):
				with self.subTest(doctype=doctype):
					plan = batch_permission.batch_plan(doctype)
					self.assertEqual((plan.mode, plan.reason), ("batch", None))

	def test_a_controller_that_adds_a_setup_method_goes_per_document(self):
		lazy = frappe.model.document.get_lazy_controller(SI)
		changed = type("ChangesAField", (lazy,), {"__setup__": lambda self: None})
		with patch.dict(frappe.lazy_controllers[frappe.local.site], {SI: changed}):
			with session_as(self.f.u1):
				plan = batch_permission.batch_plan(SI)
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "controller_override"))
