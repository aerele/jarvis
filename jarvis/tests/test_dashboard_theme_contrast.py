"""WCAG contrast tests for the curated dashboard themes.

Pure + frappe-free (reads the canonical ``theme.json`` render tokens via
``theme_spec``), so it runs standalone with
``python -m unittest jarvis.tests.test_dashboard_theme_contrast`` and guards the
a11y floor the specs themselves claim (``a11y.text_contrast_min`` 4.5:1 text /
``ui_contrast_min`` 3:1). It asserts the ACTUAL fg/bg token pair of every text
recipe the injected stylesheet composes (``dashboardThemes.js`` BASE / the skill
cheatsheet), so a token value can never regress below the floor unnoticed.
"""

import unittest

from jarvis.dashboards import theme_spec

CURATED = ("jarvis", "insight", "claude", "graphite")

# Text color recipes the injected BASE stylesheet composes → (fg token, bg token).
# Every one renders real body/label/status TEXT, so each must clear text_contrast_min.
#   muted   → .jd-subtitle/.jd-kpi-label/.jd-card-footnote/.jd-empty/th (on bg,
#             surface and the surface-2 table-header fill)
#   positive/negative → .jd-kpi-delta.up/.down (on the surface card) + .jd-error
#             (standalone → checked on bg too)
#   warning/info → offered as semantic status tokens a model may color text with
_TEXT_RECIPES = (
	("ink", "bg"),
	("ink", "surface"),
	("heading", "bg"),
	("heading", "surface"),
	("muted", "bg"),
	("muted", "surface"),
	("muted", "surface-2"),
	("positive", "surface"),
	("positive", "bg"),
	("negative", "surface"),
	("negative", "bg"),
	("warning", "surface"),
	("warning", "bg"),
	("info", "surface"),
	("info", "bg"),
)
# UI/accent recipes → (fg token, bg token) at ui_contrast_min. The accent is a
# surface-level accent/fill (chips, borders, chart series), not page-bg text.
_UI_RECIPES = (("accent", "surface"),)


def _lin(c: float) -> float:
	c = c / 255.0
	return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(hexv: str) -> float:
	h = hexv.lstrip("#")
	r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
	return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(fg: str, bg: str) -> float:
	hi, lo = max(_lum(fg), _lum(bg)), min(_lum(fg), _lum(bg))
	return (hi + 0.05) / (lo + 0.05)


class TestDashboardThemeContrast(unittest.TestCase):
	def test_every_text_recipe_meets_aa(self):
		for key in CURATED:
			spec = theme_spec.load_theme(key)
			self.assertIsNotNone(spec, f"{key}/theme.json must load")
			tokens = spec["render_tokens"]
			text_min = spec["a11y"]["text_contrast_min"]
			ui_min = spec["a11y"]["ui_contrast_min"]
			for fg, bg, mn, kind in [(f, b, text_min, "text") for f, b in _TEXT_RECIPES] + [
				(f, b, ui_min, "ui") for f, b in _UI_RECIPES
			]:
				ratio = contrast_ratio(tokens[fg], tokens[bg])
				self.assertGreaterEqual(
					ratio,
					mn,
					f"{key}: --jd-{fg} ({tokens[fg]}) on --jd-{bg} ({tokens[bg]}) "
					f"= {ratio:.2f}:1, below the {kind} floor {mn}:1",
				)


if __name__ == "__main__":
	unittest.main()
