"""Actual streams and SQLite: complete scopes, integrity, limits and source identity."""

import gzip
import io
import json
import stat
import struct
import unittest
import zipfile
import zlib
from unittest.mock import patch

from jarvis.tools import _archive_sources as a


def archive(files, compression=zipfile.ZIP_DEFLATED):
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", compression=compression) as book:
		for name, data in files:
			book.writestr(name, data)
	return buffer.getvalue()


def understated_zip():
	raw, prefix = b"amount\n0.10\n999999.00\n", b"amount\n0.10\n"
	data = bytearray(archive([("bank.csv", raw)]))
	local, central = data.index(b"PK\x03\x04"), data.index(b"PK\x01\x02")
	for offset in (local + 14, central + 16):
		struct.pack_into("<I", data, offset, zlib.crc32(prefix))
	for offset in (local + 22, central + 24):
		struct.pack_into("<I", data, offset, len(prefix))
	return bytes(data), prefix


class TestArchiveSources(unittest.TestCase):
	def test_macos_resource_forks_do_not_consume_the_data_file_slots(self):
		files = [(f"data{i}.csv", "A\n1\n") for i in range(32)]
		files += [(f"__MACOSX/._data{i}.csv", b"resource-fork") for i in range(32)]
		source = a.ArchiveSource(archive(files), "macos.zip")
		selected = [m["id"] for m in source.members if m["status"] == "supported"]
		self.assertEqual(len(source.members), 64)
		self.assertEqual(len(selected), 32)
		result = a.query_archive(source, selected)
		self.assertTrue(result["coverage"]["complete"])
		self.assertFalse(result["archive_complete"])
		self.assertEqual(result["unread_member_count"], 32)
		with self.assertRaises(a.ArchiveUnsupportedError):
			source.select(["m33"])
		with self.assertRaises(a.ArchiveCapacityError):
			a.ArchiveSource(archive(files + [("extra.csv", "A\n1")]), "too-many.zip")

	def test_omitted_markdown_tables_keep_the_original_archive_text(self):
		source = a.ArchiveSource(archive([("notes.md", "|a|b|\n|---|---|\n|1|2|3|\nTAIL")]), "notes.zip")
		result = a.query_archive(source, ["m1"], sql="SELECT text FROM m1_text_lines WHERE text='TAIL'")
		self.assertEqual(result["rows"], [["TAIL"]])
		self.assertEqual(result["warnings"], [{"code": "malformed_markdown_table", "omitted_tables": 1}])
		self.assertFalse(result["coverage"]["derived_tables_complete"])

	def test_backslash_refusal_explains_the_supported_separator(self):
		with self.assertRaisesRegex(a.ArchiveUnsupportedError, "Backslash.*forward slashes"):
			a.ArchiveSource(archive([("a\\b.csv", "x")]), "paths.zip")

	def test_six_months_query_whole_scope_and_tail(self):
		files = [
			(
				f"month{i}/bank.csv",
				"Reference,Amount,Currency\n" + "".join(f"M{i}-{r},0.10,INR\n" for r in range(1, 1201)),
			)
			for i in range(1, 7)
		]
		source = a.ArchiveSource(archive(files), "bank.zip")
		manifest = source.manifest()
		self.assertFalse(manifest["coverage"]["complete"])
		self.assertIsNone(manifest["source_rows"])
		members = [m["id"] for m in manifest["members"]]
		union = " UNION ALL ".join(f"SELECT * FROM {m}_sheet1 WHERE _row > 1" for m in members)
		result = a.query_archive(source, members, sql=f"SELECT COUNT(*), decimal_sum(B) FROM ({union})")
		self.assertEqual(result["rows"], [[7200, "720.00"]])
		self.assertEqual(result["source_rows"], 7206)
		self.assertTrue(result["archive_complete"])
		tail = a.query_archive(source, ["m6"], sql="SELECT _row,A FROM m6_sheet1 WHERE _row=1201")
		self.assertEqual(tail["rows"], [[1201, "M6-1200"]])
		self.assertFalse(tail["archive_complete"])
		self.assertEqual(tail["unread_member_count"], 5)

	def test_mixed_sources_namespaces_and_original_rows(self):
		source = a.ArchiveSource(
			archive(
				[("data.json", '{"amount":0.10}'), ("notes.md", "# Heading\n|A|B|\n|---|---|\n|x|0.20|\n")]
			),
			"mixed.zip",
		)
		result = a.query_archive(source, ["m1", "m2"])
		self.assertEqual(
			[t["table"] for t in result["sheets"]], ["m1_json_nodes", "m2_text_lines", "m2_md_table1"]
		)
		result = a.query_archive(
			source, ["m1", "m2"], sql="SELECT value FROM m1_json_nodes WHERE path='/amount'"
		)
		self.assertEqual(result["rows"], [["0.10"]])

	def test_gzip_formats_and_whole_integrity_before_first_window(self):
		content = gzip.compress(b"Reference,Amount\nLAST,0.10\n")
		source = a.ArchiveSource(content, "data.csv.gz")
		self.assertEqual(
			a.query_archive(source, ["m1"], sql="SELECT B FROM m1_sheet1 WHERE _row=2")["rows"], [["0.10"]]
		)
		bare = a.ArchiveSource(content, "data.gz")
		self.assertEqual(bare.members[0]["status"], "format_required")
		with self.assertRaises(a.ArchiveUnsupportedError):
			a.query_archive(bare, ["m1"])
		bare = a.ArchiveSource(content, "data.gz", payload_format="csv")
		self.assertTrue(a.query_archive(bare, ["m1"])["coverage"]["complete"])
		for bad in (content[:-3], content + content, content + b"extra", content[:-8] + b"\x00" * 8):
			with (
				self.subTest(bad=bad[-8:]),
				self.assertRaises((a.ArchiveIntegrityError, a.ArchiveUnsupportedError)),
			):
				a.scan_archive(a.ArchiveSource(bad, "data.csv.gz"), "m1")

	def test_understated_size_with_matching_prefix_crc_is_not_complete(self):
		content, prefix = understated_zip()
		with zipfile.ZipFile(io.BytesIO(content)) as old:
			self.assertEqual(old.read("bank.csv"), prefix)
			self.assertIsNone(old.testzip())
		with self.assertRaises(a.ArchiveIntegrityError):
			a.query_archive(
				a.ArchiveSource(content, "bank.zip"), ["m1"], sql="SELECT COUNT(*) FROM m1_sheet1"
			)

	def test_failure_in_last_member_returns_no_partial_aggregate(self):
		content = archive([("a.csv", "Amount\n0.10\n"), ("b.json", "not json")])
		with self.assertRaises(Exception):
			a.query_archive(
				a.ArchiveSource(content, "bank.zip"), ["m1", "m2"], sql="SELECT COUNT(*) FROM m1_sheet1"
			)

	def test_unsafe_paths_duplicates_links_and_unsupported_members(self):
		for path in ("../bad.csv", "/bad.csv", "C:/bad.csv", "a\\bad.csv", "a/./bad.csv", "bad\n.csv"):
			with self.subTest(path=path), self.assertRaises(a.ArchiveUnsupportedError):
				a.ArchiveSource(archive([(path, "x")]), "bad.zip")
		with self.assertWarns(UserWarning):
			content = archive([("a.csv", "x"), ("a.csv", "y")])
		with self.assertRaises(a.ArchiveUnsupportedError):
			a.ArchiveSource(content, "bad.zip")
		b = io.BytesIO()
		with zipfile.ZipFile(b, "w") as z:
			i = zipfile.ZipInfo("link.csv")
			i.create_system = 3
			i.external_attr = (stat.S_IFLNK | 0o777) << 16
			z.writestr(i, "/etc/passwd")
		with self.assertRaises(a.ArchiveUnsupportedError):
			a.ArchiveSource(b.getvalue(), "bad.zip")
		source = a.ArchiveSource(
			archive([("bank.csv", "X\n1\n"), ("nested.gz", gzip.compress(b"x")), ("x.pdf", b"PDF")]),
			"bank.zip",
		)
		self.assertEqual(
			[m["status"] for m in source.members], ["supported", "unsupported_format", "unsupported_format"]
		)
		with self.assertRaises(a.ArchiveUnsupportedError):
			a.query_archive(source, ["m1", "m2"])

	def test_actual_limits_are_shared_and_do_not_truncate(self):
		source = a.ArchiveSource(archive([("a.txt", "a" * 20), ("b.txt", "b" * 20)]), "limits.zip")
		with patch.object(a, "MAX_LEAF", 10), self.assertRaises(a.ArchiveCapacityError):
			a.query_archive(source, ["m1"])
		with patch.object(a, "MAX_LEAVES", 30), self.assertRaises(a.ArchiveCapacityError):
			a.query_archive(source, ["m1", "m2"])
		with patch.object(a, "MAX_FILES", 1), self.assertRaises(a.ArchiveCapacityError):
			a.ArchiveSource(source.content, "limits.zip")
		with self.assertRaises(a.ArchiveSelectionError):
			a.query_archive(source, ["m1", "m1"])
		with self.assertRaises(a.ArchiveSelectionError):
			a.query_archive(source, ["m3"])

	def test_manifest_bound_and_corrupt_compression(self):
		files = [(f"folder/{i}/" + "a" * 220 + ".txt", "x") for i in range(32)] + [
			(f"dir{i}/", "") for i in range(96)
		]
		source = a.ArchiveSource(archive(files), "names.zip")
		self.assertLess(len(a.encode(source.manifest())), 16000)
		self.assertEqual(source.directories, 96)
		self.assertEqual(len(source.members), 32)
		self.assertTrue(all(m["name_shortened"] for m in source.members))
		with self.assertRaises(a.ArchiveIntegrityError):
			a.ArchiveSource(b"invalid", "bad.zip")
		unsupported = a.ArchiveSource(archive([("a.csv", "X\n1")], zipfile.ZIP_BZIP2), "method.zip")
		with self.assertRaises(a.ArchiveUnsupportedError):
			a.query_archive(unsupported, ["m1"])


class TestArchiveWorkbookAndZipWriters(unittest.TestCase):
	def test_empty_deflate_thumbnail_does_not_hide_workbook_rows(self):
		from jarvis.tools._file_scan import scan_file
		from jarvis.tools._spreadsheet_query import query_spreadsheet

		buffer = io.BytesIO()
		with zipfile.ZipFile(io.BytesIO(self.workbook())) as original, zipfile.ZipFile(buffer, "w") as writer:
			for info in original.infolist():
				writer.writestr(info, original.read(info))
			writer.writestr("docProps/thumbnail.wmf", b"", compress_type=zipfile.ZIP_STORED)
		content = bytearray(buffer.getvalue())
		with zipfile.ZipFile(io.BytesIO(content)) as book:
			local = book.getinfo("docProps/thumbnail.wmf").header_offset
		central = content.rindex(b"PK\x01\x02")
		# Excel 2010 can mark a zero-byte part as deflate with no data stream.
		struct.pack_into("<H", content, local + 8, zipfile.ZIP_DEFLATED)
		struct.pack_into("<H", content, central + 10, zipfile.ZIP_DEFLATED)
		self.assertGreater(a.validate_workbook(content), 0)
		result = query_spreadsheet(
			bytes(content), "empty-thumbnail.xlsx", sql="SELECT A,B FROM sheet1 WHERE _row=2"
		)
		self.assertEqual(result["rows"], [["LAST", "0.10"]])
		self.assertEqual(len(scan_file(bytes(content), "empty-thumbnail.xlsx")["records"]), 2)
		for crc, size in ((1, 0), (0, 1)):
			broken = bytearray(content)
			for offset in (local + 14, central + 16):
				struct.pack_into("<I", broken, offset, crc)
			for offset in (local + 22, central + 24):
				struct.pack_into("<I", broken, offset, size)
			with self.assertRaises(a.ArchiveIntegrityError):
				a.validate_workbook(bytes(broken))

	def test_high_ratio_streams_keep_the_last_copy(self):
		for length in (65536, 65537, 131071, 131072, 131073, 1048576 + 17):
			content = b"a" * length
			self.assertEqual(a._decode(gzip.compress(content), wbits=31, limit=length), content)
			source = a.ZipSource(archive([("a.txt", content)]))
			self.assertEqual(source.read(source.infos[0], limit=length), content)

	def test_zip64_and_data_descriptor_writers(self):
		class Stream(io.BytesIO):
			def seek(self, *args):
				raise OSError("nonseekable writer")

		for streaming in (False, True):
			for force64 in (False, True):
				for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
					with self.subTest(streaming=streaming, zip64=force64, method=method):
						buffer = Stream() if streaming else io.BytesIO()
						with zipfile.ZipFile(buffer, "w", compression=method) as writer:
							with writer.open("bank.csv", "w", force_zip64=force64) as out:
								out.write(b"Amount\n0.10\n")
						content = buffer.getvalue()
						result = a.query_archive(
							a.ArchiveSource(content, "bank.zip"),
							["m1"],
							sql="SELECT decimal_sum(A) FROM m1_sheet1 WHERE _row>1",
						)
						self.assertEqual(result["rows"], [["0.10"]])
						if streaming:
							# An unsigned descriptor is valid too. The central directory moves back four bytes.
							position = content.index(b"PK\x07\x08")
							unsigned = bytearray(content[:position] + content[position + 4 :])
							eocd = unsigned.rindex(b"PK\x05\x06")
							struct.pack_into(
								"<I",
								unsigned,
								eocd + 16,
								struct.unpack_from("<I", unsigned, eocd + 16)[0] - 4,
							)
							self.assertEqual(
								a.query_archive(a.ArchiveSource(bytes(unsigned), "bank.zip"), ["m1"])[
									"source_rows"
								],
								2,
							)

	@staticmethod
	def workbook():
		from openpyxl import Workbook

		book = Workbook()
		book.active.append(["Ref", "Amount"])
		book.active.append(["LAST", "0.10"])
		buffer = io.BytesIO()
		book.save(buffer)
		return buffer.getvalue()

	def test_workbook_parity_plain_zip_gzip_and_shared_inner_budget(self):
		from jarvis.tools._spreadsheet_query import query_spreadsheet

		content = self.workbook()
		plain = query_spreadsheet(content, "bank.xlsx", sql="SELECT B FROM sheet1 WHERE _row=2")
		self.assertEqual(plain["rows"], [["0.10"]])
		for wrapped, name in (
			(archive([("bank.xlsx", content)]), "bank.zip"),
			(gzip.compress(content), "bank.xlsx.gz"),
		):
			self.assertEqual(
				a.query_archive(
					a.ArchiveSource(wrapped, name), ["m1"], sql="SELECT B FROM m1_sheet1 WHERE _row=2"
				)["rows"],
				plain["rows"],
			)
		xml = a.validate_workbook(content)
		source = a.ArchiveSource(archive([("a.xlsx", content), ("b.xlsx", content)]), "two.zip")
		with patch.object(a, "MAX_XML", xml + xml // 2), self.assertRaises(a.ArchiveCapacityError):
			a.query_archive(source, ["m1", "m2"])

	def test_valid_workbook_part_location_and_large_ignored_xml(self):
		from jarvis.tools._spreadsheet_query import query_spreadsheet

		buffer = io.BytesIO()
		with (
			zipfile.ZipFile(io.BytesIO(self.workbook())) as original,
			zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as writer,
		):
			for info in original.infolist():
				name, data = info.filename, original.read(info)
				if name == "[Content_Types].xml" or name == "_rels/.rels":
					data = data.replace(b"workbook.xml", b"custom.xml")
				name = name.replace("workbook.xml", "custom.xml")
				writer.writestr(name, data)
			writer.writestr("customXml/repeated.xml", b"<r>" + b"a" * 1048577 + b"</r>")
		content = buffer.getvalue()
		self.assertEqual(
			query_spreadsheet(content, "relocated.xlsx", sql="SELECT A FROM sheet1 WHERE _row=2")["rows"],
			[["LAST"]],
		)
		self.assertGreater(a.validate_workbook(content), 1048577)

	def test_workbook_understated_stream_is_refused_on_every_new_path(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools._file_scan import scan_file
		from jarvis.tools._spreadsheet_query import query_spreadsheet

		content = bytearray(self.workbook())
		with zipfile.ZipFile(io.BytesIO(content)) as old:
			info = old.infolist()[0]
			prefix = old.read(info)[:20]
		local = info.header_offset
		central = content.index(b"PK\x01\x02")
		for offset in (local + 14, central + 16):
			struct.pack_into("<I", content, offset, zlib.crc32(prefix))
		for offset in (local + 22, central + 24):
			struct.pack_into("<I", content, offset, len(prefix))
		for call in (
			lambda: query_spreadsheet(bytes(content), "bank.xlsx"),
			lambda: scan_file(bytes(content), "bank.xlsx"),
			lambda: a.query_archive(a.ArchiveSource(archive([("bank.xlsx", content)]), "bank.zip"), ["m1"]),
			lambda: a.query_archive(a.ArchiveSource(gzip.compress(content), "bank.xlsx.gz"), ["m1"]),
		):
			with self.assertRaises(InvalidArgumentError):
				call()

	def test_global_row_and_cell_limits_across_formats(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools import _spreadsheet_query as query

		source = a.ArchiveSource(archive([("a.csv", "A,B\nx,y\n"), ("b.txt", "one\ntwo\n")]), "mixed.zip")
		with patch.object(query, "MAX_TOTAL_ROWS", 3), self.assertRaises(InvalidArgumentError):
			a.query_archive(source, ["m1", "m2"])
		with patch.object(query, "MAX_CELLS", 5), self.assertRaises(InvalidArgumentError):
			a.query_archive(source, ["m1", "m2"])
