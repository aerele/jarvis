"""Each reason that sends a doctype to the per-document loop, the live gate, the stop and the job freeze."""

import ast
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import frappe

from jarvis.payment_calendar import batch_permission, compatibility, permissions, routing
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.batch_permission_fixtures import (
	SI,
	BatchCase,
	meta_patched,
	meta_with_field,
	oracle,
	role_rights,
	session_as,
	switch_on,
)
from jarvis.tests.payment_calendar_majors import GATE_NO_V15_PERMISSION_PINS, v16_only

SALES_TEAM = "Sales Team"


def delegating(real: type, names: dict) -> dict:
	"""Methods that behave as `real` does, so Frappe's own per-document path still works on the class."""
	methods = {}
	for name in names:
		original = getattr(real, name, None)
		methods[name] = (
			lambda original: lambda self, *a, **k: original(self, *a, **k) if original else None
		)(original)
	return methods


class GuardCase(BatchCase):
	def assert_per_doc(self, reason, *, doctype=SI, user=None, names=None, ptype="read"):
		"""The plan names `reason`, no batch runs, and the result is the per-document oracle's."""
		names = set(names if names is not None else self.f.names(doctype))
		with switch_on(), session_as(user or self.f.u1):
			plan = batch_permission.batch_plan(doctype)
			with patch.object(batch_permission, "_evaluate_batch") as batch:
				got = batch_permission.batch_visible(doctype, set(names), ptype)
			expected = oracle(doctype, names, ptype)
		self.assertEqual((plan.mode, plan.reason), ("per_doc", reason))
		batch.assert_not_called()
		self.assertEqual(got, expected)

	def assert_batch(self, doctype=SI, user=None):
		with session_as(user or self.f.u1):
			plan = batch_permission.batch_plan(doctype)
		self.assertEqual((plan.mode, plan.reason), ("batch", None))


@v16_only(GATE_NO_V15_PERMISSION_PINS)
class TestBatchPermissionReasons(GuardCase):
	def test_a_user_with_a_non_company_user_permission(self):
		self.assert_per_doc("scope_users", user=self.f.u7)

	def test_a_child_doctype(self):
		rows = frappe.get_all(
			"Tax Withholding Entry", filters={"parent": ["like", self.f.fx.prefix + "%"]}, pluck="name"
		)
		self.assert_per_doc("table_doctype", doctype="Tax Withholding Entry", names=rows[:3])

	def test_a_single_doctype(self):
		self.assert_per_doc("single_parent", doctype="System Settings", names={"System Settings"})

	def test_a_virtual_parent(self):
		with meta_patched(SI, is_virtual=1):
			self.assert_per_doc("virtual_parent")

	def test_a_computed_table_field_that_no_user_permission_reads(self):
		with meta_patched(SI) as meta, patch.object(meta.get_field("sales_team"), "is_virtual", 1):
			self.assert_per_doc("computed_table")

	def test_a_virtual_child_doctype_on_a_table_that_no_user_permission_reads(self):
		with meta_patched(SALES_TEAM, is_virtual=1):
			self.assert_per_doc("virtual_child")

	def test_a_child_table_that_does_not_exist(self):
		real = batch_permission._table_columns
		with patch.object(
			batch_permission, "_table_columns", lambda t: {} if t == "tab" + SALES_TEAM else real(t)
		):
			self.assert_per_doc("no_table")

	def test_a_link_to_company_without_a_column(self):
		with meta_with_field(SI, fieldname="jpc_virtual", fieldtype="Link", options="Company", is_virtual=1):
			self.assert_per_doc("no_column")

	def test_a_custom_permission_type_named_select(self):
		f = self.f
		rights = ({"role": f.select_role, "select": 1, "read": 0},)
		# Frappe's own per-document path needs a DocShare column for a custom right, so only the plan is checked.
		with (
			role_rights("Customer", *rights),
			patch.object(frappe.permissions, "get_doctype_ptype_map", lambda: {"Customer": ["select"]}),
		):
			with session_as(f.u5):
				plan = batch_permission.batch_plan("Customer")
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "custom_ptype"))

	def test_joined_columns_with_different_collations(self):
		real = batch_permission._table_columns

		def columns(table):
			found = dict(real(table))
			if table == "tabDocShare":
				found["share_name"] = ("varchar", "utf8mb4_bin")
			return found

		with patch.object(batch_permission, "_table_columns", columns):
			self.assert_per_doc("collation")

	def test_the_real_controllers_stay_on_the_batch_path(self):
		for doctype in (SI, "Purchase Invoice", "Payment Entry", "Journal Entry", "Customer", "Supplier"):
			with self.subTest(doctype=doctype):
				self.assert_batch(doctype)


@v16_only(GATE_NO_V15_PERMISSION_PINS)
class TestBatchPermissionControllers(GuardCase):
	def lazy_with(self, **attributes):
		lazy = frappe.model.document.get_lazy_controller(SI)
		changed = type("Changed", (lazy,), delegating(lazy, attributes))
		return patch.dict(frappe.lazy_controllers[frappe.local.site], {SI: changed})

	def child_with(self, **attributes):
		real = frappe.model.document.get_controller(SALES_TEAM)
		changed = type("Changed", (real,), delegating(real, attributes))
		return patch.dict(frappe.controllers[frappe.local.site], {SALES_TEAM: changed})

	def test_a_class_that_overrides_get_or_init_or_setup(self):
		for method in ("get", "__init__", "__setup__"):
			with self.subTest(method=method), self.lazy_with(**{method: True}):
				self.assert_per_doc("controller_override")

	def test_a_child_controller_on_an_irrelevant_table_field_is_guarded(self):
		for method in ("__init__", "__setup__", "get"):
			with self.subTest(method=method), self.child_with(**{method: True}):
				self.assert_per_doc("controller_override")

	def test_a_child_controller_that_defines_no_guarded_method_is_fine(self):
		with self.child_with(unrelated=None):
			self.assert_batch()

	def test_an_extra_attribute_on_the_generated_lazy_class_is_an_override(self):
		lazy = frappe.model.document.get_lazy_controller(SI)
		with patch.object(lazy, "get", lambda self, *a, **k: None, create=True):
			self.assert_per_doc("controller_override")


@v16_only(GATE_NO_V15_PERMISSION_PINS)
class TestBatchPermissionFailClosed(GuardCase):
	def test_any_failure_of_the_pin_check_is_a_pin_drift(self):
		def boom():
			raise RuntimeError("pins")

		for check in (lambda: ["Some.pin"], boom, None):
			with self.subTest(check=check), patch.object(compatibility, "permission_pin_drift", check):
				frappe.local.pcal_permission_events = []
				self.assert_per_doc("pin_drift")
				self.assertEqual(
					frappe.local.pcal_permission_events[0]["operation"], "pcal_permission_pin_drift"
				)

	def test_any_failure_of_the_identity_check_is_entry_patched(self):
		def boom(*args):
			raise RuntimeError("identity")

		for check in (lambda *a: ["4"], boom, None):
			with (
				self.subTest(check=check),
				patch.object(compatibility, "permission_identity_failures", check),
			):
				frappe.local.pcal_permission_events = []
				self.assert_per_doc("entry_patched")
				self.assertEqual(
					frappe.local.pcal_permission_events[0]["operation"], "pcal_permission_identity"
				)

	def test_an_identity_failure_on_the_lazy_class_rows_is_an_override(self):
		with patch.object(compatibility, "permission_identity_failures", lambda *a: ["37:get"]):
			self.assert_per_doc("controller_override")

	def test_an_events_message_carries_no_document_name(self):
		with patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]):
			frappe.local.pcal_permission_events = []
			self.assert_per_doc("pin_drift")
		self.assertNotIn(self.f.si_plain, repr(frappe.local.pcal_permission_events))


class TestBatchPermissionLiveGate(GuardCase):
	def test_the_switch_and_the_trip_flag(self):
		self.assertFalse(batch_permission.switch_enabled())
		with switch_on():
			self.assertTrue(batch_permission.switch_enabled())
			frappe.local.pcal_permission_tripped = True
			try:
				self.assertFalse(batch_permission.switch_enabled())
				self.assertEqual(batch_permission._live_gate(SI), "switch_off")
			finally:
				frappe.local.pcal_permission_tripped = False

	def test_a_breaker_key_survives_a_cache_clear_and_a_redis_error_fails_closed(self):
		doctype = "JPC Breaker Doctype"
		key = frappe.cache.make_key(batch_permission.BREAKER_KEY + doctype)
		with switch_on():
			self.assertIsNone(batch_permission._live_gate(doctype))
			frappe.cache.execute_command("SET", key, "now|test")
			try:
				frappe.clear_cache()
				self.assertEqual(batch_permission._live_gate(doctype), "breaker")
			finally:
				frappe.cache.execute_command("DEL", key)
			with patch.object(frappe.cache, "execute_command", side_effect=ConnectionError):
				self.assertEqual(batch_permission._live_gate(doctype), "breaker_unknown")

	@v16_only(GATE_NO_V15_PERMISSION_PINS)
	def test_a_user_without_user_permissions_and_few_names_takes_the_loop(self):
		f = self.f
		names = set(f.names(SI)[:4])
		with switch_on(), session_as(f.u3):
			with (
				patch.object(
					permissions, "_visible_names_per_doc", wraps=permissions._visible_names_per_doc
				) as loop,
				patch.object(batch_permission, "_evaluate_batch") as batch,
			):
				got = batch_permission.batch_visible(SI, set(names), "read")
				self.assertEqual(got, oracle(SI, names, "read"))
				self.assertEqual((loop.call_count, batch.call_count), (1, 0))
				batch.return_value = batch_permission.EvalResult(set(), [], {}, 0, 0)
				batch_permission.batch_visible(SI, set(f.names(SI)[:5]), "read")
				self.assertEqual(batch.call_count, 1)

	@v16_only(GATE_NO_V15_PERMISSION_PINS)
	def test_a_user_with_a_user_permission_takes_the_batch_even_for_few_names(self):
		f = self.f
		names = set(f.names(SI)[:3])
		with switch_on(), session_as(f.u1):
			with (
				patch.object(
					permissions, "_visible_names_per_doc", wraps=permissions._visible_names_per_doc
				) as loop,
				patch.object(
					batch_permission, "_evaluate_batch", wraps=batch_permission._evaluate_batch
				) as batch,
			):
				got = batch_permission.batch_visible(SI, set(names), "read")
			expected = oracle(SI, names, "read")
		self.assertEqual(got, expected)
		self.assertEqual((loop.call_count, batch.call_count), (0, 1))


@v16_only(GATE_NO_V15_PERMISSION_PINS)
class TestBatchPermissionSqlErrors(GuardCase):
	def test_an_error_of_the_batch_sql_propagates_and_no_partial_set_is_returned(self):
		f = self.f
		real = batch_permission._Run._shares
		calls = []

		def failing_on_the_second_chunk(self, chunk):
			calls.append(chunk)
			if len(calls) == 2:
				raise frappe.QueryTimeoutError("sql")
			return real(self, chunk)

		with switch_on(), session_as(f.u1), patch.object(batch_permission, "CHUNK", 2):
			with patch.object(batch_permission._Run, "_shares", failing_on_the_second_chunk):
				with self.assertRaises(frappe.QueryTimeoutError):
					batch_permission.batch_visible(SI, set(f.names(SI)[:6]), "read")
		self.assertEqual(len(calls), 2)


class TestBatchPermissionStop(GuardCase):
	def tearDown(self):
		batch_permission.check_permission_stop()

	def stop_at(self, offset, message):
		return (batch_permission.monotonic() + offset, lambda: RuntimeError(message))

	def test_no_stop_is_a_no_op_and_the_stop_is_gone_after_the_block(self):
		batch_permission.check_permission_stop()
		with batch_permission.permission_stop(*self.stop_at(-1, "late")):
			with self.assertRaisesRegex(RuntimeError, "late"):
				batch_permission.check_permission_stop()
		batch_permission.check_permission_stop()

	def test_nested_stops_keep_the_earlier_one_and_restore_the_outer(self):
		with batch_permission.permission_stop(*self.stop_at(-1, "outer")):
			with batch_permission.permission_stop(*self.stop_at(1000, "inner")):
				with self.assertRaisesRegex(RuntimeError, "outer"):
					batch_permission.check_permission_stop()
		with batch_permission.permission_stop(*self.stop_at(1000, "outer")):
			with batch_permission.permission_stop(*self.stop_at(-1, "inner")):
				with self.assertRaisesRegex(RuntimeError, "inner"):
					batch_permission.check_permission_stop()
			batch_permission.check_permission_stop()

	def test_a_stop_ends_the_batch_and_the_loop_and_visible_names(self):
		f = self.f
		names = set(f.names(SI))
		with switch_on(), session_as(f.u1), batch_permission.permission_stop(*self.stop_at(-1, "stop")):
			with self.assertRaisesRegex(RuntimeError, "stop"):
				batch_permission.batch_visible(SI, names, "read")
			with self.assertRaisesRegex(RuntimeError, "stop"):
				permissions._visible_names_per_doc(SI, names, "read")
			with self.assertRaisesRegex(RuntimeError, "stop"):
				permissions.visible_names(SI, names)


@v16_only(GATE_NO_V15_PERMISSION_PINS)
class TestBatchPermissionJobFreeze(GuardCase):
	def tearDown(self):
		frappe.local.pcal_permission_freeze = None

	def test_a_changed_plan_in_a_job_poisons_the_view_and_refuses(self):
		f = self.f
		names = set(f.names(SI))
		batch_permission.begin_mode_freeze()
		frappe.local.pcal_permission_events = []
		with switch_on(), session_as(f.u1):
			batch_permission.batch_visible(SI, set(names), "read")
			other = batch_permission.BatchPlan("per_doc", "pin_drift", ())
			with (
				patch.object(batch_permission, "batch_plan", return_value=other),
				patch.object(routing, "_poison_view") as poison,
				self.assertRaises(CalendarSourceUnavailable) as raised,
			):
				batch_permission.batch_visible(SI, set(names), "read")
		self.assertEqual(raised.exception.code, "source_changed")
		poison.assert_called_once()
		self.assertIn("permission_mode_changed", frappe.local.pcal_permission_events[-1]["message"])
		self.assertEqual(frappe.local.pcal_permission_stats[-1]["permission_refusal"], "mode_changed")

	def test_a_breaker_key_that_appears_inside_a_job_refuses_the_job(self):
		f = self.f
		names = set(f.names(SI))
		prefix = "jarvis:pcal:permission_breaker_test_" + uuid4().hex[:8] + ":"
		key = frappe.cache.make_key(prefix + SI)
		batch_permission.begin_mode_freeze()
		with switch_on(), session_as(f.u1), patch.object(batch_permission, "BREAKER_KEY", prefix):
			self.assertEqual(
				batch_permission.batch_visible(SI, set(names), "read"), oracle(SI, names, "read")
			)
			frappe.cache.execute_command("SET", key, "now|test")
			try:
				with (
					patch.object(routing, "_poison_view") as poison,
					self.assertRaises(CalendarSourceUnavailable) as raised,
				):
					batch_permission.batch_visible(SI, set(names), "read")
			finally:
				frappe.cache.execute_command("DEL", key)
		self.assertEqual(raised.exception.code, "source_changed")
		poison.assert_called_once()

	def test_without_a_freeze_each_call_takes_the_live_state(self):
		f = self.f
		names = set(f.names(SI))
		with switch_on(), session_as(f.u1):
			batch_permission.batch_visible(SI, set(names), "read")
			other = batch_permission.BatchPlan("per_doc", "pin_drift", ())
			with patch.object(batch_permission, "batch_plan", return_value=other):
				self.assertEqual(
					batch_permission.batch_visible(SI, set(names), "read"), oracle(SI, names, "read")
				)


class TestBatchPermissionSource(GuardCase):
	def test_no_frappe_callable_is_imported_by_name_and_hooks_go_through_frappe_call(self):
		# the batch code lives in every module `batch_permission*.py` of the package
		directory = Path(batch_permission.__file__).parent
		sources = [path.read_text() for path in sorted(directory.glob("batch_permission*.py"))]
		self.assertGreaterEqual(len(sources), 1)
		source = "\n".join(sources)
		imported = [
			node.module
			for text in sources
			for node in ast.walk(ast.parse(text))
			if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("frappe")
		]
		self.assertEqual(imported, [])
		self.assertIn("frappe.call(", source)
		self.assertNotIn("get_attr(method", source)
		# the batch reads the raw hooks, as Frappe does; `effective_hooks` drops hooks that Frappe still calls
		self.assertNotIn("effective_hooks", source)
