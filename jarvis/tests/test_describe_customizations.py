"""Integration tests for describe_customizations: two-union discovery, the
per-user permission fence (counts follow by construction), scope/match, the
system-generated exclusion, and the budget bound."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.site_profile.collect import collect_profile
from jarvis.site_profile.fence import fence_for_user
from jarvis.tools.describe_customizations import describe_customizations

FAKE_APP = "custdisc_s2_app"
FAKE_MODULE = "CustDisc S2 Module"
DT_OPEN = "CustDisc S2 Open DT"
DT_RESTRICTED = "CustDisc S2 Restricted DT"
DT_SHIPPED = "CustDisc S2 Shipped DT"
CF_TODO = "custdisc_s2_probe"
CF_SYSGEN = "custdisc_s2_sysgen"
LIMITED_USER = "custdisc-limited@example.com"

_APPS_SEAM = "jarvis.site_profile.apps._installed_apps"
_FAKE_INSTALLED = ["frappe", "erpnext", "jarvis", FAKE_APP]


def _make_doctype(name: str, module: str, permissions: list[dict]) -> None:
	if frappe.db.exists("DocType", name):
		return
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": name,
			"module": module,
			"custom": 1,
			"fields": [{"fieldname": "title", "fieldtype": "Data", "label": "Title"}],
			"permissions": permissions,
		}
	).insert(ignore_permissions=True)


class _CustDiscFixtures(FrappeTestCase):
	"""Shared fixture base: both test classes below need the doctype/module
	fixtures, and unittest runs each class's setUpClass/tearDownClass
	independently - fixtures created in one class are gone before the next."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		if not frappe.db.exists("Module Def", FAKE_MODULE):
			frappe.get_doc(
				{
					"doctype": "Module Def",
					"module_name": FAKE_MODULE,
					"custom": 1,
					"app_name": FAKE_APP,
				}
			).insert(ignore_permissions=True)

		_make_doctype(DT_OPEN, "Custom", [{"role": "All", "read": 1}])
		_make_doctype(
			DT_RESTRICTED,
			"Custom",
			[{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
		)
		# Simulate an app-SHIPPED doctype: created custom=1 (UI path works
		# without developer mode), then flipped to custom=0 at the DB level so
		# only the module->custom-app union can discover it.
		_make_doctype(DT_SHIPPED, FAKE_MODULE, [{"role": "All", "read": 1}])
		frappe.db.set_value("DocType", DT_SHIPPED, "custom", 0, update_modified=False)
		frappe.clear_cache(doctype=DT_SHIPPED)

		# Delete-and-recreate (never skip-if-exists): a stale row from an
		# earlier aborted run may carry the wrong is_system_generated flag.
		# NOTE: create_custom_field DEFAULTS to is_system_generated=True - the
		# probe must pass False explicitly or the collector's own filter
		# (correctly) hides it.
		for dt_, fieldname in (("ToDo", CF_TODO), ("Note", CF_SYSGEN)):
			stale = frappe.db.get_value("Custom Field", {"dt": dt_, "fieldname": fieldname})
			if stale:
				frappe.delete_doc("Custom Field", stale, force=True, ignore_permissions=True)
		create_custom_field(
			"ToDo",
			{"fieldname": CF_TODO, "label": "CustDisc S2 Probe", "fieldtype": "Data", "in_list_view": 1},
			is_system_generated=False,
		)
		create_custom_field(
			"Note",
			{"fieldname": CF_SYSGEN, "label": "CustDisc S2 SysGen", "fieldtype": "Data"},
			is_system_generated=True,
		)

		if not frappe.db.exists("User", LIMITED_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": LIMITED_USER,
					"first_name": "CustDisc Limited",
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, filters in (
			("Custom Field", {"dt": "ToDo", "fieldname": CF_TODO}),
			("Custom Field", {"dt": "Note", "fieldname": CF_SYSGEN}),
		):
			name = frappe.db.get_value(dt, filters)
			if name:
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		# DT_SHIPPED sits at custom=0 (the shipped-doctype simulation) and
		# standard doctypes refuse deletion outside developer mode - flip it
		# back to custom=1 first.
		if frappe.db.exists("DocType", DT_SHIPPED):
			frappe.db.set_value("DocType", DT_SHIPPED, "custom", 1, update_modified=False)
			frappe.clear_cache(doctype=DT_SHIPPED)
		for name in (DT_OPEN, DT_RESTRICTED, DT_SHIPPED):
			if frappe.db.exists("DocType", name):
				frappe.delete_doc("DocType", name, force=True, ignore_permissions=True)
		if frappe.db.exists("Module Def", FAKE_MODULE):
			frappe.delete_doc("Module Def", FAKE_MODULE, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="ToDo")
		frappe.clear_cache(doctype="Note")
		super().tearDownClass()


class TestDescribeCustomizations(_CustDiscFixtures):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ #
	# discovery
	# ------------------------------------------------------------------ #

	def test_two_union_discovery(self):
		"""custom=1 doctypes AND custom=0 doctypes shipped by a custom app's
		module are both found; the shipped one is invisible to the naive
		custom=1 filter (guarded by asserting its DB flag really is 0)."""
		self.assertEqual(frappe.db.get_value("DocType", DT_SHIPPED, "custom"), 0)
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			names = {d["name"] for d in collect_profile()["custom_doctypes"]}
		self.assertIn(DT_OPEN, names)
		self.assertIn(DT_SHIPPED, names)

	def test_shipped_doctype_missed_without_module_union(self):
		"""Same site, no custom app installed -> the module union is empty and
		only custom=1 doctypes are found (proves union 2 does the work)."""
		with patch(_APPS_SEAM, return_value=["frappe", "erpnext", "jarvis"]):
			names = {d["name"] for d in collect_profile()["custom_doctypes"]}
		self.assertIn(DT_OPEN, names)
		self.assertNotIn(DT_SHIPPED, names)

	# ------------------------------------------------------------------ #
	# fence
	# ------------------------------------------------------------------ #

	def test_fence_drops_restricted_doctype_everywhere(self):
		"""Every section attributable to an unreadable doctype is dropped for
		the limited user; unkeyed entries pass; counts follow by construction
		(render derives them from the fenced list lengths)."""
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			data = collect_profile()
		# Extend with synthetic entries naming the fixtures, covering the
		# sections that have no cheap real fixture (workflow/report/etc.).
		data["workflows"].append({"name": "S2 WF", "doctype": DT_RESTRICTED, "states": ["A", "B"]})
		data["reports"].append(
			{"name": "S2 Report", "doctype": DT_RESTRICTED, "report_type": "Report Builder"}
		)
		data["reports"].append({"name": "S2 Unkeyed Report", "doctype": "", "report_type": "Script Report"})
		data["print_formats"].append({"name": "S2 PF", "doctype": DT_OPEN})
		data["scripts"]["server"][DT_RESTRICTED] = 2
		data["scripts"]["server"][""] = 1

		admin_view = fence_for_user(data, user="Administrator")
		limited_view = fence_for_user(data, user=LIMITED_USER)

		admin_names = {d["name"] for d in admin_view["custom_doctypes"]}
		limited_names = {d["name"] for d in limited_view["custom_doctypes"]}
		self.assertIn(DT_RESTRICTED, admin_names)
		self.assertNotIn(DT_RESTRICTED, limited_names)
		self.assertIn(DT_OPEN, limited_names)
		# Set-difference, not an exact count: a dev site may carry OTHER
		# restricted custom doctypes the limited user legitimately loses too.
		self.assertTrue(limited_names <= admin_names)
		self.assertIn(DT_RESTRICTED, admin_names - limited_names)

		self.assertFalse([w for w in limited_view["workflows"] if w["doctype"] == DT_RESTRICTED])
		report_names = {r["name"] for r in limited_view["reports"]}
		self.assertNotIn("S2 Report", report_names)
		self.assertIn("S2 Unkeyed Report", report_names)  # no doctype -> nothing to leak
		self.assertIn("S2 PF", {p["name"] for p in limited_view["print_formats"]})
		self.assertNotIn(DT_RESTRICTED, limited_view["scripts"]["server"])
		self.assertIn("", limited_view["scripts"]["server"])

	def test_tool_output_is_fenced_per_session_user(self):
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			admin_md = describe_customizations()["markdown"]
			frappe.set_user(LIMITED_USER)
			try:
				limited_md = describe_customizations()["markdown"]
			finally:
				frappe.set_user("Administrator")
		self.assertIn(DT_RESTRICTED, admin_md)
		self.assertNotIn(DT_RESTRICTED, limited_md)
		self.assertIn(DT_OPEN, limited_md)

	# ------------------------------------------------------------------ #
	# scope / match / messages / budget
	# ------------------------------------------------------------------ #

	def test_unknown_scope_raises(self):
		with self.assertRaises(InvalidArgumentError):
			describe_customizations(scope=["doctypes", "bogus"])
		with self.assertRaises(InvalidArgumentError):
			describe_customizations(scope=123)
		with self.assertRaises(InvalidArgumentError):
			describe_customizations(match=123)

	def test_scope_narrows_sections(self):
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			md = describe_customizations(scope=["custom_fields"])["markdown"]
		self.assertIn("ToDo", md)
		self.assertNotIn(DT_OPEN, md)

	def test_scope_accepts_comma_string(self):
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			md = describe_customizations(scope="doctypes, custom_fields")["markdown"]
		self.assertIn(DT_OPEN, md)

	def test_match_filters_by_name(self):
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			md = describe_customizations(match="custdisc s2 open")["markdown"]
		self.assertIn(DT_OPEN, md)
		self.assertNotIn(DT_RESTRICTED, md)

	def test_empty_site_message(self):
		empty = {
			"apps": [],
			"modules": {},
			"custom_doctypes": [],
			"core_customizations": [],
			"workflows": [],
			"reports": [],
			"print_formats": [],
			"scripts": {"server": {}, "client": {}},
		}
		with patch("jarvis.tools.describe_customizations.collect_profile", return_value=empty):
			md = describe_customizations()["markdown"]
			self.assertIn("No customizations detected", md)
			scoped = describe_customizations(scope=["workflows"])["markdown"]
			self.assertIn("No customizations match", scoped)

	def test_budget_bound(self):
		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			md = describe_customizations()["markdown"]
		self.assertLessEqual(len(md), 18000)

	# ------------------------------------------------------------------ #
	# collection rules
	# ------------------------------------------------------------------ #

	def test_system_generated_custom_fields_excluded(self):
		data = collect_profile()
		for entry in data["core_customizations"]:
			self.assertNotIn(CF_SYSGEN, entry["notable_fields"])
		note = next((e for e in data["core_customizations"] if e["doctype"] == "Note"), None)
		if note is not None:
			real = frappe.db.count("Custom Field", {"dt": "Note", "is_system_generated": 0})
			self.assertEqual(note["custom_field_count"], real)

	def test_custom_fields_on_custom_doctypes_excluded(self):
		"""A Custom Field on a custom doctype is that doctype's own schema
		(get_schema territory), not a core customization."""
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		if not frappe.db.exists("Custom Field", {"dt": DT_OPEN, "fieldname": "s2_extra"}):
			create_custom_field(DT_OPEN, {"fieldname": "s2_extra", "label": "S2 Extra", "fieldtype": "Data"})
		try:
			data = collect_profile()
			self.assertNotIn(DT_OPEN, {e["doctype"] for e in data["core_customizations"]})
		finally:
			name = frappe.db.get_value("Custom Field", {"dt": DT_OPEN, "fieldname": "s2_extra"})
			if name:
				frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)

	def test_notable_field_surfaces(self):
		data = collect_profile()
		todo = next((e for e in data["core_customizations"] if e["doctype"] == "ToDo"), None)
		self.assertIsNotNone(todo)
		self.assertIn(CF_TODO, todo["notable_fields"])

	def test_known_module_fixture_fields_excluded(self):
		"""A Custom Field stamped with a KNOWN app's module is app-shipped
		fixture schema even when is_system_generated=0 (older exports predate
		the flag) - it must not count as a customization, in the collector or
		in the clause's counts."""
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		from jarvis.chat.customizations_clause import _counts

		fieldname = "custdisc_s2_fixture_probe"
		stale = frappe.db.get_value("Custom Field", {"dt": "Note", "fieldname": fieldname})
		if stale:
			frappe.delete_doc("Custom Field", stale, force=True, ignore_permissions=True)
		create_custom_field(
			"Note",
			{
				"fieldname": fieldname,
				"label": "Fixture Probe",
				"fieldtype": "Data",
				"module": "Desk",
			},  # frappe's module -> known-app lineage
			is_system_generated=False,
		)
		try:
			data = collect_profile()
			cf_dts = {
				e["doctype"]
				for e in data["core_customizations"]
				if e["custom_field_count"]  # PS-only entries aren't CF-counted
			}
			self.assertNotIn("Note", cf_dts)
			from jarvis.site_profile import apps as sp_apps

			_, n_cf_doctypes, _ = _counts(sp_apps.custom_module_names())
			self.assertEqual(n_cf_doctypes, len(cf_dts))  # clause == collector
		finally:
			name = frappe.db.get_value("Custom Field", {"dt": "Note", "fieldname": fieldname})
			if name:
				frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)


class TestToolTelemetry(_CustDiscFixtures):
	"""The stage-4 emitter: correct JSON shape, fast no-op for untracked
	tools, custom-target flagging, turn-flag lifecycle, and the never-raise
	contract (a telemetry bug must never break a tool call)."""

	def setUp(self):
		frappe.set_user("Administrator")
		from jarvis import telemetry

		frappe.cache().delete_value(telemetry.DOCTYPE_SET_CACHE_KEY)

	def _emitted(self, telemetry, patcher_target="jarvis.telemetry._emit"):
		return patch(patcher_target)

	def test_describe_customizations_always_emits(self):
		from jarvis import telemetry

		with patch("jarvis.telemetry._emit") as emit:
			telemetry.record_tool(
				tool="describe_customizations",
				args={},
				conversation="c1",
				duration_ms=12,
				result={"ok": True, "data": {"markdown": "x"}},
			)
		emit.assert_called_once()
		entry = emit.call_args.args[0]
		self.assertEqual(entry["kind"], "tool")
		self.assertEqual(entry["tool"], "describe_customizations")
		self.assertEqual(entry["conversation"], "c1")
		self.assertFalse(entry["custom_target"])
		self.assertGreater(entry["result_chars"], 0)
		self.assertNotIn("Administrator", str(entry))  # user is hashed

	def test_user_hash_is_salted_not_a_bare_email_digest(self):
		# D9: the tool-telemetry caller hash must be SALTED with a per-site secret,
		# not a bare sha1(email) that a dictionary/rainbow attack reverses over the
		# small, enumerable address space. Reddens if _user_hash drops the salt.
		import hashlib

		from jarvis import telemetry

		email = "telemetry-hash@example.test"
		bare = hashlib.sha1(email.encode()).hexdigest()[:12]
		h = telemetry._user_hash(email)
		self.assertNotEqual(h, bare, "telemetry user_hash is a bare, reversible sha1 of the email")
		self.assertEqual(len(h), 12)
		self.assertEqual(h, telemetry._user_hash(email))  # stable within a site

	def test_untracked_tool_is_noop(self):
		from jarvis import telemetry

		with patch("jarvis.telemetry._emit") as emit:
			telemetry.record_tool(
				tool="get_doc", args={"doctype": "ToDo"}, conversation="c1", duration_ms=1, result={}
			)
		emit.assert_not_called()

	def test_custom_target_flags_and_marks_turn(self):
		from jarvis import telemetry

		with (
			patch("jarvis.telemetry.custom_doctype_set", return_value=frozenset({DT_OPEN})),
			patch("jarvis.telemetry._emit") as emit,
		):
			telemetry.record_tool(
				tool="get_schema", args={"doctype": DT_OPEN}, conversation="c-t", duration_ms=3, result={}
			)
			telemetry.record_tool(
				tool="get_schema",
				args={"doctype": "Sales Invoice"},
				conversation="c-t",
				duration_ms=3,
				result={},
			)
		emit.assert_called_once()  # only the custom target emitted
		self.assertTrue(emit.call_args.args[0]["custom_target"])
		# The turn flag was set for the conversation, and emit_turn consumes it.
		with patch("jarvis.telemetry._emit") as emit_turn:
			telemetry.emit_turn("c-t", "r1", 500)
			telemetry.emit_turn("c-t", "r2", 500)
		first, second = [c.args[0] for c in emit_turn.call_args_list]
		self.assertTrue(first["touched_custom"])
		self.assertFalse(second["touched_custom"])  # read-and-clear

	def test_emitter_failure_never_raises(self):
		from jarvis import telemetry

		with patch("jarvis.telemetry._emit", side_effect=RuntimeError("boom")):
			telemetry.record_tool(
				tool="describe_customizations", args={}, conversation=None, duration_ms=1, result={}
			)
			telemetry.emit_turn("c-x", "r1", 1)  # must not raise either

	def test_record_budget_event_emits_expected_shape(self):
		from jarvis import telemetry

		with patch("jarvis.telemetry._emit") as emit:
			telemetry.record_budget_event(
				tool="query", outcome="truncated", original_chars=90000, shown=120, total=3000
			)
		emit.assert_called_once()
		entry = emit.call_args.args[0]
		# `kind` is this line's discriminator; the guard result rides under `outcome`
		# so the two never collide (M5).
		self.assertEqual(entry["kind"], "result_budget")
		self.assertEqual(entry["outcome"], "truncated")
		self.assertEqual(entry["tool"], "query")
		self.assertEqual(entry["original_chars"], 90000)
		self.assertEqual(entry["shown"], 120)
		self.assertEqual(entry["total"], 3000)
		self.assertNotIn("event", entry)  # renamed away from the colliding key

	def test_record_budget_event_never_raises(self):
		from jarvis import telemetry

		with patch("jarvis.telemetry._emit", side_effect=RuntimeError("boom")):
			# None chars/shown/total (measure_failed / uncapped shapes) must be fine.
			telemetry.record_budget_event(
				tool="get_doc", outcome="measure_failed", original_chars=None, shown=None, total=None
			)

	def test_telemetry_logger_is_pinned_to_info(self):
		# C1: frappe.logger defaults to ERROR in prod, which would silently drop
		# every telemetry INFO line. The helper must pin the level so tool +
		# result_budget telemetry actually reaches disk (mirrors chat/latency.py).
		import logging

		from jarvis import telemetry

		logger = telemetry._telemetry_logger()
		# frappe.logger appends the site suffix (jarvis.tool_telemetry-<site>).
		self.assertTrue(logger.name.startswith(telemetry._LOGGER))
		self.assertNotEqual(logger.level, logging.NOTSET)
		self.assertLessEqual(logger.level, logging.INFO)
		self.assertTrue(logger.isEnabledFor(logging.INFO))

	def test_doctype_set_uses_two_unions_and_caches(self):
		from jarvis import telemetry

		with patch(_APPS_SEAM, return_value=_FAKE_INSTALLED):
			names = telemetry.custom_doctype_set()
		self.assertIn(DT_OPEN, names)
		self.assertIn(DT_SHIPPED, names)  # custom=0, found via module union
		self.assertIsNotNone(frappe.cache().get_value(telemetry.DOCTYPE_SET_CACHE_KEY))
		# clear_clause_cache invalidates this set too (same schema events).
		from jarvis.chat.customizations_clause import clear_clause_cache

		clear_clause_cache()
		self.assertIsNone(frappe.cache().get_value(telemetry.DOCTYPE_SET_CACHE_KEY))
