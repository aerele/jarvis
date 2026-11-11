"""Slide text and speaker notes from a .pptx, standard library only (no frappe import).

Slide order comes from ppt/presentation.xml, not from file names. Every read is
bounded: member count, uncompressed bytes per part and in total, slide count and
output characters. XML with a DTD or entities is refused (entity-expansion bombs).
"""

import posixpath
import zipfile
from io import BytesIO
from xml.etree import ElementTree as ET
from xml.parsers import expat

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

MAX_MEMBERS = 5000
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SLIDES = 500
MAX_CHARS = 1_000_000


class PptxError(ValueError):
	"""Raised with a message that is safe to show to the user."""


class _Budget:
	def __init__(self):
		self.bytes_left = MAX_TOTAL_BYTES


def extract_pptx(content: bytes, max_chars: int = MAX_CHARS) -> dict:
	"""Return {"text", "slides": [{"index", "text", "notes"}], "slide_count", "truncated"}."""
	try:
		zf = zipfile.ZipFile(BytesIO(content))
	except (zipfile.BadZipFile, OSError, EOFError):
		raise PptxError(
			"This is not a valid .pptx file (it could not be opened as a PowerPoint package)."
		) from None
	with zf:
		if len(zf.namelist()) > MAX_MEMBERS:
			raise PptxError("This .pptx has too many parts to read.")
		budget = _Budget()
		paths = _slide_paths(zf, budget)
		total = len(paths)
		slides, chars, truncated = [], 0, total > MAX_SLIDES
		for index, path in enumerate(paths[:MAX_SLIDES], start=1):
			slide = _read_slide(zf, budget, index, path)
			slides.append(slide)
			chars += len(_render(slide)) + 2
			if chars > max_chars:
				truncated = True
				break
	text = "\n\n".join(_render(s) for s in slides)
	if len(text) > max_chars:
		text, truncated = text[:max_chars], True
	return {"text": text, "slides": slides, "slide_count": total, "truncated": truncated}


def _render(slide: dict) -> str:
	lines = [f"Slide {slide['index']}"]
	if slide["text"]:
		lines.append(slide["text"])
	if slide["notes"]:
		lines.append("Notes:\n" + slide["notes"])
	return "\n".join(lines)


def _part(zf, budget, path):
	try:
		info = zf.getinfo(path)
	except KeyError:
		return None
	if info.file_size > MAX_PART_BYTES or info.file_size > budget.bytes_left:
		raise PptxError("This .pptx is too large to read safely.")
	with zf.open(info) as fh:
		data = fh.read(MAX_PART_BYTES + 1)
	if len(data) > MAX_PART_BYTES or len(data) > budget.bytes_left:
		raise PptxError("This .pptx is too large to read safely.")
	budget.bytes_left -= len(data)
	return data


def _refuse(*_args):
	raise PptxError("This .pptx contains unsafe XML and was not read.")


def _qname(name):
	return "{" + name if "}" in name else name


def _xml(data, what):
	"""Parse with expat directly so DTDs and entities are refused whatever the encoding."""
	builder = ET.TreeBuilder()
	parser = expat.ParserCreate(namespace_separator="}")
	parser.buffer_text = True
	parser.StartDoctypeDeclHandler = _refuse
	parser.EntityDeclHandler = _refuse
	parser.UnparsedEntityDeclHandler = _refuse
	parser.ExternalEntityRefHandler = _refuse
	parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
	parser.StartElementHandler = lambda n, attrs: builder.start(
		_qname(n), {_qname(k): v for k, v in attrs.items()}
	)
	parser.EndElementHandler = lambda n: builder.end(_qname(n))
	parser.CharacterDataHandler = builder.data
	try:
		parser.Parse(data, True)
		return builder.close()
	except expat.ExpatError:
		raise PptxError(f"This .pptx is damaged ({what} could not be parsed).") from None


def _rels(zf, budget, part_path):
	folder, name = posixpath.split(part_path)
	data = _part(zf, budget, posixpath.join(folder, "_rels", name + ".rels"))
	if data is None:
		return {}
	out = {}
	for rel in _xml(data, "relationships").iter(f"{REL}Relationship"):
		if rel.get("TargetMode") == "External":
			continue
		target = rel.get("Target") or ""
		if target.startswith("/"):
			path = target.lstrip("/")
		else:
			path = posixpath.normpath(posixpath.join(folder, target))
		out[rel.get("Id")] = (rel.get("Type") or "", path)
	return out


def _slide_paths(zf, budget):
	data = _part(zf, budget, "ppt/presentation.xml")
	if data is None:
		raise PptxError("This is not a PowerPoint presentation (ppt/presentation.xml is missing).")
	rels = _rels(zf, budget, "ppt/presentation.xml")
	paths = []
	for sld in _xml(data, "the slide list").iter(f"{P}sldId"):
		rel = rels.get(sld.get(f"{R}id"))
		if rel and rel[1] in zf.NameToInfo:
			paths.append(rel[1])
	return paths


def _read_slide(zf, budget, index, path):
	root = _xml(_part(zf, budget, path) or b"<x/>", f"slide {index}")
	notes = ""
	for rel_type, target in _rels(zf, budget, path).values():
		if rel_type.endswith("/notesSlide"):
			data = _part(zf, budget, target)
			if data is not None:
				notes = "\n".join(_lines(_xml(data, f"notes of slide {index}"), notes_only=True))
			break
	return {"index": index, "text": "\n".join(_lines(root)), "notes": notes}


def _para(p):
	out = []
	for el in p.iter():
		if el.tag == f"{A}t" and el.text:
			out.append(el.text)
		elif el.tag == f"{A}br":
			out.append("\n")
	return "".join(out).strip()


def _lines(node, notes_only=False):
	"""Paragraph lines in document order; a table row becomes one tab-joined line."""
	if node.tag == f"{A}tr":
		cells = [
			" ".join(filter(None, (_para(p) for p in tc.iter(f"{A}p")))) for tc in node.findall(f"{A}tc")
		]
		line = "\t".join(cells)
		return [line] if line.strip() else []
	if node.tag == f"{A}p":
		text = _para(node)
		return [text] if text else []
	if notes_only and node.tag == f"{P}sp":
		ph = node.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
		if ph is None or ph.get("type") != "body":
			return []
	out = []
	for child in node:
		out.extend(_lines(child, notes_only))
	return out
