"""Blank optional args are treated as absent for the listed read-only tools.

Customer models with a strict tool schema (gpt-5.6-terra) fill every optional
argument they do not use with a blank (``""``, ``[]``, ``{}``). The tools read
``is not None`` as "supplied", so ``export_query(report_name=..., doctype="",
spec={}, result_id="", ...)`` failed 11 times with "Pass exactly one of ...".

``api._parse_args`` now drops a blank value for the keys named in
``api._BLANK_AS_ABSENT`` (per tool, so a blank that carries meaning elsewhere is
never touched). ``0``, ``False`` and ``"0"`` are real values and stay. A direct
Python call is unchanged: ``export_query`` itself stays strict.
"""

from __future__ import annotations

import io
import os
import zipfile
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_run_report import INLINE_REPORT, _ensure_report
from jarvis.tools.export_query import export_query

BLANKS = ("", "  ", "\t\n", {}, [])

# The exact export_query arguments gpt-5.6-terra sent in the Prakash Steel transcript.
TRANSCRIPT_ARGS = {
	"report_name": "Bright Free Stock",
	"result_id": "",
	"highlights": [],
	"doctype": "",
	"spec": {},
	"filters": {"include_zero_stock": 0},
	"fields": [],
	"order_by": "",
	"format": "xlsx",
	"title": "Bright Free Stock",
	"parent_doctype": "",
}


def _blank_cases():
	for tool, keys in sorted(api._BLANK_AS_ABSENT.items()):
		for key in sorted(keys):
			yield tool, key


class TestBlankAsAbsentContract(FrappeTestCase):
	def test_the_map_names_only_params_the_tool_accepts(self):
		# Derived from the registry's accepted-params map, so a renamed or removed
		# tool parameter fails here instead of leaving a dead entry.
		for tool, keys in api._BLANK_AS_ABSENT.items():
			with self.subTest(tool=tool):
				self.assertIn(tool, api._ACCEPTED_PARAMS)
				self.assertLessEqual(keys, api._ACCEPTED_PARAMS[tool])

	def test_the_exact_key_set_per_tool_is_pinned(self):
		# Literal sets: removing or adding a key must be a deliberate edit here too.
		self.assertEqual(
			api._BLANK_AS_ABSENT,
			{
				"export_query": frozenset(
					{
						"doctype",
						"spec",
						"result_id",
						"report_name",
						"filters",
						"fields",
						"order_by",
						"parent_doctype",
						"highlights",
						"format",
						"title",
					}
				),
				"query_file": frozenset(
					{
						"file_url",
						"filename",
						"file_id",
						"sql",
						"sheets",
						"version",
						"members",
						"archive_version",
						"payload_format",
					}
				),
				"read_file": frozenset(
					{
						"file_url",
						"filename",
						"file_id",
						"sheet",
						"read_mode",
						"cursor",
						"version",
						"member",
						"archive_version",
						"payload_format",
					}
				),
				"query_report": frozenset({"sql", "version"}),
				"report_pdf": frozenset({"filters", "orientation", "title"}),
			},
		)

	def test_the_covered_tools_are_the_read_only_ones_from_the_plan(self):
		self.assertEqual(
			set(api._BLANK_AS_ABSENT),
			{"export_query", "query_file", "read_file", "query_report", "report_pdf"},
		)


class TestIsBlank(FrappeTestCase):
	def test_blank_forms(self):
		for value in BLANKS:
			with self.subTest(value=value):
				self.assertTrue(api._is_blank(value))

	def test_real_values_are_not_blank(self):
		for value in (0, 0.0, False, "0", "x", " x ", [0], [""], {"a": 1}, None):
			with self.subTest(value=value):
				self.assertFalse(api._is_blank(value))


class TestParseArgsBlankAsAbsent(FrappeTestCase):
	def test_every_mapped_key_drops_every_blank_form(self):
		for tool, key in _blank_cases():
			for blank in BLANKS:
				with self.subTest(tool=tool, key=key, blank=blank):
					out = api._parse_args({key: blank, "other": "kept"}, tool)
					self.assertNotIn(key, out)
					self.assertEqual(out["other"], "kept")

	def test_a_non_blank_value_is_kept(self):
		samples = ("abc", ["a"], {"a": 1}, 5)
		for tool, key in _blank_cases():
			for value in samples:
				with self.subTest(tool=tool, key=key, value=value):
					out = api._parse_args({key: value}, tool)
					self.assertEqual(out[key], value)

	def test_zero_false_and_string_zero_are_real_values(self):
		for tool, key in _blank_cases():
			for value in (0, False, "0"):
				with self.subTest(tool=tool, key=key, value=value):
					out = api._parse_args({key: value}, tool)
					self.assertIn(key, out)
					self.assertEqual(out[key], value)
					self.assertIs(type(out[key]), type(value))

	def test_a_key_the_map_does_not_list_is_kept(self):
		# max_rows is a real tool param but not mapped: untouched even when blank.
		for tool in api._BLANK_AS_ABSENT:
			with self.subTest(tool=tool):
				out = api._parse_args({"max_rows": "", "not_a_param": []}, tool)
				self.assertEqual(out, {"max_rows": "", "not_a_param": []})

	def test_an_unlisted_tool_is_untouched(self):
		args = {"doctype": "", "spec": {}, "filters": {}, "sql": "", "report_name": ""}
		for tool in ("run_report", "get_list", "create_doc", "run_method", "export_excel", None):
			with self.subTest(tool=tool):
				out = api._parse_args(dict(args), tool)
				self.assertEqual(out, args)

	def test_a_blank_filters_for_a_tool_that_lists_it_only(self):
		# export_query and report_pdf list filters; read_file / query_report do not.
		for tool in ("export_query", "report_pdf"):
			with self.subTest(tool=tool):
				self.assertNotIn("filters", api._parse_args({"filters": {}}, tool))
		for tool in ("read_file", "query_report"):
			with self.subTest(tool=tool):
				self.assertIn("filters", api._parse_args({"filters": {}}, tool))

	def test_nested_blank_values_are_kept(self):
		out = api._parse_args({"filters": {"status": "", "tags": []}, "report_name": "R"}, "export_query")
		self.assertEqual(out["filters"], {"status": "", "tags": []})

	def test_idempotent_it_runs_twice_per_call(self):
		# api.py parses once at the agent gate and again in _run_tool.
		for tool, key in _blank_cases():
			with self.subTest(tool=tool, key=key):
				args = {key: [], "report_name": "R", "max_rows": 5}
				once = dict(api._parse_args(args, tool))
				twice = api._parse_args(args, tool)
				self.assertEqual(once, twice)

	def test_mutates_in_place_so_every_reader_agrees(self):
		args = {"doctype": "", "report_name": "R"}
		out = api._parse_args(args, "export_query")
		self.assertIs(out, args)
		self.assertEqual(args, {"report_name": "R"})

	def test_json_string_args_are_normalised_too(self):
		out = api._parse_args('{"sql": "", "prepared_report": "x"}', "query_report")
		self.assertEqual(out, {"prepared_report": "x"})

	def test_the_transcript_args_leave_only_the_real_ones(self):
		out = api._parse_args(dict(TRANSCRIPT_ARGS, filters={"include_zero_stock": 0}), "export_query")
		self.assertEqual(
			out,
			{
				"report_name": "Bright Free Stock",
				"filters": {"include_zero_stock": 0},
				"format": "xlsx",
				"title": "Bright Free Stock",
			},
		)

	def test_the_batch_strip_still_applies_and_now_covers_empty_objects(self):
		out = api._parse_args({"doctype": "ToDo", "name": "x", "names": []}, "get_doc")
		self.assertNotIn("names", out)
		# `{}` is blank for the dual-form strip as well (it is not a batch).
		out = api._parse_args({"doctype": "ToDo", "values": {}, "docs": {}}, "create_doc")
		self.assertNotIn("docs", out)
		out = api._parse_args({"doctype": "ToDo", "names": ["a"]}, "get_doc")
		self.assertEqual(out["names"], ["a"])

	def test_direct_export_query_stays_strict(self):
		# The normalisation lives at the dispatch seam, not in the tool.
		with self.assertRaises(InvalidArgumentError):
			export_query(report_name=INLINE_REPORT, format="csv", highlights=[])
		with self.assertRaises(InvalidArgumentError):
			export_query(report_name=INLINE_REPORT, doctype="")


class TestAgentGateParsesBlanks(FrappeTestCase):
	"""The FIRST ``_parse_args`` of the agent path (``_dispatch_from_session``) feeds the
	persisted/audited args, so the blanks must be gone there, not only in ``_run_tool``."""

	def test_the_persisted_args_of_the_transcript_call_hold_no_blank_key(self):
		real_get_value = frappe.db.get_value

		def get_value(doctype, *args, **kwargs):
			# Only the conversation lookup is faked; the tool path reads real metadata.
			if doctype == "Jarvis Conversation":
				return "conv1"
			return real_get_value(doctype, *args, **kwargs)

		with (
			patch("jarvis.api._run_tool", return_value={"ok": True, "data": {}}) as run_tool,
			patch("jarvis.api.impersonate"),
			patch("jarvis.api._persist_and_publish_tool_call") as persist,
			patch("jarvis.tools._delegate_capability.tool_denial", return_value=None),
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api.frappe.db.get_value", side_effect=get_value),
		):
			api._dispatch_from_session(
				user="Administrator",
				session_key="agent:test:blank-args",
				tool="export_query",
				args=dict(TRANSCRIPT_ARGS),
			)
		expected = {
			"report_name": "Bright Free Stock",
			"filters": {"include_zero_stock": 0},
			"format": "xlsx",
			"title": "Bright Free Stock",
		}
		persist.assert_called_once()
		self.assertEqual(persist.call_args.kwargs["args"], expected)
		self.assertEqual(run_tool.call_args.args[1], expected)


class TestTranscriptReplay(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_report(INLINE_REPORT, prepared=0)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ["like", "ba-%"]})
		for i in range(3):
			frappe.get_doc({"doctype": "ToDo", "description": f"ba-{i}"}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_the_transcript_args_return_the_export_file(self):
		args = dict(TRANSCRIPT_ARGS, report_name=INLINE_REPORT, title="Transcript replay")
		r = api._run_tool("export_query", args)
		self.assertTrue(r["ok"], r)
		self.assertTrue(r["data"]["file_url"].endswith(".xlsx"))
		self.assertEqual(r["data"]["report_name"], INLINE_REPORT)
		self.assertGreater(r["data"]["total"], 0)

	def test_the_same_args_failed_before_the_normalisation(self):
		# The pre-fix failure, kept as a direct-call contrast (the tool is strict).
		args = dict(TRANSCRIPT_ARGS, report_name=INLINE_REPORT)
		with self.assertRaisesRegex(InvalidArgumentError, "exactly one of"):
			export_query(**args)

	def test_blank_formatting_args_do_not_break_a_csv_export(self):
		r = api._run_tool(
			"export_query", {"report_name": INLINE_REPORT, "format": "csv", "highlights": [], "spec": {}}
		)
		self.assertTrue(r["ok"], r)
		self.assertTrue(r["data"]["file_url"].endswith(".csv"))


class TestFileToolsBlankArgs(FrappeTestCase):
	"""query_file / read_file with the strict-schema blanks, on a real CSV File."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		from frappe.utils.file_manager import save_file

		body = b"Item,Qty\n" + b"".join(f"item{i},{i}\n".encode() for i in range(1, 6))
		self.file = save_file("blank-args.csv", body, None, None, is_private=1)
		self.addCleanup(self._remove_file, self.file, "blank-args.csv")

	@staticmethod
	def _remove_file(doc, plain_name):
		# save_file writes the bytes to disk (not undone by the test rollback), and
		# Frappe also leaves a scratch copy under the requested plain name.
		url, folder = doc.file_url, frappe.get_site_path("private", "files")
		frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)
		paths = [frappe.get_site_path(url.lstrip("/")), os.path.join(folder, plain_name)]
		for path in paths:
			if os.path.exists(path) and not frappe.db.exists(
				"File", {"file_url": "/private/files/" + os.path.basename(path)}
			):
				os.remove(path)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_query_file_with_blank_optionals_returns_the_inspect_manifest(self):
		r = api._run_tool(
			"query_file",
			{
				"file_id": self.file.name,
				"sql": "",
				"sheets": [],
				"members": [],
				"archive_version": "",
				"payload_format": "",
				"version": "",
				"file_url": "",
				"filename": "",
			},
		)
		self.assertTrue(r["ok"], r)
		data = r["data"]
		self.assertEqual(data["file_id"], self.file.name)
		self.assertTrue(data["version"])
		self.assertEqual(data["read_contract"], "spreadsheet-query-v1")
		self.assertEqual(data["sheets"][0]["table"], "sheet1")
		self.assertEqual(data["rows"], [])

	def test_query_file_blank_optionals_fail_when_called_directly(self):
		# Documents why the dispatch strip is needed: the tool reads "" as supplied.
		from jarvis.tools.query_file import query_file

		with self.assertRaises(InvalidArgumentError):
			query_file(file_id=self.file.name, sql="", sheets=[], members=[], archive_version="")

	def test_read_file_blank_cursor_with_a_real_version_is_the_pair_error(self):
		r = api._run_tool(
			"read_file",
			{"file_id": self.file.name, "read_mode": "file-cursor-v1", "cursor": "", "version": "a" * 64},
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIn("supplied together", r["error"]["message"])

	def test_read_file_both_blank_is_a_preview(self):
		r = api._run_tool(
			"read_file",
			{
				"file_id": self.file.name,
				"cursor": "",
				"version": "",
				"read_mode": "",
				"sheet": "",
				"member": "",
				"archive_version": "",
				"payload_format": "",
			},
		)
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["format"], "csv")
		self.assertEqual(r["data"]["sheets"][0]["row_count"], 6)  # header + 5 rows

	def _save_zip(self, name):
		from frappe.utils.file_manager import save_file

		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w") as book:
			book.writestr("a.csv", "Item,Qty\nx,1\n")
		doc = save_file(name, buffer.getvalue(), None, None, is_private=1)
		self.addCleanup(self._remove_file, doc, name)
		return doc

	def test_an_archive_with_blank_members_gets_the_inventory_manifest(self):
		# members=[] is dropped, so no archive scope is claimed: with archive reading
		# on, the archive feature answers with its own inventory (no member selected).
		doc = self._save_zip("blank-args-members.zip")
		with patch.dict(frappe.conf, {"jarvis_archive_reading_enabled": True}):
			r = api._run_tool("query_file", {"file_id": doc.name, "members": [], "sql": ""})
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["read_contract"], "archive-manifest-v1")
		self.assertEqual([m["name"] for m in r["data"]["members"]], ["a.csv"])
