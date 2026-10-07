"""The write-risk definitions and the argument layer (round 2, J1-guard).

``risk_of`` / ``check`` classify a tool call by the doctypes it names; the
refusal envelope carries ``structure_refused`` and a Desk path. The ORM guard
and the routes are proven with real documents in ``test_write_guard``.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api
from jarvis.exceptions import (
	SensitiveWriteRefusedError,
	StructureRefusedError,
)
from jarvis.tools import _write_risk as wr

R2_12 = {
	"Server Script",
	"Client Script",
	"Webhook",
	"Notification",
	"Auto Email Report",
	"Custom DocPerm",
	"Property Setter",
	"Scheduled Job Type",
	"Website Script",
	"Custom HTML Block",
	"Email Account",
	"Email Domain",
	"User",
	"Role",
	"Role Profile",
	"Module Profile",
	"User Permission",
	"Social Login Key",
	"OAuth Client",
	"Connected App",
	"LDAP Settings",
	# defaults under R2-12 (review cycle 2)
	"Automation Flow",
	"Custom Role",
}


class TestDefinitions(FrappeTestCase):
	def test_structure_list_is_the_spiked_set_plus_accounting_dimension(self):
		self.assertEqual(
			wr.STRUCTURE_DOCTYPES,
			{
				"Custom Field",
				"DocType",
				"Workflow",
				"Inventory Dimension",
				"Service Level Agreement",
				"CRM Settings",
				"Domain Settings",
				"Permission Type",
				"Accounting Dimension",
			},
		)

	def test_sensitive_list_is_r2_12_and_every_one_has_a_risk_line(self):
		self.assertEqual(set(wr.SENSITIVE_DOCTYPES), R2_12)
		for dt in [*R2_12, "Report", "Jarvis Trigger", "Web Page", "Web Form", "Auto Repeat"]:
			self.assertIn(wr.risk_class(dt), wr.RISK_LINES, dt)

	def test_every_structure_doctype_has_a_desk_path(self):
		for dt in wr.STRUCTURE_DOCTYPES:
			self.assertTrue(wr.desk_path(dt).startswith("/app/"), dt)

	def test_agent_write_outcomes_gain_refused_and_partial_in_both_places(self):
		self.assertTrue({"refused", "partial"} <= agent_audit.OUTCOMES)
		options = frappe.get_meta("Jarvis Agent Write").get_field("outcome").options.split("\n")
		self.assertEqual(set(options), set(agent_audit.OUTCOMES))


class TestRiskOf(FrappeTestCase):
	def test_single_structure_create_update_delete(self):
		for dt in wr.STRUCTURE_DOCTYPES - {"Custom Field"}:
			self.assertEqual(wr.risk_of("create_doc", {"doctype": dt, "values": {}}), "structure", dt)
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Custom Field", "values": {"dt": "ToDo"}}),
			"custom_field_new",
		)
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Custom Field", "name": "x", "changes": {"label": "y"}}),
			"custom_field_edit",
		)
		# Only ONE named field: no name, or a batch, is plain structure.
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Custom Field", "changes": {"label": "y"}}), "structure"
		)
		updates = [{"name": "x", "changes": {"label": "y"}}]
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Custom Field", "updates": updates}), "structure"
		)
		self.assertEqual(wr.risk_of("delete_doc", {"doctype": "Custom Field", "name": "x"}), "structure")
		self.assertEqual(wr.risk_of("delete_doc", {"doctype": "DocType", "name": "ToDo"}), "structure")
		self.assertEqual(wr.risk_of("run_import", {"doctype": "Custom Field"}), "structure")

	def test_case_and_space_variants_match(self):
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": " custom field ", "values": {}}), "custom_field_new"
		)
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "SERVER SCRIPT", "values": {}}), "sensitive")
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "workflow", "name": "w", "changes": {}}), "structure"
		)

	def test_any_structure_doctype_in_a_batch_is_structure(self):
		docs = [
			{"doctype": "ToDo", "values": {"description": "a"}},
			{"doctype": "Custom Field", "values": {"dt": "ToDo"}},
		]
		self.assertEqual(wr.risk_of("create_doc", {"docs": docs}), "structure")
		self.assertEqual(wr.risk_of("create_docs", {"docs": docs}), "structure")
		self.assertEqual(
			wr.risk_of("create_docs", {"docs": docs[1:]}), "structure", "a batch of one is a batch"
		)
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Custom Field", "updates": [{"name": "a", "changes": {}}]}),
			"structure",
		)
		self.assertEqual(wr.risk_of("delete_doc", {"doctype": "Workflow", "names": ["a", "b"]}), "structure")

	def test_sensitive_targets(self):
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Webhook", "values": {}}), "sensitive")
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "User", "name": "x@example.com", "changes": {"enabled": 0}}),
			"sensitive",
		)
		self.assertEqual(wr.risk_of("delete_doc", {"doctype": "Property Setter", "name": "x"}), "sensitive")
		self.assertEqual(
			wr.risk_of(
				"update_doc", {"doctype": "Property Setter", "updates": [{"name": "a", "changes": {}}]}
			),
			"sensitive",
		)
		mixed = [{"doctype": "ToDo", "values": {}}, {"doctype": "Client Script", "values": {}}]
		self.assertEqual(wr.risk_of("create_doc", {"docs": mixed}), "sensitive")
		self.assertEqual(wr.risk_of("run_import", {"doctype": "Server Script"}), "sensitive")

	def test_ordinary_and_unknown_doctypes(self):
		self.assertIsNone(wr.risk_of("create_doc", {"doctype": "ToDo", "values": {}}))
		self.assertIsNone(wr.risk_of("create_doc", {"doctype": "No Such Doctype Zz", "values": {}}))
		self.assertIsNone(wr.risk_of("update_doc", {"doctype": "Stock Settings", "name": "Stock Settings"}))
		self.assertIsNone(wr.risk_of("add_comment", {"doctype": "Server Script", "name": "x"}))
		self.assertIsNone(wr.risk_of("create_doc", None))

	def test_conditional_code_fields(self):
		web = {"doctype": "Web Page", "values": {"title": "t", "javascript": "alert(1)"}}
		self.assertEqual(wr.risk_of("create_doc", web), "sensitive")
		self.assertIsNone(wr.risk_of("create_doc", {"doctype": "Web Page", "values": {"title": "t"}}))
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Web Page", "values": {"main_section_html": "<b>x</b>"}}),
			"sensitive",
		)
		blocks = {"page_blocks": [{"web_template": "x", "web_template_values": '{"a": 1}'}]}
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Web Page", "values": blocks}), "sensitive")
		self.assertIsNone(
			wr.risk_of("update_doc", {"doctype": "Web Page", "name": "p", "changes": {"title": "new"}}),
			"an update that leaves the script alone is ordinary",
		)
		self.assertIsNone(
			wr.risk_of("update_doc", {"doctype": "Web Page", "name": "p", "changes": {"javascript": ""}}),
			"clearing a script is not the risk",
		)
		self.assertEqual(
			wr.risk_of(
				"update_doc", {"doctype": "Web Form", "name": "f", "changes": {"client_script": "x()"}}
			),
			"sensitive",
		)
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Website Theme", "name": "t", "changes": {"js": "x()"}}),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of(
				"update_doc", {"doctype": "Website Theme", "name": "t", "changes": {"theme_scss": "a{}"}}
			)
		)
		self.assertEqual(
			wr.risk_of(
				"update_doc",
				{
					"doctype": "Website Settings",
					"name": "Website Settings",
					"changes": {"head_html": "<script>"},
				},
			),
			"sensitive",
		)
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Web Template", "values": {"template": "{{ x }}"}}),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Web Template", "values": {"template": "x", "standard": 1}})
		)

	def test_typed_and_mail_conditions(self):
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Report", "values": {"report_type": "Script Report"}}),
			"sensitive",
		)
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Report", "values": {"report_type": "Query Report"}}),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Report", "values": {"report_type": "Report Builder"}})
		)
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Jarvis Trigger", "values": {"action_type": "Script"}}),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Jarvis Trigger", "values": {"action_type": "LLM"}})
		)
		mails = {"notify_by_email": 1, "recipients": "a@example.com"}
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Auto Repeat", "values": mails}), "sensitive")
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Auto Repeat", "values": {"notify_by_email": 1}})
		)
		self.assertIsNone(
			wr.risk_of("create_doc", {"doctype": "Auto Repeat", "values": {"frequency": "Daily"}})
		)


class TestRunMethodStaysOpen(FrappeTestCase):
	"""R2-13: run_method is not classified by its arguments; the ORM guard judges
	what it saves, and a confirmed card admits the sensitive writes it causes."""

	def test_run_method_is_not_classified(self):
		for args in (
			{"method": "frappe.client.insert", "args": {"doc": {"doctype": "Custom Field"}}},
			{"method": "m", "args": {"target": "User"}},
			{"method": "frappe.core.doctype.user.user.reset_password", "args": {"user": "x"}},
		):
			self.assertIsNone(wr.risk_of("run_method", args), args)
			self.assertIsNone(wr.check("run_method", args), args)

	def test_a_confirmed_run_method_admits_sensitive_never_structure(self):
		(entry,) = wr.allow_entries("run_method", {"method": "some.app.method"})
		self.assertEqual((entry.doctype, entry.multi), ("*", True))
		self.assertEqual(entry.risks, frozenset({"sensitive"}))


class TestCheck(FrappeTestCase):
	def test_structure_and_custom_field_new_are_refused_with_a_desk_path(self):
		with self.assertRaises(StructureRefusedError) as ctx:
			wr.check("create_doc", {"doctype": "Workflow", "values": {}})
		self.assertEqual(ctx.exception.desk_path, "/app/workflow/new")
		self.assertIn("Workflow changes the database structure", str(ctx.exception))
		with self.assertRaises(StructureRefusedError) as ctx:
			wr.check("create_doc", {"doctype": "Custom Field", "values": {"dt": "ToDo"}})
		self.assertEqual(ctx.exception.desk_path, "/app/customize-form")

	def test_a_guarded_structure_write_is_handed_back_only_to_a_caller_that_cards_it(self):
		"""R2-10: one new Custom Field / a single Custom Field edit. Every caller that
		does not say ``guarded=True`` keeps the refusal, and so does everyone while the
		site switch is set."""
		from jarvis.tools import _guarded_structure

		new = ("create_doc", {"doctype": "Custom Field", "values": {"dt": "ToDo"}})
		edit = ("update_doc", {"doctype": "Custom Field", "name": "x", "changes": {"label": "y"}})
		self.assertEqual(wr.check(*new, guarded=True), "custom_field_new")
		self.assertEqual(wr.check(*edit, guarded=True), "custom_field_edit")
		for call in (new, edit):
			with self.assertRaises(StructureRefusedError):
				wr.check(*call)
		with self.assertRaises(StructureRefusedError) as ctx:
			wr.check(*edit)
		self.assertEqual(ctx.exception.desk_path, "/app/custom-field/x")
		frappe.conf[_guarded_structure.OFF_SWITCH] = 1
		self.addCleanup(frappe.conf.pop, _guarded_structure.OFF_SWITCH, None)
		for call in (new, edit):
			with self.assertRaises(StructureRefusedError):
				wr.check(*call, guarded=True)
		# Never for a structure doctype that has no guarded class.
		with self.assertRaises(StructureRefusedError):
			wr.check("create_doc", {"doctype": "DocType", "values": {}}, guarded=True)

	def test_sensitive_is_returned_not_refused(self):
		self.assertEqual(wr.check("create_doc", {"doctype": "Webhook", "values": {}}), "sensitive")
		self.assertIsNone(wr.check("create_doc", {"doctype": "ToDo", "values": {}}))

	def test_whitelisted_db_writers_are_not_reachable(self):
		"""``frappe.db.set_value`` / ``sql`` / ``set_single_value`` are not
		whitelisted, so run_method can never call them (rev 4 item 3)."""
		from jarvis.tools.run_method import run_method

		for method in ("frappe.db.set_value", "frappe.db.sql", "frappe.db.set_single_value"):
			with self.assertRaises(Exception, msg=method) as ctx:
				run_method(method, {})
			self.assertNotIsInstance(ctx.exception, AssertionError)


class TestWave2ArgumentLayer(FrappeTestCase):
	"""Review cycles 1 and 2: imports, access-granting fields, Desk paths, the
	implicit-commit re-raise, the refusal envelope and the log line."""

	def test_new_classifications(self):
		"""A6 + A5: Automation Flow and Custom Role; Jarvis's own configuration
		(argument layer only)."""
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Automation Flow", "values": {}}), "sensitive")
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Custom Role", "values": {}}), "sensitive")
		for dt in (
			"Jarvis Settings",
			"Jarvis Connector",
			"Jarvis Agent Installation",
			"Jarvis User Settings",
		):
			self.assertEqual(
				wr.risk_of("update_doc", {"doctype": dt, "name": dt, "changes": {"x": 1}}), "sensitive", dt
			)
			self.assertIsNone(wr.doc_risk(frappe._dict(doctype=dt, name=dt), "before_validate"), dt)

	def test_import_of_a_conditional_doctype_is_sensitive(self):
		"""M4: a Data Import runs in a job where the ORM guard is inert."""
		for dt in ("Web Page", "Website Theme", "Auto Repeat", "Report", "Web Form", "Jarvis Trigger"):
			self.assertEqual(wr.risk_of("run_import", {"doctype": dt}), "sensitive", dt)
		self.assertIsNone(wr.risk_of("run_import", {"doctype": "ToDo"}))
		(entry,) = wr.allow_entries("run_import", {"doctype": "Web Page"})
		self.assertTrue(entry.multi, "m1: every row of the import, not only the first")

	def test_access_granting_fields_are_sensitive(self):
		"""m6: Employee user_id / create_user_permission, Customer / Supplier portal_users."""
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": "Employee", "name": "E", "changes": {"user_id": "a@b.c"}}),
			"sensitive",
		)
		self.assertEqual(
			wr.risk_of("create_doc", {"doctype": "Employee", "values": {"create_user_permission": 1}}),
			"sensitive",
		)
		self.assertIsNone(
			wr.risk_of("update_doc", {"doctype": "Employee", "name": "E", "changes": {"cell_number": "1"}})
		)
		portal = {"portal_users": [{"user": "a@b.c"}]}
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Supplier", "values": portal}), "sensitive")
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Customer", "values": portal}), "sensitive")
		self.assertEqual(
			wr.risk_line("update_doc", {"doctype": "Employee", "name": "E", "changes": {"user_id": "a"}}),
			wr.RISK_LINES["access"],
		)

	def test_desk_path_names_the_record_of_an_update(self):
		"""m9: an update of a named record links to it."""
		with self.assertRaises(StructureRefusedError) as ctx:
			wr.check("update_doc", {"doctype": "Workflow", "name": "Leave Flow", "changes": {}})
		self.assertEqual(ctx.exception.desk_path, "/app/workflow/Leave Flow")
		with self.assertRaises(StructureRefusedError) as ctx:
			wr.check("create_doc", {"doctype": "Workflow", "values": {}})
		self.assertEqual(ctx.exception.desk_path, "/app/workflow/new")

	def test_implicit_commit_found_through_a_re_raise(self):
		"""m4: country-setup handlers re-raise ImplicitCommitError as another error."""
		try:
			try:
				raise frappe.exceptions.ImplicitCommitError("This statement can cause implicit commit")
			except Exception as inner:
				raise frappe.ValidationError("Country setup failed") from inner
		except Exception as outer:
			self.assertTrue(wr.is_implicit_commit(outer))
		self.assertTrue(
			wr.is_implicit_commit(frappe.ValidationError("x: This statement can cause implicit commit"))
		)
		self.assertFalse(wr.is_implicit_commit(frappe.ValidationError("x")))
		env = api._translate_write_error(
			frappe.ValidationError("Country setup failed: can cause implicit commit"),
			api._msglog_mark(),
			doctype="Company",
		)
		self.assertEqual(env["error"]["code"], "structure_refused")
		self.assertEqual(env["error"]["desk_path"], "/app/company")

	def test_refusal_envelope_names_the_refusing_doctype(self):
		env = wr.refused_envelope(wr.structure_refusal("DocType"))
		self.assertEqual(env["error"]["doctype"], "DocType")

	def test_log_line_is_key_value_with_quoted_names(self):
		from unittest.mock import patch

		with patch("frappe.logger") as logger:
			wr.log_line("sensitive", "Server Script", "my script", "refused", tool="run_method")
		line = logger.return_value.info.call_args[0][0]
		self.assertIn('doctype="Server Script"', line)
		self.assertIn('name="my script"', line)
		self.assertIn("outcome=refused", line)
		self.assertIn('tool="run_method"', line)


class TestRefusalEnvelope(FrappeTestCase):
	def test_translate_carries_code_message_and_desk_path(self):
		env = api._translate_write_error(wr.structure_refusal("Inventory Dimension"), api._msglog_mark())
		self.assertFalse(env["ok"])
		self.assertEqual(env["error"]["code"], "structure_refused")
		self.assertEqual(env["error"]["desk_path"], "/app/inventory-dimension/new")
		self.assertIn("/app/inventory-dimension/new", env["error"]["hint"])
		self.assertIn("set up in Desk, not from chat", env["error"]["message"])

	def test_implicit_commit_is_a_structure_refusal(self):
		e = frappe.exceptions.ImplicitCommitError("This statement can cause implicit commit")
		env = api._translate_write_error(e, api._msglog_mark())
		self.assertEqual(env["error"]["code"], "structure_refused")
		self.assertEqual(api._preview_error(e)["error"]["code"], "structure_refused")

	def test_preview_error_uses_the_refusal_code(self):
		env = api._preview_error(wr.structure_refusal("DocType"))
		self.assertEqual(env["error"]["code"], "structure_refused")
		self.assertEqual(env["error"]["desk_path"], "/app/doctype/new")
