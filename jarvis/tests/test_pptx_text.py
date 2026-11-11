"""Pure tests for jarvis.tools._pptx_text (no frappe site needed)."""

import importlib.util
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
	"_pptx_text", Path(__file__).resolve().parents[1] / "tools" / "_pptx_text.py"
)
pptx_text = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pptx_text)

NS = (
	'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
	'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
	'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _paras(*texts):
	return "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)


def _slide(body):
	return f"<p:sld {NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"


def _shape(*texts):
	return f"<p:sp><p:txBody>{_paras(*texts)}</p:txBody></p:sp>"


def _rels(items):
	rows = "".join(f'<Relationship Id="{i}" Type="{RT}/{t}" Target="{g}"/>' for i, t, g in items)
	return f'<Relationships xmlns="{REL_NS}">{rows}</Relationships>'


def build(slides, order=None, extra=None):
	"""slides: {filename: (xml, notes_xml|None)}; order: file names in presentation order."""
	order = order or list(slides)
	buf = BytesIO()
	with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
		ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(order)))
		z.writestr(
			"ppt/presentation.xml", f"<p:presentation {NS}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>"
		)
		z.writestr(
			"ppt/_rels/presentation.xml.rels",
			_rels([(f"rId{i + 1}", "slide", f"slides/{n}") for i, n in enumerate(order)]),
		)
		for n, (xml, notes) in slides.items():
			z.writestr(f"ppt/slides/{n}", xml)
			if notes:
				z.writestr(f"ppt/notesSlides/notes_{n}", notes)
				z.writestr(
					f"ppt/slides/_rels/{n}.rels", _rels([("rId1", "notesSlide", f"../notesSlides/notes_{n}")])
				)
		for name, data in (extra or {}).items():
			z.writestr(name, data)
	return buf.getvalue()


def _notes(text):
	body = (
		'<p:sp><p:nvSpPr><p:cNvPr id="1" name="n"/><p:cNvSpPr/><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr></p:sp>'
		'<p:sp><p:nvSpPr><p:cNvPr id="2" name="b"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
		f"<p:txBody>{_paras(text)}</p:txBody></p:sp>"
		'<p:sp><p:nvSpPr><p:cNvPr id="3" name="s"/><p:cNvSpPr/><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
		f"<p:txBody>{_paras('7')}</p:txBody></p:sp>"
	)
	return f"<p:notes {NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:notes>"


TABLE = (
	"<p:graphicFrame><a:graphic><a:graphicData><a:tbl>"
	f"<a:tr><a:tc><a:txBody>{_paras('Name')}</a:txBody></a:tc><a:tc><a:txBody>{_paras('Qty')}</a:txBody></a:tc></a:tr>"
	f"<a:tr><a:tc><a:txBody>{_paras('Widget')}</a:txBody></a:tc><a:tc><a:txBody>{_paras('3')}</a:txBody></a:tc></a:tr>"
	"</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
)


class TestPptxText(unittest.TestCase):
	def test_two_slides_notes_table_unicode(self):
		data = build(
			{
				"slide1.xml": (
					_slide(_shape("Quarterly review", "Zażółć gęślą 日本語 ✓")),
					_notes("Say hello"),
				),
				"slide2.xml": (_slide(TABLE), None),
			}
		)
		out = pptx_text.extract_pptx(data)
		self.assertEqual(out["slide_count"], 2)
		self.assertFalse(out["truncated"])
		self.assertIn("Slide 1\nQuarterly review\nZażółć gęślą 日本語 ✓\nNotes:\nSay hello", out["text"])
		self.assertIn("Slide 2\nName\tQty\nWidget\t3", out["text"])
		self.assertNotIn("\n7", out["text"])  # slide-number placeholder is not a note

	def test_order_follows_presentation_xml(self):
		data = build(
			{
				"slide1.xml": (_slide(_shape("first file")), None),
				"slide2.xml": (_slide(_shape("second file")), None),
			},
			order=["slide2.xml", "slide1.xml"],
		)
		out = pptx_text.extract_pptx(data)
		self.assertEqual(out["text"].split("\n\n")[0], "Slide 1\nsecond file")

	def test_empty_slide(self):
		out = pptx_text.extract_pptx(build({"slide1.xml": (_slide(""), None)}))
		self.assertEqual(out["text"], "Slide 1")

	def test_corrupt_zip(self):
		with self.assertRaises(pptx_text.PptxError) as ctx:
			pptx_text.extract_pptx(b"not a zip at all")
		self.assertIn("not a valid .pptx", str(ctx.exception))

	def test_missing_presentation(self):
		buf = BytesIO()
		with zipfile.ZipFile(buf, "w") as z:
			z.writestr("x.txt", "hi")
		with self.assertRaises(pptx_text.PptxError):
			pptx_text.extract_pptx(buf.getvalue())

	def test_output_capped(self):
		data = build(
			{"slide1.xml": (_slide(_shape("x" * 5000)), None), "slide2.xml": (_slide(_shape("y")), None)}
		)
		out = pptx_text.extract_pptx(data, max_chars=100)
		self.assertTrue(out["truncated"])
		self.assertEqual(len(out["text"]), 100)

	def test_oversized_part_refused(self):
		data = build({"slide1.xml": (_slide(_shape("a")), None)}, extra={"ppt/slides/slide9.xml": "x"})
		old = pptx_text.MAX_PART_BYTES
		pptx_text.MAX_PART_BYTES = 10
		try:
			with self.assertRaises(pptx_text.PptxError):
				pptx_text.extract_pptx(data)
		finally:
			pptx_text.MAX_PART_BYTES = old

	def test_entities_refused(self):
		bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + _slide(_shape("&a;"))
		with self.assertRaises(pptx_text.PptxError):
			pptx_text.extract_pptx(build({"slide1.xml": (bomb, None)}))


if __name__ == "__main__":
	unittest.main()
