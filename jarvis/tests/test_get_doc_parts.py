"""F11 (#715): a get_doc document over the agent's result budget comes back cut,
and the get_doc calls it names read exactly the omitted part, through the same
permission check and field masking as the whole document."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, InvalidFieldValueError, PermissionDeniedError
from jarvis.tools import _doc_parts
from jarvis.tools._doc_parts import TEXT_WINDOW_CHARS, DocPart
from jarvis.tools._result_guard import MAX_RESULT_CHARS, enforce_result_budget
from jarvis.tools.create_doc import create_doc
from jarvis.tools.get_doc import get_doc
from jarvis.tools.registry import dispatch
from jarvis.tools.update_doc import update_doc

PART_ARGS = ("fields", "child_table", "start", "limit", "text_offset", "text_length")

DESK_USER = "jv-f11-desk@example.com"
SEEN_ROWS = 250


def _size(o):
	return len(json.dumps(o, default=str, ensure_ascii=False, separators=(",", ":")))


def _agent_read(**args):
	"""get_doc as the agent receives it: through the result budget."""
	out, _ = enforce_result_budget(get_doc(**args), tool="get_doc")
	return out


def _desk_user() -> str:
	if not frappe.db.exists("User", DESK_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": DESK_USER,
				"first_name": "F11",
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", DESK_USER).add_roles("Desk User")
	return DESK_USER


def _note(content_chars: int, public: int = 0, text: str = "x", rows: int = SEEN_ROWS) -> str:
	"""A Note with long content and many seen_by rows (permlevel 2: System
	Manager reads them, a Desk User does not), owned by the session user."""
	return (
		frappe.get_doc(
			{
				"doctype": "Note",
				"title": "F11 bounded document",
				"public": public,
				"content": "<p>" + (text * content_chars)[:content_chars] + "</p>",
				"seen_by": [{"user": "Administrator"} for _ in range(rows)],
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestGetDocParts(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def read_rest(self, shown: str, call: dict | None, read=None) -> str:
		"""A cut value's shown part plus every window its next_call chain returns."""
		read = read or _agent_read
		text = shown
		for _ in range(100):
			if not call:
				return text
			out = read(**call["args"])
			window = out["_text_window"]
			self.assertEqual(window["offset"], len(text))
			self.assertLessEqual(_size(out), MAX_RESULT_CHARS)
			text += out[window["field"]]
			call = window.get("next_call")
		self.fail("the window chain did not end")

	def test_cut_document_continuation_reads_exactly_the_omitted_rows(self):
		name = _note(content_chars=20_000)
		whole = get_doc(doctype="Note", name=name)
		self.assertGreater(_size(whole), MAX_RESULT_CHARS)

		cut = _agent_read(doctype="Note", name=name)
		self.assertLessEqual(_size(cut), MAX_RESULT_CHARS)
		table = cut["_partial"]["child_tables"]["seen_by"]
		self.assertEqual(table["total"], SEEN_ROWS)
		self.assertEqual(cut["seen_by"], whole["seen_by"][: table["returned"]])

		rows, call = [], table["next_call"]
		for _ in range(SEEN_ROWS):  # bounded walk; each page is itself budgeted
			self.assertEqual(call["tool"], "get_doc")
			page = _agent_read(**call["args"])
			self.assertLessEqual(_size(page), MAX_RESULT_CHARS)
			rows += page["rows"]
			if not page["coverage"]["has_more"]:
				break
			call = {"tool": "get_doc", "args": {**call["args"], "start": page["coverage"]["next_start"]}}
		self.assertEqual(rows, whole["seen_by"][table["returned"] :])

		text = cut["_partial"]["text_fields"]["content"]
		self.assertEqual(text["total_chars"], len(whole["content"]))
		shown = cut["content"][: text["returned_chars"]]
		self.assertEqual(self.read_rest(shown, text["next_call"]), whole["content"])

	def test_text_far_over_the_budget_reads_back_byte_identical(self):
		# The audit's own case: a text field larger than any one result. Quotes
		# and newlines take two JSON chars each, so windows shrink to fit.
		name = _note(content_chars=100_040, text='He said "ok" and left.\n', rows=2)
		whole = get_doc(doctype="Note", name=name)
		self.assertGreater(_size(whole["content"]), 2 * MAX_RESULT_CHARS)
		cut = _agent_read(doctype="Note", name=name)
		self.assertLessEqual(_size(cut), MAX_RESULT_CHARS)
		text = cut["_partial"]["text_fields"]["content"]
		shown = cut["content"][: text["returned_chars"]]
		self.assertEqual(self.read_rest(shown, text["next_call"]), whole["content"])

	def test_a_list_value_reads_back_as_its_json_text(self):
		doc = {"doctype": "Note", "name": "N-1", "tags": ['a "quoted" tag'] * 6000}

		def read(fields, text_offset=None, text_length=None, **_identity):
			return DocPart(fields, None, 0, None, text_offset, text_length).of(doc)

		first = read(fields=["tags"], text_offset=0)
		self.assertIs(first["_text_window"]["as_json"], True)
		self.assertEqual(
			json.loads(self.read_rest("", {"args": {"fields": ["tags"], "text_offset": 0}}, read)),
			doc["tags"],
		)

	def test_text_window_arguments_are_checked(self):
		name = _note(content_chars=100, rows=1)
		length = len(get_doc(doctype="Note", name=name)["content"])
		cases = (
			({"fields": ["content"], "text_offset": length + 1}, "past the end of content"),
			({"fields": ["content", "title"], "text_offset": 5}, "read one field"),
			({"child_table": "seen_by", "fields": ["user"], "text_offset": 5}, "read one field"),
			({"text_offset": 5}, "read one field"),
			({"fields": ["content"], "text_offset": -1}, "text_offset must be"),
			({"fields": ["content"], "text_length": TEXT_WINDOW_CHARS + 1}, "text_length must be"),
		)
		for args, message in cases:
			with self.subTest(args=args), self.assertRaisesRegex(InvalidArgumentError, message):
				get_doc(doctype="Note", name=name, **args)
		window = get_doc(doctype="Note", name=name, fields=["content"], text_offset=10, text_length=20)
		self.assertEqual(window["content"], get_doc(doctype="Note", name=name)["content"][10:30])
		self.assertEqual(window["_text_window"]["next_call"]["args"]["text_length"], 20)
		self.assertEqual(window["_text_window"]["next_offset"], 30)

	def test_child_page_returns_named_fields_plus_row_identity(self):
		name = _note(content_chars=10)
		whole = get_doc(doctype="Note", name=name)
		page = get_doc(doctype="Note", name=name, child_table="seen_by", fields=["user"], start=10, limit=5)
		self.assertEqual(
			page["rows"],
			[{"name": r["name"], "idx": r["idx"], "user": r["user"]} for r in whole["seen_by"][10:15]],
		)
		expected = {
			"start": 10,
			"limit": 5,
			"returned": 5,
			"total": SEEN_ROWS,
			"has_more": True,
			"next_start": 15,
		}
		self.assertEqual({k: page["coverage"][k] for k in expected}, expected)
		self.assertEqual(page["list_contract"], "list-page-v1")

	def test_fields_returns_only_those_fields(self):
		name = _note(content_chars=10)
		self.assertEqual(
			set(get_doc(doctype="Note", name=name, fields=["title"])), {"doctype", "name", "title"}
		)

	def test_part_reads_see_no_more_than_the_whole_document(self):
		# seen_by is permlevel 2: the owner (a Desk User) reads the Note but not
		# those rows, in the whole document and in every part of it.
		frappe.set_user(_desk_user())
		name = _note(content_chars=10)
		whole = get_doc(doctype="Note", name=name)
		self.assertEqual(whole["seen_by"], [])  # masked: looks empty in the whole read
		# A part read says why instead of passing the mask off as no data.
		for args in (
			{"child_table": "seen_by"},
			{"fields": ["seen_by"]},
			{"fields": ["seen_by"], "text_offset": 0},
		):
			with (
				self.subTest(args=args),
				self.assertRaisesRegex(
					PermissionDeniedError, "no read permission on Note field\\(s\\) seen_by"
				),
			):
				get_doc(doctype="Note", name=name, **args)
		self.assertEqual(get_doc(doctype="Note", name=name, fields=["public"])["public"], 0)

	def test_a_child_field_the_user_cannot_read_is_left_out_and_named(self):
		name = _note(content_chars=10, rows=3)
		real = _doc_parts._unreadable

		def unreadable(doctype, of=None):
			return {"user"} if of == "Note Seen By" else real(doctype, of)

		with patch.object(_doc_parts, "_unreadable", side_effect=unreadable):
			for fields in (None, ["user"]):
				page = get_doc(doctype="Note", name=name, child_table="seen_by", fields=fields)
				self.assertEqual(page["omitted_fields"], ["user"])
				self.assertTrue(all("user" not in row and row["name"] for row in page["rows"]))
				self.assertIn("omitted_fields", page["note"])

	def test_part_reads_refuse_a_document_the_user_cannot_read(self):
		name = _note(content_chars=10)  # private, owned by Administrator
		frappe.set_user(_desk_user())
		for args in ({"child_table": "seen_by"}, {"fields": ["content"]}):
			with self.assertRaises(PermissionDeniedError):
				get_doc(doctype="Note", name=name, **args)

	def test_bad_part_arguments_are_explained(self):
		name = _note(content_chars=10)
		cases = (
			({"child_table": "title"}, "no child table 'title'.*seen_by"),
			({"fields": ["nope"]}, "no field\\(s\\) nope"),
			({"child_table": "seen_by", "fields": ["nope"]}, "Note Seen By has no field"),
			({"start": 5}, "pass child_table too"),
			({"child_table": "seen_by", "limit": 1001}, "limit must be"),
			({"child_table": "seen_by", "limit": 2.5}, "limit must be"),
			({"child_table": "seen_by", "start": "ten"}, "start must be"),
			({"child_table": "seen_by", "start": -1}, "start must be"),
			({"child_table": "seen_by", "start": True}, "start must be"),
		)
		for args, message in cases:
			with self.subTest(args=args), self.assertRaisesRegex(InvalidArgumentError, message):
				get_doc(doctype="Note", name=name, **args)
		with self.assertRaisesRegex(InvalidArgumentError, "pass name, not names"):
			get_doc(doctype="Note", names=[name], child_table="seen_by")

	def test_unused_empty_part_arguments_mean_a_whole_read(self):
		# Models fill unused optional arguments with null, [], "" or 0: through the
		# registry, as a tool call arrives, a plain read must stay a plain read.
		name = _note(content_chars=10)
		whole = get_doc(doctype="Note", name=name)
		fillers = (
			dict.fromkeys(PART_ARGS),
			{"fields": [], "child_table": "", "start": 0, "limit": 0, "text_offset": 0, "text_length": 0},
		)
		for filler in fillers:
			with self.subTest(filler=filler):
				self.assertEqual(dispatch("get_doc", {"doctype": "Note", "name": name, **filler}), whole)

	def test_numbers_sent_as_strings_or_floats_are_read_as_numbers(self):
		name = _note(content_chars=100, rows=30)
		page = get_doc(doctype="Note", name=name, child_table="seen_by", start=10, limit=5)
		self.assertEqual(
			get_doc(doctype="Note", name=name, child_table="seen_by", start="10", limit="5.0"), page
		)
		window = get_doc(doctype="Note", name=name, fields=["content"], text_offset=20, text_length=10)
		self.assertEqual(
			get_doc(doctype="Note", name=name, fields=["content"], text_offset="20", text_length=10.0), window
		)

	def test_single_doctype_child_page(self):
		whole = get_doc(doctype="Website Settings")
		page = get_doc(doctype="Website Settings", child_table="top_bar_items")
		self.assertEqual(page["name"], "Website Settings")
		self.assertEqual(page["rows"], whole["top_bar_items"][:20])
		with self.assertRaisesRegex(InvalidArgumentError, "no child table"):
			get_doc(doctype="Website Settings", child_table="nope_not_a_table")


def _sales_invoice(items: int) -> dict:
	"""An unsaved Sales Invoice dict in get_doc's own shape (as_dict with every
	default field), sized like a real one without needing accounting masters."""
	si = frappe.new_doc("Sales Invoice")
	si.name = "ACC-SINV-2026-00042"
	si.customer = "Acme Traders"
	si.terms = "<p>" + "Payment is due within 30 days of the invoice date. " * 60 + "</p>"
	for i in range(items):
		si.append(
			"items",
			{
				"item_code": f"ITEM-{i:04d}",
				"item_name": f"Widget {i}",
				"description": f"<p>Widget {i}, spare part</p>",
				"qty": 3,
				"rate": 125.5,
				"amount": 376.5,
				"uom": "Nos",
			},
		)
	for i in range(3):
		si.append("taxes", {"charge_type": "On Net Total", "account_head": f"GST {i}", "rate": 9})
	for row in si.items + si.taxes:
		row.name = frappe.generate_hash(length=10)
	return si.as_dict(no_default_fields=False)


class TestSalesInvoiceBudget(FrappeTestCase):
	def test_large_invoice_is_bounded_and_points_at_the_rest(self):
		whole = _sales_invoice(items=300)
		out, event = enforce_result_budget(whole, tool="get_doc")
		self.assertEqual(event["kind"], "bounded_document")
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)
		items = out["_partial"]["child_tables"]["items"]
		self.assertEqual(items["total"], 300)
		self.assertEqual(out["items"], whole["items"][: items["returned"]])
		self.assertEqual(out["taxes"], whole["taxes"])
		self.assertEqual(items["next_call"]["args"]["start"], items["returned"])
		self.assertEqual(out["grand_total"], whole["grand_total"])

	def test_small_invoice_is_byte_identical(self):
		whole = _sales_invoice(items=3)
		before = json.dumps(whole, default=str, ensure_ascii=False, separators=(",", ":"))
		out, event = enforce_result_budget(whole, tool="get_doc")
		self.assertIsNone(event)
		self.assertIs(out, whole)
		self.assertEqual(json.dumps(out, default=str, ensure_ascii=False, separators=(",", ":")), before)


class TestCutValuesAreNeverWrittenBack(FrappeTestCase):
	"""A cut value carries its TRUNCATED marker; written back, it would replace the
	real data with a fragment. Every create / update path refuses it."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.name = _note(content_chars=50_000, rows=2)
		cut = _agent_read(doctype="Note", name=self.name)
		self.cut_content = cut["content"]
		self.assertIn(" [TRUNCATED: ", self.cut_content)

	def test_update_refuses_a_cut_value(self):
		with self.assertRaisesRegex(
			InvalidFieldValueError, "content.*TRUNCATED.*get_doc fields=\\['content'\\]"
		):
			update_doc("Note", self.name, {"content": self.cut_content})
		with self.assertRaisesRegex(InvalidFieldValueError, "TRUNCATED"):
			update_doc("Note", updates=[{"name": self.name, "changes": {"title": self.cut_content}}])

	def test_create_refuses_a_cut_value_also_in_a_row(self):
		cases = (
			{"doctype": "Note", "values": {"title": "copy", "content": self.cut_content}},
			{
				"doctype": "Note",
				"values": {"title": "copy", "seen_by": [{"user": "Administrator [TRUNCATED: 3 more chars]"}]},
			},
		)
		for case in cases:
			with self.subTest(case=case), self.assertRaisesRegex(InvalidFieldValueError, "TRUNCATED"):
				create_doc(**case)
		with self.assertRaisesRegex(InvalidFieldValueError, "TRUNCATED"):
			create_doc(docs=[cases[0]])
