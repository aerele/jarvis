"""Tests for the "Jarvis User Activity" Script Report and the migration that turns
the old "Agent Write Log" report into it (plan 5 task 6).

The migration pair is exercised by calling each patch's ``execute()`` directly (never
``bench migrate``): the rename runs in pre_model_sync, the reload in post_model_sync.
The fixtures mimic a live site: an old report row whose ``modified`` is NEWER than
the new JSON (so ordinary sync would skip the file), a Custom Role and a Workspace
shortcut pointing at the old name. The class-level rollback restores whatever the
test site held (it normally has the old row from an earlier migrate)."""

import json
import os
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.patches.v2_32_reload_jarvis_user_activity_report import execute as reload_patch
from jarvis.patches.v2_32_rename_agent_write_log_report import execute as rename_patch

OLD = "Agent Write Log"
NEW = "Jarvis User Activity"
SHORTCUT_LABEL = "uar-test-shortcut"
LINK_LABEL = "uar-test-link"
SIDEBAR_LABEL = "uar-test-sidebar"
WORKSPACE = "uar-test-workspace"
PLAIN = "uar-plain@example.com"
ADMIN = "uar-admin@example.com"

SCOPE = "Covers actions taken in Jarvis only (not Desk)."


def _report_dir(name):
	return frappe.get_app_path("jarvis", "jarvis", "report", name)


def _json():
	with open(os.path.join(_report_dir("jarvis_user_activity"), "jarvis_user_activity.json")) as f:
		return json.load(f)


def _wipe_reports():
	"""Remove both report rows (and what hangs off them) so each test starts from a
	known state whatever the test site currently holds. Raw deletes: the real
	delete path would try to remove report folders from disk."""
	for name in (OLD, NEW):
		frappe.db.delete("Has Role", {"parent": name, "parenttype": "Report"})
		frappe.db.delete("Custom Role", {"report": name})
		frappe.db.delete("Report", {"name": name})
	frappe.db.delete("Workspace", {"name": WORKSPACE})
	frappe.db.delete("Workspace Shortcut", {"label": SHORTCUT_LABEL})
	frappe.db.delete("Workspace Link", {"label": LINK_LABEL})
	if frappe.db.table_exists("Workspace Sidebar Item"):
		frappe.db.delete("Workspace Sidebar Item", {"label": SIDEBAR_LABEL})


def _insert_old_report():
	"""A standard Report row like the old JSON, with a LATER ``modified`` than the new
	JSON. ``in_import`` stops developer_mode's on_update from exporting the fixture
	to a report folder on disk."""
	frappe.flags.in_import = True
	try:
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": OLD,
				"ref_doctype": "Jarvis Agent Write",
				"report_type": "Script Report",
				"is_standard": "Yes",
				"module": "Jarvis",
				"roles": [{"role": "System Manager"}, {"role": "Report Manager"}],
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.flags.in_import = False
	frappe.db.set_value("Report", OLD, "modified", "2030-01-01 00:00:00", update_modified=False)


def _insert_custom_role():
	frappe.get_doc({"doctype": "Custom Role", "report": OLD, "roles": [{"role": "Report Manager"}]}).insert(
		ignore_permissions=True
	)


def _insert_workspace_shortcut():
	spec = {
		"doctype": "Workspace",
		"name": WORKSPACE,
		"label": WORKSPACE,
		"title": WORKSPACE,
		"module": "Jarvis",
		"public": 1,
		"content": "[]",
		"shortcuts": [{"label": SHORTCUT_LABEL, "type": "Report", "link_to": OLD}],
		"links": [{"label": LINK_LABEL, "type": "Link", "link_type": "Report", "link_to": OLD}],
	}
	if frappe.get_meta("Workspace").has_field("type"):  # Frappe 16
		spec["type"] = "Workspace"
	frappe.flags.in_import = True
	try:
		frappe.get_doc(spec).insert(ignore_permissions=True)
	finally:
		frappe.flags.in_import = False


def _insert_sidebar_item():
	"""Frappe 16 only: a Workspace Sidebar Item pointing at the old report. Inserted
	raw (it is a child row; the parent sidebar is not what is under test)."""
	if frappe.db.table_exists("Workspace Sidebar Item"):
		frappe.get_doc(
			{
				"doctype": "Workspace Sidebar Item",
				"name": frappe.generate_hash(length=10),
				"parent": "uar-test-sidebar-parent",
				"parenttype": "Workspace Sidebar",
				"parentfield": "items",
				"label": SIDEBAR_LABEL,
				"type": "Link",
				"link_type": "Report",
				"link_to": OLD,
			}
		).db_insert()


def _link_target():
	return frappe.db.get_value("Workspace Link", {"label": LINK_LABEL}, "link_to")


def _sidebar_target():
	if frappe.db.table_exists("Workspace Sidebar Item"):
		return frappe.db.get_value("Workspace Sidebar Item", {"label": SIDEBAR_LABEL}, "link_to")
	return NEW  # nothing to move on this Frappe version


def _shortcut_target():
	return frappe.db.get_value("Workspace Shortcut", {"label": SHORTCUT_LABEL}, "link_to")


def _custom_role_report():
	return frappe.db.get_value("Custom Role", {"report": ["in", [OLD, NEW]]}, "report")


def _report_names():
	return sorted(frappe.get_all("Report", filters={"name": ["in", [OLD, NEW]]}, pluck="name"))


def _roles(name):
	return sorted(frappe.get_all("Has Role", filters={"parent": name, "parenttype": "Report"}, pluck="role"))


class TestReportMigration(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_reports()
		_insert_old_report()
		_insert_custom_role()
		_insert_workspace_shortcut()
		_insert_sidebar_item()

	def tearDown(self):
		frappe.flags.in_import = False
		frappe.set_user("Administrator")

	def test_fixture_is_newer_than_the_json(self):
		# The point of the scenario: plain sync would skip the JSON for this row.
		from frappe.utils import get_datetime

		self.assertGreater(
			get_datetime(frappe.db.get_value("Report", OLD, "modified")), get_datetime(_json()["modified"])
		)

	def test_rename_moves_row_and_keeps_links(self):
		rename_patch()

		self.assertEqual(_report_names(), [NEW])
		self.assertEqual(_custom_role_report(), NEW)
		self.assertEqual(_shortcut_target(), NEW)
		# the child rows followed the parent
		self.assertEqual(_roles(NEW), ["Report Manager", "System Manager"])

	def test_reload_refreshes_a_row_newer_than_the_json(self):
		rename_patch()
		# rename_doc changed the name (and the autoname field) only; the row still
		# describes the old report
		self.assertEqual(frappe.db.get_value("Report", NEW, "ref_doctype"), "Jarvis Agent Write")

		reload_patch()

		spec = _json()
		self.assertEqual(_report_names(), [NEW])
		row = frappe.db.get_value(
			"Report", NEW, ["report_name", "ref_doctype", "module", "is_standard"], as_dict=True
		)
		self.assertEqual(row.report_name, NEW)
		self.assertEqual(row.ref_doctype, "Jarvis Activity Event")
		self.assertEqual(row.ref_doctype, spec["ref_doctype"])
		self.assertEqual(row.module, "Jarvis")
		self.assertEqual(row.is_standard, "Yes")
		self.assertEqual(_roles(NEW), sorted(r["role"] for r in spec["roles"]))
		# the reload (for_reload) must not take the Custom Role or the shortcut with it
		self.assertEqual(_custom_role_report(), NEW)
		self.assertEqual(_shortcut_target(), NEW)

	def test_rerun_is_a_noop(self):
		rename_patch()
		reload_patch()
		before = (
			_report_names(),
			frappe.db.get_value("Report", NEW, ["report_name", "ref_doctype", "module"]),
			_roles(NEW),
			_custom_role_report(),
			_shortcut_target(),
		)

		rename_patch()
		reload_patch()

		after = (
			_report_names(),
			frappe.db.get_value("Report", NEW, ["report_name", "ref_doctype", "module"]),
			_roles(NEW),
			_custom_role_report(),
			_shortcut_target(),
		)
		self.assertEqual(before, after)
		self.assertEqual(frappe.db.count("Report", {"name": NEW}), 1)

	def test_fresh_site_without_the_old_report(self):
		_wipe_reports()
		rename_patch()  # nothing to rename
		self.assertEqual(_report_names(), [])

		reload_patch()  # a plain first import

		self.assertEqual(_report_names(), [NEW])
		self.assertEqual(frappe.db.get_value("Report", NEW, "report_name"), NEW)
		self.assertEqual(frappe.db.get_value("Report", NEW, "ref_doctype"), "Jarvis Activity Event")

	def test_workspace_links_move_even_if_the_dynamic_link_rewrite_misses_them(self):
		# rename_doc's Dynamic Link rewrite reads a cache that can be hours stale.
		# With it disabled, the patch's own sweep must still retarget every link.
		with mock.patch("frappe.model.rename_doc.rename_dynamic_links"):
			rename_patch()

		self.assertEqual(_report_names(), [NEW])
		self.assertEqual(_shortcut_target(), NEW)
		self.assertEqual(_link_target(), NEW)
		self.assertEqual(_sidebar_target(), NEW)

	def test_rename_moves_workspace_link_rows_in_the_normal_path(self):
		rename_patch()
		self.assertEqual(_link_target(), NEW)
		self.assertEqual(_sidebar_target(), NEW)

	def test_both_rows_exist_links_move_and_old_row_is_dropped(self):
		reload_patch()  # the new row exists alongside the old one
		self.assertEqual(_report_names(), [OLD, NEW])
		frappe.flags.in_patch = True
		self.addCleanup(setattr, frappe.flags, "in_patch", False)

		rename_patch()

		self.assertEqual(_report_names(), [NEW])
		self.assertEqual(_custom_role_report(), NEW)
		self.assertEqual(frappe.db.count("Custom Role", {"report": NEW}), 1)
		self.assertEqual(_shortcut_target(), NEW)
		self.assertEqual(_link_target(), NEW)
		self.assertEqual(_sidebar_target(), NEW)
		self.assertFalse(frappe.db.exists("Has Role", {"parent": OLD, "parenttype": "Report"}))

		rename_patch()  # and a re-run is a no-op
		self.assertEqual(_report_names(), [NEW])

	def test_both_rows_exist_keeps_the_new_reports_own_custom_role(self):
		reload_patch()
		frappe.get_doc(
			{"doctype": "Custom Role", "report": NEW, "roles": [{"role": "System Manager"}]}
		).insert(ignore_permissions=True)
		frappe.flags.in_patch = True
		self.addCleanup(setattr, frappe.flags, "in_patch", False)

		rename_patch()

		self.assertEqual(_report_names(), [NEW])
		roles = frappe.get_all("Custom Role", filters={"report": NEW}, pluck="name")
		self.assertEqual(len(roles), 1)
		self.assertEqual(
			frappe.get_all("Has Role", filters={"parent": roles[0]}, pluck="role"), ["System Manager"]
		)


class TestReportFolders(FrappeTestCase):
	def test_old_folder_is_gone_and_new_one_complete(self):
		self.assertFalse(os.path.exists(_report_dir("agent_write_log")))
		for fn in (
			"__init__.py",
			"jarvis_user_activity.json",
			"jarvis_user_activity.py",
			"jarvis_user_activity.js",
		):
			self.assertTrue(os.path.isfile(os.path.join(_report_dir("jarvis_user_activity"), fn)), fn)

	def test_json_identity(self):
		spec = _json()
		self.assertEqual(spec["name"], NEW)
		self.assertEqual(spec["report_name"], NEW)
		self.assertEqual(spec["ref_doctype"], "Jarvis Activity Event")
		self.assertEqual(spec["report_type"], "Script Report")
		self.assertEqual(spec["is_standard"], "Yes")
		self.assertEqual(spec["module"], "Jarvis")
		self.assertEqual({r["role"] for r in spec["roles"]}, {"System Manager", "Jarvis Admin"})

	def test_a_prepared_report_cannot_bypass_the_export_limit(self):
		# D9: a prepared report runs ``execute`` once in a background job and serves the saved
		# rows to every export, so the 50,000-row ceiling (and the per-request admin gate) would
		# be skipped. The report is never prepared.
		spec = _json()
		self.assertEqual(spec["disable_prepared_report"], 1)
		self.assertEqual(spec["prepared_report"], 0)

	def test_the_report_has_no_private_scope_line_alias(self):
		# D7: one name for the scope line, ``activity_report.scope_line``.
		from jarvis import activity_report
		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		self.assertFalse(hasattr(report, "_scope_line"))
		self.assertTrue(callable(activity_report.scope_line))

	def test_the_auto_email_report_method_the_export_signal_relies_on_still_exists(self):
		# D7: ``_from_auto_email_report`` looks for this method name in this file in the call
		# stack. A Frappe upgrade that renames or moves it would silently turn a scheduled mail
		# into a 500-row screen export; this fails the build instead.
		import inspect

		from frappe.email.doctype.auto_email_report.auto_email_report import AutoEmailReport

		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		method = getattr(AutoEmailReport, report._AUTO_EMAIL_METHOD, None)
		self.assertTrue(callable(method), "AutoEmailReport.get_report_content is gone")
		code = inspect.unwrap(method).__code__
		self.assertEqual(code.co_name, report._AUTO_EMAIL_METHOD)
		self.assertTrue(code.co_filename.replace("\\", "/").endswith(report._AUTO_EMAIL_FILE))

	def test_no_em_dash_in_report_files(self):
		for fn in ("jarvis_user_activity.json", "jarvis_user_activity.py", "jarvis_user_activity.js"):
			with open(os.path.join(_report_dir("jarvis_user_activity"), fn)) as f:
				self.assertNotIn("—", f.read(), fn)

	def test_every_translation_call_in_the_filter_script_is_a_literal(self):
		# Translation extraction reads string literals: __(variable) is invisible to it.
		import re

		with open(os.path.join(_report_dir("jarvis_user_activity"), "jarvis_user_activity.js")) as f:
			src = f.read()
		calls = re.findall(r"__\(([^)]*)\)", re.sub(r"//[^\n]*", "", src))
		self.assertGreater(len(calls), 30)
		for arg in calls:
			self.assertRegex(arg, r'^"[^"]+"$')
		self.assertIn("Open in Jarvis", src)


class TestReportExecute(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _user(self, email, role=None):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Uar", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		if role:
			frappe.get_doc("User", email).add_roles(role)
		return email

	@staticmethod
	def _execute(filters):
		from jarvis.jarvis.report.jarvis_user_activity.jarvis_user_activity import execute

		return execute(filters)

	def test_jarvis_admin_runs_both_views(self):
		frappe.set_user(self._user(ADMIN, "Jarvis Admin"))
		for view, first_col in (("Events", "at"), ("Summary", "user")):
			columns, data, message = self._execute({"view": view})
			self.assertEqual(columns[0]["fieldname"], first_col)
			self.assertIsInstance(data, list)
			self.assertIn(SCOPE, message)
			self.assertNotIn("—", message)

	def test_default_view_is_events_and_filters_may_be_json(self):
		columns, _data, _message = self._execute(json.dumps({"from_date": "2026-01-01"}))
		self.assertEqual(
			[c["fieldname"] for c in columns],
			[
				"at",
				"actor",
				"actor_name",
				"activity",
				"label",
				"method",
				"ref_doctype",
				"ref_name",
				"rows",
				"outcome",
				"channel",
				"code",
			],
		)

	def test_unknown_view_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._execute({"view": "Everything"})

	def test_plain_user_is_refused(self):
		frappe.set_user(self._user(PLAIN))
		for view in ("Events", "Summary"):
			with self.assertRaises(frappe.PermissionError):
				self._execute({"view": view})

	def test_scope_line_states_each_source_start(self):
		both = {"agent": "2026-10-01 09:00:00", "app": "2026-11-04 10:00:00"}
		with mock.patch("jarvis.activity_report.first_event_at", return_value=both):
			_c, _d, message = self._execute({})
		self.assertIn(
			f"{SCOPE} Agent activity recorded from 2026-10-01; app actions from 2026-11-04.", message
		)

		agent_only = {"agent": "2026-10-01 09:00:00", "app": None}
		with mock.patch("jarvis.activity_report.first_event_at", return_value=agent_only):
			_c, _d, message = self._execute({})
		self.assertIn(
			f"{SCOPE} Agent activity recorded from 2026-10-01. App actions are not recorded yet.", message
		)
		self.assertNotIn("app actions from", message)

		app_only = {"agent": None, "app": "2026-11-04 10:00:00"}
		with mock.patch("jarvis.activity_report.first_event_at", return_value=app_only):
			_c, _d, message = self._execute({})
		self.assertIn(f"{SCOPE} No agent activity recorded yet; app actions from 2026-11-04.", message)

		neither = {"agent": None, "app": None}
		with mock.patch("jarvis.activity_report.first_event_at", return_value=neither):
			_c, _d, message = self._execute({})
		self.assertIn(f"{SCOPE} No agent activity recorded yet. App actions are not recorded yet.", message)

	def test_each_scope_line_case_is_one_translatable_sentence_template(self):
		from jarvis import activity_report
		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		seen = []

		def marked(template):
			seen.append(template)
			return template

		cases = (
			({"agent": "2026-10-01", "app": "2026-11-04"}, 2),
			({"agent": "2026-10-01", "app": None}, 1),
			({"agent": None, "app": "2026-11-04"}, 1),
			({"agent": None, "app": None}, 0),
		)
		for first, args in cases:
			seen.clear()
			with mock.patch.object(activity_report, "_", marked):
				line = activity_report.scope_line(first)
			self.assertEqual(len(seen), 1, first)  # one template, not fragments to join
			self.assertTrue(seen[0].startswith("Covers actions taken in Jarvis only (not Desk). "))
			self.assertEqual(sum(f"{{{i}}}" in seen[0] for i in range(2)), args, first)
			self.assertTrue(line.endswith("."))

	def test_events_truncation_is_stated_for_the_screen_and_for_an_export(self):
		page = {"rows": [], "next_cursor": "x", "truncated": True}
		with mock.patch("jarvis.activity_report.events", return_value=page) as ev:
			_c, _d, message = self._execute({"user": "a@example.com"})
		self.assertIn(" Showing the newest 500 events; narrow the filters or export to see more.", message)
		self.assertNotIn("50,000", message)
		self.assertTrue(ev.call_args.kwargs["for_export"])  # the paging ceiling, not the 200 default
		self.assertEqual(ev.call_args.kwargs["limit"], 500)

		page["truncated"] = False
		with mock.patch("jarvis.activity_report.events", return_value=page):
			_c, _d, message = self._execute({})
		self.assertNotIn("Showing the newest", message)

		page["truncated"] = True
		with (
			mock.patch("jarvis.activity_report.events", return_value=page) as ev,
			mock.patch.dict(frappe.local.form_dict, {"cmd": "frappe.desk.query_report.export_query"}),
		):
			_c, _d, message = self._execute({})
		self.assertEqual(ev.call_args.kwargs["limit"], 50_000)
		self.assertIn("Showing the newest 50,000 events; narrow the date range to see the rest.", message)

	def test_a_normal_view_never_asks_for_fifty_thousand_rows(self):
		for fd in ({}, {"cmd": "frappe.desk.query_report.run"}, {"cmd": "frappe.client.get_list"}):
			page = {"rows": [], "next_cursor": None, "truncated": False}
			with (
				mock.patch("jarvis.activity_report.events", return_value=page) as ev,
				mock.patch.dict(frappe.local.form_dict, fd),
			):
				self._execute({})
			self.assertEqual(ev.call_args.kwargs["limit"], 500, fd)

	def test_a_desk_export_is_recognised_in_the_request_and_in_the_background_job(self):
		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		self.assertFalse(report._is_export())
		with mock.patch.dict(frappe.local.form_dict, {"cmd": "frappe.desk.query_report.export_query"}):
			self.assertTrue(report._is_export())
		frappe.local.job = frappe._dict(method="frappe.desk.query_report.run_export_query_job")
		try:
			self.assertTrue(report._is_export())
			frappe.local.job = frappe._dict(method="jarvis.activity_rollup.rollup_days")
			self.assertFalse(report._is_export())
		finally:
			del frappe.local.job
		self.assertFalse(report._is_export())

	def test_an_auto_email_report_gets_the_export_limit_and_the_truncation_message(self):
		# Frappe hands a script report nothing but the filters (no cmd, no job), so the
		# mail used to get the 500-row screen cut with no hint. Drive the real method.
		frappe.set_user("Administrator")
		# The report row may have been wiped by a patch test earlier in the run.
		frappe.reload_doc("jarvis", "report", "jarvis_user_activity", force=True)
		doc = frappe.new_doc("Auto Email Report")
		doc.update(
			{
				"report": "Jarvis User Activity",
				"report_type": "Script Report",
				"user": "Administrator",
				"format": "HTML",
				"filters": json.dumps({"view": "Events"}),
				"no_of_rows": 10,
			}
		)
		page = {"rows": [], "next_cursor": "x", "truncated": True}
		seen = {}

		def run(*a, **kw):
			seen["limit"] = kw["limit"]
			return page

		with mock.patch("jarvis.activity_report.events", side_effect=run):
			doc.get_report_content()
		self.assertEqual(seen["limit"], 50_000)

		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		self.assertFalse(report._from_auto_email_report())  # not on that call path
		with mock.patch("jarvis.activity_report.events", side_effect=run):
			_c, _d, message = self._execute({})
		self.assertEqual(seen["limit"], 500)
		self.assertIn("Showing the newest 500 events", message)

	def test_the_auto_email_signal_is_that_method_in_that_file_only(self):
		from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report

		def under(filename, name):
			ns = {}
			exec(
				compile(f"def {name}(cb):\n\treturn cb()\n", filename, "exec"),
				ns,
			)
			return ns[name](report._from_auto_email_report)

		here = "/x/frappe/email/doctype/auto_email_report/auto_email_report.py"
		self.assertTrue(under(here, "get_report_content"))
		self.assertFalse(under(here, "send"))
		self.assertFalse(under("/x/other/report.py", "get_report_content"))

	def test_the_screen_limit_and_the_export_limit_on_real_rows(self):
		import datetime as dt

		who = "uar-limit@example.com"
		day = dt.date.today() - dt.timedelta(days=3)
		base = dt.datetime.combine(day, dt.time(10, 0, 0))
		frappe.db.bulk_insert(
			"Jarvis Activity Event",
			[
				"name",
				"at",
				"actor",
				"actor_name",
				"event",
				"method",
				"outcome",
				"row_count",
				"creation",
				"modified",
				"owner",
				"modified_by",
			],
			[
				(
					f"uarlim{i:05d}",
					base + dt.timedelta(seconds=i),
					who,
					"L",
					"read",
					"get_list",
					"ok",
					1,
					base,
					base,
					"Administrator",
					"Administrator",
				)
				for i in range(505)
			],
		)
		filters = {"user": who, "from_date": str(day), "to_date": str(day)}
		_c, data, message = self._execute(filters)
		self.assertEqual(len(data), 500)
		self.assertEqual(data[0]["at"], base + dt.timedelta(seconds=504))  # the newest ones
		self.assertIn("Showing the newest 500 events; narrow the filters or export to see more.", message)
		with mock.patch.dict(frappe.local.form_dict, {"cmd": "frappe.desk.query_report.export_query"}):
			_c, data, message = self._execute(filters)
		self.assertEqual(len(data), 505)
		self.assertNotIn("Showing the newest", message)

	def test_events_view_is_not_capped_at_the_api_default_page(self):
		# Not mocked: the report must ask for the export ceiling, not the 200-row page.
		import datetime as dt

		who = "uar-many@example.com"
		day = dt.date.today() - dt.timedelta(days=3)  # inside the kept window (events() clamps to it)
		n = 205
		for i in range(n):
			frappe.get_doc(
				{
					"doctype": "Jarvis Activity Event",
					"at": dt.datetime.combine(day, dt.time(10, 0, 0)) + dt.timedelta(seconds=i),
					"actor": who,
					"actor_name": "Many",
					"event": "read",
					"method": "get_list",
					"outcome": "ok",
					"row_count": 1,
				}
			).insert(ignore_permissions=True, ignore_links=True)

		_c, data, message = self._execute({"user": who, "from_date": str(day), "to_date": str(day)})

		self.assertEqual(len(data), n)
		self.assertNotIn("Showing the newest", message)

	def test_first_event_at_is_gated(self):
		from jarvis import activity_report

		frappe.set_user(self._user(PLAIN))
		with self.assertRaises(frappe.PermissionError):
			activity_report.first_event_at()

	def test_summary_rows_carry_numbers_and_joined_flags(self):
		person = {
			"user": "a@example.com",
			"name": "A",
			"turns": 3,
			"tokens": 40,
			"reads": 5,
			"rows_read": 50,
			"exports": 1,
			"rows_exported": 60000,
			"writes": 2,
			"erp_deletes": 51,
			"refused_denied": 0,
			"failed": 0,
			"actions": 4,
			"flags": [
				{"key": "delete_burst", "label": "51 deletes in 60 minutes", "value": 51},
				{"key": "big_export", "label": "Export of 60000 rows", "value": 60000},
			],
		}
		res = {
			"people": [person],
			"first_event_at": {"agent": None, "app": None},
			"logging_health": {"warn": True},
			"thresholds": {},
		}
		with mock.patch("jarvis.activity_report.summary", return_value=res):
			columns, data, message = self._execute({"view": "Summary"})
		self.assertEqual(data[0]["flags"], "51 deletes in 60 minutes; Export of 60000 rows")
		self.assertEqual(data[0]["erp_deletes"], 51)
		self.assertEqual(set(data[0]), {c["fieldname"] for c in columns})
		self.assertIn("No agent activity recorded yet", message)
		self.assertIn("logging may be broken", message)

	def test_column_order_puts_flags_after_the_person_and_what_before_method(self):
		columns, _d, _m = self._execute({"view": "Summary"})
		names = [c["fieldname"] for c in columns]
		self.assertEqual(names[:3], ["user", "name", "flags"])
		columns, _d, _m = self._execute({"view": "Events"})
		names = [c["fieldname"] for c in columns]
		self.assertLess(names.index("label"), names.index("method"))
		self.assertEqual(next(c["label"] for c in columns if c["fieldname"] == "rows"), "Records")
		columns, _d, _m = self._execute({"view": "Summary"})
		labels = {c["fieldname"]: c["label"] for c in columns}
		self.assertEqual((labels["rows_read"], labels["rows_exported"]), ("Records Read", "Records Exported"))
		self.assertNotIn("Rows", " ".join(labels.values()))

	def test_a_summary_still_being_prepared_says_so_and_has_no_rows(self):
		res = {"people": [], "preparing": True, "first_event_at": {}, "logging_health": {}}
		with mock.patch("jarvis.activity_report.summary", return_value=res):
			columns, data, message = self._execute({"view": "Summary"})
		self.assertEqual(data, [])
		self.assertTrue(columns)
		self.assertEqual(
			message, "Activity totals are still being prepared. Please try again in a few minutes."
		)

	def test_an_events_view_before_the_tables_exist_says_so_instead_of_failing(self):
		with mock.patch.object(frappe.db, "table_exists", return_value=False):
			columns, data, message = self._execute({"view": "Events"})
		self.assertEqual((data, bool(columns)), ([], True))
		self.assertIn("still being prepared", message)

	def test_filters_that_are_not_an_object_are_refused_clearly(self):
		for bad in (["a"], "[1]", "[]", "5", "null", "not json", "true", "0"):
			with self.subTest(filters=bad), self.assertRaises(frappe.ValidationError) as cm:
				self._execute(bad)
			self.assertIn("object", str(cm.exception))
		for ok in (None, "", [], {}, "{}", {"view": "Events"}):
			self._execute(ok)

	def _summary_message(self, **overrides):
		res = {
			"people": [],
			"first_event_at": {"agent": None, "app": None},
			"logging_health": {"warn": False},
			"thresholds": {},
			"stale_days": 0,
			"clamped": False,
			"partial": False,
			"excluded_days": 0,
		}
		res.update(overrides)
		with mock.patch("jarvis.activity_report.summary", return_value=res):
			_c, _d, message = self._execute({"view": "Summary"})
		self.assertNotIn("—", message)
		return message

	def test_summary_message_is_quiet_when_nothing_to_say(self):
		message = self._summary_message()
		self.assertNotIn("kept for", message)
		self.assertNotIn("prepared", message)
		self.assertNotIn("paused", message)
		self.assertNotIn("behind", message)

	def test_summary_message_stale_days_singular_and_plural(self):
		self.assertIn(" Totals for 1 day are still being prepared.", self._summary_message(stale_days=1))
		self.assertIn(" Totals for 3 days are still being prepared.", self._summary_message(stale_days=3))

	def test_summary_message_clamped(self):
		self.assertIn(" Data is kept for 90 days.", self._summary_message(clamped=True))

	def test_summary_message_scheduler_disabled(self):
		message = self._summary_message(logging_health={"warn": False, "scheduler_disabled": True})
		self.assertIn(
			" Background jobs are paused on this site, so daily totals are not being updated.", message
		)

	def test_summary_message_rollup_lag_only_above_one_day(self):
		self.assertNotIn(
			"behind", self._summary_message(logging_health={"warn": False, "rollup_lag_days": 1})
		)
		self.assertIn(
			" Daily totals are 4 days behind. Ask your system administrator to check the scheduler.",
			self._summary_message(logging_health={"warn": False, "rollup_lag_days": 4}),
		)

	def test_summary_message_partial_days_are_not_included(self):
		message = self._summary_message(stale_days=9, partial=True, excluded_days=2)
		self.assertIn(" Totals for 9 days are still being prepared.", message)
		self.assertIn(" Totals for the 2 oldest days in this range are not included yet.", message)
		self.assertIn(
			" Totals for the oldest day in this range are not included yet.",
			self._summary_message(stale_days=8, partial=True, excluded_days=1),
		)
		self.assertNotIn("not included", self._summary_message(stale_days=3))

	def test_summary_message_order(self):
		message = self._summary_message(
			clamped=True,
			stale_days=2,
			partial=True,
			excluded_days=2,
			logging_health={"warn": True, "scheduler_disabled": True, "rollup_lag_days": 3},
		)
		marks = [
			SCOPE,
			"Data is kept for 90 days.",
			"Totals for 2 days are still being prepared.",
			"Totals for the 2 oldest days in this range are not included yet.",
			"logging may be broken.",
			"Background jobs are paused",
			"Daily totals are 3 days behind.",
		]
		positions = [message.index(m) for m in marks]
		self.assertEqual(positions, sorted(positions))

	def test_summary_range_before_the_horizon_states_the_clamp_unmocked(self):
		_c, _d, message = self._execute({"view": "Summary", "from_date": "2020-01-01"})
		self.assertIn("Data is kept for 90 days.", message)

		_c, _d, message = self._execute({"view": "Summary"})
		self.assertNotIn("Data is kept for 90 days.", message)

	def test_events_view_states_the_horizon_only_when_the_range_starts_before_it(self):
		from jarvis import activity_report

		page = {"rows": [], "next_cursor": None, "truncated": False}
		with mock.patch("jarvis.activity_report.events", return_value=page):
			_c, _d, message = self._execute({"from_date": "2020-01-01"})
			self.assertIn(" Data is kept for 90 days.", message)

			_c, _d, message = self._execute({"from_date": str(activity_report.horizon_day())})
			self.assertNotIn("kept for", message)

			_c, _d, message = self._execute({})
			self.assertNotIn("kept for", message)
