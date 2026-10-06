"""Unit tests for jarvis.chat.vision (pure media helpers; no DB/frappe)."""

import base64
import io
import unittest

from jarvis.chat import vision


def _png(w=12, h=12) -> bytes:
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", (w, h), (200, 50, 50)).save(buf, format="PNG")
	return buf.getvalue()


def _sideways_photo(w=200, h=100, orientation=6) -> bytes:
	"""A camera JPEG: pixels stored sideways, EXIF says how to turn them upright.
	A dark block marks the STORED top-left corner."""
	from PIL import Image

	im = Image.new("RGB", (w, h), (255, 255, 255))
	im.paste((0, 0, 0), (0, 0, w // 4, h // 4))
	exif = Image.Exif()
	exif[274] = orientation  # Orientation
	buf = io.BytesIO()
	im.save(buf, format="JPEG", exif=exif)
	return buf.getvalue()


def _pdf(pages: int) -> bytes:
	from pypdf import PdfWriter

	w = PdfWriter()
	for _ in range(pages):
		w.add_blank_page(width=200, height=200)
	buf = io.BytesIO()
	w.write(buf)
	return buf.getvalue()


class TestVision(unittest.TestCase):
	def test_supports_vision(self):
		self.assertTrue(vision.supports_vision("Anthropic"))
		self.assertTrue(vision.supports_vision("OpenAI"))
		self.assertTrue(vision.supports_vision("Google Gemini"))
		self.assertFalse(vision.supports_vision("Ollama"))
		self.assertFalse(vision.supports_vision(None))
		self.assertFalse(vision.supports_vision(""))

	def test_supports_vision_reads_the_catalog_slugs(self):
		# #654: a site connected through the admin's presets stores "anthropic";
		# vision was silently off there and every photo reached the model as
		# "couldn't be viewed".
		for provider in ("anthropic", " ANTHROPIC ", "openai", "gemini", "google", "google gemini"):
			with self.subTest(provider=provider):
				self.assertTrue(vision.supports_vision(provider))
		for provider in ("ollama", "mistral", "anthropic_cli", "openai-codex", "openai_compat"):
			with self.subTest(provider=provider):
				self.assertFalse(vision.supports_vision(provider))

	def test_image_part(self):
		part = vision.image_part(_png(), "x.png")
		self.assertIsNotNone(part)
		self.assertEqual(part["mime"], "image/jpeg")
		self.assertTrue(part["data_b64"])
		self.assertEqual(part["file_name"], "x.png")

	def test_image_part_turns_a_camera_photo_upright(self):
		# #654: a phone photo of an invoice reached the model sideways (Orientation 6).
		from PIL import Image

		part = vision.image_part(_sideways_photo(), "invoice.jpg")
		im = Image.open(io.BytesIO(base64.b64decode(part["data_b64"])))
		self.assertEqual(im.size, (100, 200))
		# Orientation 6 is viewed turned 90 degrees clockwise: the stored top-left
		# corner is shown at the top-right.
		self.assertLess(sum(im.getpixel((95, 5))), 120)
		self.assertGreater(sum(im.getpixel((5, 5))), 600)
		self.assertIsNone(im.getexif().get(274))  # never turned twice by a viewer

	def test_image_part_undecodable_returns_none(self):
		self.assertIsNone(vision.image_part(b"not really an image", "x.png"))

	def test_image_part_under_byte_cap(self):
		import base64

		part = vision.image_part(_png(4000, 4000), "big.png")
		self.assertIsNotNone(part)
		self.assertLessEqual(len(base64.b64decode(part["data_b64"])), vision._MAX_IMAGE_BYTES)

	def test_pdf_parts(self):
		parts, total = vision.pdf_parts(_pdf(3), "doc.pdf")
		self.assertEqual(total, 3)
		self.assertEqual(len(parts), 3)
		self.assertTrue(all(p["mime"] == "image/jpeg" for p in parts))
		self.assertEqual(parts[0]["file_name"], "doc.pdf-p1.jpg")

	def test_pdf_parts_page_capped(self):
		parts, total = vision.pdf_parts(_pdf(vision._MAX_PDF_PAGES + 5), "big.pdf")
		self.assertEqual(total, vision._MAX_PDF_PAGES + 5)
		self.assertEqual(len(parts), vision._MAX_PDF_PAGES)


if __name__ == "__main__":
	unittest.main()
