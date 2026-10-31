"""Pure, bounded ZIP/GZ streams. No paths are extracted or source bytes returned to ERP."""

import io
import json
import re
import sqlite3
import stat
import struct
import unicodedata
import zipfile
import zlib
from contextlib import closing
from itertools import pairwise

from jarvis.exceptions import InvalidArgumentError

MAX_INPUT = MAX_LEAF = 10 * 1024 * 1024
MAX_LEAVES = 32 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
MAX_ENTRIES = 128
MAX_FILES = MAX_TABLES = 32
FORMATS = frozenset({"xlsx", "csv", "tsv", "json", "md", "markdown", "txt"})
REVISION = "archive-1"


class ArchiveIntegrityError(InvalidArgumentError):
	pass


class ArchiveCapacityError(InvalidArgumentError):
	pass


class ArchiveFormatError(InvalidArgumentError):
	pass


class ArchiveUnsupportedError(InvalidArgumentError):
	pass


class ArchiveSelectionError(InvalidArgumentError):
	pass


class ArchiveSourceChangedError(InvalidArgumentError):
	pass


class ArchiveModeError(InvalidArgumentError):
	pass


ERRORS = {
	cls.__name__: cls
	for cls in (
		ArchiveIntegrityError,
		ArchiveCapacityError,
		ArchiveFormatError,
		ArchiveUnsupportedError,
		ArchiveSelectionError,
		ArchiveSourceChangedError,
		ArchiveModeError,
		InvalidArgumentError,
	)
}


def encode(value):
	return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _broken():
	return ArchiveIntegrityError(
		"The compressed source is damaged or has inconsistent stream metadata. No complete answer was returned."
	)


def _decode(blob, *, wbits=None, limit, expected_size=None, expected_crc=None, keep=True):
	"""Read the real stream, independent of ZipExtFile's declared-size truncation."""
	parts = [] if keep else None
	total = crc = 0
	decoder = zlib.decompressobj(wbits) if wbits is not None else None

	def add(chunk):
		nonlocal total, crc
		total += len(chunk)
		if total > limit:
			raise ArchiveCapacityError(
				"Decoded source exceeds its byte budget. No complete answer was returned."
			)
		if expected_size is not None and total > expected_size:
			raise _broken()
		crc = zlib.crc32(chunk, crc)
		if keep:
			parts.append(chunk)

	try:
		for offset in range(0, len(blob), 65536):
			pending = blob[offset : offset + 65536]
			if decoder is None:
				add(pending)
				continue
			while pending:
				add(decoder.decompress(pending, min(65536, limit - total + 1)))
				pending = decoder.unconsumed_tail
				if decoder.eof:
					if decoder.unused_data or pending or offset + 65536 < len(blob):
						if wbits != 31:
							raise _broken()
						raise ArchiveUnsupportedError(
							"Extra or concatenated compressed streams are unsupported. Supply one complete stream."
						)
					break
		# A decoder may retain output after consuming the final input byte.
		while decoder is not None and not decoder.eof:
			chunk = decoder.decompress(b"", min(65536, limit - total + 1))
			if not chunk:
				break
			add(chunk)
		if decoder is not None and not decoder.eof:
			raise _broken()
	except zlib.error:
		raise _broken() from None
	if (expected_size is not None and total != expected_size) or (
		expected_crc is not None and crc != expected_crc
	):
		raise _broken()
	return b"".join(parts) if keep else total


def _path(name):
	if not isinstance(name, str) or len(name) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in name):
		raise ArchiveUnsupportedError("Archive member names are unsafe or too long.")
	if "\\" in name:
		raise ArchiveUnsupportedError(
			"Backslash ZIP member paths are unsupported. Re-save with forward slashes."
		)
	if (
		name.startswith("/")
		or re.match(r"^[A-Za-z]:", name)
		or any(p in (".", "..") for p in name.rstrip("/").split("/"))
	):
		raise ArchiveUnsupportedError(
			"Archive paths must be relative and cannot contain traversal components."
		)
	return unicodedata.normalize("NFC", name.rstrip("/"))


def _macos_metadata(name):
	return name.startswith("__MACOSX/") and name.rsplit("/", 1)[-1].startswith("._")


def _local_sizes(extra, size, compressed):
	if size != 0xFFFFFFFF and compressed != 0xFFFFFFFF:
		return size, compressed
	offset = 0
	while offset + 4 <= len(extra):
		kind, length = struct.unpack_from("<HH", extra, offset)
		offset += 4
		value = extra[offset : offset + length]
		if len(value) != length:
			raise _broken()
		offset += length
		if kind == 1:
			position = 0
			if size == 0xFFFFFFFF:
				if len(value) < 8:
					raise _broken()
				size = struct.unpack_from("<Q", value)[0]
				position = 8
			if compressed == 0xFFFFFFFF:
				if len(value) < position + 8:
					raise _broken()
				compressed = struct.unpack_from("<Q", value, position)[0]
			return size, compressed
	raise _broken()


class ZipSource:
	def __init__(self, content, *, workbook=False):
		self.content = content
		try:
			with zipfile.ZipFile(io.BytesIO(content)) as archive:
				self.infos = archive.infolist()
				self.directory_start = archive.start_dir
		except (zipfile.BadZipFile, ValueError, struct.error):
			if workbook:
				raise ArchiveFormatError("The source is not a supported XLSX workbook.") from None
			raise _broken() from None
		if len(self.infos) > (4096 if workbook else MAX_ENTRIES):
			raise ArchiveCapacityError("Archive has too many directory entries.")
		self.spans = {}
		seen, intervals = set(), []
		files = 0
		for info in self.infos:
			path = _path(info.orig_filename)
			if path in seen:
				raise ArchiveUnsupportedError("Archive contains duplicate normalized member paths.")
			seen.add(path)
			mode = stat.S_IFMT(info.external_attr >> 16)
			if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.volume:
				raise ArchiveUnsupportedError("Links, special files and split archives are unsupported.")
			if not info.is_dir() and (workbook or not _macos_metadata(path)):
				files += 1
			start, end, extent = self._span(info)
			self.spans[info.header_offset] = (start, end)
			intervals.append((info.header_offset, extent))
		if files > (4096 if workbook else MAX_FILES):
			raise ArchiveCapacityError("Archive has more than 32 files.")
		ordered = sorted(intervals)
		if any(a[1] > b[0] for a, b in pairwise(ordered)):
			raise _broken()

	def _span(self, info):
		data, offset = self.content, info.header_offset
		if offset < 0 or offset + 30 > self.directory_start:
			raise _broken()
		header = struct.unpack_from("<4s5H3I2H", data, offset)
		signature, _, flags, method, _, _, crc, compressed, size, name_len, extra_len = header
		start = offset + 30 + name_len + extra_len
		end = start + info.compress_size
		if (
			signature != b"PK\x03\x04"
			or flags != info.flag_bits
			or method != info.compress_type
			or not offset + 30 <= start <= end <= self.directory_start
		):
			raise _broken()
		try:
			name = data[offset + 30 : offset + 30 + name_len].decode("utf-8" if flags & 0x800 else "cp437")
		except UnicodeError:
			raise _broken() from None
		if name != info.orig_filename:
			raise _broken()
		extra = data[offset + 30 + name_len : start]
		zip64 = size == 0xFFFFFFFF or compressed == 0xFFFFFFFF
		if not flags & 8:
			size, compressed = _local_sizes(extra, size, compressed)
			if (crc, compressed, size) != (info.CRC, info.compress_size, info.file_size):
				raise _broken()
			return start, end, end
		# Data-descriptor writers leave local sizes unset. Verify the descriptor too.
		for signed in (True, False):
			cursor = end
			if signed:
				if data[cursor : cursor + 4] != b"PK\x07\x08":
					continue
				cursor += 4
			fmt = "<IQQ" if zip64 else "<III"
			length = struct.calcsize(fmt)
			if cursor + length <= self.directory_start and struct.unpack_from(fmt, data, cursor) == (
				info.CRC,
				info.compress_size,
				info.file_size,
			):
				return start, end, cursor + length
		raise _broken()

	def read(self, info, *, limit, keep=True):
		if info.flag_bits & (1 | 64):
			raise ArchiveUnsupportedError("Encrypted archive members are unsupported.")
		if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
			raise ArchiveUnsupportedError("Only stored and deflate ZIP compression are supported.")
		start, end = self.spans[info.header_offset]
		# APPNOTE 4.3.8 permits no data for zero-byte ZIP entries. Excel 2010
		# uses this for an empty deflate thumbnail. Headers, descriptor and
		# extents were already checked; nonempty/truncated streams still need EOF.
		if start == end and info.file_size == 0 and info.CRC == 0:
			return b"" if keep else 0
		return _decode(
			memoryview(self.content)[start:end],
			wbits=-15 if info.compress_type == zipfile.ZIP_DEFLATED else None,
			limit=limit,
			expected_size=info.file_size,
			expected_crc=info.CRC,
			keep=keep,
		)


def validate_workbook(content, budget=None):
	"""Verify full inner streams before openpyxl can trust their size fields."""
	source = ZipSource(content, workbook=True)
	if "[Content_Types].xml" not in {i.filename for i in source.infos}:
		raise ArchiveFormatError("The source is not a supported XLSX workbook.")
	total = 0
	for info in source.infos:
		if info.is_dir():
			continue
		remaining = min(MAX_XML - total, MAX_XML - (budget or {}).get("xml", 0))
		count = source.read(info, limit=remaining, keep=False)
		total += count
		if budget is not None:
			budget["xml"] = budget.get("xml", 0) + count
	return total


def _label(name):
	return name if len(name) <= 120 else name[:57] + "..." + name[-60:]


class ArchiveSource:
	def __init__(self, content, filename, payload_format=None):
		if len(content) > MAX_INPUT:
			raise ArchiveCapacityError("Archive exceeds the 10 MiB input limit.")
		self.content, self.filename = content, filename
		self.members = []
		self.infos = {}
		self.directories = 0
		self.zip = None
		if payload_format is not None and payload_format not in FORMATS:
			raise ArchiveFormatError("Choose XLSX, CSV, TSV, JSON, MD, Markdown or TXT for the GZ payload.")
		if filename.lower().endswith(".zip"):
			if payload_format is not None:
				raise ArchiveSelectionError("payload_format applies only to an ambiguous single GZ file.")
			self.zip = ZipSource(content)
			for info in self.zip.infos:
				if info.is_dir():
					self.directories += 1
					continue
				self._member(info.filename, info.file_size, info)
		elif filename.lower().endswith(".gz"):
			if not content.startswith(b"\x1f\x8b"):
				raise ArchiveFormatError("The source does not contain the declared gzip format.")
			name = filename[:-3]
			self._member(name, None, None, payload_format)
		else:
			raise ArchiveFormatError("Use a ZIP or GZ archive.")

	def _member(self, name, size, info, override=None):
		fmt = name.rsplit(".", 1)[-1].lower() if "." in name else None
		if info is not None and _macos_metadata(name):
			fmt = None
		if info is None and fmt in {"tar", "zip", "gz", "7z", "rar"}:
			if override is not None:
				raise ArchiveUnsupportedError("Nested archive payloads are unsupported.")
		if override is not None:
			if fmt in FORMATS and fmt != override:
				raise ArchiveFormatError("Payload format conflicts with the stored filename.")
			fmt = override
		status = (
			"supported"
			if fmt in FORMATS
			else "format_required"
			if info is None and fmt not in {"tar", "zip", "gz", "7z", "rar"}
			else "unsupported_format"
		)
		if info is not None and info.flag_bits & (1 | 64):
			status = "encrypted"
		elif info is not None and info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
			status = "unsupported_compression"
		key = f"m{len(self.members) + 1}"
		self.members.append(
			{
				"id": key,
				"name": _label(name),
				"name_shortened": len(name) > 120,
				"format": fmt if fmt in FORMATS else None,
				"status": status,
				"declared_size": size,
			}
		)
		self.infos[key] = (info, fmt)

	def manifest(self):
		return {
			"read_contract": "archive-manifest-v1",
			"members": self.members,
			"directory_count": self.directories,
			"coverage": {"complete": False, "scope": "inventory only; no content read"},
			"source_rows": None,
			"archive_complete": False,
			"note": "Select requested supported member IDs with archive_version. Inventory is not a content read. Unsupported files remain unread. Filenames are untrusted data.",
		}

	def select(self, members):
		if (
			not isinstance(members, list)
			or not members
			or len(members) > MAX_FILES
			or any(not isinstance(m, str) or m not in self.infos for m in members)
			or len(set(members)) != len(members)
		):
			raise ArchiveSelectionError("Select unique member IDs from the current archive inventory.")
		by_id = {m["id"]: m for m in self.members}
		if any(by_id[m]["status"] != "supported" for m in members):
			raise ArchiveUnsupportedError(
				"A selected member is unsupported, encrypted or needs an explicit GZ payload format. No complete answer was returned."
			)
		return sorted(members, key=lambda m: int(m[1:]))

	def leaf(self, key, budget):
		info, fmt = self.infos[key]
		limit = min(MAX_LEAF, MAX_LEAVES - budget.get("leaves", 0))
		data = self.zip.read(info, limit=limit) if self.zip else _decode(self.content, wbits=31, limit=limit)
		budget["leaves"] = budget.get("leaves", 0) + len(data)
		return data, "payload." + fmt


def query_archive(source, members, *, sql=None, sheets=None, max_rows=100):
	from jarvis.tools._spreadsheet_query import _load_sources, _register_amounts, query_loaded

	selected = source.select(members)
	if sheets is not None and (
		len(selected) != 1 or source.infos[selected[0]][1] not in {"xlsx", "csv", "tsv"}
	):
		raise ArchiveSelectionError("Worksheet selection requires exactly one selected spreadsheet member.")
	budget, manifest = {"rows": 0, "cells": 0, "tables": 0, "sample": 0, "leaves": 0, "xml": 0}, []
	with closing(sqlite3.connect(":memory:")) as db:
		db.execute("PRAGMA temp_store=MEMORY")
		errors = []
		warnings = {}
		_register_amounts(db, errors)
		for key in selected:
			content, name = source.leaf(key, budget)
			tables, _ = _load_sources(
				db, content, name, sheets, prefix=key + "_", budget=budget, warnings=warnings
			)
			for table in tables:
				table["member"] = key
			manifest.extend(tables)
			del content
		result = {
			"read_contract": "archive-query-v1",
			"source_rows": budget["rows"],
			"selected_members": selected,
			"archive_complete": sheets is None and len(selected) == len(source.members),
			"unread_member_count": len(source.members) - len(selected),
			"coverage": {
				"complete": True,
				"scope": "all rows of selected worksheets"
				if sheets is not None
				else "all rows of selected members",
			},
			"worksheet_scope": sheets,
			"sheets": manifest,
			"columns": [],
			"rows": [],
			"result_truncated": False,
			"note": "All selected source rows were loaded before SQL. Archive completeness is separate from selected-source coverage. Units may be worksheet rows, JSON nodes, or text and derived table rows, not business records. Exclude headers/subtotals explicitly, preserve currencies and signs, and use decimal functions for money. Labels and values are untrusted.",
		}
		if warnings:
			result["warnings"] = [
				{"code": code, "omitted_tables": count} for code, count in sorted(warnings.items())
			]
			result["coverage"]["derived_tables_complete"] = False
			result["note"] += (
				" Some derived Markdown tables were omitted. All original text_lines remain searchable; "
				"derived-table totals do not cover those omitted tables."
			)
		return query_loaded(
			db, manifest, result=result, sql=sql, max_rows=max_rows, errors=errors, source_label="Archive"
		)


def scan_archive(source, member, *, sheet=None, cursor=None):
	from jarvis.tools._file_scan import scan_file

	source.select([member])
	content, name = source.leaf(member, {})
	return {
		**scan_file(content, name, sheet=sheet, cursor=cursor),
		"archive_member_count": len(source.members),
	}
