"""Guards the brand pack in brand/ against drifting from the shipping app.

frontend/src/lib/brand.js is the single source of truth for the brand glyph on
the JS side: JarvisMark.vue, JvSpinner.vue and the onboarding canvas all import
BRAND_STAR_PATH from it rather than carrying their own copy. brand/generate.py
holds a second copy of that glyph and of the two brand colours, because it
generates the raster brand pack and cannot import from JS, which is a real drift
risk. These tests turn a silent divergence into a loud CI failure naming both
files, without putting any build-time coupling into the shipping component.

The glyph used to live as a path literal inside JarvisMark.vue, and this file
asserted against that literal. It no longer does, so the assertion now reads
lib/brand.js, and a second test pins every consumer to the shared constant so
nobody can quietly reintroduce a hardcoded copy and pass.

No database access. Runs as a plain unit test:
  bench --site <site> run-tests --module jarvis.tests.test_brand_assets
"""

import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
BRAND_JS = FRONTEND_SRC / "lib" / "brand.js"
MAIN_CSS = FRONTEND_SRC / "main.css"
GENERATE_PY = REPO_ROOT / "brand" / "generate.py"

# Every surface that draws the glyph. Each must import the shared constant
# rather than inlining its own path literal.
GLYPH_CONSUMERS = (
	FRONTEND_SRC / "components" / "JarvisMark.vue",
	FRONTEND_SRC / "components" / "JvSpinner.vue",
	FRONTEND_SRC / "onboarding" / "SetupNeuralNet.vue",
)


def load_generate():
	spec = importlib.util.spec_from_file_location("jarvis_brand_generate", GENERATE_PY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestBrandGlyphDrift(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.brand = load_generate()

	def test_glyph_path_matches_shared_brand_module(self):
		"""The exported glyph is the same polygon the app renders."""
		match = re.search(r'BRAND_STAR_PATH\s*=\s*"([^"]+)"', BRAND_JS.read_text())
		self.assertIsNotNone(match, f"no BRAND_STAR_PATH export found in {BRAND_JS}")

		shared_path = _normalise(match.group(1))
		self.assertEqual(
			shared_path,
			_normalise(self.brand.GLYPH_PATH),
			"Brand glyph drift: BRAND_STAR_PATH in frontend/src/lib/brand.js no "
			"longer matches GLYPH_PATH in brand/generate.py. Update both, then "
			"re-run `python brand/generate.py`.",
		)

	def test_every_consumer_uses_the_shared_glyph(self):
		"""No surface may reintroduce its own copy of the path.

		The drift this whole file exists to catch is one surface being updated
		and another not. Asserting only that the shared constant is correct
		would miss a component that stopped importing it and hardcoded the old
		polygon back in, which would pass every other test here.
		"""
		for path in GLYPH_CONSUMERS:
			with self.subTest(consumer=path.name):
				source = path.read_text()
				self.assertIn(
					"BRAND_STAR_PATH",
					source,
					f"{path.name} no longer references BRAND_STAR_PATH. Every surface "
					f"that draws the glyph must import it from lib/brand.js.",
				)
				# A path literal is recognisable by its leading moveto. Catching it
				# here is what stops a well-meaning inline "fix" from silently
				# forking the glyph again.
				self.assertNotRegex(
					source,
					r'["\']M\s*12[\s,]+2\.5',
					f"{path.name} contains a hardcoded glyph path literal. Import "
					f"BRAND_STAR_PATH from lib/brand.js instead.",
				)

	def test_glyph_points_match_the_path(self):
		"""GLYPH_POINTS is what Pillow draws, so it must track GLYPH_PATH."""
		self.assertEqual(
			_points_from_path(self.brand.GLYPH_PATH),
			[tuple(float(n) for n in point) for point in self.brand.GLYPH_POINTS],
			"GLYPH_POINTS and GLYPH_PATH disagree inside brand/generate.py.",
		)

	def test_brand_colours_match_main_css(self):
		css = MAIN_CSS.read_text()
		for variable, expected in (("--brand-1", self.brand.BRAND_1), ("--brand-2", self.brand.BRAND_2)):
			match = re.search(rf"{variable}:\s*(#[0-9a-fA-F]{{3,8}})\s*;", css)
			self.assertIsNotNone(match, f"{variable} not found in {MAIN_CSS}")
			self.assertEqual(
				match.group(1).lower(),
				expected.lower(),
				f"Brand colour drift: {variable} in main.css no longer matches "
				f"brand/generate.py. Update both, then re-run `python brand/generate.py`.",
			)


class TestBrandAssetsCommitted(unittest.TestCase):
	"""The committed pack must be what the current definition produces."""

	def test_committed_assets_are_current(self):
		brand = load_generate()
		self.assertEqual(
			brand.stale_assets(brand.build_assets()),
			[],
			"Committed brand assets are stale. Re-run `python brand/generate.py`.",
		)


def _normalise(path: str) -> str:
	return " ".join(path.split()).upper()


def _points_from_path(path: str) -> list[tuple[float, float]]:
	"""Parse the lines-only path into its vertices."""
	numbers = re.findall(r"-?\d+(?:\.\d+)?", path)
	return [(float(numbers[i]), float(numbers[i + 1])) for i in range(0, len(numbers), 2)]


if __name__ == "__main__":
	unittest.main()
