"""DOCX text/table blocks. Runs only inside the bounded file-reader child."""

from jarvis.tools._docx_numbering import Numbering
from jarvis.tools._docx_package import (
	DOCX_ERROR,
	MAX_PARTS,
	MC,
	DocxReadError,
	Package,
	attr,
	child,
	integer,
	is_word,
	local,
)

DOCX_REVISION = "docx-1"
MAX_TABLE_DEPTH = 8
MAX_COLUMNS = 128


class Reader:
	def __init__(self, package, summary, numbering=None, styles=None):
		self.package = package
		self.summary = summary
		self.numbering = Numbering(numbering, styles, self.issue)
		self.fields = []
		self.table_id = 0

	def issue(self, code):
		for issue in self.summary["issues"]:
			if issue["code"] == code:
				issue["count"] += 1
				return
		self.summary["issues"].append({"code": code, "count": 1})

	def children(self, element):
		for item in element:
			if item.tag != f"{{{MC}}}AlternateContent":
				yield item
				continue
			selected = next(
				(x for x in item if x.tag == f"{{{MC}}}Choice" and self.package.choices.get(id(x))), None
			)
			if selected is None:
				selected = next((x for x in item if x.tag == f"{{{MC}}}Fallback"), None)
			if selected is None:
				self.issue("embedded_content_omitted")
			else:
				yield from self.children(selected)

	def hidden(self, element):
		vanish = child(child(element, "rPr"), "vanish")
		return vanish is not None and attr(vanish, "val", "1").lower() not in {"0", "false", "off"}

	def inline(self, element, chunks, boxes):
		tag = local(element)
		word = is_word(element)
		omitted = self.summary["omitted"]
		if word:
			if tag in {"del", "moveFrom"}:
				omitted["tracked_changes"] = "accepted"
				return
			if tag in {"ins", "moveTo"}:
				omitted["tracked_changes"] = "accepted"
			if tag == "r" and self.hidden(element):
				omitted["hidden_runs"] += 1
				return
			if tag == "txbxContent":
				boxes.append(element)
				return
			if tag in {"drawing", "pict"}:
				omitted["graphics"] += 1
			if tag in {"altChunk", "object", "subDoc"}:
				self.issue("embedded_content_omitted")
				return
			if tag == "sym":
				self.issue("symbols_omitted")
				return
			if tag == "fldChar":
				kind = attr(element, "fldCharType")
				if kind == "begin":
					self.fields.append(False)
				elif kind == "separate" and self.fields:
					self.fields[-1] = True
				elif kind == "end" and self.fields:
					self.fields.pop()
				return
			visible = not self.fields or all(self.fields)
			if tag == "t":
				if visible:
					chunks.append(element.text or "")
				return
			if tag in {"tab", "ptab", "br", "cr", "noBreakHyphen", "softHyphen"}:
				if visible:
					chunks.append(
						{
							"tab": "\t",
							"ptab": "\t",
							"br": "\n",
							"cr": "\n",
							"noBreakHyphen": "\u2011",
							"softHyphen": "\u00ad",
						}[tag]
					)
				return
			if tag in {"instrText", "delText"}:
				return
		elif tag in {"oMath", "oMathPara"}:
			self.issue("equations_omitted")
			return
		for item in self.children(element):
			self.inline(item, chunks, boxes)

	def descendants(self, element, wanted):
		for item in self.children(element):
			if is_word(item) and local(item) in {"del", "moveFrom"}:
				self.summary["omitted"]["tracked_changes"] = "accepted"
				continue
			if is_word(item) and local(item) == wanted:
				yield item
			else:
				yield from self.descendants(item, wanted)

	def blocks(self, element, meta, depth=0):
		for item in self.children(element):
			tag = local(item)
			if is_word(item) and (tag in {"del", "moveFrom"} or tag.endswith("PrChange")):
				self.summary["omitted"]["tracked_changes"] = "accepted"
				continue
			if is_word(item) and tag == "p":
				chunks, boxes = [], []
				self.inline(item, chunks, boxes)
				text = self.numbering.prefix(item) + "".join(chunks)
				yield "p", text, meta
				for box in boxes:
					yield from self.blocks(box, {**meta, "textbox": True}, depth)
			elif is_word(item) and tag == "tbl":
				if depth >= MAX_TABLE_DEPTH:
					raise DocxReadError("DOCX tables are nested too deeply. Simplify the document.")
				self.table_id += 1
				table_meta = {**meta, "table": self.table_id}
				for row in self.descendants(item, "tr"):
					props = child(row, "trPr")
					if child(props, "del") is not None:
						self.summary["omitted"]["tracked_changes"] = "accepted"
						continue
					before, after = child(props, "gridBefore"), child(props, "gridAfter")
					values = [""] * integer(attr(before, "val"), maximum=MAX_COLUMNS)
					for cell in self.descendants(row, "tc"):
						cp = child(cell, "tcPr")
						span = integer(attr(child(cp, "gridSpan"), "val"), 1, MAX_COLUMNS)
						if not span:
							raise DocxReadError(DOCX_ERROR)
						merge = child(cp, "vMerge")
						hmerge = child(cp, "hMerge")
						continued = any(
							x is not None and attr(x, "val", "continue") == "continue"
							for x in (merge, hmerge)
						)
						if span > 1 or merge is not None or hmerge is not None:
							self.summary["omitted"]["merged_cells"] = True
						text = []
						if not continued:
							for kind, value, _ in self.blocks(cell, meta, depth + 1):
								if kind == "row":
									self.issue("nested_table_flattened")
									text.append("\t".join(value))
								else:
									text.append(value)
						values.extend(["\n".join(text)] + [""] * (span - 1))
					values.extend([""] * integer(attr(after, "val"), maximum=MAX_COLUMNS))
					if len(values) > MAX_COLUMNS:
						self.issue("column_limit")
					yield "row", values[:MAX_COLUMNS], table_meta
			elif is_word(item) and tag in {"altChunk", "object", "subDoc"}:
				self.issue("embedded_content_omitted")
			elif tag in {"oMath", "oMathPara"}:
				self.issue("equations_omitted")
			else:
				yield from self.blocks(item, meta, depth)

	def references(self, root):
		found = {"header": set(), "footer": set(), "footnotes": set(), "endnotes": set()}

		def visit(element):
			tag = local(element)
			if is_word(element):
				if (
					tag in {"del", "moveFrom"}
					or tag.endswith("PrChange")
					or (tag == "r" and self.hidden(element))
				):
					return
				kind = {
					"headerReference": "header",
					"footerReference": "footer",
					"footnoteReference": "footnotes",
					"endnoteReference": "endnotes",
				}.get(tag)
				if kind:
					found[kind].add(attr(element, "id"))
			for item in self.children(element):
				visit(item)

		visit(root)
		return found


def iter_docx(content, summary):
	"""Yield (p|row, text|cells, metadata) in deterministic text-region order."""
	summary.update(
		blocks_total=0,
		issues=[],
		omitted={
			"graphics": 0,
			"comments": False,
			"hidden_runs": 0,
			"tracked_changes": False,
			"merged_cells": False,
		},
	)
	package = None
	try:
		package = Package(content)
		main = package.main_part()
		root = package.xml(main)
		if not is_word(root) or local(root) != "document" or child(root, "body") is None:
			raise DocxReadError(DOCX_ERROR)
		rels = package.relationships(main)

		def part(kind):
			matches = [
				package.target(main, target)
				for k, target, external in rels.values()
				if k == kind and not external
			]
			if len(matches) > 1:
				raise DocxReadError(DOCX_ERROR)
			if not matches:
				return None
			value = package.xml(matches[0])
			if not is_word(value) or local(value) != kind:
				raise DocxReadError(DOCX_ERROR)
			return value

		reader = Reader(package, summary, part("numbering"), part("styles"))
		refs = reader.references(root)
		summary["omitted"]["comments"] = any(k == "comments" for k, _, _ in rels.values())
		regions = []
		for kind in ("header", "body", "footnotes", "endnotes", "footer"):
			if kind == "body":
				regions.append((kind, main, child(root, "body")))
				continue
			if kind in {"header", "footer"}:
				names = set()
				for identifier in refs[kind]:
					if identifier not in rels:
						raise DocxReadError(DOCX_ERROR)
					k, target, external = rels[identifier]
					if k != kind or external:
						raise DocxReadError(DOCX_ERROR)
					names.add(package.target(main, target))
				for name in sorted(names):
					if len(regions) >= MAX_PARTS:
						raise DocxReadError("DOCX contains too many text parts. Split the document.")
					node = package.xml(name)
					if not is_word(node) or local(node) != {"header": "hdr", "footer": "ftr"}[kind]:
						raise DocxReadError(DOCX_ERROR)
					regions.append((kind, name, node))
			elif refs[kind]:
				node = part(kind)
				if node is None:
					raise DocxReadError(DOCX_ERROR)
				names = [
					package.target(main, target)
					for k, target, external in rels.values()
					if k == kind and not external
				]
				regions.append((kind, names[0], node))
		if len(regions) > MAX_PARTS:
			raise DocxReadError("DOCX contains too many text parts. Split the document.")
		summary["parts_total"] = len(regions)
		readable = False
		for kind, name, node in regions:
			reader.numbering.reset()
			reader.fields = []
			if kind in {"footnotes", "endnotes"}:
				selected = [
					x
					for x in node
					if attr(x, "id") in refs[kind]
					and attr(x, "type") not in {"separator", "continuationSeparator", "continuationNotice"}
				]
				seen = [attr(x, "id") for x in selected]
				if len(seen) != len(set(seen)) or set(seen) != refs[kind]:
					raise DocxReadError(DOCX_ERROR)
			else:
				selected = [node]
			for section in selected:
				for record in reader.blocks(section, {"part": kind, "part_name": name}):
					block_kind, value, _ = record
					readable = readable or (
						any(x.strip() for x in value) if block_kind == "row" else bool(value.strip())
					)
					summary["blocks_total"] += 1
					yield record
			if reader.fields:
				reader.issue("field_result_partial")
		if not readable:
			reader.issue("no_extractable_text")
	except (DocxReadError, MemoryError):
		raise
	except Exception:
		raise DocxReadError(DOCX_ERROR) from None
	finally:
		if package is not None:
			package.close()
