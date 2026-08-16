"""Attack corpus for ``sanitize_rich`` - the rich-PDF security core.

``sanitize_rich`` is a pure module (imports only ``nh3``), so this suite runs
WITHOUT a bench/site: ``python -m pytest`` against it directly. Each case is a
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

import pytest

from jarvis.tools._export.document.sanitizer import _DECOMPOSE_TAGS, sanitize_rich

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
)


def _assert_neutralized(payload: str, forbidden: list[str], required: list[str]) -> None:
	out = sanitize_rich(payload)
	assert isinstance(out, str)
	low = out.lower()
	for needle in forbidden:
		assert needle.lower() not in low, f"{needle!r} survived in {out!r}"
	for needle in required:
		assert needle in out, f"{needle!r} missing from {out!r}"


@pytest.mark.parametrize("tag", _DECOMPOSE_TAGS)
def test_decompose_tag_and_attr_stripped(tag: str) -> None:
	"""EVERY tag in the decompose set is removed together with any fetchable
	attribute - the core SSRF property, exercised across the whole set so no tag
	is left without a regression shield."""
	out = sanitize_rich(f'<{tag} src="http://evil-{tag}" href="http://evil-{tag}">X</{tag}>').lower()
	assert f"<{tag}" not in out, f"<{tag}> survived: {out!r}"
	assert f"evil-{tag}" not in out, f"{tag} fetchable attribute survived: {out!r}"


@pytest.mark.parametrize("tag", _CONTENT_BEARING)
def test_decompose_tag_content_removed(tag: str) -> None:
	"""A content-bearing decompose tag has its INNER TEXT removed, not kept as
	inert visible text (nh3 alone would keep it - clean_content_tags is what
	deletes the subtree)."""
	out = sanitize_rich(f"<{tag}>ZAP_{tag}</{tag}><p>ok</p>")
	assert f"ZAP_{tag}" not in out, f"{tag} inner content survived: {out!r}"
	assert "ok" in out, f"legit sibling content dropped for {tag}: {out!r}"


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


@pytest.mark.parametrize(
	"payload,forbidden,required", [c[1:] for c in _ATTACKS], ids=[c[0] for c in _ATTACKS]
)
def test_attack_neutralized(payload: str, forbidden: list[str], required: list[str]) -> None:
	_assert_neutralized(payload, forbidden, required)


def test_long_nested_returns_promptly() -> None:
	"""A 200k-char DEEPLY NESTED fragment (the pathological input that made the old
	BeautifulSoup/html5lib pre-pass run ~80s) must sanitize well within budget -
	nh3's single Rust pass does the tree construction natively. This is the guard
	the sanitizer collapse exists to make true."""
	payload = "<div>" * 20_000 + "x" + "</div>" * 20_000
	start = time.monotonic()
	out = sanitize_rich(payload)
	elapsed = time.monotonic() - start
	assert "x" in out
	assert elapsed < 5.0, f"sanitize took {elapsed:.3f}s on a deeply-nested 200k fragment"


def test_long_style_value_returns_promptly() -> None:
	"""A 200k-char inline style value must be dropped and must not stall the
	sanitizer (ReDoS / pathological-parse guard)."""
	payload = '<div style="width:' + ("a" * 200_000) + '">x</div>'
	start = time.monotonic()
	out = sanitize_rich(payload)
	elapsed = time.monotonic() - start
	assert "style" not in out.lower()
	assert "aaaa" not in out.lower()
	assert "x" in out
	assert elapsed < 5.0, f"sanitize took {elapsed:.3f}s on a 200k style value"


# --- positive cases: legitimate rich content must survive intact --------------


def test_rich_document_survives() -> None:
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
		assert tag in out, f"{tag} was dropped"
	for cls in ("cover-title", "section", "lead", "zebra", "hd", "num"):
		assert cls in out, f"class {cls!r} was dropped"
	assert "Q3 Report" in out and "Summary text." in out


def test_table_cell_layout_attrs_survive() -> None:
	"""colspan/rowspan/scope on cells inside a real table are the ONLY cell attrs
	kept (id/background already proven stripped)."""
	doc = (
		"<table><thead><tr>"
		'<th colspan="2" scope="col">Wide</th></tr></thead>'
		'<tbody><tr><td rowspan="3">Tall</td></tr></tbody></table>'
	)
	out = sanitize_rich(doc)
	assert 'colspan="2"' in out
	assert 'scope="col"' in out
	assert 'rowspan="3"' in out


@pytest.mark.parametrize(
	"href",
	['href="https://ok"', 'href="#sec"', 'href="mailto:x@y"'],
)
def test_safe_anchor_hrefs_survive(href: str) -> None:
	"""http/https/mailto and bare ``#`` fragment hrefs are the allowed link
	schemes and must pass through."""
	out = sanitize_rich(f"<a {href}>link</a>")
	assert href in out
	assert ">link</a>" in out
