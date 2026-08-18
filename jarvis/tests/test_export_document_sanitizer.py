"""Attack corpus for ``sanitize_rich`` - the rich-PDF security core.

``sanitize_rich`` is a pure module (imports only ``nh3``), so this suite runs
WITHOUT a bench/site: plain ``unittest`` against it directly. Each case is a
real vector the agent could compose; the assertion proves the vector is
neutralized before it could reach the (unauthenticated, fetch-capable)
wkhtmltopdf render.

``sanitize_rich`` is a SINGLE ``nh3.clean`` pass: a tag/attribute/url-scheme
allowlist plus ``clean_content_tags`` (removes each fetch-capable / active /
raw-text tag together with its subtree, natively - so a ``<script>``'s payload is
deleted, not kept as visible text). The two data-driven tests below exercise the
WHOLE ``_DECOMPOSE_TAGS`` set (not a hand-picked subset), so they are
mutation-verified for every tag and auto-cover any tag added to the set later:
drop a tag from ``_DECOMPOSE_TAGS`` and its ``<tag`` + fetchable attribute (and,
for content-bearing tags, its inner text) survive - the case fails.
"""

import time
import unittest

from jarvis.tools._export.document.sanitizer import (
	_DECOMPOSE_TAGS,
	sanitize_letterhead,
	sanitize_rich,
)

# Content-bearing subset of the decompose set: their INNER TEXT must be removed
# too (not just the tag), so a stripped tag can't leave inert payload text behind.
# The void / special-parsing tags (img/input/source/base/link/meta/embed and
# xmp/plaintext/textarea/title) are covered by the tag+attr test only - they have
# no normal element content to assert on.
_CONTENT_BEARING = (
	"script",
	"style",
	"svg",
	"iframe",
	"object",
	"audio",
	"video",
	"noscript",
	"template",
	"form",
	"button",
	# raw-text / escapable / metadata elements — their content must be DISCARDED,
	# not merely stripped-tag-keep-text (the module docstring names these). A
	# hardcoded tuple (NOT derived from _DECOMPOSE_TAGS) so removing one of these
	# from the decompose set makes the matching case go RED.
	"title",
	"textarea",
	"xmp",
	"plaintext",
)


def _assert_neutralized(payload: str, forbidden: list[str], required: list[str]) -> None:
	out = sanitize_rich(payload)
	assert isinstance(out, str)
	low = out.lower()
	for needle in forbidden:
		assert needle.lower() not in low, f"{needle!r} survived in {out!r}"
	for needle in required:
		assert needle in out, f"{needle!r} missing from {out!r}"


# (id, payload, forbidden-substrings, required-substrings). ``forbidden`` is
# matched case-insensitively; ``required`` case-sensitively. Specific vectors
# beyond the whole-set sweep above.
_ATTACKS = [
	("script-mixedcase", "<ScRiPt>alert(1)</ScRiPt>", ["<script", "alert(1)"], []),
	("comment-script", "<!--><script>x</script>", ["<script"], []),
	("nested-split-script", "<scr<script>ipt>bad()</scr</script>ipt>", ["<script"], []),
	("href-js", '<a href="javascript:alert(1)">click</a>', ["javascript"], []),
	("href-js-mixedcase", '<a href="JavaScript:alert(1)">click</a>', ["javascript"], []),
	("img-data", '<img src="data:image/png;base64,AAAA">', ["<img", "data:"], []),
	("img-http", '<img src="http://evil/x.png">', ["<img", "evil"], []),
	# A same-site path is still an agent-supplied <img> - it MUST be stripped too
	# (agent content never carries images; only trusted letterhead logos do, and
	# those are inlined in furniture.py, never through this sanitizer).
	("img-same-site", '<img src="/files/x.png">', ["<img", "/files"], []),
	("img-file", '<img src="file:///etc/passwd">', ["<img", "file:", "passwd"], []),
	("svg-image", '<svg><image href="http://evil"/></svg>', ["svg", "<image", "evil"], []),
	("math-foreign", '<math><mtext><img src="http://evil"></mtext></math>', ["<img", "evil"], []),
	("meta-refresh", '<meta http-equiv="refresh" content="0;url=http://evil">', ["<meta", "evil"], []),
	("link-css", '<link rel="stylesheet" href="http://evil/x.css">', ["<link", "evil"], []),
	("base-href", '<base href="http://evil/">', ["<base", "evil"], []),
	("noscript-img", '<noscript><img src="http://evil"></noscript>', ["<img", "evil"], []),
	("td-background", '<td background="http://evil">x</td>', ["background", "evil"], ["x"]),
	# style attribute dropped entirely (classes-only), text preserved.
	(
		"style-bg-url",
		'<div style="background-image:url(http://evil)">x</div>',
		["style", "url(", "evil"],
		["x"],
	),
	("style-color", '<div style="color:red">x</div>', ["style"], ["x"]),
	("id-attr", '<div id="header-html">x</div>', ['id="', "header-html"], ["x"]),
	("onclick", '<div onclick="x()">y</div>', ["onclick"], ["y"]),
	# Markdown image syntax passed as literal text must not regenerate an <img>.
	("markdown-img", "![alt](http://evil)", ["<img"], []),
	("malformed-table", "<table><tr><td>unclosed", [], ["unclosed"]),
]

# --- letterhead SSRF vectors --------------------------------------------------

_DATA_LOGO = "data:image/png;base64,iVBORw0KGgo="

# Every remote/relative image src form + fetch-capable construct that must NOT
# survive the letterhead gate (each would make wkhtmltopdf fetch server-side).
_LETTERHEAD_SSRF = [
	("remote-quoted", '<img src="http://169.254.169.254/x">', ["169.254", "http://"]),
	("remote-noquote", "<img src=http://evil/x.png>", ["evil"]),
	("image-alias", '<image src="http://evil/x.png">', ["evil"]),
	("image-href", '<image href="http://evil/x.png">', ["evil"]),
	("alt-truncation", '<img alt="a>b" src="http://evil/x.png">', ["evil"]),
	("protocol-relative", '<img src="//evil/x.png">', ["evil"]),
	("relative-files", '<img src="/files/logo.png">', ["/files/logo.png"]),
	("srcset", '<img srcset="http://evil/x 1x">', ["evil"]),
	("link-css", '<link rel="stylesheet" href="http://evil/x.css">', ["evil", "<link"]),
	("style-tag-url", "<style>body{background:url(http://evil/x)}</style>", ["evil", "<style"]),
	("style-attr-url", '<div style="background:url(http://evil/x)">y</div>', ["evil", "style="]),
	("svg-image", '<svg><image href="http://evil/x"/></svg>', ["evil", "<svg", "<image"]),
	("iframe", '<iframe src="http://evil/x"></iframe>', ["evil", "<iframe"]),
	("input-image", '<input type="image" src="http://evil/x">', ["evil", "<input"]),
	("video-poster", '<video poster="http://evil/x"></video>', ["evil", "<video"]),
	("object-data", '<object data="http://evil/x"></object>', ["evil", "<object"]),
	("base-href", '<base href="http://evil/">', ["evil", "<base"]),
	("meta-refresh", '<meta http-equiv="refresh" content="0;url=http://evil">', ["evil", "<meta"]),
]


class TestDecomposeTags(unittest.TestCase):
	def test_decompose_tag_and_attr_stripped(self) -> None:
		"""EVERY tag in the decompose set is removed together with any fetchable
		attribute - the core SSRF property, exercised across the whole set so no tag
		is left without a regression shield."""
		for tag in _DECOMPOSE_TAGS:
			with self.subTest(tag=tag):
				out = sanitize_rich(
					f'<{tag} src="http://evil-{tag}" href="http://evil-{tag}">X</{tag}>'
				).lower()
				self.assertNotIn(f"<{tag}", out, f"<{tag}> survived: {out!r}")
				self.assertNotIn(f"evil-{tag}", out, f"{tag} fetchable attribute survived: {out!r}")

	def test_decompose_tag_content_removed(self) -> None:
		"""A content-bearing decompose tag has its INNER TEXT removed, not kept as
		inert visible text (nh3 alone would keep it - clean_content_tags is what
		deletes the subtree). The legit sibling is placed BEFORE the tag so a
		raw-text element like <plaintext>/<xmp> (which consumes everything AFTER it)
		does not eat it."""
		for tag in _CONTENT_BEARING:
			with self.subTest(tag=tag):
				out = sanitize_rich(f"<p>ok</p><{tag}>ZAP_{tag}</{tag}>")
				self.assertNotIn(f"ZAP_{tag}", out, f"{tag} inner content survived: {out!r}")
				self.assertIn("ok", out, f"legit sibling content dropped for {tag}: {out!r}")


class TestAttackCorpus(unittest.TestCase):
	def test_attack_neutralized(self) -> None:
		for case_id, payload, forbidden, required in _ATTACKS:
			with self.subTest(case_id=case_id):
				_assert_neutralized(payload, forbidden, required)


class TestPerformanceGuards(unittest.TestCase):
	def test_long_nested_returns_promptly(self) -> None:
		"""A 200k-char DEEPLY NESTED fragment (the pathological input that made the old
		BeautifulSoup/html5lib pre-pass run ~80s) must sanitize well within budget -
		nh3's single Rust pass does the tree construction natively. This is the guard
		the sanitizer collapse exists to make true."""
		payload = "<div>" * 20_000 + "x" + "</div>" * 20_000
		start = time.monotonic()
		out = sanitize_rich(payload)
		elapsed = time.monotonic() - start
		self.assertIn("x", out)
		self.assertTrue(elapsed < 5.0, f"sanitize took {elapsed:.3f}s on a deeply-nested 200k fragment")

	def test_long_style_value_returns_promptly(self) -> None:
		"""A 200k-char inline style value must be dropped and must not stall the
		sanitizer (ReDoS / pathological-parse guard)."""
		payload = '<div style="width:' + ("a" * 200_000) + '">x</div>'
		start = time.monotonic()
		out = sanitize_rich(payload)
		elapsed = time.monotonic() - start
		self.assertNotIn("style", out.lower())
		self.assertNotIn("aaaa", out.lower())
		self.assertIn("x", out)
		self.assertTrue(elapsed < 5.0, f"sanitize took {elapsed:.3f}s on a 200k style value")


# --- positive cases: legitimate rich content must survive intact --------------


class TestLegitimateContentSurvives(unittest.TestCase):
	def test_rich_document_survives(self) -> None:
		"""A realistic rich doc keeps its structural tags and class hooks (styling is
		classes-only, so classes MUST pass through)."""
		doc = (
			'<h1 class="cover-title">Q3 Report</h1>'
			'<section class="section"><p class="lead">Summary text.</p>'
			"<ul><li>one</li><li>two</li></ul>"
			'<table class="zebra"><thead><tr><th scope="col" class="hd">Metric</th></tr></thead>'
			'<tbody><tr><td class="num">42</td></tr></tbody></table>'
			"<blockquote>quote</blockquote><pre><code>x = 1</code></pre></section>"
		)
		out = sanitize_rich(doc)
		for tag in (
			"<h1",
			"<section",
			"<p",
			"<ul",
			"<li",
			"<table",
			"<thead",
			"<th",
			"<tbody",
			"<td",
			"<blockquote",
			"<pre",
			"<code",
		):
			self.assertIn(tag, out, f"{tag} was dropped")
		for cls in ("cover-title", "section", "lead", "zebra", "hd", "num"):
			self.assertIn(cls, out, f"class {cls!r} was dropped")
		self.assertTrue("Q3 Report" in out and "Summary text." in out)

	def test_table_cell_layout_attrs_survive(self) -> None:
		"""colspan/rowspan/scope on cells inside a real table are the ONLY cell attrs
		kept (id/background already proven stripped)."""
		doc = (
			"<table><thead><tr>"
			'<th colspan="2" scope="col">Wide</th></tr></thead>'
			'<tbody><tr><td rowspan="3">Tall</td></tr></tbody></table>'
		)
		out = sanitize_rich(doc)
		self.assertIn('colspan="2"', out)
		self.assertIn('scope="col"', out)
		self.assertIn('rowspan="3"', out)

	def test_safe_anchor_hrefs_survive(self) -> None:
		"""http/https/mailto and bare ``#`` fragment hrefs are the allowed link
		schemes and must pass through."""
		for href in ['href="https://ok"', 'href="#sec"', 'href="mailto:x@y"']:
			with self.subTest(href=href):
				out = sanitize_rich(f"<a {href}>link</a>")
				self.assertIn(href, out)
				self.assertIn(">link</a>", out)


# --- sanitize_letterhead: the F1 SSRF gate (data: images only) ----------------


class TestSanitizeLetterheadGate(unittest.TestCase):
	def test_letterhead_gate_blocks_ssrf(self) -> None:
		for case_id, payload, forbidden in _LETTERHEAD_SSRF:
			with self.subTest(case_id=case_id):
				out = sanitize_letterhead(payload).lower()
				for needle in forbidden:
					self.assertNotIn(needle.lower(), out, f"{needle!r} survived the letterhead gate: {out!r}")

	def test_letterhead_gate_keeps_data_logo_and_structure(self) -> None:
		out = sanitize_letterhead(f'<div class="lh"><img src="{_DATA_LOGO}"><span>Acme Ltd</span></div>')
		self.assertIn("data:image/png;base64,", out)  # the inlined logo survives
		self.assertIn("Acme Ltd", out)
		self.assertIn('class="lh"', out)

	def test_letterhead_gate_drops_anchor_but_keeps_text(self) -> None:
		# <a> is dropped (keeping http on <a href> would re-open the image hole).
		out = sanitize_letterhead('<a href="http://x">Visit</a>')
		self.assertNotIn("<a", out.lower())
		self.assertIn("Visit", out)
