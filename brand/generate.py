#!/usr/bin/env python3
"""Generate the Jarvis brand asset pack.

Single source of truth for the brand glyph outside the Vue app. Everything in
brand/svg and brand/png is emitted from the constants at the top of this file,
so a brand refresh is one edit here plus a re-run.

The glyph is a lines-only polygon, so no SVG rasteriser is needed. Pillow
reproduces it exactly, which is why this script has no dependencies beyond what
the bench virtualenv already ships.

    python brand/generate.py            write the pack
    python brand/generate.py --check    verify committed assets are current
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# --- brand definition: the single source of truth -------------------------

BRAND_1 = "#6e8bff"
BRAND_2 = "#8b5cf6"

# Mirrors the path in frontend/src/components/JarvisMark.vue. The drift test in
# jarvis/tests/test_brand_assets.py fails if the two ever disagree.
GLYPH_PATH = "M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
GLYPH_POINTS = [
	(12, 2.5),
	(14, 10),
	(21.5, 12),
	(14, 14),
	(12, 21.5),
	(10, 14),
	(2.5, 12),
	(10, 10),
]
VIEWBOX = 24

# Both ratios come from JarvisMark.vue's defaults: radius 14 / size 56, and the
# inner svg rendered at size * 0.55.
TILE_RADIUS_RATIO = 0.25
GLYPH_RATIO = 0.55

PNG_SIZES = [16, 32, 64, 128, 256, 512, 1024]
SUPERSAMPLE = 4

ROOT = Path(__file__).resolve().parent


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--check",
		action="store_true",
		help="verify committed assets match this script instead of writing them",
	)
	args = parser.parse_args()

	assets = build_assets()
	if args.check:
		return report_stale(assets)

	for relative_path, payload in assets.items():
		target = ROOT / relative_path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(payload)
	print(f"wrote {len(assets)} assets under {ROOT}")
	return 0


def build_assets() -> dict[str, bytes]:
	"""Every asset in the pack, keyed by path relative to brand/."""
	svg = SvgRenderer()
	png = PngRenderer()
	assets: dict[str, bytes] = {}

	for name, markup in svg.all_variants().items():
		assets[f"svg/{name}.svg"] = markup.encode()

	for name in png.builders():
		for size in PNG_SIZES:
			assets[f"png/{name}-{size}.png"] = png.variant(name, size)

	return assets


def stale_assets(assets: dict[str, bytes], root: Path = None) -> list[str]:
	"""Names of assets on disk that no longer match what we would emit.

	The single definition of staleness. generate.py --check, manifest_icons.py,
	and the brand drift test all call this so they cannot disagree about what
	"current" means.
	"""
	root = ROOT if root is None else root
	stale = []
	for relative_path, payload in assets.items():
		target = root / relative_path
		if not target.exists() or not _matches(target, payload):
			stale.append(relative_path)
	return sorted(stale)


def _matches(target: Path, payload: bytes) -> bool:
	"""Compare PNGs by decoded pixels and everything else by raw bytes.

	Pillow is not version-pinned, and its PNG encoder can emit different
	compressed bytes for an identical image across releases. Comparing encoded
	bytes would turn a routine Pillow bump into a red CI run that a contributor
	could only "fix" by regenerating from CI's environment. Pixels are what we
	actually care about, and they are stable across encoder versions.
	"""
	on_disk = target.read_bytes()
	if on_disk == payload:
		return True
	if target.suffix != ".png":
		return False

	from io import BytesIO

	with Image.open(BytesIO(on_disk)) as before, Image.open(BytesIO(payload)) as after:
		if before.size != after.size:
			return False
		return before.convert("RGBA").tobytes() == after.convert("RGBA").tobytes()


def report_stale(assets: dict[str, bytes]) -> int:
	"""Exit non-zero and name any committed asset that no longer matches."""
	stale = stale_assets(assets)
	if stale:
		print("stale or missing assets, re-run python brand/generate.py:")
		for relative_path in stale:
			print(f"  {relative_path}")
		return 1

	print(f"all {len(assets)} assets current")
	return 0


class SvgRenderer:
	"""Hand-editable masters. These are what a designer receives."""

	def all_variants(self) -> dict[str, str]:
		return {
			"jarvis-mark-tile": self.tile(),
			"jarvis-mark": self.transparent(),
			"jarvis-mark-mono-black": self.mono("#000000"),
			"jarvis-mark-mono-white": self.mono("#ffffff"),
		}

	def tile(self, size: int = 64) -> str:
		radius = size * TILE_RADIUS_RATIO
		glyph = size * GLYPH_RATIO
		offset = (size - glyph) / 2
		scale = glyph / VIEWBOX
		return self._document(
			size,
			f"{self._gradient_defs()}"
			f'<rect width="{_n(size)}" height="{_n(size)}" rx="{_n(radius)}" fill="url(#jarvis-brand)"/>'
			f'<path d="{GLYPH_PATH}" fill="#ffffff"'
			f' transform="translate({_n(offset)} {_n(offset)}) scale({_n(scale)})"/>',
		)

	def transparent(self) -> str:
		return self._document(
			VIEWBOX,
			f'{self._gradient_defs()}<path d="{GLYPH_PATH}" fill="url(#jarvis-brand)"/>',
		)

	def mono(self, colour: str) -> str:
		return self._document(VIEWBOX, f'<path d="{GLYPH_PATH}" fill="{colour}"/>')

	def _gradient_defs(self) -> str:
		# x1,y1 -> x2,y2 across the bounding box is the 135deg of --brand-grad.
		return (
			'<defs><linearGradient id="jarvis-brand" x1="0" y1="0" x2="1" y2="1">'
			f'<stop offset="0" stop-color="{BRAND_1}"/>'
			f'<stop offset="1" stop-color="{BRAND_2}"/>'
			"</linearGradient></defs>"
		)

	def _document(self, size: float, body: str) -> str:
		return (
			'<svg xmlns="http://www.w3.org/2000/svg" '
			f'viewBox="0 0 {_n(size)} {_n(size)}" width="{_n(size)}" height="{_n(size)}" '
			'role="img" aria-label="Jarvis">'
			f"{body}</svg>\n"
		)


class PngRenderer:
	"""Rasterises at SUPERSAMPLE times the target size, then downsamples."""

	def builders(self) -> dict:
		"""Variant name to the method that draws it. The only list of names."""
		return {
			"jarvis-mark-tile": self.tile,
			"jarvis-mark": self.transparent,
			"jarvis-mark-mono-black": lambda size: self.mono(size, (0, 0, 0)),
			"jarvis-mark-mono-white": lambda size: self.mono(size, (255, 255, 255)),
		}

	def variant(self, name: str, size: int) -> bytes:
		builders = self.builders()
		if name not in builders:
			raise ValueError(f"unknown variant {name}")
		return _encode(builders[name](size))

	def tile(self, size: int, bleed: bool = False) -> Image.Image:
		"""White glyph on the gradient square. bleed skips the rounded corners,
		which is what a maskable icon needs since the platform applies its own."""
		width = size * SUPERSAMPLE
		canvas = _gradient(width)

		if bleed:
			alpha = Image.new("L", (width, width), 255)
		else:
			alpha = Image.new("L", (width, width), 0)
			ImageDraw.Draw(alpha).rounded_rectangle(
				(0, 0, width - 1, width - 1), radius=width * TILE_RADIUS_RATIO, fill=255
			)
		canvas.putalpha(alpha)

		glyph = width * GLYPH_RATIO
		offset = (width - glyph) / 2
		mask = _glyph_mask(width, scale=glyph / VIEWBOX, offset=offset)
		canvas.paste(Image.new("RGBA", (width, width), (255, 255, 255, 255)), (0, 0), mask)
		return _downsample(canvas, size)

	def transparent(self, size: int) -> Image.Image:
		width = size * SUPERSAMPLE
		canvas = _gradient(width)
		canvas.putalpha(_glyph_mask(width, scale=width / VIEWBOX, offset=0))
		return _downsample(canvas, size)

	def mono(self, size: int, colour: tuple[int, int, int]) -> Image.Image:
		width = size * SUPERSAMPLE
		canvas = Image.new("RGBA", (width, width), (*colour, 0))
		canvas.putalpha(_glyph_mask(width, scale=width / VIEWBOX, offset=0))
		return _downsample(canvas, size)


def _gradient(width: int) -> Image.Image:
	"""135deg two-stop gradient, matching --brand-grad.

	Built small and upscaled: bilinear interpolation of a linear ramp is exact,
	and it avoids a per-pixel Python loop over millions of pixels.
	"""
	seed = 256
	ramp = Image.new("L", (seed, seed))
	ramp.putdata([round(255 * (x + y) / (2 * (seed - 1))) for y in range(seed) for x in range(seed)])
	ramp = ramp.resize((width, width), Image.BILINEAR)

	start = Image.new("RGB", (width, width), _rgb(BRAND_1))
	end = Image.new("RGB", (width, width), _rgb(BRAND_2))
	return Image.composite(end, start, ramp).convert("RGBA")


def _glyph_mask(width: int, scale: float, offset: float) -> Image.Image:
	mask = Image.new("L", (width, width), 0)
	points = [(x * scale + offset, y * scale + offset) for x, y in GLYPH_POINTS]
	ImageDraw.Draw(mask).polygon(points, fill=255)
	return mask


def _downsample(image: Image.Image, size: int) -> Image.Image:
	return image.resize((size, size), Image.LANCZOS)


def _encode(image: Image.Image) -> bytes:
	from io import BytesIO

	buffer = BytesIO()
	# optimize keeps the pack small; Pillow writes no timestamp, so output is
	# byte-stable across runs and --check stays meaningful.
	image.save(buffer, format="PNG", optimize=True)
	return buffer.getvalue()


def _rgb(value: str) -> tuple[int, int, int]:
	value = value.lstrip("#")
	return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _n(value: float) -> str:
	"""Trim trailing zeros so the SVG reads cleanly."""
	return f"{value:.6f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
	sys.exit(main())
