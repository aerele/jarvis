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
	RULE_MALFORMED,
	RULE_OFF_PALETTE,
	RULE_PREFERS_SCHEME,
	RULE_TOKEN_REDEFINE,
	validate_dashboard_html,
)


def codes(html, theme="Jarvis"):
	return sorted({v.rule for v in validate_dashboard_html(html, theme)})


class TestThemeValidatorConforming(unittest.TestCase):
	def test_token_based_dashboard_passes(self):
		html = (
			"<style>.jd-card{background:var(--jd-surface);color:var(--jd-ink);"
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
		self.assertEqual(codes("<style>body{font-family:var(--jd-font)}</style>"), [])
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
		self.assertEqual(
			codes("<style>@font-face{font-family:X;src:url(data:font/woff2;base64,AA)}</style>"),
			[RULE_FONT_FACE],
		)

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

	def test_important_banned_entirely(self):
		# hardening: !important escapes the theme layer on ANY property, so it is
		# banned outright — not just on color/font.
		self.assertEqual(codes("<style>.x{color:var(--jd-accent) !important}</style>"), [RULE_IMPORTANT])
		self.assertEqual(codes("<style>.x{width:50% !important}</style>"), [RULE_IMPORTANT])
		self.assertEqual(codes("<style>.x{padding:80px !important}</style>"), [RULE_IMPORTANT])

	# ---- F1: modern color functions in any syntax ------------------------- #
	def test_modern_color_functions_rejected(self):
		for css in (
			"<style>.k{color:oklch(0.6 0.25 25)}</style>",
			"<style>.k{color:oklab(0.5 0.1 0.1)}</style>",
			"<style>.k{color:lab(50% 80 60)}</style>",
			"<style>.k{color:lch(50% 80 60)}</style>",
			"<style>.k{background:hwb(0 0% 0%)}</style>",
			"<style>.k{color:color(display-p3 1 0 0)}</style>",
			"<style>.k{color:color-mix(in srgb,red,blue)}</style>",
		):
			self.assertIn(RULE_OFF_PALETTE, codes(css), css)

	def test_inline_modern_color_function_rejected(self):
		self.assertEqual(codes('<div style="color:oklch(0.7 0.2 145)">x</div>'), [RULE_INLINE_COLOR])

	# ---- F2: an unclosed <style> is scanned to EOF ------------------------ #
	def test_unclosed_style_is_scanned(self):
		# off-palette color + hardcoded font inside a <style> with no </style>.
		vs = codes('<body><style>.k{color:#0f172a;font-family:"Comic Sans MS"}</body></html>')
		self.assertIn(RULE_OFF_PALETTE, vs)
		self.assertIn(RULE_FONT_FAMILY, vs)

	def test_unclosed_style_conforming_still_passes(self):
		self.assertEqual(codes("<body><style>.k{color:var(--jd-ink)}"), [])

	# ---- hardening: --jd-* redefinition + !important remap ---------------- #
	def test_token_redefinition_rejected(self):
		self.assertIn(RULE_TOKEN_REDEFINE, codes("<style>:root{--jd-ink:#ffffff}</style>"))
		# the F4 remap (approved-neutral value + !important) is caught by BOTH the
		# token-redefinition ban and the blanket !important ban.
		vs = codes("<style>:root{--jd-ink:#ffffff !important}</style>")
		self.assertIn(RULE_TOKEN_REDEFINE, vs)
		self.assertIn(RULE_IMPORTANT, vs)

	def test_non_theme_custom_property_allowed(self):
		# an author's OWN non-jd custom property is fine.
		self.assertEqual(codes("<style>.x{--gap:16px;padding:var(--gap)}</style>"), [])

	# ---- F5: named colors through extra color properties ------------------ #
	def test_extra_color_props_named_color_rejected(self):
		self.assertEqual(codes("<style>.k{-webkit-text-fill-color:red}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes("<style>.k{border-inline-color:crimson}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes("<style>.k{scrollbar-color:red blue}</style>"), [RULE_OFF_PALETTE])

	# ---- F6: font-family via var() indirection ---------------------------- #
	def test_font_var_indirection_rejected(self):
		self.assertEqual(
			codes('<style>:root{--f:"Comic Sans MS"} .k{font-family:var(--f)}</style>'),
			[RULE_FONT_FAMILY],
		)

	def test_theme_font_var_allowed(self):
		self.assertEqual(codes("<style>body{font-family:var(--jd-font-display)}</style>"), [])

	# ---- F8: protocol-relative @import ------------------------------------ #
	def test_protocol_relative_import_rejected(self):
		self.assertEqual(codes('<style>@import "//evil.example/x.css";</style>'), [RULE_EXTERNAL_URL])

	def test_structured_violation_shape(self):
		v = validate_dashboard_html("<style>.x{color:#0f172a}</style>", "Jarvis")[0]
		d = v.as_dict()
		self.assertEqual(set(d), {"rule", "location", "offending", "message"})
		self.assertEqual(d["rule"], RULE_OFF_PALETTE)
		self.assertEqual(d["location"], "<style>")
		self.assertEqual(d["offending"], "#0f172a")


class TestThemeValidatorCustom(unittest.TestCase):
	def test_custom_relaxes_design_rules(self):
		# off-palette hex + hardcoded font + inline color + a modern color function
		# + !important are ALL fine under the bespoke Custom theme.
		html = (
			"<style>.x{color:oklch(0.6 0.25 25);font-family:Inter;padding:8px !important}"
			':root{--jd-ink:#fff}</style><div style="color:red">y</div>'
		)
		self.assertEqual(validate_dashboard_html(html, "Custom"), [])

	def test_custom_keeps_safety_bans(self):
		html = '<style>@font-face{font-family:X}</style><img src="http://x.example/y.png">'
		self.assertEqual(codes(html, "Custom"), [RULE_EXTERNAL_URL, RULE_FONT_FACE])

	def test_custom_still_rejects_protocol_relative_import(self):
		self.assertEqual(
			codes('<style>@import "//evil.example/x.css";</style>', "Custom"), [RULE_EXTERNAL_URL]
		)


class TestThemeValidatorCodexRound1(unittest.TestCase):
	"""Regressions for the parse-based rewrite: each Codex round-1 bypass now
	behaves because the validator tokenizes/decodes CSS + HTML the way the browser
	does, instead of matching raw spelling with regex."""

	# ---- #1 var() color laundering ---------------------------------------- #
	def test_non_jd_var_in_color_is_off_palette(self):
		self.assertEqual(codes("<style>:root{--rogue:red}.x{color:var(--rogue)}</style>"), [RULE_OFF_PALETTE])

	def test_non_jd_var_resolving_on_palette_still_off(self):
		# var() in a color value is allowed ONLY to a --jd-* token, whatever it resolves to.
		self.assertEqual(codes("<style>:root{--x:#383838}.k{color:var(--x)}</style>"), [RULE_OFF_PALETTE])

	def test_inline_non_jd_var_off_palette(self):
		self.assertEqual(codes('<div style="color:var(--rogue)">x</div>'), [RULE_INLINE_COLOR])

	def test_jd_var_in_color_allowed(self):
		self.assertEqual(codes("<style>.k{color:var(--jd-accent)}</style>"), [])
		self.assertEqual(codes('<div style="color:var(--jd-ink)">x</div>'), [])

	# ---- #2 CSS escapes decode -------------------------------------------- #
	def test_escaped_modern_color_function_rejected(self):
		# `o\6b lch(` decodes to oklch(
		self.assertIn(RULE_OFF_PALETTE, codes("<style>.k{color:o\\6b lch(0.6 0.25 25)}</style>"))

	def test_escaped_font_face_is_safety_ban(self):
		# `@f\6f nt-face` decodes to @font-face — caught even under Custom.
		self.assertIn(
			RULE_FONT_FACE,
			codes("<style>@f\\6f nt-face{src:url(data:font/woff2;base64,AA)}</style>", "Custom"),
		)

	# ---- #3 inline styles: full check set, incl. unquoted + entity -------- #
	def test_inline_token_redefine_and_important(self):
		vs = codes('<div style="--jd-ink:red;color:var(--jd-ink)!important">x</div>')
		self.assertIn(RULE_TOKEN_REDEFINE, vs)
		self.assertIn(RULE_IMPORTANT, vs)

	def test_unquoted_inline_style_color_scanned(self):
		self.assertEqual(codes("<div style=color:red>x</div>"), [RULE_INLINE_COLOR])

	def test_entity_encoded_inline_color_scanned(self):
		# `&#114;ed` decodes to `red` inside the style attribute (the browser decodes it).
		self.assertEqual(codes('<div style="color:&#114;ed">x</div>'), [RULE_INLINE_COLOR])

	# ---- #4 malformed / brace-unbalanced author CSS ----------------------- #
	def test_stray_leading_brace_rejected(self):
		# a `}` closes @layer author{} early → the rule would render UNLAYERED.
		self.assertIn(RULE_MALFORMED, codes("<style>}.jd-card{color:var(--jd-negative)}</style>"))

	def test_excess_trailing_brace_rejected(self):
		self.assertIn(RULE_MALFORMED, codes("<style>.jd-card{color:var(--jd-ink)}}</style>"))

	def test_wellformed_unclosed_style_is_not_malformed(self):
		# an unclosed `{` stays contained inside the wrapper — not a brace escape.
		self.assertNotIn(RULE_MALFORMED, codes("<body><style>.k{color:var(--jd-ink)}"))

	def test_malformed_not_enforced_under_custom(self):
		# Custom author CSS is never @layer-wrapped, so a stray } cannot escape a
		# layer; the structural ban does not apply (design relaxed).
		self.assertNotIn(RULE_MALFORMED, codes("<style>}.x{color:red}</style>", "Custom"))

	# ---- #5 entity-encoded external URL + narrowed W3C exemption ---------- #
	def test_entity_encoded_external_url_rejected(self):
		# `https&#58;//` decodes to `https://` — caught even under Custom.
		self.assertEqual(codes('<img src="https&#58;//evil/x.png">', "Custom"), [RULE_EXTERNAL_URL])

	def test_w3org_non_svg_namespace_rejected(self):
		# only the EXACT SVG/xlink namespace VALUES are exempt, not the w3.org host.
		self.assertEqual(codes('<img src="https://www.w3.org/foo.png">'), [RULE_EXTERNAL_URL])

	def test_svg_and_xlink_namespaces_allowed(self):
		html = (
			'<svg xmlns="http://www.w3.org/2000/svg" '
			'xmlns:xlink="http://www.w3.org/1999/xlink"><rect fill="var(--jd-accent)"/></svg>'
		)
		self.assertEqual(codes(html), [])

	# ---- #6 hash-like selector / string is NOT a color -------------------- #
	def test_id_selector_hash_not_read_as_color(self):
		self.assertEqual(codes("<style>#abc{color:var(--jd-ink)}</style>"), [])

	def test_hash_in_string_content_not_read_as_color(self):
		self.assertEqual(codes('<style>.x::before{content:"#0f172a"}</style>'), [])

	def test_off_palette_hash_in_value_still_caught(self):
		# the VALUE position is still inspected (only selectors/strings are exempt).
		self.assertEqual(codes("<style>.x{color:#abcdef}</style>"), [RULE_OFF_PALETTE])

	# ---- native CSS nesting: nested rule bodies are still scanned --------- #
	def test_nested_off_palette_rejected(self):
		self.assertEqual(
			codes("<style>.a{color:var(--jd-ink);.b{color:#ff0000}}</style>"), [RULE_OFF_PALETTE]
		)

	def test_nested_tokens_and_id_selector_pass(self):
		# a nested id selector (#card) is not read as a color; nested tokens pass.
		self.assertEqual(
			codes("<style>#card{color:var(--jd-ink);&:hover{color:var(--jd-accent)}}</style>"), []
		)


class TestThemeValidatorCodexRound2(unittest.TestCase):
	"""Round-2 canonicalization regressions: token EXISTENCE + var() fallbacks,
	CSS system colors, and browser URL preprocessing (control chars / special-
	scheme backslashes) — the surfaces beyond raw tokenization."""

	# ---- DR2-1 var() token-existence + fallback validation ---------------- #
	def test_nonexistent_jd_token_with_fallback_rejected(self):
		# `--jd-nonexistent` is NOT a render token; the browser renders the #ff0000
		# fallback. Caught block-level AND inline.
		self.assertEqual(codes("<style>.x{color:var(--jd-nonexistent,#ff0000)}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(
			codes('<div style="color:var(--jd-nonexistent,#ff0000)">x</div>'), [RULE_INLINE_COLOR]
		)

	def test_existing_jd_token_no_fallback_passes(self):
		self.assertEqual(codes("<style>.x{color:var(--jd-ink)}</style>"), [])

	def test_existing_jd_token_bad_fallback_rejected(self):
		# the token exists, but a bad fallback (`red`) would still render.
		self.assertEqual(codes("<style>.x{color:var(--jd-ink, red)}</style>"), [RULE_OFF_PALETTE])
		# a system-color fallback is caught too.
		self.assertEqual(codes("<style>.x{color:var(--jd-ink, Highlight)}</style>"), [RULE_OFF_PALETTE])

	def test_existing_jd_token_good_fallback_passes(self):
		# an approved-hex, benign-keyword, or nested-token fallback is fine.
		self.assertEqual(codes("<style>.x{color:var(--jd-ink, #383838)}</style>"), [])
		self.assertEqual(codes("<style>.x{color:var(--jd-line, currentColor)}</style>"), [])
		self.assertEqual(codes("<style>.x{color:var(--jd-accent, var(--jd-ink))}</style>"), [])

	def test_font_var_token_existence_and_fallback(self):
		# non-existent font token → rejected.
		self.assertEqual(codes("<style>body{font-family:var(--jd-nonexistent)}</style>"), [RULE_FONT_FAMILY])
		# a non-font --jd token in a font-family is not a font token → rejected.
		self.assertEqual(codes("<style>body{font-family:var(--jd-ink)}</style>"), [RULE_FONT_FAMILY])
		# existing font token, bad fallback face → rejected.
		self.assertEqual(
			codes('<style>body{font-family:var(--jd-font, "Comic Sans MS")}</style>'), [RULE_FONT_FAMILY]
		)
		# existing font token, generic / nested-token fallback → passes.
		self.assertEqual(codes("<style>body{font-family:var(--jd-font, sans-serif)}</style>"), [])
		self.assertEqual(codes("<style>body{font-family:var(--jd-font, var(--jd-font-display))}</style>"), [])

	# ---- DR2-3 CSS system colors ------------------------------------------ #
	def test_system_colors_rejected(self):
		for css in (
			"<style>.k{color:Highlight}</style>",
			"<style>.k{color:CanvasText}</style>",
			"<style>.k{background:AccentColor}</style>",
			"<style>.k{color:ButtonText}</style>",
			"<style>.k{color:WindowText}</style>",  # deprecated set
		):
			self.assertEqual(codes(css), [RULE_OFF_PALETTE], css)

	def test_benign_keywords_still_pass(self):
		self.assertEqual(codes("<style>.k{color:currentColor;background:transparent}</style>"), [])
		self.assertEqual(codes("<style>.k{color:inherit}</style>"), [])
		# a non-color ident in a color-bearing shorthand is not flagged as a color.
		self.assertEqual(codes("<style>.k{border:1px solid var(--jd-line)}</style>"), [])

	# ---- DR2-4 URL control-char / backslash canonicalization -------------- #
	def test_control_char_split_url_rejected_under_custom(self):
		# src attribute (entity-decoded): https:/<TAB>/evil → https://evil
		self.assertEqual(codes('<img src="https:/&#x09;/evil.example/x.png">', "Custom"), [RULE_EXTERNAL_URL])
		# url() in an inline style (attribute is entity-decoded)
		self.assertEqual(
			codes("<div style=\"background:url('https:/&#x09;/evil.example/x.png')\">x</div>", "Custom"),
			[RULE_EXTERNAL_URL],
		)
		# url() in a <style> block via a CSS escape (\9 = TAB; raw text isn't
		# entity-decoded, but tinycss2 decodes the CSS escape).
		self.assertEqual(
			codes('<style>.x{background:url("https:/\\9 /evil.example/x.png")}</style>', "Custom"),
			[RULE_EXTERNAL_URL],
		)
		# @import via a CSS escape.
		self.assertEqual(
			codes('<style>@import "https:/\\9 /evil.example/x.css";</style>', "Custom"), [RULE_EXTERNAL_URL]
		)

	def test_special_scheme_backslash_rejected_under_custom(self):
		# browsers fold `\` to `/` for special schemes: https:\\evil → https://evil
		self.assertEqual(codes('<img src="https:\\\\evil.example/x.png">', "Custom"), [RULE_EXTERNAL_URL])

	def test_canonicalization_keeps_xml_namespace_exempt(self):
		# the exemption is applied to the canonical value — svg xmlns still passes.
		self.assertEqual(
			codes('<svg xmlns="http://www.w3.org/2000/svg"><rect fill="var(--jd-accent)"/></svg>', "Custom"),
			[],
		)


class TestThemeValidatorCodexRound3(unittest.TestCase):
	"""Round-3 regressions: scheme-first URL rejection (any separator count),
	generic off-palette color LITERALS in properties the enumerated list forgot,
	and CASE-SENSITIVE custom-property existence."""

	# ---- DR3-2 single/zero-separator special-scheme URLs ------------------ #
	def test_zero_and_one_separator_http_url_rejected(self):
		# Browsers resolve `http:evil` / `http:/evil` / `http:\evil` (and https) to
		# an external authority — the ban must not require `//`. Checked under Custom
		# (the safety bans still apply there) across src, url() and @import.
		for html in (
			'<img src="http:/evil.example/x.png">',
			'<img src="http:evil.example/x.png">',
			'<img src="http:\\evil.example/x.png">',
			'<img src="https:/evil.example/x.png">',
			'<style>.x{background:url("http:/evil.example/x.png")}</style>',
			'<style>.x{background:url("http:evil.example/x.png")}</style>',
			'<style>@import "http:/evil.example/x.css";</style>',
			'<style>@import "https:evil.example/x.css";</style>',
		):
			self.assertEqual(codes(html, "Custom"), [RULE_EXTERNAL_URL], html)

	def test_scheme_first_keeps_xml_namespace_and_relative_ok(self):
		# the exact SVG/xlink namespaces and non-http(s) refs still pass.
		self.assertEqual(
			codes('<svg xmlns="http://www.w3.org/2000/svg"><rect fill="var(--jd-accent)"/></svg>'), []
		)
		self.assertEqual(codes("<style>.x{background:url(data:image/png;base64,AA)}</style>"), [])
		self.assertEqual(codes('<style>.x{background:url("#grad")}</style>'), [])

	# ---- DR3-3 color literals in properties beyond the enumerated list ----- #
	def test_system_color_in_filter_rejected(self):
		self.assertEqual(
			codes("<style>.k{filter:drop-shadow(0 0 4px Highlight)}</style>"), [RULE_OFF_PALETTE]
		)
		self.assertEqual(
			codes("<style>.k{backdrop-filter:drop-shadow(0 0 4px CanvasText)}</style>"), [RULE_OFF_PALETTE]
		)

	def test_off_palette_color_in_border_image_rejected(self):
		self.assertEqual(
			codes("<style>.k{border-image:linear-gradient(45deg,red,#0f172a) 30}</style>"), [RULE_OFF_PALETTE]
		)
		self.assertEqual(
			codes("<style>.k{border-image-source:linear-gradient(Highlight,red)}</style>"), [RULE_OFF_PALETTE]
		)

	def test_generic_literal_scan_catches_uncovered_property(self):
		# durability: a hex literal in a property NOT in the enumerated color list
		# is still caught by the generic literal sweep.
		self.assertEqual(
			codes("<style>.k{-webkit-box-reflect:below 0 linear-gradient(transparent,#ff0000)}</style>"),
			[RULE_OFF_PALETTE],
		)

	def test_generic_scan_does_not_false_positive_on_custom_idents(self):
		# color-WORD idents double as custom-idents / keywords in these properties;
		# the generic sweep must NOT flag them, nor a var() to an author custom prop.
		for html in (
			"<style>.k{transition-property:background}</style>",
			"<style>.k{transition:background .2s}</style>",
			"<style>.k{animation-name:red}</style>",
			"<style>.k{will-change:background}</style>",
			"<style>.k{padding:var(--gap)}</style>",
			"<style>.k{--gap:16px;padding:var(--gap)}</style>",
			"<style>.k{filter:blur(4px)}</style>",
		):
			self.assertEqual(codes(html), [], html)

	# ---- DR3-4 CASE-SENSITIVE custom-property existence ------------------- #
	def test_mismatched_case_jd_var_rejected(self):
		# CSS custom-property names are case-sensitive; `var(--JD-INK)` is unset in
		# the browser, so it must NOT be accepted as if `--jd-ink` existed.
		self.assertEqual(codes("<style>.x{color:var(--JD-INK)}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes("<style>.x{color:var(--Jd-Ink)}</style>"), [RULE_OFF_PALETTE])
		self.assertEqual(codes('<div style="color:var(--JD-INK)">x</div>'), [RULE_INLINE_COLOR])

	def test_mismatched_case_font_var_rejected(self):
		self.assertEqual(codes("<style>body{font-family:var(--JD-FONT)}</style>"), [RULE_FONT_FAMILY])
		self.assertEqual(codes("<style>body{font-family:var(--Jd-Font-Display)}</style>"), [RULE_FONT_FAMILY])

	def test_exact_case_jd_var_still_passes(self):
		self.assertEqual(codes("<style>.x{color:var(--jd-ink)}</style>"), [])
		self.assertEqual(codes("<style>body{font-family:var(--jd-font)}</style>"), [])


if __name__ == "__main__":
	unittest.main()
