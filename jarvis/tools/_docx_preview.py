"""Bounded legacy preview and additive browser DTO. No cursor authority."""

from jarvis.tools._file_scan_process import scan_file

NOTES = {
	"list_numbering_partial": "Some list numbers could not be read.",
	"equations_omitted": "Equations are available in the original document.",
	"embedded_content_omitted": "Embedded content is available in the original document.",
	"symbols_omitted": "Some symbols are available only in the original document.",
	"nested_table_flattened": "Nested tables appear as text within their parent cells.",
	"no_extractable_text": "No readable text or tables were found. Use a PDF or text copy if needed.",
	"column_limit": "Some table columns are outside this preview.",
	"field_result_partial": "Some document fields could not be read.",
}


def preview_docx(content, filename, max_chars, max_rows):
	scan = scan_file(content, filename)
	blocks, text = [], []
	remaining, rows = max_chars, 0
	truncated = scan["next_cursor"] is not None or scan["stop_reason"] is not None
	for record in scan["records"]:
		if record.get("source_empty"):
			continue
		if record.get("encoding") == "json-row":
			blocks.append(dict(record))
			text.append("A table row is too long to preview. Download the original.")
			truncated = True
			continue
		if remaining <= 0 or ("values" in record and rows >= max_rows):
			truncated = True
			break
		copy = dict(record)
		if "values" in record:
			values = []
			for value in record["values"]:
				clipped = value[:remaining]
				values.append(clipped)
				remaining -= len(clipped)
				if len(clipped) < len(value):
					truncated = True
			copy["values"] = values
			rows += 1
			text.append("\t".join(values))
		else:
			key = "fragment" if "fragment" in record else "text"
			value = record.get(key, "")
			copy[key] = value[:remaining]
			remaining -= len(copy[key])
			text.append(copy[key])
			if key == "fragment" or len(copy[key]) < len(value):
				truncated = True
		blocks.append(copy)
	full_text = "\n".join(text)
	truncated = truncated or len(full_text) > max_chars
	issues = scan["issues"]
	return {
		"kind": "document",
		"format": "docx",
		"text": full_text[:max_chars],
		"blocks": blocks,
		"truncated": truncated,
		"source": scan["source"],
		"coverage": {"complete": not truncated and not issues, "issues": issues},
		"note": "DOCX text and tables only; layout, images, charts, diagram text and comments are not read. This is a bounded preview. Use file-cursor-v1 for sequential text windows.",
	}


def document_view(data):
	blocks, notes = [], []
	shown = 0
	for record in data["blocks"]:
		part = record.get("part", "body")
		if "values" in record:
			table = record.get("table")
			if (
				blocks
				and blocks[-1].get("table") == table
				and blocks[-1]["type"] == "table"
				and blocks[-1]["part"] == part
			):
				blocks[-1]["rows"].append(record["values"])
			else:
				blocks.append({"type": "table", "table": table, "rows": [record["values"]], "part": part})
		elif record.get("encoding") == "json-row":
			notes.append("A table row is too long to preview. Download the original.")
			continue
		else:
			blocks.append(
				{"type": "paragraph", "text": record.get("text", record.get("fragment", "")), "part": part}
			)
		shown += 1
	for issue in data["coverage"]["issues"]:
		notes.append(NOTES.get(issue["code"], "Some content could not be previewed."))
	if data["source"]["omitted"].get("graphics"):
		notes.append("Images, charts and diagram text are available in the original document.")
	return {
		"blocks": blocks,
		"truncated": data["truncated"],
		"shown": shown,
		"total": data["source"]["blocks_total"],
		"notes": list(dict.fromkeys(notes)),
	}
