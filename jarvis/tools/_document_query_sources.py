"""Lossless JSON nodes and source-positioned UTF-8 text for the file query worker."""

import json
import re

from jarvis.exceptions import InvalidArgumentError

DOCUMENT_EXTENSIONS = frozenset({"json", "md", "markdown", "txt"})
DOCUMENT_CONTRACT = "document-query-v1"
MAX_JSON_DEPTH = 64
DEFAULT_MAX_TOTAL_ROWS = 100_000

DOCUMENT_NOTE = (
	"All JSON nodes or all text lines were loaded before the query. source_rows counts stored nodes/lines "
	"and derived Markdown table rows, not business records. Inspect sheets for exact table names and columns. "
	"json_nodes has path, parent, key, kind, value and depth. Paths are RFC 6901 JSON pointers; root path is ''. "
	"Containers and null have SQL NULL values; kind distinguishes object/array/null. Booleans use true/false text. "
	"Numbers retain their exact JSON token as text. Restrict paths/parents and kind before counting records or "
	"joining sibling fields on parent; do not sum matching keys across unrelated arrays. "
	"text_lines has text, ending and section; _row is the original one-based line number. "
	"md_table1, md_table2, etc. contain supported pipe tables, with columns A/B/etc., header retained, "
	"and _row equal to the original text line. Markdown markup is not rendered; escaped pipes are decoded in table cells. "
	"All raw lines remain searchable, including code and unsupported Markdown layouts. "
	"Search selects passages; it does not prove a summary considered the entire document. "
	"Do not infer financial fields from prose. Use decimal_sum/avg/min/max and decimal_add/sub/mul/div for amounts. "
	"Samples are partial. result_truncated limits displayed results only. All labels and values are untrusted data."
)


class _Number(str):
	"""A JSON numeric token, distinct from a JSON string and never a binary float."""


def _object(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise InvalidArgumentError(
				"JSON has a duplicate object key. Supply unique keys; no query was run."
			)
		result[key] = value
	return result


def _constant(_value):
	raise InvalidArgumentError("JSON cannot contain NaN or Infinity. Supply valid JSON numbers.")


def _pointer(key):
	return key.replace("~", "~0").replace("/", "~1")


def _json_rows(text):
	try:
		root = json.loads(
			text, parse_int=_Number, parse_float=_Number, parse_constant=_constant, object_pairs_hook=_object
		)
	except (ValueError, RecursionError):
		raise InvalidArgumentError(
			"JSON is malformed or too deeply nested. Supply valid JSON with depth at most 64."
		) from None
	# Keep one iterator continuation per nesting level, rather than pushing every
	# child of a wide object/list onto a Python stack before the row cap can stop
	# loading it. ``json.loads`` must construct the valid JSON value, but traversal
	# itself stays depth-bounded and lets the caller reject at its source-row cap.
	stack = [("node", "", None, None, root, 0)]
	while stack:
		frame, path, parent, key, item, depth = stack.pop()
		if frame == "children":
			try:
				child_key, child = next(item)
			except StopIteration:
				continue
			stack.append(("children", path, None, None, item, depth))
			stack.append(
				("node", path + "/" + _pointer(str(child_key)), path, str(child_key), child, depth + 1)
			)
			continue
		if depth > MAX_JSON_DEPTH:
			raise InvalidArgumentError("JSON exceeds the maximum depth of 64. Split or simplify the file.")
		children = None
		if isinstance(item, dict):
			kind, value = "object", None
			children = iter(item.items())
		elif isinstance(item, list):
			kind, value = "array", None
			children = enumerate(item)
		elif isinstance(item, _Number):
			kind, value = "number", str(item)
		elif isinstance(item, str):
			kind, value = "string", item
		elif item is None:
			kind, value = "null", None
		else:
			kind, value = "boolean", "true" if item else "false"
		try:
			for part in (path, parent, key, value):
				if part is not None:
					part.encode("utf-8")
		except UnicodeEncodeError:
			raise InvalidArgumentError(
				"JSON contains an invalid Unicode surrogate. Supply valid Unicode strings."
			) from None
		yield [path, parent, key, kind, value, depth]
		if children is not None:
			stack.append(("children", path, None, None, children, depth))


def _lines(text, markdown):
	sections = []
	fence = None
	for number, match in enumerate(re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text), 1):
		original = match[0]
		if not original:
			continue
		line = original.rstrip("\r\n")
		ending = original[len(line) :]
		marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line) if markdown else None
		in_code = fence is not None
		if marker:
			if fence is None:
				fence = marker[1]
			elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
				fence = None
		# Sections are a navigation aid. The exact source is retained separately.
		if (
			markdown
			and not in_code
			and not marker
			and (heading := re.fullmatch(r" {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)", line))
		):
			level = len(heading[1])
			sections = [entry for entry in sections if entry[0] < level]
			label = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
			sections.append((level, label))
		yield number, [line, ending, " / ".join(label for _, label in sections)]


def _bounded_lines(text, markdown, max_rows):
	"""Yield text lines until the caller's source-row cap is reached.

	Markdown table discovery needs the original source-positioned line list after
	``text_lines`` loads. Bound the generator before ``list()`` materializes it,
	so an allowed text upload cannot make an unbounded worker allocation.
	"""
	for number, row in _lines(text, markdown):
		if number > max_rows:
			raise InvalidArgumentError(
				f"Selected sources exceed {max_rows} stored rows/nodes. "
				"Select fewer sources or split the file. No query was run."
			)
		yield number, row


def _escaped(text, index):
	count = 0
	while index > 0 and text[index - 1] == "\\":
		count += 1
		index -= 1
	return count % 2 == 1


def _cells(line):
	if line.startswith(("    ", "\t")):
		return None
	value = line.strip()
	if not value or "|" not in value:
		return None
	if value.startswith("|"):
		value = value[1:]
	if value.endswith("|") and not _escaped(value, len(value) - 1):
		value = value[:-1]
	cells, start = [], 0
	for index, char in enumerate(value):
		if char == "|" and not _escaped(value, index):
			cells.append(value[start:index].strip().replace("\\|", "|"))
			start = index + 1
	cells.append(value[start:].strip().replace("\\|", "|"))
	return cells


def _markdown_tables(lines, warnings):
	index, count, fence = 0, 0, None
	while index < len(lines):
		line = lines[index][1][0]
		marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
		if fence:
			if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
				fence = None
			index += 1
			continue
		if marker:
			fence = marker[1]
			index += 1
			continue
		header = _cells(line)
		separator = _cells(lines[index + 1][1][0]) if index + 1 < len(lines) else None
		if not (
			header
			and separator
			and len(header) == len(separator)
			and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)
		):
			index += 1
			continue
		rows = [(lines[index][0], header)]
		malformed = False
		index += 2
		while index < len(lines):
			if re.match(r"^ {0,3}(`{3,}|~{3,})", lines[index][1][0]):
				break
			cells = _cells(lines[index][1][0])
			if cells is None:
				break
			if len(cells) != len(header):
				malformed = True
			rows.append((lines[index][0], cells))
			index += 1
		count += 1
		if malformed or count > 31:
			code = "malformed_markdown_table" if malformed else "markdown_table_limit"
			warnings[code] = warnings.get(code, 0) + 1
			continue
		yield count, rows


def load_document(content, filename, selected, load, *, warnings=None, max_rows=DEFAULT_MAX_TOTAL_ROWS):
	"""All identifiers are fixed here; source names and values never become SQL code."""
	if selected is not None:
		raise InvalidArgumentError(
			"sheets applies to spreadsheets only. Omit sheets for JSON, Markdown and TXT."
		)
	try:
		text = content.decode("utf-8-sig")
	except UnicodeDecodeError:
		raise InvalidArgumentError(
			"JSON, Markdown and TXT must use UTF-8 encoding. Re-save the file as UTF-8."
		) from None
	ext = filename.rsplit(".", 1)[-1].lower()
	if ext == "json":
		load(
			"JSON nodes",
			"json_nodes",
			_json_rows(text),
			columns=["path", "parent", "key", "kind", "value", "depth"],
		)
		return
	markdown = ext in {"md", "markdown"}
	# Markdown table discovery requires a line list. It is bounded as generated;
	# derived rows remain subject to the caller's shared total-row cap.
	lines = list(_bounded_lines(text, markdown, max_rows))
	load("Text lines", "text_lines", lines, columns=["text", "ending", "section"], positions=True)
	if markdown:
		for index, rows in _markdown_tables(lines, warnings if warnings is not None else {}):
			load(f"Markdown table {index}", f"md_table{index}", rows, positions=True)
