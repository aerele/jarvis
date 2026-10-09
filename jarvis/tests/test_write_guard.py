"""The write-risk guard with real documents (round 2, J1-guard).

Structure changes are refused from chat on every route; sensitive configuration
parks behind its own card in every mode and only the card the user confirmed can
write it; the ORM guard refuses a ROOT write no card named (a ``run_method``
through ``frappe.client`` or a custom whitelisted method, a second save, an
``ignore_validate`` save, a ``db_set``, a delete) while saves a document's own
controller makes pass, as from Desk; outside a tool call it does nothing.

Real documents throughout. Mocks only where a test proves a negative (nothing was
dispatched) or stands in for an app outside the structure list (the fence test).
Run serially (the fence class runs real DDL on a scratch ``custom=1`` DocType).
"""

import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.exceptions import SensitiveWriteRefusedError, StructureRefusedError
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user
from jarvis.tools import _write_risk as wr

PENDING = "Jarvis Pending Action"
MSG = "Jarvis Chat Message"
AGENT_WRITE = "Jarvis Agent Write"
PREFIX = "jarvis-wg-"


# --------------------------------------------------------------------------- #
# Whitelisted stand-ins for "a custom app's method" (run_method targets). None of
# their arguments names a doctype, so only the ORM guard can catch them.
# --------------------------------------------------------------------------- #
def _ps_values(prop: str = "bold", field: str = "description") -> dict:
	return {
		"doctype": "Property Setter",
		"doctype_or_field": "DocField",
		"doc_type": "ToDo",
		"field_name": field,
		"property": prop,
		"property_type": "Check",
		"value": "1",
	}


def wg_save_property_setters(names):
	"""A background job body that saves the Property Setters it is given."""
	for name in names:
		doc = frappe.get_doc("Property Setter", name)
		doc.value = "0"
		doc.save(ignore_permissions=True)


_SEEN_JOBS = []


def wg_noop(marker=None):
	pass


def wg_record_job(marker=None):
	_SEEN_JOBS.append((frappe.local.job.method, dict(frappe.local.job.kwargs)))


def wg_job_insert_todo():
	frappe.get_doc({"doctype": "ToDo", "description": PREFIX + "job-todo"}).insert(ignore_permissions=True)


@frappe.whitelist()
def wg_insert_todo():
	return frappe.get_doc({"doctype": "ToDo", "description": PREFIX + "rm-todo"}).insert().name


@frappe.whitelist()
def wg_insert_property_setter():
	return frappe.get_doc(_ps_values("bold")).insert(ignore_permissions=True).name


@frappe.whitelist()
def wg_insert_user_permission(user: str):
	return (
		frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "User", "for_value": user})
		.insert(ignore_permissions=True)
		.name
	)


@frappe.whitelist()
def wg_ignore_validate_update(name: str):
	doc = frappe.get_doc("Property Setter", name)
	doc.flags.ignore_validate = True
	doc.value = "0"
	doc.save(ignore_permissions=True)
	return doc.name


@frappe.whitelist()
def wg_mapper(source_name=None):
	"""A source mapper that writes a structure document at the root as it maps."""
	frappe.get_doc(
		{"doctype": "Custom Field", "dt": "ToDo", "fieldname": "jarvis_wg_map", "fieldtype": "Data"}
	).insert(ignore_permissions=True)
	return frappe.new_doc("ToDo")


def wg_ddl_hook(doc, method=None):
	"""A doc_events handler that changes the schema (an app outside the lists)."""
	frappe.db.sql_ddl(f"ALTER TABLE `tab{doc.doctype}` ADD COLUMN `{HOOK_COLUMN}` INT")


HOOK_COLUMN = "jarvis_wg_hookcol"


def wg_swallow_hook(doc, method=None):
	"""Stands in for a hook whose own write is refused and which swallows that."""
	try:
		wr._refuse(wr.structure_refusal("DocType"))
	except Exception:
		pass


def _local_hook(case, doctype: str, event: str, handler: str) -> None:
	"""Register a doc_events handler for one test (this request's hook cache)."""
	import copy

	hooks = copy.deepcopy(frappe.get_doc_hooks())
	hooks.setdefault(doctype, {}).setdefault(event, []).append(handler)
	frappe.local.doc_events_hooks = hooks
	case.addCleanup(setattr, frappe.local, "doc_events_hooks", None)


def _ps_exists(prop: str = "bold") -> bool:
	return bool(
		frappe.db.exists(
			"Property Setter", {"doc_type": "ToDo", "field_name": "description", "property": prop}
		)
	)


def _drop_ps(prop: str = "bold") -> None:
	for name in frappe.get_all(
		"Property Setter", {"doc_type": "ToDo", "field_name": "description", "property": prop}, pluck="name"
	):
		frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)


def _client_script_values(name: str) -> dict:
	return {"name": name, "dt": "ToDo", "view": "Form", "enabled": 0, "script": "// " + "x" * 2000}


def _drop_client_scripts() -> None:
	for name in frappe.get_all("Client Script", {"name": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("Client Script", name, force=True, ignore_permissions=True)


def _drop_skills() -> None:
	for name in frappe.get_all("Jarvis Custom Skill", {"skill_name": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("Jarvis Custom Skill", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _quiet_user(email: str, **values) -> str:
	"""A test User created outside Frappe's user-creation throttle (60 an hour per
	site), which a full test run reaches: ``in_import`` is the flag it honours."""
	if not frappe.db.exists("User", email):
		prev = frappe.flags.in_import
		frappe.flags.in_import = True
		try:
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Wg", "send_welcome_email": 0, **values}
			).insert(ignore_permissions=True)
		finally:
			frappe.flags.in_import = prev
	return email


def _cleanup_convs(owner: str) -> None:
	for conv in frappe.get_all(CONV, filters={"owner": owner, "title": "conv test"}, pluck="name"):
		pending_confirm.clear_for_conversation(owner, conv)
		frappe.db.delete(PENDING, {"conversation": conv})
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)


def _refused_rows(tool: str, doctype: str) -> int:
	return frappe.db.count(AGENT_WRITE, {"tool": tool, "ref_doctype": doctype, "outcome": "refused"})


# --------------------------------------------------------------------------- #
# The ORM guard
# --------------------------------------------------------------------------- #
class TestOrmGuard(FrappeTestCase):
	"""Root vs nested, allow entries, the four events, the sandbox, the scope."""

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		_drop_ps("bold")
		_drop_ps("italic")

	def tearDown(self):
		frappe.db.rollback()
		_drop_ps("bold")
		_drop_ps("italic")
		frappe.db.commit()
		frappe.set_user(self._orig)

	def test_inert_outside_a_tool_call(self):
		"""Desk, REST and scheduler saves never enter a guard scope."""
		self.assertIsNone(getattr(frappe.local, wr._LOCAL, None))
		doc = frappe.get_doc(_ps_values("bold")).insert()
		doc.value = "0"
		doc.save()
		doc.db_set("value", "1")
		frappe.delete_doc("Property Setter", doc.name)
		self.assertFalse(_ps_exists("bold"))

	def test_scope_is_request_local(self):
		"""A Desk request on another worker thread never sees a chat turn's scope."""
		seen = {}
		with wr.guard_scope():
			t = threading.Thread(target=lambda: seen.update(state=getattr(frappe.local, wr._LOCAL, None)))
			t.start()
			t.join()
			self.assertIsNotNone(getattr(frappe.local, wr._LOCAL, None))
		self.assertIsNone(seen["state"])
		self.assertIsNone(getattr(frappe.local, wr._LOCAL, None), "cleared on exit")

	def test_root_insert_refused_in_a_tool_call(self):
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			frappe.get_doc(_ps_values("bold")).insert()
		self.assertFalse(_ps_exists("bold"))

	def test_allow_entry_admits_the_card_record_and_refuses_a_second_root_save(self):
		allow = wr.allow_entries("create_doc", {"doctype": "Property Setter", "values": _ps_values("bold")})
		self.assertEqual(len(allow), 1)
		with wr.guard_scope(allow):
			first = frappe.get_doc(_ps_values("bold")).insert()
			first.value = "0"
			first.save()  # the same record again: still the card's
			with self.assertRaises(SensitiveWriteRefusedError) as ctx:
				frappe.get_doc(_ps_values("italic")).insert()
		self.assertIn("did not show", str(ctx.exception))
		self.assertTrue(_ps_exists("bold"))
		self.assertFalse(_ps_exists("italic"))

	def test_update_entry_admits_only_the_named_record(self):
		a = frappe.get_doc(_ps_values("bold")).insert()
		b = frappe.get_doc(_ps_values("italic")).insert()
		allow = wr.allow_entries(
			"update_doc", {"doctype": "Property Setter", "name": a.name, "changes": {"value": "0"}}
		)
		with wr.guard_scope(allow):
			a.value = "0"
			a.save()
			b.value = "0"
			with self.assertRaises(SensitiveWriteRefusedError):
				b.save()

	def test_update_entry_refuses_another_record_saved_first(self):
		"""m8: the name check, not first-come binding, decides an update entry."""
		a = frappe.get_doc(_ps_values("bold")).insert()
		b = frappe.get_doc(_ps_values("italic")).insert()
		allow = wr.allow_entries(
			"update_doc", {"doctype": "Property Setter", "name": a.name, "changes": {"value": "0"}}
		)
		with wr.guard_scope(allow):
			b.value = "0"
			with self.assertRaises(SensitiveWriteRefusedError):
				b.save()
			a.value = "0"
			a.save()

	def test_run_import_entry_admits_every_row(self):
		"""m1: a confirmed import admits each root save of its doctype."""
		allow = wr.allow_entries("run_import", {"doctype": "Property Setter"})
		with wr.guard_scope(allow):
			frappe.get_doc(_ps_values("bold")).insert()
			frappe.get_doc(_ps_values("italic")).insert()
		self.assertTrue(_ps_exists("bold") and _ps_exists("italic"))

	def test_conditional_delete_rule(self):
		"""m8: deleting a scripted Web Page removes the risk (allowed); deleting a
		Script Report is still sensitive (refused)."""
		page = frappe.get_doc(
			{
				"doctype": "Web Page",
				"title": PREFIX + "del",
				"route": PREFIX + "del",
				"published": 0,
				"javascript": "console.log(0)",
			}
		).insert()
		report = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": PREFIX + "report",
				"ref_doctype": "ToDo",
				"report_type": "Script Report",
				"is_standard": "No",
				"module": "Custom",
				"report_script": "result = []",
			}
		).insert()
		with wr.guard_scope():
			frappe.delete_doc("Web Page", page.name)
			with self.assertRaises(SensitiveWriteRefusedError):
				frappe.delete_doc("Report", report.name)
		self.assertFalse(frappe.db.exists("Web Page", page.name))
		self.assertTrue(frappe.db.exists("Report", report.name))

	def test_ignore_validate_save_still_refused(self):
		doc = frappe.get_doc(_ps_values("bold")).insert()
		doc.reload()
		doc.flags.ignore_validate = True
		doc.value = "0"
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			doc.save()
		self.assertEqual(frappe.db.get_value("Property Setter", doc.name, "value"), "1")

	def test_root_db_set_refused(self):
		doc = frappe.get_doc(_ps_values("bold")).insert()
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			doc.db_set("value", "0")
		self.assertEqual(frappe.db.get_value("Property Setter", doc.name, "value"), "1")

	def test_root_delete_refused_unless_the_card_named_it(self):
		doc = frappe.get_doc(_ps_values("bold")).insert()
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			frappe.delete_doc("Property Setter", doc.name)
		self.assertTrue(frappe.db.exists("Property Setter", doc.name))
		allow = wr.allow_entries("delete_doc", {"doctype": "Property Setter", "name": doc.name})
		with wr.guard_scope(allow):
			frappe.delete_doc("Property Setter", doc.name)
		self.assertFalse(frappe.db.exists("Property Setter", doc.name))

	def test_root_rename_refused(self):
		role = frappe.get_doc({"doctype": "Role", "role_name": PREFIX + "role-a", "desk_access": 0}).insert()
		self.addCleanup(lambda: frappe.delete_doc_if_exists("Role", PREFIX + "role-b", force=True))
		self.addCleanup(lambda: frappe.delete_doc_if_exists("Role", PREFIX + "role-a", force=True))
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			frappe.rename_doc("Role", role.name, PREFIX + "role-b", force=True)
		self.assertTrue(frappe.db.exists("Role", PREFIX + "role-a"))

	def test_a_controllers_own_save_is_nested_and_passes(self):
		"""Server Script (Scheduler Event) saves its Scheduled Job Type, itself
		sensitive: allowed, because a different document's save is on the stack."""
		values = {
			"script_type": "Scheduler Event",
			"event_frequency": "Daily",
			"script": "pass",
			"name": PREFIX + "sched",
		}
		allow = wr.allow_entries("create_doc", {"doctype": "Server Script", "values": values})
		with wr.guard_scope(allow):
			ss = frappe.get_doc({"doctype": "Server Script", **values}).insert()
		self.assertTrue(frappe.db.exists("Scheduled Job Type", {"server_script": ss.name}))

	def test_structure_refused_even_in_a_dry_run(self):
		from jarvis.tools._preview_sandbox import preview_sandbox

		cf = {"doctype": "Custom Field", "dt": "ToDo", "fieldname": "jarvis_wg_x", "fieldtype": "Data"}
		with wr.guard_scope(), self.assertRaises(StructureRefusedError), preview_sandbox():
			frappe.get_doc(cf).insert()
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": "jarvis_wg_x"}))

	def test_sensitive_dry_run_builds_without_a_card(self):
		from jarvis.tools._preview_sandbox import preview_sandbox

		with wr.guard_scope(), preview_sandbox():
			frappe.get_doc(_ps_values("bold")).insert()
		self.assertFalse(_ps_exists("bold"), "rolled back by the sandbox")

	def test_conditional_doctype_guarded_only_when_its_field_changes(self):
		page = frappe.get_doc(
			{
				"doctype": "Web Page",
				"title": PREFIX + "page",
				"route": PREFIX + "page",
				"published": 0,
				"javascript": "console.log(0)",
			}
		).insert()
		with wr.guard_scope():
			page.title = PREFIX + "page renamed"
			page.save()  # the script it already had is left alone: an ordinary write
			page.javascript = "console.log(1)"
			with self.assertRaises(SensitiveWriteRefusedError):
				page.save()


class TestNestedWritesFromChat(FrappeTestCase):
	"""Acceptance (rev 4 item 10): Employee with a user and Stock Settings save
	sensitive documents through their own controllers; both succeed from chat."""

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		self.conv = _make_conv("Administrator")
		frappe.db.set_value(CONV, self.conv, "auto_mode", 1, update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		_cleanup_convs("Administrator")
		frappe.db.commit()
		frappe.set_user(self._orig)

	def test_stock_settings_update_succeeds(self):
		before = frappe.db.get_single_value("Stock Settings", "show_barcode_field")
		self.addCleanup(
			lambda: (
				frappe.db.set_single_value("Stock Settings", "show_barcode_field", before),
				frappe.db.commit(),
			)
		)
		r = api._run_tool(
			"update_doc",
			{
				"doctype": "Stock Settings",
				"name": "Stock Settings",
				"changes": {"show_barcode_field": 0 if before else 1},
			},
			conversation=self.conv,
		)
		self.assertTrue(r["ok"], r)
		self.assertEqual(
			frappe.db.get_value(
				"Property Setter",
				{"doc_type": "Item", "field_name": "barcodes", "property": "hidden"},
				"value",
			),
			"1" if before else "0",
			"the Property Setters Stock Settings makes were saved",
		)

	def test_employee_update_with_a_user_parks_then_succeeds(self):
		"""m6: setting an Employee's user grants roles and User Permissions, so it
		parks a card even in auto mode; confirmed, Employee's own User / User
		Permission saves pass as nested."""
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no Company on this site")
		email = PREFIX + "emp@example.com"
		_quiet_user(email)
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Jarvis Wg",
				"gender": frappe.db.get_value("Gender", {}, "name"),
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": company,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		def _drop():
			frappe.db.delete("User Permission", {"user": email})
			frappe.delete_doc_if_exists("Employee", emp.name, force=True)
			frappe.delete_doc_if_exists("User", email, force=True)
			frappe.db.commit()

		self.addCleanup(_drop)
		published = []
		with patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: published.append(p)):
			r = api._run_tool(
				"update_doc",
				{"doctype": "Employee", "name": emp.name, "changes": {"user_id": email}},
				conversation=self.conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertEqual(r["data"]["preview"].get("risk_line"), wr.RISK_LINES["access"])
		self.assertFalse(frappe.db.get_value("Employee", emp.name, "user_id"), "not run uncarded")
		from jarvis.chat.actions_api import confirm_tool

		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(published[-1]["token"], conversation=self.conv)
		self.assertTrue(res["ok"], res)
		self.assertEqual(frappe.db.get_value("Employee", emp.name, "user_id"), email)
		self.assertTrue(
			frappe.db.exists("User Permission", {"user": email, "allow": "Employee", "for_value": emp.name}),
			"Employee's own User Permission insert passed as nested",
		)
		# Minor: change-based on update; echoing the stored user is not a change.
		self.assertIsNone(
			wr.risk_of("update_doc", {"doctype": "Employee", "name": emp.name, "changes": {"user_id": email}})
		)
		self.assertEqual(
			wr.risk_of(
				"update_doc",
				{"doctype": "Employee", "name": emp.name, "changes": {"user_id": "other@example.com"}},
			),
			"sensitive",
		)


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #
class _RouteBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		self._published = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		_drop_client_scripts()
		_drop_ps("bold")
		_cleanup_convs(TEST_USER)
		frappe.db.commit()
		frappe.set_user(self._orig)

	def _conv(self, **flags) -> str:
		conv = _make_conv(TEST_USER)
		if flags:
			frappe.db.set_value(CONV, conv, flags, update_modified=False)
			frappe.db.commit()
		return conv

	def _run(self, tool, args, conv=None):
		with patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: self._published.append(p)):
			return api._run_tool(tool, args, conversation=conv)

	def _token(self) -> str:
		return self._published[-1]["token"]


class TestStructureRefusedEverywhere(_RouteBase):
	def _assert_refused(self, r, doctype=None):
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		if doctype:
			self.assertEqual(r["error"]["desk_path"], wr.desk_path(doctype))

	def test_each_structure_doctype_refused_on_the_gated_route(self):
		conv = self._conv()
		# One new Custom Field and one new Workflow are guarded exceptions, each refused
		# by its own park check when empty (test_custom_field_exception,
		# test_workflow_settings_exception). Creating a settings document stays refused.
		for dt in sorted(wr.STRUCTURE_DOCTYPES - {"Custom Field", "Workflow"}):
			before = _refused_rows("create_doc", dt)
			r = self._run("create_doc", {"doctype": dt, "values": {}}, conv)
			self._assert_refused(r, dt)
			self.assertEqual(_refused_rows("create_doc", dt), before + 1, f"{dt}: refused attempt recorded")
		# An empty one names no form: refused by its own park check, nothing parked.
		r = self._run("create_doc", {"doctype": "Custom Field", "values": {}}, conv)
		self.assertEqual((r["ok"], r["error"]["code"]), (False, "InvalidArgumentError"), r)
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}), "no card parked")

	def test_update_delete_and_batch_refused(self):
		conv = self._conv()
		sla = "Service Level Agreement"
		self._assert_refused(
			self._run("update_doc", {"doctype": sla, "name": "x", "changes": {"enabled": 0}}, conv)
		)
		self._assert_refused(self._run("delete_doc", {"doctype": "Custom Field", "name": "x"}, conv))
		self._assert_refused(self._run("delete_doc", {"doctype": "Workflow", "name": "x"}, conv))
		self._assert_refused(self._run("delete_doc", {"doctype": "DocType", "name": "ToDo"}, conv))
		docs = [
			{"doctype": "ToDo", "values": {"description": PREFIX + "batch"}},
			{"doctype": "Custom Field", "values": {"dt": "ToDo", "fieldname": "jarvis_wg_b"}},
		]
		self._assert_refused(self._run("create_doc", {"docs": docs}, conv))
		self.assertFalse(frappe.db.exists("ToDo", {"description": PREFIX + "batch"}))

	def test_refused_in_every_uncarded_mode(self):
		from jarvis.tests.test_skill_approve_and_run import _make_skill

		_drop_skills()
		skill = _make_skill(TEST_USER, armed=True, name=PREFIX + "skill")
		self.addCleanup(_drop_skills)
		now = frappe.utils.now()
		modes = [
			{"auto_mode": 1},
			{"skip_confirmation": 1},
			{"request_autorun": 1, "request_autorun_at": now},
			{"skill_autorun": 1, "skill_autorun_at": now, "skill_autorun_skill": skill},
		]
		for flags in modes:
			conv = self._conv(**flags)
			with patch("jarvis.api.dispatch_confirmed") as disp:
				r = self._run("create_doc", {"doctype": "Inventory Dimension", "values": {}}, conv)
			self._assert_refused(r, "Inventory Dimension")
			self.assertFalse(disp.called, flags)

	def test_file_box_chat_refused_before_its_policy(self):
		conv = self._conv(file_box=1)
		r = self._run("create_doc", {"doctype": "Service Level Agreement", "values": {}}, conv)
		self._assert_refused(r, "Service Level Agreement")

	def test_draft_panel_refuses(self):
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		r = apply_action(
			{
				"verb": "create",
				"doctype": "Inventory Dimension",
				"values": {"dimension_name": "jarvis_wg_panel"},
				"conversation": conv,
			}
		)
		self._assert_refused(r, "Inventory Dimension")
		r = apply_action(
			{
				"verb": "update",
				"doctype": "Service Level Agreement",
				"name": "x",
				"values": {"enabled": 0},
				"conversation": conv,
			}
		)
		self._assert_refused(r)
		self.assertEqual(
			r["error"]["desk_path"], "/app/service-level-agreement/x", "m9: the record being edited"
		)

	def test_approval_board_edit_refuses(self):
		from jarvis.chat import approvals_api

		args = {"doctype": "Inventory Dimension", "values": {"dimension_name": "x"}}
		items = [{"op": "create", "doctype": "Inventory Dimension", "values": args["values"]}]
		with patch("jarvis.api._run_preview") as preview:
			res = approvals_api._edit_dry_run("create_doc", args, items)
		preview.assert_not_called()  # refused before any dry run
		self.assertIsNotNone(res)
		self.assertIn("set up in Desk", res.get("message") or str(res))

	def test_preview_doc_refuses(self):
		r = self._run("preview_doc", {"doctype": "Custom Field", "values": {"dt": "ToDo"}})
		self._assert_refused(r, "Custom Field")

	def test_old_parked_structure_card_refused_at_confirm(self):
		"""A card parked before this change still cannot run the structure write."""
		frappe.set_user("Administrator")
		with patch("jarvis.api.dispatch") as disp:
			r = api.dispatch_confirmed(
				"create_doc",
				{
					"doctype": "Custom Field",
					"values": {"dt": "ToDo", "fieldname": "jarvis_wg_old", "fieldtype": "Data"},
				},
				allow_risky=True,
			)
		disp.assert_not_called()  # refused before dispatch, not only by the ORM guard
		self._assert_refused(r, "Custom Field")
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": "jarvis_wg_old"}))


class TestSensitiveAlwaysCarded(_RouteBase):
	def _assert_parked(self, r, conv=None):
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertFalse(frappe.db.exists("Client Script", {"name": ["like", PREFIX + "%"]}))

	def test_parks_in_every_mode_and_never_runs_uncarded(self):
		from jarvis.tests.test_skill_approve_and_run import _make_skill

		_drop_skills()
		skill = _make_skill(TEST_USER, armed=True, name=PREFIX + "skill2")
		self.addCleanup(_drop_skills)
		now = frappe.utils.now()
		modes = [
			{"auto_mode": 1},
			{"skip_confirmation": 1},
			{"request_autorun": 1, "request_autorun_at": now},
			{"skill_autorun": 1, "skill_autorun_at": now, "skill_autorun_skill": skill},
		]
		for i, flags in enumerate(modes):
			conv = self._conv(**flags)
			args = {"doctype": "Client Script", "values": _client_script_values(f"{PREFIX}mode-{i}")}
			with patch("jarvis.api._run_covered_write") as covered:
				r = self._run("create_doc", args, conv)
			self._assert_parked(r)
			self.assertFalse(covered.called, flags)
			self.assertTrue(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_confirmed_card_writes_it_in_full(self):
		"""The pending-action confirm path authorises exactly the card's record."""
		conv = self._conv(auto_mode=1)
		name = PREFIX + "confirm"
		r = self._run("create_doc", {"doctype": "Client Script", "values": _client_script_values(name)}, conv)
		self._assert_parked(r)
		self.assertIsNotNone(r["data"]["preview"].get("would"), "today's card: a trial-built preview")
		from jarvis.chat.actions_api import confirm_tool

		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(self._token(), conversation=conv)
		self.assertTrue(res["ok"], res)
		self.assertEqual(len(frappe.db.get_value("Client Script", name, "script")), 2003, "the whole script")

	def test_old_shape_legacy_token_still_confirms(self):
		"""A legacy (Redis) card minted before this change, preview with ``would`` and
		no risk keys, still confirms through the legacy dispatch."""
		from jarvis.chat.actions_api import confirm_tool

		name = PREFIX + "legacy"
		args = {"doctype": "Client Script", "values": _client_script_values(name)}
		token = pending_confirm.mint(
			conversation="",
			owner=TEST_USER,
			tool="create_doc",
			args=args,
			run_id="",
			preview={"preview": True, "would": {"name": name}, "note": "checked"},
		)
		res = confirm_tool(token)
		self.assertTrue(res["ok"], res)
		self.assertTrue(frappe.db.exists("Client Script", name))

	def test_draft_panel_turns_it_into_a_gated_card(self):
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		with patch("jarvis.chat.events.publish_to_user"):
			r = apply_action(
				{
					"verb": "create",
					"doctype": "Client Script",
					"name": PREFIX + "panel",
					"values": _client_script_values(PREFIX + "panel"),
					"conversation": conv,
				}
			)
		self.assertTrue(r["ok"], r)
		self.assertTrue(r.get("parked"))
		self.assertIn("confirmation card", r["note"])
		self.assertFalse(frappe.db.exists("Client Script", PREFIX + "panel"), "not written by the panel")
		row = frappe.get_all(PENDING, {"conversation": conv}, ["tool"])
		self.assertEqual([x.tool for x in row], ["create_doc"], "one gated card parked")

	def test_parked_draft_survives_a_reload(self):
		"""The card carries the draft's message, so a reloaded client still shows
		that draft as waiting (preview.from_draft through list_pending_confirmations)."""
		from jarvis.chat.actions_api import apply_action, list_pending_confirmations

		conv = self._conv()
		with patch("jarvis.chat.events.publish_to_user"):
			apply_action(
				{
					"verb": "create",
					"doctype": "Client Script",
					"name": PREFIX + "reload",
					"values": _client_script_values(PREFIX + "reload"),
					"conversation": conv,
					"message": "msg-draft-1",
				}
			)
		items = list_pending_confirmations(conv)["data"]["pending"]
		self.assertEqual([i["preview"].get("from_draft") for i in items], ["msg-draft-1"])
		# The desktop seeds a reloaded card from the conversation's message row, which
		# carries only the card: the card itself names the draft too.
		from jarvis.chat.api import get_conversation

		rows = [
			m
			for m in get_conversation(conv)["messages"]
			if m.get("role") == "tool" and m.get("tool_status") == "pending"
		]
		self.assertEqual([(m["pending_card"] or {}).get("from_draft") for m in rows], ["msg-draft-1"])

	def test_panel_shows_a_plain_message_when_a_card_already_waits(self):
		"""The single-flight refusal is written for the model; a person clicking the
		draft panel gets plain words instead."""
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		self._run("create_doc", {"doctype": "ToDo", "values": {"description": PREFIX + "first"}}, conv)
		with patch("jarvis.chat.events.publish_to_user"):
			r = apply_action(
				{
					"verb": "create",
					"doctype": "Client Script",
					"name": PREFIX + "second",
					"values": _client_script_values(PREFIX + "second"),
					"conversation": conv,
					"message": "msg-draft-2",
				}
			)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "ConfirmationPendingError")
		self.assertEqual(
			r["error"]["message"],
			"A confirmation for an earlier change is already waiting in this chat. Confirm or discard it first.",
		)
		self.assertNotIn("end your turn", str(r))

	def test_draft_panel_reports_a_swallowed_refusal(self):
		"""A hook that catches the guard's refusal cannot turn the panel's save into
		a success (the guard records it; apply_action checks)."""
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		_local_hook(self, "ToDo", "before_validate", "jarvis.tests.test_write_guard.wg_swallow_hook")
		r = apply_action(
			{
				"verb": "create",
				"doctype": "ToDo",
				"values": {"description": PREFIX + "swallow"},
				"conversation": conv,
			}
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "structure_refused")
		self.assertFalse(frappe.db.exists("ToDo", {"description": PREFIX + "swallow"}))


class TestOrdinaryWritesUnchanged(_RouteBase):
	def tearDown(self):
		frappe.db.rollback()
		for name in frappe.get_all("ToDo", {"description": ["like", PREFIX + "%"]}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def test_gated_create_still_runs_its_trial(self):
		r = self._run(
			"create_doc", {"doctype": "ToDo", "values": {"description": PREFIX + "trial"}}, self._conv()
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertTrue(r["data"]["preview"].get("preview"), "the dry run built the card")

	def test_auto_mode_create_still_runs_uncarded(self):
		r = self._run(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": PREFIX + "auto"}},
			self._conv(auto_mode=1),
		)
		self.assertTrue(r["ok"], r)
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertTrue(frappe.db.exists("ToDo", {"description": PREFIX + "auto"}))


class TestRunMethodGuard(_RouteBase):
	"""R2-13: run_method stays open (a card normally, none in the uncarded modes);
	the ORM guard judges what it saves on every route."""

	def test_an_ordinary_method_runs_uncarded_in_auto_mode(self):
		frappe.set_user("Administrator")
		conv = self._conv(auto_mode=1)
		r = self._run("run_method", {"method": "jarvis.tests.test_write_guard.wg_insert_todo"}, conv)
		self.assertTrue(r["ok"], r)
		self.assertIsInstance(r["data"], str, "the method's own return value, not a parked card")
		self.assertTrue(frappe.db.exists("ToDo", {"description": PREFIX + "rm-todo"}))
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}), "no card")
		frappe.db.delete("ToDo", {"description": PREFIX + "rm-todo"})
		frappe.db.commit()

	def test_frappe_client_insert_judged_at_the_save(self):
		"""Structure: refused uncarded and even after Confirm. Sensitive: refused
		uncarded (reported refused, not ok), admitted after Confirm."""
		frappe.set_user("Administrator")
		cf = {"doctype": "Custom Field", "dt": "ToDo", "fieldname": "jarvis_wg_rm", "fieldtype": "Data"}
		call = {"method": "frappe.client.insert", "args": {"doc": cf}}
		r = self._run("run_method", call, self._conv())
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a card in ordinary chat")
		r = self._run("run_method", call, self._conv(auto_mode=1))
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		r = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": "jarvis_wg_rm"}))
		cs = {"doctype": "Client Script", **_client_script_values(PREFIX + "rm-cs")}
		call = {"method": "frappe.client.insert", "args": {"doc": cs}}
		r = self._run("run_method", call, self._conv(auto_mode=1))
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "sensitive_refused")
		self.assertFalse(frappe.db.exists("Client Script", PREFIX + "rm-cs"))
		r = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(r["ok"], r)
		self.assertTrue(frappe.db.exists("Client Script", PREFIX + "rm-cs"))

	def test_custom_method_root_insert_carded_admitted_uncarded_refused(self):
		"""A whitelisted method inserting a Property Setter / User Permission itself:
		parked in chat; refused by the ORM when it runs uncarded (auto mode, filed
		refused against the doctype it refused, never ok), and admitted once a human
		confirmed the run_method card (R2-13)."""
		frappe.set_user("Administrator")
		method = "jarvis.tests.test_write_guard.wg_insert_property_setter"
		r = self._run("run_method", {"method": method}, self._conv())
		self.assertEqual(r["data"]["status"], "pending_confirmation", "run_method still parks in chat")
		before = frappe.db.count(
			AGENT_WRITE, {"tool": "run_method", "outcome": "refused", "ref_doctype": "Property Setter"}
		)
		r = self._run("run_method", {"method": method}, self._conv(auto_mode=1))
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(_ps_exists("bold"))
		self.assertEqual(
			frappe.db.count(
				AGENT_WRITE, {"tool": "run_method", "outcome": "refused", "ref_doctype": "Property Setter"}
			),
			before + 1,
			"filed as refused, against the doctype the guard refused (m9)",
		)
		r = self._run(
			"run_method",
			{
				"method": "jarvis.tests.test_write_guard.wg_insert_user_permission",
				"args": {"user": TEST_USER},
			},
			self._conv(auto_mode=1),
		)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(frappe.db.exists("User Permission", {"user": TEST_USER, "allow": "User"}))
		# R2-13: the user confirmed the call itself, so its sensitive writes are admitted.
		r = api.dispatch_confirmed("run_method", {"method": method}, allow_risky=True)
		self.assertTrue(r["ok"], r)
		self.assertTrue(_ps_exists("bold"), "a confirmed run_method may write a sensitive record")

	def test_ignore_validate_update_through_a_method_refused(self):
		frappe.set_user("Administrator")
		ps = frappe.get_doc(_ps_values("bold")).insert()
		frappe.db.commit()
		r = self._run(
			"run_method",
			{"method": "jarvis.tests.test_write_guard.wg_ignore_validate_update", "args": {"name": ps.name}},
			self._conv(auto_mode=1),
		)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertEqual(frappe.db.get_value("Property Setter", ps.name, "value"), "1")


class TestConfirmedRunMethod(_RouteBase):
	"""R2-13: a confirmed run_method admits the sensitive writes it causes (never
	structure); uncarded they are refused, and a method that swallows the refusal
	still fails."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Frappe refuses a 61st new User within an hour ("Throttled"), which a full
		# test run reaches; these tests create Users on purpose, so lift it here.
		throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		throttle.start()
		self.addCleanup(throttle.stop)

	def tearDown(self):
		frappe.db.rollback()
		for email in frappe.get_all("User", {"name": ["like", PREFIX + "%"]}, pluck="name"):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		for emp in frappe.get_all("Employee", {"first_name": "Jarvis Wg Run"}, pluck="name"):
			frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
		for c in frappe.get_all("Contact", {"first_name": "Jarvis Wg Run"}, pluck="name"):
			frappe.delete_doc("Contact", c, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _both(self, method, args):
		call = {"method": method, "args": args}
		uncarded = api.dispatch_confirmed("run_method", call, uncarded=True)
		return uncarded, call

	def test_reset_password_never_hides_a_refusal(self):
		email = _quiet_user(PREFIX + "rp@example.com", enabled=1)
		method = "frappe.core.doctype.user.user.reset_password"
		key = frappe.db.get_value("User", email, "reset_password_key")
		# Auto mode: run uncarded (R2-13); its User write is refused at the save and
		# reported as refused although the method swallows the exception.
		r = self._run("run_method", {"method": method, "args": {"user": email}}, self._conv(auto_mode=1))
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "sensitive_refused")
		uncarded, call = self._both(method, {"user": email})
		self.assertFalse(uncarded["ok"], "reset_password swallows the refusal; it must not read as success")
		self.assertEqual(uncarded["error"]["code"], "sensitive_refused")
		self.assertEqual(frappe.db.get_value("User", email, "reset_password_key"), key)
		confirmed = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(confirmed["ok"], confirmed)
		self.assertNotEqual(frappe.db.get_value("User", email, "reset_password_key"), key)

	def test_generate_keys(self):
		email = _quiet_user(PREFIX + "keys@example.com", enabled=1)
		uncarded, call = self._both("frappe.core.doctype.user.user.generate_keys", {"user": email})
		self.assertEqual(uncarded["error"]["code"], "sensitive_refused", uncarded)
		self.assertFalse(frappe.db.get_value("User", email, "api_key"))
		confirmed = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(confirmed["ok"], confirmed)
		self.assertTrue(frappe.db.get_value("User", email, "api_key"))

	def test_audit_line_only_for_an_admitted_sensitive_write(self):
		"""Minor: no ``risk=sensitive outcome=applied`` line when nothing sensitive ran."""
		# reset_password's card allows User, but for Administrator it writes nothing.
		call = {"method": "frappe.core.doctype.user.user.reset_password", "args": {"user": "Administrator"}}
		with patch.object(wr, "log_line") as line:
			r = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(r["ok"], r)
		self.assertFalse([c for c in line.call_args_list if c.args[3] == "applied"])
		with patch.object(wr, "log_line") as line:
			api.dispatch_confirmed(
				"create_doc",
				{"doctype": "Client Script", "values": _client_script_values(PREFIX + "audited")},
				allow_risky=True,
			)
		applied = [c for c in line.call_args_list if c.args[3] == "applied"]
		self.assertEqual([(c.args[1], c.args[2]) for c in applied], [("Client Script", PREFIX + "audited")])

	def test_employee_create_user(self):
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no Company on this site")
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Jarvis Wg Run",
				"status": "Active",
				"gender": frappe.db.get_value("Gender", {}, "name"),
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": company,
			}
		).insert(ignore_permissions=True)
		email = PREFIX + "empuser@example.com"
		method = "erpnext.setup.doctype.employee.employee.create_user"
		uncarded, call = self._both(method, {"employee": emp.name, "email": email})
		self.assertEqual(uncarded["error"]["code"], "sensitive_refused", uncarded)
		self.assertFalse(frappe.db.exists("User", email))
		confirmed = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(confirmed["ok"], confirmed)
		self.assertTrue(frappe.db.exists("User", email))

	def test_contact_invite_user(self):
		email = PREFIX + "contact@example.com"
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "Jarvis Wg Run",
				"email_ids": [{"email_id": email, "is_primary": 1}],
			}
		).insert(ignore_permissions=True)
		uncarded, call = self._both(
			"frappe.contacts.doctype.contact.contact.invite_user", {"contact": contact.name}
		)
		self.assertEqual(uncarded["error"]["code"], "sensitive_refused", uncarded)
		self.assertFalse(frappe.db.exists("User", email))
		confirmed = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertTrue(confirmed["ok"], confirmed)
		self.assertTrue(frappe.db.exists("User", email))

	def test_a_confirmed_run_method_still_cannot_change_structure(self):
		r = api.dispatch_confirmed(
			"run_method", {"method": "jarvis.tests.test_write_guard.wg_mapper"}, allow_risky=True
		)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": "jarvis_wg_map"}))


class TestEveryConfirmRoute(_RouteBase):
	"""m8 / m2: the routes the review found untested.

	A Service Level Agreement stands for "a structure write". A single new Custom
	Field (J1b-cf) and a single Workflow (J1c) are guarded exceptions on a confirmed
	chat card and run real DDL on their target, so they are exercised only on scratch
	DocTypes (test_custom_field_exception, test_workflow_settings_exception), where
	these same routes are shown to refuse them."""

	SLA = "Service Level Agreement"
	CF = {"doctype": SLA, "values": {"service_level": "jarvis-wg-route", "document_type": "ToDo"}}

	def _no_field(self):
		self.assertFalse(frappe.db.exists(self.SLA, {"service_level": "jarvis-wg-route"}))

	def test_submit_cancel_amend_refused(self):
		conv = self._conv()
		for tool in ("submit_doc", "cancel_doc", "amend_doc"):
			r = self._run(tool, {"doctype": self.SLA, "name": "x"}, conv)
			self.assertEqual(r["error"]["code"], "structure_refused", (tool, r))

	def test_run_import(self):
		r = self._run("run_import", {"doctype": "Custom Field", "file_url": "/x.csv"}, self._conv())
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		with patch("jarvis.api._run_covered_write") as covered:
			self._run("run_import", {"doctype": "Web Page", "file_url": "/x.csv"}, self._conv(auto_mode=1))
		self.assertFalse(covered.called, "M4: a conditional doctype's import never runs uncarded")

	def test_sheet_apply(self):
		from jarvis.chat import held_sheets

		r = held_sheets.file_questions("create_doc", self.CF, self._conv(file_box=1))
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self._no_field()

	def test_approval_board_edit_apply(self):
		"""The board's Edit & create and every pending-action confirm run _dispatch."""
		from jarvis.chat.pending_actions import _execute

		row = frappe._dict(tool="create_doc", exec_user=TEST_USER, kind="file_box_held", name="wg-row")
		result, _interfered, crash = _execute._dispatch(row, self.CF, user=TEST_USER)
		self.assertFalse(crash)
		self.assertEqual(result["error"]["code"], "structure_refused", result)
		self._no_field()

	def test_approve_and_run_dispatch(self):
		from jarvis.chat import actions_api

		record = {"tool": "create_doc", "args": self.CF, "exec_user": TEST_USER}
		r = actions_api._dispatch_call(record, "wg-token", "", "wg crash")
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self._no_field()

	def test_typed_yes_to_all_sweep(self):
		from jarvis.chat import api as chat_api

		todo = {"doctype": "ToDo", "values": {"description": PREFIX + "sweep"}}
		t1 = pending_confirm.mint(
			conversation="", owner=TEST_USER, tool="create_doc", args=self.CF, run_id=""
		)
		t2 = pending_confirm.mint(conversation="", owner=TEST_USER, tool="create_doc", args=todo, run_id="")
		self.addCleanup(
			lambda: (frappe.db.delete("ToDo", {"description": PREFIX + "sweep"}), frappe.db.commit())
		)
		with patch("jarvis.chat.api._dispatch_turn"):
			out = chat_api._run_typed_batch("", [{"token": t1, "position": 1}, {"token": t2, "position": 2}])
		self.assertFalse(out["ok"])
		self._no_field()
		self.assertTrue(frappe.db.exists("ToDo", {"description": PREFIX + "sweep"}), "the ordinary card ran")

	def test_pending_action_route_files_refused(self):
		"""m2: the pending-action execute's full rollback keeps the row refused."""
		conv = self._conv()
		token = pending_confirm.mint(
			conversation=conv, owner=TEST_USER, tool="create_doc", args=self.CF, run_id=""
		)
		before = frappe.db.count(AGENT_WRITE, {"outcome": "refused", "ref_doctype": self.SLA})
		from jarvis.chat.actions_api import confirm_tool

		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=conv)
		self.assertFalse(res["ok"])
		self.assertEqual(
			frappe.db.count(AGENT_WRITE, {"outcome": "refused", "ref_doctype": self.SLA}), before + 1
		)
		self._no_field()

	def test_source_mapper_refusal_is_reported(self):
		from jarvis.tools import _source_mapper

		frappe.set_user("Administrator")
		todo = frappe.get_doc({"doctype": "ToDo", "description": PREFIX + "map"}).insert()
		mapper = "jarvis.tests.test_write_guard.wg_mapper"
		# Inside a tool call (get_creation_context runs it from dispatch).
		with patch.object(_source_mapper, "resolve_mapper", return_value=mapper), wr.guard_scope():
			values, note = _source_mapper.mapped_values("ToDo", todo.name, "ToDo")
		self.assertIsNone(values)
		self.assertIn("database structure", note or "")
		self.assertIsInstance(wr.take_refusal(), StructureRefusedError, "the refusal is not lost")
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": "jarvis_wg_map"}))


class _JobRecorder:
	"""Stands in for Redis: records what enqueue_call would push (the transport
	only), so a test can run the job the way the worker would."""

	def __init__(self, case):
		from rq import Queue

		self.calls = []
		recorder = self

		def enqueue_call(queue, func, *args, kwargs=None, **options):
			recorder.calls.append({"func": func, "kwargs": dict(kwargs or {})})

		patcher = patch.object(Queue, "enqueue_call", enqueue_call)
		patcher.start()
		case.addCleanup(patcher.stop)

	def run_last(self):
		"""Execute the recorded job as the worker would (``is_async`` off: same
		process, no re-init of the site)."""
		call = self.calls[-1]
		func = call["func"]
		func = frappe.get_attr(func) if isinstance(func, str) else func
		prev = getattr(frappe.local, "job", None)
		try:
			return func(**{**call["kwargs"], "is_async": False})
		finally:
			frappe.local.job = prev


class TestSavedRoutesCycle3(_RouteBase):
	"""Review cycle 3: deletes that run no hook, background jobs, and the field
	rule at the save (all route-independent, no method list)."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def _ps_many(self, count: int) -> list[str]:
		fields = [
			df.fieldname
			for df in frappe.get_meta("ToDo").fields
			if df.fieldtype not in ("Section Break", "Column Break", "Tab Break")
		]
		names = []
		for field in fields:
			for prop in ("bold", "hidden"):
				if len(names) >= count:
					return names
				values = {**_ps_values(prop, field)}
				names.append(frappe.get_doc(values).insert(ignore_permissions=True).name)
		return names

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete("Property Setter", {"doc_type": "ToDo", "property": ["in", ["bold", "hidden"]]})
		frappe.db.delete("ToDo", {"description": ["like", PREFIX + "%"]})
		frappe.db.commit()
		frappe.clear_cache(doctype="ToDo")
		super().tearDown()

	def test_hookless_delete_of_a_docType_refused_uncarded_and_confirmed(self):
		"""Frappe deletes a DocType without calling any hook: judged anyway."""
		name = "Jarvis WG Delete Scratch"
		frappe.delete_doc_if_exists("DocType", name, force=True)
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": name,
				"module": "Custom",
				"custom": 1,
				"fields": [{"label": "T", "fieldname": "t", "fieldtype": "Data"}],
				"permissions": [{"role": "System Manager", "read": 1}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		def _drop():
			frappe.db.rollback()
			frappe.delete_doc_if_exists("DocType", name, force=True)
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{name}`")
			frappe.db.commit()

		self.addCleanup(_drop)
		call = {"method": "frappe.client.delete", "args": {"doctype": "DocType", "name": name}}
		# Auto mode: a delete through run_method parks its own card (the delete
		# brake), and run uncarded anyway it is refused before the delete.
		r = self._run("run_method", call, self._conv(auto_mode=1))
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		r = api.dispatch_confirmed("run_method", call, uncarded=True)
		self.assertEqual(r["error"]["code"], "brake_refused", r)
		r = api.dispatch_confirmed("run_method", call, allow_risky=True)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		frappe.db.rollback()
		self.assertTrue(frappe.db.exists("DocType", name))
		self.assertTrue(frappe.db.sql("SHOW TABLES LIKE %s", f"tab{name}"))

	def test_ignore_on_trash_delete_of_a_sensitive_doctype(self):
		ps = frappe.get_doc(_ps_values("bold")).insert(ignore_permissions=True)
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError):
			frappe.delete_doc("Property Setter", ps.name, ignore_on_trash=True)
		self.assertTrue(frappe.db.exists("Property Setter", ps.name))
		allow = wr.allow_entries("delete_doc", {"doctype": "Property Setter", "name": ps.name})
		with wr.guard_scope(allow):
			frappe.delete_doc("Property Setter", ps.name, ignore_on_trash=True)
		self.assertFalse(frappe.db.exists("Property Setter", ps.name))

	def test_bulk_update_job_of_a_sensitive_doctype_refused_uncarded(self):
		"""submit_cancel_or_update_docs hands 20+ documents to a job: the job runs
		under the guard of the call that enqueued it."""
		names = self._ps_many(21)
		frappe.db.commit()
		jobs = _JobRecorder(self)
		call = {
			"method": "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
			"args": {
				"doctype": "Property Setter",
				"docnames": names,
				"action": "update",
				"data": {"value": "0"},
			},
		}
		r = self._run("run_method", call, self._conv(auto_mode=1))
		self.assertTrue(r["ok"], r)
		job = jobs.calls[-1]
		self.assertEqual(job["func"], wr._GUARDED_EXECUTE_JOB, "the job runs through the guard")
		method = job["kwargs"]["method"]
		self.assertTrue(
			str(getattr(method, "__qualname__", method)).endswith("_bulk_action"), "it keeps its real method"
		)
		with self.assertRaises(SensitiveWriteRefusedError):
			jobs.run_last()
		frappe.db.rollback()
		self.assertEqual(
			frappe.get_all("Property Setter", {"name": ["in", names], "value": "0"}), [], "nothing saved"
		)

	def test_a_confirmed_cards_allow_is_carried_into_its_job(self):
		names = self._ps_many(2)
		jobs = _JobRecorder(self)
		allow = wr.allow_entries(
			"update_doc",
			{"doctype": "Property Setter", "updates": [{"name": n, "changes": {}} for n in names]},
		)
		with wr.guard_scope(allow):
			frappe.enqueue("jarvis.tests.test_write_guard.wg_save_property_setters", names=names)
		jobs.run_last()
		self.assertEqual(len(frappe.get_all("Property Setter", {"name": ["in", names], "value": "0"})), 2)
		with wr.guard_scope():
			frappe.enqueue("jarvis.tests.test_write_guard.wg_save_property_setters", names=names)
		with self.assertRaises(SensitiveWriteRefusedError):
			jobs.run_last()

	def test_ordinary_jobs_are_unaffected(self):
		from frappe.utils.background_jobs import execute_job

		jobs = _JobRecorder(self)
		frappe.enqueue("jarvis.tests.test_write_guard.wg_job_insert_todo")
		self.assertIn(jobs.calls[-1]["func"], ("frappe.utils.background_jobs.execute_job", execute_job))
		self.assertNotIn("_jarvis_allow", jobs.calls[-1]["kwargs"])
		with wr.guard_scope():
			frappe.enqueue("jarvis.tests.test_write_guard.wg_job_insert_todo")
		jobs.run_last()
		self.assertTrue(frappe.db.exists("ToDo", {"description": PREFIX + "job-todo"}))

	def test_a_guarded_job_keeps_its_real_identity(self):
		"""Monitoring, Error Log titles and get_jobs dedupe see the real method."""
		from frappe.utils import background_jobs
		from rq.job import Job

		jobs = _JobRecorder(self)
		with wr.guard_scope():
			frappe.enqueue("jarvis.tests.test_write_guard.wg_noop", queue="short", marker=PREFIX)
		call = jobs.calls[-1]
		# The job as Redis would hold it, read back by Frappe's own get_jobs (the queue
		# listing is stood in for: no worker runs on the test bench).
		job = Job.create(call["func"], kwargs=call["kwargs"], connection=frappe.cache)

		class _Listing:
			jobs = [job]

		with (
			patch.object(background_jobs, "get_queue_list", return_value=["long"]),
			patch.object(background_jobs, "get_queue", return_value=_Listing()),
			patch.object(background_jobs, "get_running_jobs_in_queue", return_value=[]),
		):
			found = background_jobs.get_jobs(site=frappe.local.site)
		self.assertIn("jarvis.tests.test_write_guard.wg_noop", found[frappe.local.site])
		jobs = _JobRecorder(self)
		with wr.guard_scope():
			frappe.enqueue("jarvis.tests.test_write_guard.wg_record_job", marker=PREFIX)
		_SEEN_JOBS.clear()
		jobs.run_last()
		self.assertEqual(_SEEN_JOBS, [("jarvis.tests.test_write_guard.wg_record_job", {"marker": PREFIX})])

	def test_every_rq_entry_point_is_guarded(self):
		"""In-scope code that calls the queue's own rq methods gets a guarded job."""
		from datetime import timedelta

		from frappe.utils import background_jobs
		from rq import Queue

		jobs = _JobRecorder(self)
		scheduled = []
		patcher = patch.object(Queue, "schedule_job", lambda queue, job, when, **kw: scheduled.append(job))
		patcher.start()
		self.addCleanup(patcher.stop)
		raw = background_jobs.get_queue("short")  # outside a scope: the raw queue
		self.assertNotIsInstance(raw, wr._GuardedQueue)
		with wr.guard_scope() as state:
			queue = wr.guard_queue(raw)
			queue.enqueue("jarvis.tests.test_write_guard.wg_noop", marker=PREFIX)
			queue.enqueue_in(timedelta(seconds=60), "jarvis.tests.test_write_guard.wg_noop", marker=PREFIX)
		call = jobs.calls[-1]
		self.assertEqual(call["func"], wr._GUARDED_EXECUTE_JOB)
		self.assertEqual(call["kwargs"]["method"], "jarvis.tests.test_write_guard.wg_noop")
		self.assertEqual(call["kwargs"]["kwargs"], {"marker": PREFIX})
		self.assertEqual(call["kwargs"]["_jarvis_allow"], wr._allow_payload(state))
		self.assertEqual(scheduled[-1].func_name, wr._GUARDED_EXECUTE_JOB)
		self.assertEqual(scheduled[-1].kwargs["method"], "jarvis.tests.test_write_guard.wg_noop")

	def test_a_forged_allow_is_always_overwritten(self):
		"""A job's allow entries are always the enqueuing scope's own, never what the
		caller passed in."""
		from frappe.utils import background_jobs

		jobs = _JobRecorder(self)
		names = self._ps_many(1)
		allow = wr.allow_entries(
			"update_doc", {"doctype": "Property Setter", "name": names[0], "changes": {}}
		)
		forged = {"doctype": "*", "name": None, "risks": ["sensitive"], "multi": True, "bound": None}
		with wr.guard_scope(allow) as state:
			wr.guard_queue(background_jobs.get_queue("short")).enqueue_call(
				wr.execute_guarded_job,
				kwargs={"method": "jarvis.tests.test_write_guard.wg_noop", "_jarvis_allow": [forged]},
			)
		self.assertEqual(jobs.calls[-1]["kwargs"]["_jarvis_allow"], wr._allow_payload(state))
		self.assertNotIn(forged, jobs.calls[-1]["kwargs"]["_jarvis_allow"])

	def test_unbound_create_allow_is_not_carried_into_a_job(self):
		allow = wr.allow_entries("create_doc", {"doctype": "Property Setter", "values": {}})
		with wr.guard_scope(allow) as state:
			self.assertEqual(wr._allow_payload(state), [])

	def test_employee_auto_user_parks_then_creates_user(self):
		"""create_user_automatically (Frappe 16): parks a card instead of a refusal
		inside the agent's own call; confirmed, Employee + User + User Permission."""
		if not frappe.get_meta("Employee").has_field("create_user_automatically"):
			self.skipTest("Frappe 15 Employee has no create_user_automatically")
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no Company on this site")
		throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		throttle.start()
		self.addCleanup(throttle.stop)
		email = PREFIX + "autouser@example.com"
		values = {
			"first_name": "Jarvis Wg Auto",
			"status": "Active",
			"gender": frappe.db.get_value("Gender", {}, "name"),
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"company": company,
			"company_email": email,
			"create_user_automatically": 1,
			"create_user_permission": 1,
		}

		def _drop():
			frappe.db.rollback()
			for emp in frappe.get_all("Employee", {"first_name": "Jarvis Wg Auto"}, pluck="name"):
				frappe.db.delete("User Permission", {"for_value": emp})
				frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
			frappe.db.delete("User Permission", {"user": email})
			frappe.delete_doc_if_exists("User", email, force=True)
			frappe.db.commit()

		self.addCleanup(_drop)
		only_auto = {**values, "create_user_permission": 0}
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Employee", "values": only_auto}),
			"sensitive",
			"create_user_automatically alone cards",
		)
		conv = _make_conv("Administrator")  # the card's owner confirms it
		frappe.db.set_value(CONV, conv, "auto_mode", 1, update_modified=False)
		frappe.db.commit()
		self.addCleanup(lambda: (_cleanup_convs("Administrator"), frappe.db.commit()))
		r = self._run("create_doc", {"doctype": "Employee", "values": values}, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertFalse(frappe.db.exists("User", email))
		from jarvis.chat.actions_api import confirm_tool

		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(self._token(), conversation=conv)
		self.assertTrue(res["ok"], res)
		emp = frappe.db.get_value(
			"Employee", {"first_name": "Jarvis Wg Auto"}, ["name", "user_id"], as_dict=True
		)
		self.assertEqual(emp.user_id, email)
		self.assertTrue(frappe.db.exists("User", email))
		self.assertTrue(
			frappe.db.exists("User Permission", {"user": email, "allow": "Employee", "for_value": emp.name})
		)

	def _employee(self, **values):
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no Company on this site")
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Jarvis Wg Field",
				"status": "Active",
				"gender": frappe.db.get_value("Gender", {}, "name"),
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": company,
				**values,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		def _drop():
			frappe.db.rollback()
			frappe.db.delete("User Permission", {"for_value": emp.name})
			frappe.delete_doc_if_exists("Employee", emp.name, force=True)
			frappe.db.commit()

		self.addCleanup(_drop)
		return emp

	def test_field_rule_holds_through_frappe_client(self):
		"""An Employee save through run_method that sets a login parks nothing and is
		refused at the save; an everyday edit runs; Confirm admits it."""
		email = _quiet_user(PREFIX + "field@example.com", enabled=1)
		self.addCleanup(lambda: (frappe.delete_doc_if_exists("User", email, force=True), frappe.db.commit()))
		emp = self._employee()
		conv = self._conv(auto_mode=1)
		set_user = {
			"method": "frappe.client.set_value",
			"args": {"doctype": "Employee", "name": emp.name, "fieldname": "user_id", "value": email},
		}
		r = self._run("run_method", set_user, conv)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(frappe.db.get_value("Employee", emp.name, "user_id"))
		everyday = {
			"method": "frappe.client.set_value",
			"args": {"doctype": "Employee", "name": emp.name, "fieldname": "cell_number", "value": "123"},
		}
		self.assertTrue(self._run("run_method", everyday, conv)["ok"])
		r = api.dispatch_confirmed("run_method", set_user, allow_risky=True)
		self.assertTrue(r["ok"], r)
		self.assertEqual(frappe.db.get_value("Employee", emp.name, "user_id"), email)

	def test_employee_status_that_toggles_its_login(self):
		email = _quiet_user(PREFIX + "status@example.com", enabled=1)
		self.addCleanup(lambda: (frappe.delete_doc_if_exists("User", email, force=True), frappe.db.commit()))
		emp = self._employee(user_id=email)
		call = {
			"method": "frappe.client.set_value",
			"args": {"doctype": "Employee", "name": emp.name, "fieldname": "status", "value": "Inactive"},
		}
		r = self._run("run_method", call, self._conv(auto_mode=1))
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertEqual(frappe.db.get_value("User", email, "enabled"), 1, "the login was not cut")
		self.assertEqual(
			wr.risk_of(
				"update_doc", {"doctype": "Employee", "name": emp.name, "changes": {"status": "Inactive"}}
			),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of(
				"update_doc", {"doctype": "Employee", "name": emp.name, "changes": {"status": "Active"}}
			),
			"no change",
		)

	def test_portal_users_change_is_judged_at_the_save(self):
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		if not (group and territory):
			self.skipTest("no Customer Group / Territory")
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": PREFIX + "portal",
				"customer_group": group,
				"territory": territory,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(
			lambda: (frappe.delete_doc_if_exists("Customer", customer.name, force=True), frappe.db.commit())
		)
		doc = customer.as_dict()
		doc["portal_users"] = [{"user": TEST_USER}]
		r = self._run(
			"run_method",
			{"method": "frappe.client.save", "args": {"doc": frappe.as_json(doc)}},
			self._conv(auto_mode=1),
		)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(frappe.get_all("Portal User", {"parent": customer.name}))


# --------------------------------------------------------------------------- #
# The no-commit fence: a doctype outside the list whose save runs DDL
# --------------------------------------------------------------------------- #
SCRATCH = "WG Fence Scratch"  # not "Jarvis ...": chat never customises Jarvis's own DocTypes
STATE_FIELD = "jarvis_wg_state"


def _columns(doctype: str) -> set[str]:
	return {c.lower() for c in frappe.db.get_table_columns(doctype)}


class TestNoCommitFence(FrappeTestCase):
	"""Workflow stands in for an app's doctype outside the structure list (removed
	from the list for these tests only): its on_update inserts a Custom Field for the
	state field and runs ALTER TABLE. Uncarded, that must be refused before any DDL."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.delete_doc_if_exists("DocType", SCRATCH, force=True)
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{SCRATCH}`")
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": SCRATCH,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"fields": [{"label": "Title", "fieldname": "title", "fieldtype": "Data"}],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		cls.addClassCleanup(cls._teardown_scratch)

	@classmethod
	def _teardown_scratch(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for wf in frappe.get_all("Workflow", {"document_type": SCRATCH}, pluck="name"):
			frappe.delete_doc("Workflow", wf, force=True, ignore_permissions=True)
		for cf in frappe.get_all("Custom Field", {"dt": SCRATCH}, pluck="name"):
			frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)
		frappe.delete_doc_if_exists("DocType", SCRATCH, force=True)
		# Deleting a DocType keeps its table; this one was made here, so drop it.
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{SCRATCH}`")
		frappe.db.commit()
		assert not frappe.db.exists("DocType", SCRATCH), "scratch DocType left behind"
		assert not frappe.db.sql("SHOW TABLES LIKE %s", f"tab{SCRATCH}"), "scratch table left behind"
		assert not frappe.db.exists("Custom Field", {"dt": SCRATCH}), "scratch Custom Field left behind"

	def setUp(self):
		frappe.set_user("Administrator")
		self.conv = _make_conv("Administrator")
		frappe.db.set_value(CONV, self.conv, "auto_mode", 1, update_modified=False)
		frappe.db.commit()
		patcher = patch.object(wr, "STRUCTURE_DOCTYPES", wr.STRUCTURE_DOCTYPES - {"Workflow"})
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()
		_cleanup_convs("Administrator")
		frappe.db.commit()

	def _workflow_values(self, name: str) -> dict:
		return {
			"workflow_name": name,
			"document_type": SCRATCH,
			"workflow_state_field": STATE_FIELD,
			"is_active": 1,
			"states": [{"state": "Pending", "doc_status": "0", "allow_edit": "System Manager"}],
		}

	def _assert_nothing_left(self, name: str):
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("Workflow", name))
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": SCRATCH, "fieldname": STATE_FIELD}))
		self.assertNotIn(STATE_FIELD, _columns(SCRATCH), "no ALTER ran")

	def test_custom_field_leaves_no_row_and_no_column_before_confirm(self):
		"""One new Custom Field is the guarded exception (J1b-cf): on the gated route
		and in auto mode alike it only PARKS a card, never runs, and the target table
		is exactly as it was (DESCRIBE before / after). Two in one call, or a delete,
		are refused as before."""
		before = _columns(SCRATCH)
		cf = {"dt": SCRATCH, "fieldname": "jarvis_wg_cf", "label": "Wg", "fieldtype": "Data"}
		gated = _make_conv("Administrator")
		for conv in (gated, self.conv):
			with patch("jarvis.chat.events.publish_to_user"):
				r = api._run_tool("create_doc", {"doctype": "Custom Field", "values": cf}, conversation=conv)
			self.assertEqual(r["data"]["status"], "pending_confirmation", r)
			two = {"docs": [{"doctype": "Custom Field", "values": cf}] * 2}
			r = api._run_tool("create_doc", two, conversation=conv)
			self.assertEqual(r["error"]["code"], "structure_refused", r)
		frappe.db.rollback()
		self.assertEqual(_columns(SCRATCH), before)
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": SCRATCH}))

	def _hook(self, event: str):
		"""Register ``wg_ddl_hook`` on SCRATCH for this test only."""
		import copy

		hooks = copy.deepcopy(frappe.get_doc_hooks())
		hooks.setdefault(SCRATCH, {}).setdefault(event, []).append(
			"jarvis.tests.test_write_guard.wg_ddl_hook"
		)
		frappe.local.doc_events_hooks = hooks
		self.addCleanup(setattr, frappe.local, "doc_events_hooks", None)

		def _drop_column():
			frappe.db.rollback()
			if HOOK_COLUMN in _columns(SCRATCH):
				frappe.db.sql_ddl(f"ALTER TABLE `tab{SCRATCH}` DROP COLUMN `{HOOK_COLUMN}`")
			frappe.clear_cache(doctype=SCRATCH)

		self.addCleanup(_drop_column)

	def _assert_control_intact(self):
		self.assertEqual(
			getattr(frappe.db, "_disable_transaction_control", 0),
			0,
			"commit / rollback still work afterwards (Frappe 16)",
		)

	def test_ddl_in_a_doc_events_handler_leaves_transaction_control_intact(self):
		"""M3: uncarded (the fence) and at park (the dry run)."""
		self._hook("on_update")
		uncarded = api._run_tool(
			"create_doc", {"doctype": SCRATCH, "values": {"title": "a"}}, conversation=self.conv
		)
		self.assertEqual(uncarded["error"]["code"], "structure_refused", uncarded)
		self._assert_control_intact()
		gated = _make_conv("Administrator")
		with patch("jarvis.chat.events.publish_to_user"):
			parked = api._run_tool(
				"create_doc", {"doctype": SCRATCH, "values": {"title": "b"}}, conversation=gated
			)
		self.assertEqual(parked["error"]["code"], "structure_refused", parked)
		self._assert_control_intact()
		frappe.db.rollback()
		self.assertNotIn(HOOK_COLUMN, _columns(SCRATCH))

	def test_ddl_as_the_first_statement_is_refused(self):
		"""m3: a before_insert handler runs DDL before the row is written."""
		self._hook("before_insert")
		r = api._run_tool(
			"create_doc", {"doctype": SCRATCH, "values": {"title": "c"}}, conversation=self.conv
		)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		frappe.db.rollback()
		self.assertNotIn(HOOK_COLUMN, _columns(SCRATCH))

	def test_uncarded_create_raises_before_any_ddl(self):
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Workflow", "values": {}}), "stand-in is unknown"
		)
		r = api._run_tool(
			"create_doc",
			{"doctype": "Workflow", "values": self._workflow_values(PREFIX + "wf-auto")},
			conversation=self.conv,
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self._assert_nothing_left(PREFIX + "wf-auto")

	def test_draft_panel_create_raises_before_any_ddl(self):
		from jarvis.chat.actions_api import apply_action

		before = _refused_rows("create_doc", "Workflow")
		r = apply_action(
			{
				"verb": "create",
				"doctype": "Workflow",
				"values": self._workflow_values(PREFIX + "wf-panel"),
				"conversation": self.conv,
			}
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assertEqual(_refused_rows("create_doc", "Workflow"), before + 1, "filed as refused")
		self._assert_nothing_left(PREFIX + "wf-panel")
