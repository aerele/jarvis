"""Contract tests for the rich-PDF branded component stylesheet.

``jarvis.tools._export.document.theme`` is a pure module (no ``frappe``
import), so this suite runs WITHOUT a bench/site - plain ``unittest`` against
it directly, same as the sanitizer corpus.

The class names asserted here are the CONTRACT other Slice-1 tasks (Task 4's
number/RAG/chart builders, Task 6's ``export_document`` HTML assembly) build
their output against. A class disappearing or getting renamed here would
silently break those callers without this guard.
"""

import ast
import re
import unittest
from pathlib import Path

from jarvis.tools._export.document import theme

# The full class-name contract from theme.py's module docstring. ".bar" is
# listed separately from ".bar-chart"/".bar-row"/etc - the regex helper below
# matches a class as a whole token, so ".bar" only matches the real `.bar {`
# selector, never as a prefix of ".bar-chart".
_CONTRACT_CLASSES = [
	"zebra",
	"grouped",
	"indent-1",
	"indent-2",
	"indent-3",
	"indent-4",
	"indent-5",
	"neg",
	"rag-red",
	"rag-amber",
	"rag-green",
	"num",
	"callout",
	"kpi-tile",
	"cover",
	"section-divider",
	"signature-block",
	"bar-chart",
	"bar",
]

# Bare typography tag selectors (no class needed - the sanitizer allows these
# tags directly with only a `class` attribute).
_TYPOGRAPHY_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _has_class_token(css: str, class_name: str) -> bool:
	"""True if ``.class_name`` appears in ``css`` as a whole CSS class token
	(not merely as a prefix of a longer class, e.g. ``.bar`` inside
	``.bar-chart``).

	No lookbehind on what precedes the ``.`` - a class selector is legitimately
	preceded by an element name (``table.zebra``), a combinator, a comma, or
	nothing at all, so only the character AFTER the class name distinguishes a
	real token from a longer one it happens to prefix."""
	pattern = r"\." + re.escape(class_name) + r"(?![\w-])"
	return re.search(pattern, css) is not None


def _has_tag_selector(css: str, tag: str) -> bool:
	"""True if ``tag`` appears in ``css`` as a bare tag selector token (not
	inside a longer identifier or a class name)."""
	pattern = r"(?<![\w.-])" + re.escape(tag) + r"(?![\w-])"
	return re.search(pattern, css) is not None


class TestComponentCss(unittest.TestCase):
	def test_non_empty(self):
		css = theme.component_css()
		self.assertTrue(isinstance(css, str))
		self.assertTrue(len(css.strip()) > 0)

	def test_theme_css_matches_component_css(self):
		"""THEME_CSS is the cached, deterministic output of component_css()."""
		self.assertEqual(theme.THEME_CSS, theme.component_css())

	def test_deterministic(self):
		self.assertEqual(theme.component_css(), theme.component_css())

	def test_contract_class_present(self):
		for class_name in _CONTRACT_CLASSES:
			with self.subTest(class_name=class_name):
				css = theme.component_css()
				self.assertTrue(
					_has_class_token(css, class_name), f".{class_name} missing from component_css()"
				)

	def test_typography_tag_present(self):
		for tag in _TYPOGRAPHY_TAGS:
			with self.subTest(tag=tag):
				css = theme.component_css()
				self.assertTrue(
					_has_tag_selector(css, tag), f"bare `{tag}` selector missing from component_css()"
				)

	def test_print_color_adjust_present(self):
		"""wkhtmltopdf drops print background fills without this - required on
		anything with a background fill; theme.py applies it globally."""
		css = theme.component_css()
		self.assertIn("-webkit-print-color-adjust:exact", css)
		self.assertIn("print-color-adjust:exact", css)

	def test_bar_has_no_width(self):
		"""`.bar` styles color/height only - the tool sets `width` per bar via a
		tool-controlled inline style spliced in after sanitize_rich; if the
		stylesheet also set width, the two could fight."""
		css = theme.component_css()
		match = re.search(r"(?<![\w-])\.bar\s*\{([^}]*)\}", css)
		self.assertTrue(match, ".bar rule not found")
		self.assertNotIn("width", match.group(1))

	def test_neg_is_reused_by_kpi_delta(self):
		"""`.kpi-tile .kpi-delta.neg` reuses the single `.neg` contract class
		rather than inventing a second negative-color token."""
		css = theme.component_css()
		self.assertIn(".kpi-tile .kpi-delta.neg", css)

	# --- snapshot: a handful of key selectors, verbatim -----------------------

	def test_snapshot_key_selector(self):
		selectors = [
			"table.zebra tbody tr:nth-child(even) td",
			"tr.grouped td",
			".kpi-tile .kpi-value",
			".rag-red { background: #fbe9e7; color: #b3261e; }",
			".cover {",
			".signature-block .signature-line {",
		]
		for selector in selectors:
			with self.subTest(selector=selector):
				css = theme.component_css()
				self.assertIn(selector, css, f"expected selector/rule {selector!r} not found verbatim")

	def test_indent_levels_step_consistently(self):
		""".indent-1..indent-5 step by a fixed padding-left amount, largest last."""
		css = theme.component_css()
		values = []
		for level in range(1, 6):
			match = re.search(rf"\.indent-{level}\s*{{\s*padding-left:\s*(\d+)pt", css)
			self.assertTrue(match, f".indent-{level} rule not found")
			values.append(int(match.group(1)))
		self.assertEqual(values, sorted(values))
		self.assertEqual(len(set(values)), 5, "indent levels must all be distinct")

	def test_unknown_class_is_inert(self):
		"""A class the contract never names produces no selector - nothing in
		the stylesheet can accidentally style agent-chosen class names."""
		css = theme.component_css()
		self.assertFalse(_has_class_token(css, "totally-made-up-class-xyz"))


def _rule_body(css: str, selector_regex: str) -> str:
	"""Return the ``{...}`` body of the first rule whose selector matches."""
	m = re.search(selector_regex + r"\s*\{([^}]*)\}", css)
	return m.group(1) if m else ""


# New class-name contract added by the template-polish pass.
_POLISH_CLASSES = [
	"doc-title-block",
	"doc-title",
	"doc-subtitle",
	"doc-meta",
	"doc-brand",
	"brand-name",
	"brand-addr",
	"page-break",
	"bar-chart-title",
	"bar-chart-caption",
	"series-1",
	"series-2",
	"series-3",
	"series-4",
]


class TestPolishContract(unittest.TestCase):
	"""Locks the presentation-ready polish: refined type scale + rhythm, the
	tool-built title block, the section-divider/page-break split, the computed
	cover height, and the chart/table alignment fixes."""

	def test_polish_classes_present(self):
		css = theme.component_css()
		for class_name in _POLISH_CLASSES:
			with self.subTest(class_name=class_name):
				self.assertTrue(
					_has_class_token(css, class_name), f".{class_name} missing from component_css()"
				)

	def test_heading_scale_refined_with_top_rhythm(self):
		"""h2 is 16pt (was an oversized 20pt) AND carries a nonzero TOP margin -
		the vertical-rhythm fix (the old `margin:0 0 10pt` gave sections no space
		above)."""
		css = theme.component_css()
		body = _rule_body(css, r"(?<![\w.-])h2")
		self.assertIn("font-size: 16pt", body)
		m = re.search(r"margin:\s*(\d+)pt", body)
		self.assertTrue(m and int(m.group(1)) > 0, "h2 must have a nonzero top margin")

	def test_num_right_aligns_the_header_too(self):
		"""`.num` right-aligns the <th>, not just the <td> - numeric header/number
		alignment fix."""
		self.assertIn("th.num", theme.component_css())

	def test_callout_surface_distinct_from_zebra(self):
		"""The callout card background differs from the zebra stripe, so a callout
		next to a striped table doesn't read as the same surface."""
		self.assertNotEqual(theme._CALLOUT_BG, theme._ZEBRA)
		css = theme.component_css()
		self.assertIn(theme._CALLOUT_BG, css)
		self.assertIn(theme._ZEBRA, css)

	def test_section_divider_no_longer_forces_a_page_break(self):
		"""`.section-divider` is now a rule only; forcing a break moves to
		`.page-break` (the old auto-break ejected a page on every use)."""
		css = theme.component_css()
		self.assertNotIn("page-break-before", _rule_body(css, r"\.section-divider"))
		self.assertIn("page-break-before", _rule_body(css, r"\.page-break"))

	def test_cover_height_is_page_geometry_aware(self):
		"""The full cover fills the printable page: its height differs by page size
		and orientation and margins (the old `height:100%` collapsed to nothing)."""
		a4 = _rule_body(theme.component_css("A4", "portrait", 15), r"\.cover")
		letter = _rule_body(theme.component_css("Letter", "portrait", 15), r"\.cover")
		a4_landscape = _rule_body(theme.component_css("A4", "landscape", 15), r"\.cover")
		wide_margin = _rule_body(theme.component_css("A4", "portrait", 40), r"\.cover")

		def _h(rule):
			m = re.search(r"height:\s*([\d.]+)pt", rule)
			self.assertTrue(m, "cover height not found")
			return float(m.group(1))

		self.assertNotEqual(_h(a4), _h(letter))
		self.assertNotEqual(_h(a4), _h(a4_landscape))
		self.assertLess(_h(wide_margin), _h(a4))  # bigger margins -> shorter cover
		self.assertGreater(_h(a4), 100)

	def test_component_css_default_unchanged_by_new_signature(self):
		"""The new geometry params default to A4/portrait/15mm; the no-arg call and
		THEME_CSS stay identical (zero regression for existing callers)."""
		self.assertEqual(theme.component_css(), theme.component_css("A4", "portrait", 15))
		self.assertEqual(theme.THEME_CSS, theme.component_css())

	def test_page_break_inside_avoid_on_grouped_components(self):
		css = theme.component_css()
		for sel in (r"\.kpi-tile", r"\.callout", r"\.signature-block"):
			with self.subTest(sel=sel):
				self.assertIn("page-break-inside: avoid", _rule_body(css, sel))

	def test_thead_repeats_across_pages(self):
		self.assertIn("display: table-header-group", _rule_body(theme.component_css(), r"thead"))

	def test_prose_measure_is_capped(self):
		"""Paragraphs cap their measure (line length) for readability."""
		self.assertIn("max-width", _rule_body(theme.component_css(), r"(?<![\w.-])p"))


class TestThemeModulePurity(unittest.TestCase):
	def test_theme_module_is_pure(self):
		"""theme.py must never import frappe - it has to be unit-testable without a
		bench, same purity guarantee sanitizer.py makes for the same reason."""
		path = Path(theme.__file__)
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		imported_names = set()
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				imported_names.update(alias.name.split(".")[0] for alias in node.names)
			elif isinstance(node, ast.ImportFrom) and node.module:
				imported_names.add(node.module.split(".")[0])
		self.assertNotIn("frappe", imported_names)
