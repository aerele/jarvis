"""A blank optional argument is treated as absent, for every tool.

Customer models with a strict tool schema (gpt-5.6-terra) send every argument and
fill the ones they do not use with a blank (``""``, ``[]``, ``{}``). The tools read
``is not None`` as "supplied", so ``export_query(report_name=..., doctype="",
spec={}, result_id="", ...)`` failed 11 times with "Pass exactly one of ...", and
other tools raised, took the wrong branch or (update_wiki) erased a page body.

``api._parse_args`` drops a blank value of every optional parameter of every tool
(``registry._OPTIONAL_PARAMS``), except the tools in ``api._BLANK_IS_A_VALUE``.
Required parameters, nested values, ``0``, ``False`` and ``"0"`` are kept. A direct
Python call is unchanged: ``export_query`` itself stays strict.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_run_report import INLINE_REPORT, _ensure_report
from jarvis.tools.export_excel import export_excel
from jarvis.tools.export_query import export_query
from jarvis.tools.registry import _OPTIONAL_PARAMS
from jarvis.tools.run_method import run_method
from jarvis.tools.update_wiki import update_wiki

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


def _covered_tools():
	return sorted(set(api._ACCEPTED_PARAMS) - api._BLANK_IS_A_VALUE)


def _required(tool):
	return sorted(api._ACCEPTED_PARAMS[tool] - _OPTIONAL_PARAMS[tool])


class TestOptionalParamsMap(FrappeTestCase):
	def test_every_registered_tool_has_an_entry(self):
		self.assertEqual(set(_OPTIONAL_PARAMS), set(api._ACCEPTED_PARAMS))

	def test_optional_params_are_accepted_params(self):
		for tool, keys in _OPTIONAL_PARAMS.items():
			with self.subTest(tool=tool):
				self.assertLessEqual(keys, api._ACCEPTED_PARAMS[tool])

	def test_it_follows_the_signature_defaults(self):
		self.assertIn("doctype", _OPTIONAL_PARAMS["export_query"])
		self.assertIn("format", _OPTIONAL_PARAMS["export_query"])  # default "xlsx"
		self.assertIn("replace_body_md", _OPTIONAL_PARAMS["update_wiki"])
		self.assertNotIn("slug", _OPTIONAL_PARAMS["update_wiki"])  # required
		self.assertNotIn("method", _OPTIONAL_PARAMS["run_method"])  # required

	def test_the_exception_list_is_pinned(self):
		# A new exception must be a deliberate edit here, with its reason in api.py.
		self.assertEqual(api._BLANK_IS_A_VALUE, frozenset({"get_my_access", "record_agent_run"}))


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
	def test_every_optional_param_of_every_tool_drops_every_blank_form(self):
		for tool in _covered_tools():
			for key in sorted(_OPTIONAL_PARAMS[tool]):
				for blank in BLANKS:
					with self.subTest(tool=tool, key=key, blank=blank):
						out = api._parse_args({key: blank, "not_a_param": "kept"}, tool)
						self.assertNotIn(key, out)
						self.assertEqual(out["not_a_param"], "kept")

	def test_a_required_param_keeps_its_blank(self):
		# The tool's own error ("slug is required") is clearer than a missing argument.
		for tool in _covered_tools():
			for key in _required(tool):
				with self.subTest(tool=tool, key=key):
					self.assertEqual(api._parse_args({key: ""}, tool), {key: ""})

	def test_real_values_are_kept(self):
		for tool in _covered_tools():
			for key in sorted(_OPTIONAL_PARAMS[tool]):
				for value in ("abc", ["a"], {"a": 1}, 5, 0, False, "0"):
					with self.subTest(tool=tool, key=key, value=value):
						out = api._parse_args({key: value}, tool)
						self.assertEqual(out[key], value)
						self.assertIs(type(out[key]), type(value))

	def test_a_key_the_tool_does_not_accept_is_kept(self):
		self.assertEqual(api._parse_args({"not_a_param": []}, "export_query"), {"not_a_param": []})

	def test_record_agent_run_keeps_its_blanks(self):
		# Payroll deposit: findings=[] means "zero findings", and None fails as malformed.
		args = {"findings": [], "coverage": {}, "report_sha256": "", "dashboard": ""}
		self.assertEqual(api._parse_args(dict(args), "record_agent_run"), args)

	def test_get_my_access_keeps_its_empty_query_lists(self):
		args = {"docs": [], "doctypes": []}
		self.assertEqual(api._parse_args(dict(args), "get_my_access"), args)

	def test_without_a_tool_nothing_is_dropped(self):
		self.assertEqual(api._parse_args({"doctype": "", "spec": {}}, None), {"doctype": "", "spec": {}})

	def test_nested_blank_values_are_kept(self):
		out = api._parse_args({"filters": {"status": "", "tags": []}, "report_name": "R"}, "export_query")
		self.assertEqual(out["filters"], {"status": "", "tags": []})

	def test_idempotent_it_runs_twice_per_call(self):
		# api.py parses once at the agent gate and again in _run_tool.
		for tool in _covered_tools():
			with self.subTest(tool=tool):
				args = {key: [] for key in _OPTIONAL_PARAMS[tool]}
				args.update({key: "x" for key in _required(tool)})
				once = dict(api._parse_args(args, tool))
				self.assertEqual(api._parse_args(args, tool), once)

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

	def test_the_batch_strip_still_applies_and_covers_empty_objects(self):
		out = api._parse_args({"doctype": "ToDo", "name": "x", "names": []}, "get_doc")
		self.assertNotIn("names", out)
		out = api._parse_args({"doctype": "ToDo", "values": {}, "docs": {}}, "create_doc")
		self.assertNotIn("docs", out)
		out = api._parse_args({"doctype": "ToDo", "names": ["a"]}, "get_doc")
		self.assertEqual(out["names"], ["a"])

	def test_the_other_tools_that_broke_on_blanks(self):
		# Each of these raised or took the wrong branch on the blank before.
		cases = {
			"run_method": ({"method": "frappe.ping", "args": {}, "doctype": "", "name": ""}, {"method"}),
			"export_excel": ({"rows": [["a"]], "sheets": [], "charts": [], "highlights": []}, {"rows"}),
			"send_email": (
				{"recipients": "a@example.com", "subject": "s", "recipients_from": {}, "messages": []},
				{"recipients", "subject"},
			),
			"preview_landed_cost_voucher": (
				{"company": "C", "receipts": ["PR-1"], "existing_draft": "", "posting_date": ""},
				{"company", "receipts"},
			),
		}
		for tool, (args, kept) in cases.items():
			with self.subTest(tool=tool):
				self.assertEqual(set(api._parse_args(dict(args), tool)), kept)

	def test_direct_export_query_stays_strict(self):
		# The normalisation lives at the dispatch seam, not in the tool.
		with self.assertRaises(InvalidArgumentError):
			export_query(report_name=INLINE_REPORT, format="csv", highlights=[])
		with self.assertRaises(InvalidArgumentError):
			export_query(report_name=INLINE_REPORT, doctype="")


class TestBlankArgsReachTheRealTools(FrappeTestCase):
	"""The parsed args, run through the real tool: what a strict model's call now does."""

	SLUG = "org--wikitest-blank-args"

	def setUp(self):
		frappe.set_user("Administrator")
		self._wiki_was = frappe.db.get_single_value("Jarvis Settings", "wiki_enabled")
		frappe.db.set_single_value("Jarvis Settings", "wiki_enabled", 1, update_modified=False)
		frappe.db.delete("Jarvis Wiki Page", {"slug": self.SLUG})

	def tearDown(self):
		frappe.db.delete("Jarvis Wiki Page", {"slug": self.SLUG})
		frappe.db.set_single_value("Jarvis Settings", "wiki_enabled", self._wiki_was, update_modified=False)

	def _strict_wiki_call(self, **real):
		blanks = dict.fromkeys(
			(
				"title",
				"page_type",
				"ref_doctype",
				"ref_name",
				"summary",
				"append_md",
				"replace_body_md",
				"scope",
			),
			"",
		)
		return dict(blanks, slug=self.SLUG, **real)

	def _page(self):
		return frappe.db.get_value(
			"Jarvis Wiki Page", {"slug": self.SLUG}, ["body_md", "summary"], as_dict=True
		)

	def test_update_wiki_blank_replace_body_no_longer_erases_the_page(self):
		update_wiki(slug=self.SLUG, title="T", page_type="Org", summary="S", replace_body_md="Original body")
		update_wiki(**api._parse_args(self._strict_wiki_call(append_md="Added"), "update_wiki"))
		page = self._page()
		self.assertEqual(page.body_md, "Original body\n\nAdded")
		self.assertEqual(page.summary, "S")

	def test_update_wiki_blanks_called_directly_still_erase_the_body(self):
		# Documents the bug the dispatch seam now prevents: the tool itself is unchanged.
		update_wiki(slug=self.SLUG, title="T", page_type="Org", replace_body_md="Original body")
		blank_call = {k: v for k, v in self._strict_wiki_call().items() if k != "scope"}
		update_wiki(**blank_call)
		self.assertEqual(self._page().body_md, "")

	def test_run_method_with_blank_doctype_runs_the_method(self):
		args = {"method": "frappe.ping", "args": {}, "doctype": "", "name": ""}
		with self.assertRaises(InvalidArgumentError):
			run_method(**args)
		self.assertEqual(run_method(**api._parse_args(dict(args), "run_method")), "pong")

	def test_export_excel_with_blank_sheets_exports_the_rows(self):
		args = {
			"rows": [{"Item": "Widget", "Qty": 2}],
			"sheets": [],
			"charts": [],
			"highlights": [],
			"title": "",
		}
		with self.assertRaises(InvalidArgumentError):
			export_excel(**args)
		with patch("frappe.utils.file_manager.save_file") as save:
			save.return_value = frappe._dict(name="F-1", file_url="/private/files/x.xlsx", file_name="x.xlsx")
			export_excel(**api._parse_args(dict(args), "export_excel"))
		save.assert_called_once()


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

	def _save_zip(self):
		import io
		import uuid
		import zipfile

		try:
			from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS
		except ImportError:  # Frappe v15 has no such constant: the encodings v16 tries
			FILE_ENCODING_OPTIONS = ("utf-8-sig", "utf-8", "windows-1250", "windows-1252")
		from frappe.utils.file_manager import save_file

		# Frappe's File reads saved content as text when any of FILE_ENCODING_OPTIONS
		# decodes it, and a later write re-encodes that text as UTF-8. A small zip
		# (its bytes vary with the zip timestamp) can decode as windows-1250 and is
		# then damaged on disk: ArchiveIntegrityError, on some runs only. Byte 0x81
		# in the zip comment is invalid in all four encodings, so the zip stays bytes.
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as book:
			book.writestr("a.csv", "Item,Qty\nx,1\n")
			book.comment = b"\x81"
		content = buffer.getvalue()
		for encoding in FILE_ENCODING_OPTIONS:
			with self.assertRaises(UnicodeDecodeError, msg=encoding):
				content.decode(encoding)

		name = f"blank-args-members-{uuid.uuid4().hex[:10]}.zip"
		doc = save_file(name, content, None, None, is_private=1)
		self.addCleanup(self._remove_file, doc, name)
		return doc

	def test_an_archive_with_blank_members_gets_the_inventory_manifest(self):
		# members=[] is dropped, so no archive scope is claimed: with archive reading
		# on, the archive feature answers with its own inventory (no member selected).
		doc = self._save_zip()
		with patch.dict(frappe.conf, {"jarvis_archive_reading_enabled": True}):
			r = api._run_tool("query_file", {"file_id": doc.name, "members": [], "sql": ""})
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["read_contract"], "archive-manifest-v1")
		self.assertEqual([m["name"] for m in r["data"]["members"]], ["a.csv"])
