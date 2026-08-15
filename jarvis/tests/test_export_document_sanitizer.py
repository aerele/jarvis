"""Attack corpus for ``sanitize_rich`` - the rich-PDF security core.

``sanitize_rich`` is a pure module (imports only ``nh3`` + ``bs4``), so this
suite runs WITHOUT a bench/site: ``python -m pytest`` against it directly. Each
case is a real vector the agent could compose; the assertion proves the vector is
neutralized before it could reach the (unauthenticated, fetch-capable) wkhtmltopdf
render.

The sanitizer has TWO independent layers (BeautifulSoup decompose + ``nh3.clean``
allowlist), so the corpus is mutation-verified per layer:
  * decompose layer -- drop ``audio`` from ``_DECOMPOSE_TAGS`` and the
    ``AUDIO-LEAK`` inner text survives nh3 (the ``audio`` case fails).
  * nh3 layer -- the ``<img>`` cases only fail if BOTH layers are defeated (drop
    ``img`` from ``_DECOMPOSE_TAGS`` AND add it to ``_ALLOWED_TAGS``), proving
    nh3's allowlist independently backstops image stripping.
"""

import time

import pytest

from jarvis.tools._export.document.sanitizer import sanitize_rich


def _assert_neutralized(payload: str, forbidden: list[str], required: list[str]) -> None:
	out = sanitize_rich(payload)
	assert isinstance(out, str)
	low = out.lower()
	for needle in forbidden:
		assert needle.lower() not in low, f"{needle!r} survived in {out!r}"
	for needle in required:
		assert needle in out, f"{needle!r} missing from {out!r}"


# (id, payload, forbidden-substrings, required-substrings). ``forbidden`` is
# matched case-insensitively; ``required`` case-sensitively.
_ATTACKS = [
	("script", "<script>alert(1)</script>", ["<script", "alert(1)"], []),
	("script-mixedcase", "<ScRiPt>alert(1)</ScRiPt>", ["script", "alert(1)"], []),
	("comment-script", "<!--><script>x</script>", ["<script"], []),
	("href-js", '<a href="javascript:alert(1)">click</a>', ["javascript"], []),
	("href-js-mixedcase", '<a href="JavaScript:alert(1)">click</a>', ["javascript"], []),
	("img-data", '<img src="data:image/png;base64,AAAA">', ["<img", "data:"], []),
	("img-http", '<img src="http://evil/x.png">', ["<img", "evil"], []),
	# A same-site path is still an agent-supplied <img> - it MUST be stripped too.
	("img-same-site", '<img src="/files/x.png">', ["<img", "/files"], []),
	("img-file", '<img src="file:///etc/passwd">', ["<img", "file:", "passwd"], []),
	("svg-image", '<svg><image href="http://evil"/></svg>', ["svg", "<image", "evil"], []),
	# nh3 alone STRIPS the <audio>/<iframe> tag but KEEPS its inner text (unlike
	# script/style, which ammonia removes with content). The decompose pre-pass is
	# what deletes the inner text - so these cases keep the decompose layer honest
	# (mutation-verified: drop the tag from _DECOMPOSE_TAGS and the LEAK survives).
	("audio", '<audio src="http://evil">AUDIO-LEAK</audio>', ["audio", "evil", "AUDIO-LEAK"], []),
	("iframe-text", "<iframe>IFRAME-LEAK</iframe>", ["iframe", "IFRAME-LEAK"], []),
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
	assert elapsed < 2.0, f"sanitize took {elapsed:.3f}s on a 200k style value"


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
