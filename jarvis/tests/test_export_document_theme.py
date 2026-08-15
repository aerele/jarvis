"""Contract tests for the rich-PDF branded component stylesheet.

``jarvis.tools._export.document.theme`` is a pure module (no ``frappe``
import), so this suite runs WITHOUT a bench/site - plain ``pytest`` against it
directly, same as the sanitizer corpus.

The class names asserted here are the CONTRACT other Slice-1 tasks (Task 4's
number/RAG/chart builders, Task 6's ``export_document`` HTML assembly) build
their output against. A class disappearing or getting renamed here would
silently break those callers without this guard.
"""

import ast
import re
from pathlib import Path

import pytest

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


class TestComponentCss:
	def test_non_empty(self):
		css = theme.component_css()
		assert isinstance(css, str)
		assert len(css.strip()) > 0

	def test_theme_css_matches_component_css(self):
		"""THEME_CSS is the cached, deterministic output of component_css()."""
		assert theme.THEME_CSS == theme.component_css()

	def test_deterministic(self):
		assert theme.component_css() == theme.component_css()

	@pytest.mark.parametrize("class_name", _CONTRACT_CLASSES)
	def test_contract_class_present(self, class_name: str):
		css = theme.component_css()
		assert _has_class_token(css, class_name), f".{class_name} missing from component_css()"

	@pytest.mark.parametrize("tag", _TYPOGRAPHY_TAGS)
	def test_typography_tag_present(self, tag: str):
		css = theme.component_css()
		assert _has_tag_selector(css, tag), f"bare `{tag}` selector missing from component_css()"

	def test_print_color_adjust_present(self):
		"""wkhtmltopdf drops print background fills without this - required on
		anything with a background fill; theme.py applies it globally."""
		css = theme.component_css()
		assert "-webkit-print-color-adjust:exact" in css
		assert "print-color-adjust:exact" in css

	def test_bar_has_no_width(self):
		"""`.bar` styles color/height only - the tool sets `width` per bar via a
		tool-controlled inline style spliced in after sanitize_rich; if the
		stylesheet also set width, the two could fight."""
		css = theme.component_css()
		match = re.search(r"(?<![\w-])\.bar\s*\{([^}]*)\}", css)
		assert match, ".bar rule not found"
		assert "width" not in match.group(1)

	def test_neg_is_reused_by_kpi_delta(self):
		"""`.kpi-tile .kpi-delta.neg` reuses the single `.neg` contract class
		rather than inventing a second negative-color token."""
		css = theme.component_css()
		assert ".kpi-tile .kpi-delta.neg" in css

	# --- snapshot: a handful of key selectors, verbatim -----------------------

	@pytest.mark.parametrize(
		"selector",
		[
			"table.zebra tbody tr:nth-child(even) td",
			"tr.grouped td",
			".kpi-tile .kpi-value",
			".rag-red { background: #fbe9e7; color: #b3261e; }",
			".cover {",
			".signature-block .signature-line {",
		],
	)
	def test_snapshot_key_selector(self, selector: str):
		css = theme.component_css()
		assert selector in css, f"expected selector/rule {selector!r} not found verbatim"

	def test_indent_levels_step_consistently(self):
		""".indent-1..indent-5 step by a fixed padding-left amount, largest last."""
		css = theme.component_css()
		values = []
		for level in range(1, 6):
			match = re.search(rf"\.indent-{level}\s*{{\s*padding-left:\s*(\d+)pt", css)
			assert match, f".indent-{level} rule not found"
			values.append(int(match.group(1)))
		assert values == sorted(values)
		assert len(set(values)) == 5, "indent levels must all be distinct"

	def test_unknown_class_is_inert(self):
		"""A class the contract never names produces no selector - nothing in
		the stylesheet can accidentally style agent-chosen class names."""
		css = theme.component_css()
		assert not _has_class_token(css, "totally-made-up-class-xyz")


def test_theme_module_is_pure():
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
	assert "frappe" not in imported_names
