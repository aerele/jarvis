"""Turn user-attached images/PDFs into base64 image parts for native LLM vision.

No Frappe imports - pure media handling so it unit-tests without a site. The
worker (jarvis/chat/worker.py) loads the File + checks the user's read
permission, then calls these helpers to build the provider-neutral image parts
the agent gateway accepts (flat ``{type:"image", mimeType, fileName,
content}`` shape; agent normalizes per provider). PDFs cannot be sent as a
native document block on the chat entrypoint, so we rasterize their pages to
images here.

Caps (all enforced before anything leaves the bench):
- per-image bytes: kept under agent's 2MB inline threshold (hard cap 6MB) so
  images stay inline rather than disk-offloaded;
- pixels: <= ~3.75MP (Anthropic's limit) - also the decompression-bomb guard,
  set once process-wide at import;
- PDF pages: capped so a huge doc can't explode token cost.
"""

from __future__ import annotations

import base64
import io

_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_PIXELS = 3_750_000  # render/resize TARGET for the model (Anthropic ~3.75MP)
_MAX_DIM = 1568  # longest side after resize (Anthropic's optimal); 1568^2 = 2.46MP
_BOMB_MAX_PIXELS = 64_000_000  # decompression-bomb ceiling: reject absurd decodes, allow real photos
_MAX_PDF_PAGES = 20
_RASTER_SCALE = 200 / 72  # ~200 DPI
_JPEG_QUALITIES = (85, 70, 55, 40)

_VISION_PROVIDERS = {"Anthropic", "OpenAI", "Google Gemini"}

# Raise PIL's decompression-bomb error past a generous ceiling once at import
# (process-global) so a crafted tiny-file-that-decodes-huge is rejected before a
# full decode, while normal large photos/scans still decode and get resized
# down. Set here (the module that actually decodes pixels), not per-call.
try:
	from PIL import Image as _PILImage

	_PILImage.MAX_IMAGE_PIXELS = _BOMB_MAX_PIXELS
except Exception:
	pass


def supports_vision(provider: str | None) -> bool:
	"""True for providers whose models are reliably multimodal. Conservative:
	unknown/local providers return False so the worker falls back to a note
	instead of sending pixels a text-only model would reject."""
	return (provider or "").strip() in _VISION_PROVIDERS


def image_part(content: bytes, file_name: str) -> dict | None:
	"""Decode an image, downscale/recompress under the caps, and return a vision
	part ``{mime, data_b64, file_name}`` - or None if it can't be decoded/encoded."""
	try:
		from PIL import Image

		im = Image.open(io.BytesIO(content))
		im.load()
	except Exception:
		return None
	try:
		return _encode_under_caps(im, file_name)
	finally:
		try:
			im.close()
		except Exception:
			pass


def pdf_parts(
	content: bytes, file_name: str, first_page: int = 1, max_pages: int | None = None
) -> tuple[list[dict], int]:
	"""Rasterize up to _MAX_PDF_PAGES pages to JPEG image parts. Returns
	``(parts, total_pages)`` so the caller can note truncation; each part
	carries its 1-based ``page`` number. ``first_page``/``max_pages`` select a
	window (defaults keep the from-the-start behaviour). Uses pypdfium2
	(Apache/BSD) - NOT PyMuPDF (AGPL)."""
	import pypdfium2 as pdfium

	parts: list[dict] = []
	pdf = pdfium.PdfDocument(content)
	try:
		total = len(pdf)
		limit = _MAX_PDF_PAGES if max_pages is None else max(1, min(max_pages, _MAX_PDF_PAGES))
		start = max(0, int(first_page or 1) - 1)
		for i in range(start, min(total, start + limit)):
			page = pdf[i]
			scale = _RASTER_SCALE
			try:
				w, h = page.get_size()  # points
				if w and h and (w * scale) * (h * scale) > _MAX_IMAGE_PIXELS:
					scale = (_MAX_IMAGE_PIXELS / (w * h)) ** 0.5  # clamp to pixel budget
			except Exception:
				pass
			try:
				pil = page.render(scale=scale).to_pil()
			except Exception:
				continue
			part = _encode_under_caps(pil, f"{file_name}-p{i + 1}.jpg")
			if part:
				part["page"] = i + 1
				parts.append(part)
		return parts, total
	finally:
		pdf.close()


def _encode_under_caps(im, file_name: str) -> dict | None:
	"""Convert to RGB, thumbnail under the pixel cap, JPEG-recompress stepping
	quality down until under the byte cap (one extra halving as a last resort)."""
	try:
		im = im.convert("RGB")
		im.thumbnail((_MAX_DIM, _MAX_DIM))
		for quality in _JPEG_QUALITIES:
			data = _jpeg(im, quality)
			if len(data) <= _MAX_IMAGE_BYTES:
				return _part(data, file_name)
		im.thumbnail((max(1, im.width // 2), max(1, im.height // 2)))
		data = _jpeg(im, _JPEG_QUALITIES[-1])
		if len(data) <= _MAX_IMAGE_BYTES:
			return _part(data, file_name)
	except Exception:
		return None
	return None


def _jpeg(im, quality: int) -> bytes:
	buf = io.BytesIO()
	im.save(buf, format="JPEG", quality=quality)
	return buf.getvalue()


def _part(data: bytes, file_name: str) -> dict:
	return {
		"mime": "image/jpeg",
		"data_b64": base64.b64encode(data).decode("ascii"),
		"file_name": file_name,
	}
