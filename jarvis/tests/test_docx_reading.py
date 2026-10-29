"""DOCX parser, cursor and permission regression tests. No model or external I/O."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZipFile

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import _file_scan as scan
from jarvis.tools._file_sections import read_sections
from jarvis.tools.read_file import read_file

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def p(text, props=""):
	from xml.sax.saxutils import escape

	return f"<w:p>{props}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def cell(text, props=""):
	return f"<w:tc><w:tcPr>{props}</w:tcPr>{p(text)}</w:tc>"


def package(body, parts=None, rels="", namespace=W, content_type=TYPE, document=None):
	"""A real minimal OOXML package; extra parts never come from private fixtures."""
	doc = document or (
		f'<w:document xmlns:w="{namespace}" xmlns:r="{R}" '
		'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
		'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
		'xmlns:unknown="urn:unsupported-790"><w:body>' + body + "</w:body></w:document>"
	)
	files = {
		"[Content_Types].xml": f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="{content_type}"/></Types>',
		"_rels/.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="main" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
		"word/document.xml": doc,
		"word/_rels/document.xml.rels": f'<Relationships xmlns="{PKG}">{rels}</Relationships>',
		**(parts or {}),
	}
	out = io.BytesIO()
	with ZipFile(out, "w", ZIP_DEFLATED) as archive:
		for name, data in files.items():
			archive.writestr(name, data)
	return out.getvalue()


def relation(identifier, kind, target, external=False):
	mode = ' TargetMode="External"' if external else ""
	return f'<Relationship Id="{identifier}" Type="{R}/{kind}" Target="{target}"{mode}/>'


def unpack(data):
	from jarvis.tools._docx_text import iter_docx

	summary = {}
	return list(iter_docx(data, summary)), summary


class TestDocxParser(unittest.TestCase):
	def test_interleaved_paragraphs_and_table_cells(self):
		rows, meta = unpack(
			package(p("Before") + "<w:tbl><w:tr>" + cell("A") + cell("24") + "</w:tr></w:tbl>" + p("After"))
		)
		self.assertEqual(
			[(kind, value) for kind, value, _ in rows],
			[("p", "Before"), ("row", ["A", "24"]), ("p", "After")],
		)
		self.assertEqual(meta["blocks_total"], 3)

	def test_merged_amount_is_never_duplicated(self):
		body = (
			"<w:tbl><w:tr>"
			+ cell("1,500.00", '<w:gridSpan w:val="2"/><w:vMerge w:val="restart"/>')
			+ cell("A")
			+ "</w:tr><w:tr>"
			+ cell("1,500.00", '<w:gridSpan w:val="2"/><w:vMerge/>')
			+ cell("B")
			+ "</w:tr></w:tbl>"
		)
		rows, _ = unpack(package(body))
		self.assertEqual([row[1] for row in rows], [["1,500.00", "", "A"], ["", "", "B"]])
		self.assertEqual(json.dumps(rows).count("1,500.00"), 1)

	def test_reference_parts_read_once_and_orphan_notes_omitted(self):
		body = (
			p("Body")
			+ '<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p><w:sectPr><w:headerReference r:id="h"/><w:footerReference r:id="f"/></w:sectPr>'
		)
		parts = {
			"word/header1.xml": f'<w:hdr xmlns:w="{W}">{p("Header")}</w:hdr>',
			"word/footer1.xml": f'<w:ftr xmlns:w="{W}">{p("Footer")}</w:ftr>',
			"word/footnotes.xml": f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="-1">{p("Separator")}</w:footnote><w:footnote w:id="1">{p("Note")}</w:footnote><w:footnote w:id="2">{p("Orphan")}</w:footnote></w:footnotes>',
		}
		rels = (
			relation("h", "header", "header1.xml")
			+ relation("f", "footer", "footer1.xml")
			+ relation("n", "footnotes", "footnotes.xml")
		)
		rows, _ = unpack(package(body, parts, rels))
		self.assertEqual([v for k, v, _ in rows if k == "p" and v], ["Header", "Body", "Note", "Footer"])

	def test_zero_id_is_a_real_note_unless_its_type_is_a_separator(self):
		body = '<w:p><w:r><w:footnoteReference w:id="0"/></w:r></w:p>'
		notes = (
			f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="0">{p("First note")}</w:footnote></w:footnotes>'
		)
		rows, _ = unpack(
			package(body, {"word/footnotes.xml": notes}, relation("n", "footnotes", "footnotes.xml"))
		)
		self.assertIn("First note", [value for kind, value, _ in rows if kind == "p"])

	def test_link_display_text_without_external_target(self):
		body = '<w:p><w:hyperlink r:id="link"><w:r><w:t>Visible link</w:t></w:r></w:hyperlink></w:p>'
		rows, _ = unpack(
			package(body, rels=relation("link", "hyperlink", "https://outside.invalid/PRIVATE", True))
		)
		self.assertEqual(rows[0][1], "Visible link")
		self.assertNotIn("PRIVATE", json.dumps(rows))

	def test_hidden_deleted_and_field_instructions_are_not_visible_text(self):
		body = '<w:p><w:r><w:t>Visible </w:t></w:r><w:del><w:r><w:delText>DELETED</w:delText></w:r></w:del><w:moveFrom><w:r><w:t>OLD</w:t></w:r></w:moveFrom><w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN</w:t></w:r><w:ins><w:r><w:t>new </w:t></w:r></w:ins><w:r><w:fldChar w:fldCharType="begin"/><w:instrText>INSTRUCTION</w:instrText><w:fldChar w:fldCharType="separate"/><w:t>24</w:t><w:fldChar w:fldCharType="end"/></w:r></w:p>'
		rows, _ = unpack(package(body))
		self.assertEqual(rows[0][1], "Visible new 24")

	def test_alternate_choice_text_box_once_and_unknown_choice_fallback(self):
		choice = (
			'<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
			+ p("Box")
			+ "</w:txbxContent></w:drawing></mc:Choice><mc:Fallback>"
			+ p("DUPLICATE")
			+ "</mc:Fallback></mc:AlternateContent>"
		)
		unknown = (
			'<mc:AlternateContent><mc:Choice Requires="unknown">'
			+ p("UNSUPPORTED")
			+ "</mc:Choice><mc:Fallback>"
			+ p("Fallback")
			+ "</mc:Fallback></mc:AlternateContent>"
		)
		rows, _ = unpack(package("<w:p><w:r><w:t>Main</w:t></w:r>" + choice + "</w:p>" + unknown))
		self.assertEqual([v for k, v, _ in rows if k == "p"], ["Main", "Box", "Fallback"])

	def test_strict_word_namespace(self):
		rows, _ = unpack(package(p("Strict"), namespace="http://purl.oclc.org/ooxml/wordprocessingml/main"))
		self.assertEqual(rows[0][1], "Strict")

	def test_tabs_breaks_and_multiple_cell_paragraphs(self):
		body = (
			"<w:tbl><w:tr><w:tc>"
			+ p("A")
			+ "<w:p><w:r><w:t>B</w:t><w:tab/><w:t>C</w:t><w:br/><w:t>D</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
		)
		self.assertEqual(unpack(package(body))[0][0][1], ["A\nB\tC\nD"])

	def test_nested_table_and_images_are_reported_without_duplicated_values(self):
		body = (
			"<w:tbl><w:tr><w:tc>"
			+ p("Outer")
			+ "<w:tbl><w:tr>"
			+ cell("Inner")
			+ "</w:tr></w:tbl></w:tc></w:tr></w:tbl><w:p><w:r><w:drawing/></w:r></w:p>"
		)
		rows, meta = unpack(package(body))
		self.assertEqual(json.dumps(rows).count("Inner"), 1)
		self.assertIn("nested_table_flattened", [x["code"] for x in meta["issues"]])
		self.assertGreater(meta["omitted"]["graphics"], 0)

	def test_image_only_is_not_complete_text(self):
		_, meta = unpack(package("<w:p><w:r><w:drawing/></w:r></w:p>"))
		self.assertIn("no_extractable_text", [x["code"] for x in meta["issues"]])

	def test_automatic_numbering_and_override(self):
		num = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="3"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:start w:val="2"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1.%2)"/></w:lvl></w:abstractNum><w:num w:numId="7"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride></w:num></w:numbering>'

		def props(level):
			return f'<w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="7"/></w:numPr></w:pPr>'

		rows, _ = unpack(
			package(
				p("One", props(0)) + p("Nested", props(1)) + p("Two", props(0)),
				{"word/numbering.xml": num},
				relation("num", "numbering", "numbering.xml"),
			)
		)
		self.assertTrue(rows[0][1].startswith("5."), rows)
		self.assertTrue(rows[1][1].startswith("5.b)"), rows)
		self.assertTrue(rows[2][1].startswith("6."), rows)

	def test_unsupported_numbering_is_an_explicit_issue(self):
		num = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="ideographTraditional"/><w:lvlText w:val="%1"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>'
		props = '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'
		_, meta = unpack(
			package(
				p("Clause", props), {"word/numbering.xml": num}, relation("num", "numbering", "numbering.xml")
			)
		)
		self.assertIn("list_numbering_partial", [x["code"] for x in meta["issues"]])

	def test_xml_entities_depth_corruption_and_macro_are_refused(self):
		invalid = [
			package(
				"",
				document='<!DOCTYPE x [<!ENTITY boom SYSTEM "file:///etc/passwd">]><w:document xmlns:w="'
				+ W
				+ '"><w:body><w:p><w:r><w:t>&boom;</w:t></w:r></w:p></w:body></w:document>',
			),
			package("<w:sdt>" * 260 + p("Deep") + "</w:sdt>" * 260),
			package("", document=f'<w:document xmlns:w="{W}"><w:body>{p("A" * 30000)}</w:body></WRONG>'),
			package(p("Macro"), content_type="application/vnd.ms-word.document.macroEnabled.main+xml"),
			b"not a package",
			bytes.fromhex("d0cf11e0a1b11ae1") + b"locked",
		]
		for data in invalid:
			with self.subTest(size=len(data)), self.assertRaises(InvalidArgumentError):
				unpack(data)

	def test_duplicate_members_and_unsupported_compression(self):
		original = package(p("A"))
		buf = io.BytesIO(original)
		with ZipFile(buf, "a") as archive:
			archive.writestr("word/document.xml", "duplicate")
		with self.assertRaises(InvalidArgumentError):
			unpack(buf.getvalue())
		buf = io.BytesIO()
		with ZipFile(io.BytesIO(original)) as old, ZipFile(buf, "w", ZIP_BZIP2) as archive:
			for name in old.namelist():
				archive.writestr(name, old.read(name))
		with self.assertRaises(InvalidArgumentError):
			unpack(buf.getvalue())


class TestDocxWindows(unittest.TestCase):
	def read(self, data, **args):
		doc = SimpleNamespace(
			name="docx790", file_name="document.docx", file_url="/private/files/document.docx"
		)
		with (
			patch("jarvis.tools.read_file._resolve_file", return_value=doc),
			patch.object(frappe, "has_permission", return_value=True),
			patch("jarvis.compat.file_bytes", return_value=data),
		):
			return read_file(**{"file_url": doc.file_url, **args})

	def test_real_child_reads_docx_instead_of_zip_bytes(self):
		result = self.read(package(p("Real isolated reader")), read_mode="file-cursor-v1")
		self.assertEqual(result["kind"], "document")
		self.assertEqual(result["records"][0]["text"], "Real isolated reader")
		self.assertTrue(result["coverage"]["complete"])

	def test_preview_is_bounded_and_has_no_cursor_authority(self):
		result = self.read(package(p("Preview text")), max_chars=4, preview=True)
		self.assertEqual(result["kind"], "document")
		self.assertTrue(result["truncated"])
		self.assertFalse(result["coverage"]["complete"])
		for key in ("read_contract", "version", "next_read", "end_of_file"):
			self.assertNotIn(key, result)

	def test_exact_windows_and_oversized_rows_join_without_duplicates(self):
		data = package(p("x" * 30000) + "<w:tbl><w:tr>" + cell("y" * 30000) + "</w:tr></w:tbl>" + p("TAIL"))
		args = {}
		records = []
		for _ in range(20):
			r = self.read(data, read_mode="file-cursor-v1", **args)
			records.extend(r["records"])
			if not r["next_read"]:
				break
			args = r["next_read"]
		self.assertTrue(r["end_of_file"])
		text = "".join(x.get("fragment", x.get("text", "")) for x in records if x["block"] == 1)
		self.assertEqual(text, "x" * 30000)
		row = "".join(x["fragment"] for x in records if x["block"] == 2)
		self.assertEqual(json.loads(row), ["y" * 30000])
		self.assertEqual(records[-1]["text"], "TAIL")

	def test_permission_denial_precedes_bytes_and_child(self):
		for args in (
			{"preview": True},
			{"read_mode": "file-cursor-v1", "cursor": "0:1:1", "version": "a" * 64},
		):
			with (
				patch(
					"jarvis.tools.read_file._resolve_file",
					return_value=SimpleNamespace(name="private", file_name="secret.docx"),
				),
				patch.object(frappe, "has_permission", return_value=False),
				patch("jarvis.compat.file_bytes") as raw,
				patch("jarvis.tools._file_scan_process.subprocess.run") as child,
			):
				with self.assertRaises(PermissionDeniedError):
					read_file(file_url="/private/files/secret.docx", **args)
				raw.assert_not_called()
				child.assert_not_called()

	def test_docx_revision_invalidates_only_docx_versions(self):
		from jarvis.tools import _docx_text

		doc = SimpleNamespace(
			name="docx790", file_name="document.docx", file_url="/private/files/document.docx"
		)
		text = SimpleNamespace(name="text790", file_name="a.txt", file_url="/private/files/a.txt")
		data = package(p("A"))
		a = read_sections(doc, data)
		b = read_sections(text, b"A")
		with patch.object(_docx_text, "DOCX_REVISION", "changed"):
			self.assertNotEqual(read_sections(doc, data)["version"], a["version"])
			self.assertEqual(read_sections(text, b"A")["version"], b["version"])

	def test_late_malformed_tail_never_returns_an_initial_window(self):
		data = package("", document=f'<w:document xmlns:w="{W}"><w:body>{p("A" * 30000)}</w:body></WRONG>')
		for args in ({"preview": True}, {"read_mode": "file-cursor-v1"}):
			with self.subTest(args=args), self.assertRaises(InvalidArgumentError):
				self.read(data, **args)


class TestDocxSafety(unittest.TestCase):
	def test_expanded_archive_and_selected_part_limits(self):
		from jarvis.tools import _docx_package

		with patch.object(_docx_package, "MAX_EXPANDED_BYTES", 10), self.assertRaises(InvalidArgumentError):
			unpack(package(p("A")))
		with patch.object(_docx_package, "MAX_ENTRIES", 1), self.assertRaises(InvalidArgumentError):
			unpack(package(p("A")))

	def test_unsafe_relationship_and_utf16_doctype_refused(self):
		data = package(
			p("A"),
			parts={
				"word/_rels/document.xml.rels": f'<Relationships xmlns="{PKG}">{relation("h", "header", "../../outside.xml")}</Relationships>'
			},
			document=f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:sectPr><w:headerReference r:id="h"/></w:sectPr></w:body></w:document>',
		)
		with self.assertRaises(InvalidArgumentError):
			unpack(data)
		xml = (
			'<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY x "EXPAND">]><w:document xmlns:w="'
			+ W
			+ '"><w:body/></w:document>'
		).encode("utf-16")
		with self.assertRaises(InvalidArgumentError):
			unpack(package("", document=xml))

	def test_crc_corruption_and_encrypted_flag_refused(self):
		data = bytearray(package(p("A")))
		first = data.index(b"PK\x01\x02")
		data[first + 16] ^= 1
		with self.assertRaises(InvalidArgumentError):
			unpack(bytes(data))
		data = bytearray(package(p("A")))
		first = data.index(b"PK\x01\x02")
		data[first + 8] |= 1
		with self.assertRaises(InvalidArgumentError):
			unpack(bytes(data))

	def test_docx_block_limit_is_a_stop_and_not_false_completion(self):
		with patch.object(scan, "MAX_ROWS", 2):
			r = scan.scan_file(package(p("A") + p("B") + p("C")), "test.docx")
		self.assertEqual(r["stop_reason"]["code"], "block_limit")
		self.assertEqual([x["text"] for x in r["records"]], ["A", "B"])

	def test_columns_truncate_once_with_an_explicit_issue(self):
		row = "<w:tbl><w:tr>" + cell("A") * 130 + "</w:tr></w:tbl>"
		data, meta = unpack(package(row))
		self.assertEqual(len(data[0][1]), 128)
		self.assertIn("column_limit", [x["code"] for x in meta["issues"]])

	def test_fragment_response_serialized_size_includes_metadata(self):
		r = scan.scan_file(package(p('"😀\\' * 12000)), "test.docx")
		self.assertLessEqual(len(json.dumps(r["records"], ensure_ascii=False)), scan.WINDOW_CHARS)
		self.assertEqual(r["records"][0]["part"], "body")

	def test_200_block_preview_and_requested_rows_are_not_complete(self):
		w = TestDocxWindows()
		for data, args in [
			(package(p("A") * 205), {}),
			(
				package("<w:tbl>" + "<w:tr>" + cell("A") + "</w:tr><w:tr>" + cell("B") + "</w:tr></w:tbl>"),
				{"max_rows": 1},
			),
		]:
			out = w.read(data, preview=True, **args)
			self.assertTrue(out["truncated"])
			self.assertFalse(out["coverage"]["complete"])

	def test_changed_bytes_invalidate_cursor(self):
		w = TestDocxWindows()
		first = w.read(package(p("A" * 30000)), read_mode="file-cursor-v1")
		with self.assertRaises(InvalidArgumentError):
			w.read(package(p("B" * 30000)), read_mode="file-cursor-v1", **first["next_read"])

	def test_near_expanded_limit_is_bounded_or_explicit_resource_refusal(self):
		from jarvis.tools._file_scan_process import scan_file
		from jarvis.tools._file_scan_worker import RESOURCE_ERROR

		# Real child under its own CPU/address-space/wall budget. No site/model access.
		data = package(p("A" * (63 * 1024 * 1024)))
		try:
			result = scan_file(data, "large.docx")
		except InvalidArgumentError as exc:
			self.assertEqual(str(exc), RESOURCE_ERROR)
		else:
			self.assertIsNotNone(result["next_cursor"])
			self.assertEqual(result["records"][0]["offset"], 0)
			self.assertLessEqual(len(json.dumps(result["records"])), scan.WINDOW_CHARS)


class TestDocxNativePermissions(unittest.TestCase):
	"""Real private File/role checks on the dedicated test site, rolled back."""

	def setUp(self):
		import uuid

		from frappe.utils.file_manager import save_file

		self.assertEqual(frappe.local.site, "test_jarvis")
		self.original_user = frappe.session.user
		frappe.set_user("Administrator")
		self.prefix = "j790-" + uuid.uuid4().hex[:8]
		self.owner, self.other = (self.prefix + suffix + "@example.test" for suffix in ("-owner", "-other"))
		for name in (self.owner, self.other):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": name,
					"first_name": "DOCX test",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "Jarvis User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(self.owner)
		self.raw = package(
			p("Private " + self.prefix) + "<w:tbl><w:tr>" + cell("Cloth") + cell("24") + "</w:tr></w:tbl>"
		)
		self.file = save_file(self.prefix + ".docx", self.raw, None, None, is_private=1)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.set_user(self.original_user)

	def test_native_owner_reads_and_previews_original_bytes(self):
		from jarvis import compat
		from jarvis.chat.api import preview_file

		data = read_file(file_url=self.file.file_url, read_mode="file-cursor-v1")
		self.assertEqual(data["kind"], "document")
		self.assertIn(self.prefix, data["records"][0]["text"])
		view = preview_file(self.file.file_url)
		self.assertEqual(view["document"]["blocks"][1]["rows"], [["Cloth", "24"]])
		self.assertEqual(compat.file_bytes(frappe.get_doc("File", self.file.name)), self.raw)

	def test_native_outsider_never_reaches_bytes_or_child(self):
		from jarvis.chat.api import preview_file

		frappe.set_user(self.other)
		with (
			patch("jarvis.compat.file_bytes") as raw,
			patch("jarvis.tools._file_scan_process.subprocess.run") as child,
		):
			with self.assertRaises(PermissionDeniedError):
				preview_file(self.file.file_url)
			raw.assert_not_called()
			child.assert_not_called()

	def test_native_revocation_denies_a_continuation(self):
		with patch("jarvis.compat.file_bytes", return_value=package(p("x" * 30000))):
			first = read_file(file_url=self.file.file_url, read_mode="file-cursor-v1")
		frappe.set_user("Administrator")
		frappe.db.set_value("File", self.file.name, "owner", self.other)
		frappe.set_user(self.owner)
		with (
			patch("jarvis.compat.file_bytes") as raw,
			patch("jarvis.tools._file_scan_process.subprocess.run") as child,
		):
			with self.assertRaises(PermissionDeniedError):
				read_file(read_mode="file-cursor-v1", **first["next_read"])
			raw.assert_not_called()
			child.assert_not_called()

	def test_preview_refusal_is_docx_specific_but_missing_file_still_raises(self):
		from jarvis.chat.api import preview_file

		with patch("jarvis.compat.file_bytes", return_value=b"broken package"):
			result = preview_file(self.file.file_url)
		self.assertEqual(result["kind"], "binary")
		self.assertIn("DOCX", result["error"])
		with self.assertRaises(InvalidArgumentError):
			preview_file("/private/files/does-not-exist-790.docx")

	def test_turn_preparation_routes_docx_without_parse_dispatch_or_vision(self):
		from jarvis.chat.turn_handler import _prepare_attachments

		with (
			patch("frappe.enqueue") as enqueue,
			patch("jarvis.tools._file_scan_process.subprocess.run") as child,
		):
			text, images = _prepare_attachments(
				"Read this", [{"file_url": self.file.file_url, "file_name": self.file.file_name}], False
			)
		self.assertIn("Attached DOCX", text)
		self.assertIn("jarvis__read_file", text)
		self.assertNotIn("is binary", text)
		self.assertNotIn(self.prefix + "</w:t>", text)
		self.assertEqual(images, [])
		enqueue.assert_not_called()
		child.assert_not_called()


class TestDocxReviewRegressions(unittest.TestCase):
	def test_child_restart_does_not_invent_an_ambiguous_parent_label(self):
		num = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:lvlText w:val="%1.%2"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="1"><w:startOverride w:val="1"/></w:lvlOverride></w:num></w:numbering>'

		def props(n, level):
			return f'<w:pPr><w:numPr><w:numId w:val="{n}"/><w:ilvl w:val="{level}"/></w:numPr></w:pPr>'

		rows, meta = unpack(
			package(
				p("One", props(1, 0)) + p("Two", props(1, 0)) + p("Ambiguous child", props(2, 1)),
				{"word/numbering.xml": num},
				relation("n", "numbering", "numbering.xml"),
			)
		)
		self.assertIn("list_numbering_partial", [i["code"] for i in meta["issues"]])
		self.assertEqual(rows[-1][1], "Ambiguous child")

	def test_shared_abstract_without_restart_is_marked_partial(self):
		num = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>'

		def props(n):
			return f'<w:pPr><w:numPr><w:numId w:val="{n}"/></w:numPr></w:pPr>'

		_, meta = unpack(
			package(
				p("One", props(1)) + p("Two", props(1)) + p("Ambiguous", props(2)),
				{"word/numbering.xml": num},
				relation("n", "numbering", "numbering.xml"),
			)
		)
		self.assertIn("list_numbering_partial", [i["code"] for i in meta["issues"]])

	def test_legal_numbering_uses_decimal_parent_labels(self):
		num = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="upperRoman"/><w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:isLgl/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>'

		def props(n):
			return f'<w:pPr><w:numPr><w:ilvl w:val="{n}"/><w:numId w:val="1"/></w:numPr></w:pPr>'

		rows, _ = unpack(
			package(
				p("Parent", props(0)) + p("Child", props(1)),
				{"word/numbering.xml": num},
				relation("n", "numbering", "numbering.xml"),
			)
		)
		self.assertTrue(rows[1][1].startswith("1.1"), rows)

	def test_bad_grid_after_a_full_window_never_returns_partial_success(self):
		data = package(
			p("Before") * 300 + "<w:tbl><w:tr>" + cell("Bad", '<w:gridSpan w:val="-1"/>') + "</w:tr></w:tbl>"
		)
		for args in ({"preview": True}, {"read_mode": "file-cursor-v1"}):
			with self.subTest(args=args), self.assertRaises(InvalidArgumentError):
				TestDocxWindows().read(data, **args)

	def test_legacy_preview_does_not_show_serialized_row_fragments(self):
		data = package("<w:tbl><w:tr>" + cell("y" * 30000) + "</w:tr></w:tbl>")
		r = TestDocxWindows().read(data, preview=True)
		self.assertNotIn('["yyyy', r["text"])
		from jarvis.tools._docx_preview import document_view

		self.assertEqual(document_view(r)["shown"], 0)
