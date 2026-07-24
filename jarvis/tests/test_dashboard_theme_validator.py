"""Pure unit tests for jarvis.dashboards.theme_validator.

Frappe-free by design (the validator imports nothing from frappe), so this runs
standalone with ``python -m unittest jarvis.tests.test_dashboard_theme_validator``
from the app root as well as under bench. The save-path / grandfather behavior
that needs the DocType controller lives in test_dashboard_theme_standard.py.
"""

import unittest

from jarvis.dashboards.theme_validator import (
	RULE_COLOR_SCHEME,
	RULE_EXTERNAL_URL,
	RULE_FONT_FACE,
	RULE_FONT_FAMILY,
	RULE_IMPORTANT,
	RULE_INLINE_COLOR,
	RULE_OFF_PALETTE,
	RULE_PREFERS_SCHEME,
	validate_dashboard_html,
)


def codes(html, theme="Jarvis"):
	return sorted({v.rule for v in validate_dashboard_html(html, theme)})


class TestThemeValidatorConforming(unittest.TestCase):
	def test_token_based_dashboard_passes(self):
		html = (
			'<style>.jd-card{background:var(--jd-surface);color:var(--jd-ink);'
			"border:1px solid var(--jd-line)}</style>"
			'<div class="jd-card">x</div>'
		)
		self.assertEqual(validate_dashboard_html(html, "Jarvis"), [])

	def test_theme_own_hex_literal_allowed(self):
		# The ruling permits the theme's own hexes as literals (var() preferred).
		self.assertEqual(codes("<style>.x{color:#383838}</style>", "Jarvis"), [])

	def test_approved_neutrals_allowed(self):
		html = "<style>.x{color:white;background:transparent;box-shadow:0 1px 2px rgba(0,0,0,.1)}</style>"
		self.assertEqual(codes(html), [])

	def test_system_font_and_var_font_allowed(self):
		self.assertEqual(codes('<style>body{font-family:var(--jd-font)}</style>'), [])
		self.assertEqual(
			codes('<style>body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif}</style>'),
			[],
		)

	def test_claude_serif_allowed_only_on_claude(self):
		serif = '<style>h1{font-family:Georgia,"Times New Roman",serif}</style>'
		self.assertEqual(codes(serif, "Claude"), [])
		self.assertEqual(codes(serif, "Jarvis"), [RULE_FONT_FAMILY])

	def test_svg_xmlns_not_flagged_as_external(self):
		html = '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="var(--jd-accent)"/></svg>'
		self.assertEqual(codes(html), [])


class TestThemeValidatorRejects(unittest.TestCase):
	def test_off_palette_hex(self):
		vs = validate_dashboard_html("<style>.x{color:#0f172a}</style>", "Jarvis")
		self.assertEqual([v.rule for v in vs], [RULE_OFF_PALETTE])
		self.assertEqual(vs[0].offending, "#0f172a")

	def test_off_palette_rgb_and_named(self):
		self.assertEqual(codes("<style>.x{color:rgb(255,0,0)}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes("<style>.x{background:tomato}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes("<style>.x{color:gray}</style>"), [RULE_OFF_PALETTE])

	def test_font_family_override(self):
		vs = validate_dashboard_html("<style>body{font-family:Inter,sans-serif}</style>", "Jarvis")
		self.assertEqual([v.rule for v in vs], [RULE_FONT_FAMILY])
		self.assertEqual(vs[0].offending, "inter")

	def test_prefers_color_scheme(self):
		self.assertIn(
			RULE_PREFERS_SCHEME,
			codes("<style>@media (prefers-color-scheme: dark){body{color:var(--jd-ink)}}</style>"),
		)

	def test_color_scheme_property(self):
		self.assertEqual(codes("<style>:root{color-scheme:light dark}</style>"), [RULE_COLOR_SCHEME])

	def test_font_face_is_safety_ban(self):
		self.assertEqual(codes("<style>@font-face{font-family:X;src:url(data:font/woff2;base64,AA)}</style>"), [RULE_FONT_FACE])

	def test_external_http_and_protocol_relative(self):
		self.assertEqual(codes('<img src="http://evil.example/x.png">'), [RULE_EXTERNAL_URL])
		self.assertEqual(codes('<script src="//cdn.example/x.js"></script>'), [RULE_EXTERNAL_URL])

	def test_inline_style_off_palette_color(self):
		vs = validate_dashboard_html('<div style="color:#ff0000">x</div>', "Jarvis")
		self.assertEqual([v.rule for v in vs], [RULE_INLINE_COLOR])
		self.assertEqual(vs[0].offending, "#ff0000")

	def test_inline_token_and_layout_ok(self):
		self.assertEqual(codes('<div style="color:var(--jd-accent);width:50%">x</div>'), [])
		self.assertEqual(codes('<div style="width:50%;padding:8px">x</div>'), [])

	def test_important_on_color_only(self):
		self.assertEqual(codes("<style>.x{color:var(--jd-accent) !important}</style>"), [RULE_IMPORTANT])
		self.assertEqual(codes("<style>.x{width:50% !important}</style>"), [])

	def test_structured_violation_shape(self):
		v = validate_dashboard_html("<style>.x{color:#0f172a}</style>", "Jarvis")[0]
		d = v.as_dict()
		self.assertEqual(set(d), {"rule", "location", "offending", "message"})
		self.assertEqual(d["rule"], RULE_OFF_PALETTE)
		self.assertEqual(d["location"], "<style>")
		self.assertEqual(d["offending"], "#0f172a")


class TestThemeValidatorCustom(unittest.TestCase):
	def test_custom_relaxes_design_rules(self):
		html = '<style>.x{color:#0f172a;font-family:Inter}</style><div style="color:red">y</div>'
		self.assertEqual(validate_dashboard_html(html, "Custom"), [])

	def test_custom_keeps_safety_bans(self):
		html = '<style>@font-face{font-family:X}</style><img src="http://x.example/y.png">'
		self.assertEqual(codes(html, "Custom"), [RULE_EXTERNAL_URL, RULE_FONT_FACE])


if __name__ == "__main__":
	unittest.main()
