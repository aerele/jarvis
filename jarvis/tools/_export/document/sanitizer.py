"""Sanitize agent-composed rich HTML before it reaches the rich-PDF renderer.

This is the security core of the rich-PDF document engine. ``content`` (plus
header/footer/watermark) on the rich path is effectively LLM-controlled, and the
renderer that consumes it (wkhtmltopdf) will FETCH any ``<img src>`` /
``<link href>`` / SVG ``<image href>`` it finds at render time, server-side. So
the output of this module must contain no fetch-capable tag and no active markup
at all - only inert, class-styled structural HTML.

WHY a pure module (imports ONLY ``nh3`` + ``bs4``, never ``frappe``): the
sanitizer is the one place a mistake becomes an SSRF/XSS hole, so it is kept
importable and unit-testable without a running bench - the attack corpus runs in
plain CI, not only inside a site.

WHY ``nh3.clean`` directly, NOT ``frappe.utils.sanitize_html``: that helper only
exposes ``disallowed_tags``; its defaults *allow* ``id``/``name``/``background``/
``data``/``poster``/``src``/``href`` + all ~726 style properties + broad url
schemes, and it early-returns the input UNSANITIZED when the payload looks like
JSON or contains no tags. None of that is acceptable here, and none of it is
configurable through that signature. We call ``nh3.clean`` with an explicit,
tight allowlist instead.

The pipeline is two layers, mirroring the decompose-then-clean shape Frappe's own
``clean_email_html`` uses:

  1. ``_strip_unsafe_tags`` - a BeautifulSoup ``decompose`` pre-pass that REMOVES
     each fetch-capable / active tag together with its subtree. nh3 alone would
     strip a disallowed tag but KEEP its text, so ``<script>alert(1)</script>``
     would survive as the visible text ``alert(1)``. Decomposing first deletes
     the payload cleanly.
  2. ``nh3.clean`` with a tight tag/attribute/url-scheme allowlist on what
     remains - the airtight allowlist pass (belt-and-suspenders once the unsafe
     tags are already gone).

WHY inline ``style`` is DISALLOWED entirely (classes-only): nh3's
``filter_style_properties`` is a property-NAME allowlist ONLY. It does not reject
``url(...)`` values and it re-forms CSS escapes, so a permitted property is not a
safe property. The only airtight option is to forbid the ``style`` attribute on
agent content and drive every visual through THEME_CSS classes. Tool-generated
components (CSS bar charts, etc.) are spliced in AFTER sanitize, so they never
need agent-supplied inline style.
"""

import nh3
from bs4 import BeautifulSoup

# Removed OUTRIGHT (tag + subtree) by the decompose pre-pass rather than left to
# nh3's allowlist. Every one of these can trigger an out-of-band fetch when
# wkhtmltopdf renders (img/svg/iframe/object/embed/audio/video/source/link/
# base), execute (script), inject styling/url() (style), carry metadata refresh
# (meta), or submit/collect (form/input/button). Removing the whole subtree also
# deletes the inert-but-visible text nh3 would otherwise keep.
_DECOMPOSE_TAGS = (
	"script",
	"style",
	"link",
	"meta",
	"svg",
	"img",
	"iframe",
	"object",
	"embed",
	"base",
	"form",
	"input",
	"button",
	"audio",
	"video",
	"source",
)

# The rich structural set an org document needs. Deliberately NO ``img`` - agent
# content never supplies its own images; the tool splices its own permission-
# checked base64 images in AFTER sanitize.
_ALLOWED_TAGS = {
	"h1",
	"h2",
	"h3",
	"h4",
	"h5",
	"h6",
	"p",
	"br",
	"hr",
	"ul",
	"ol",
	"li",
	"blockquote",
	"pre",
	"code",
	"strong",
	"em",
	"b",
	"i",
	"u",
	"s",
	"sup",
	"sub",
	"span",
	"div",
	"section",
	"header",
	"footer",
	"table",
	"thead",
	"tbody",
	"tfoot",
	"tr",
	"th",
	"td",
	"caption",
	"colgroup",
	"col",
	"a",
}

# ``"*"`` allows ``class`` on every permitted tag (styling is classes-only);
# ``href`` only on ``a``; table-layout attrs only on cells. Everything else
# (``id``/``name``/``style``/``background``/``data-*``/``on*``/``src``/``srcset``/
# ``poster``/``formaction`` ...) is dropped by omission - nh3 keeps ONLY what is
# listed here.
_ALLOWED_ATTRIBUTES = {
	"*": {"class"},
	"a": {"href"},
	"th": {"colspan", "rowspan", "scope"},
	"td": {"colspan", "rowspan", "scope"},
}

# Only these href schemes survive on ``<a>``. ``javascript:``/``data:``/``file:``
# are rejected by omission. nh3 also permits bare ``#`` fragment and
# scheme-relative/relative paths (harmless as links - they are not fetched by the
# renderer the way an ``<img src>`` would be).
_URL_SCHEMES = {"http", "https", "mailto"}


def _strip_unsafe_tags(html: str) -> str:
	"""Remove (not escape) every tag in ``_DECOMPOSE_TAGS`` together with its
	subtree, ahead of ``nh3.clean``.

	nh3's own default for a disallowed tag is to strip the tag but keep its inner
	text, which would leave a ``<script>``'s payload sitting as visible text.
	Decomposing first deletes the payload. ``BeautifulSoup(html, "html5lib")``
	parses with the same HTML5 tree-construction algorithm a browser/renderer
	uses (the safest reading of adversarial input) and always wraps a fragment in
	its own ``<html><head></head><body>...</body></html>``, so we return the body's
	inner HTML - ``html`` here is a fragment about to be spliced into the engine's
	own page shell, not a standalone page.
	"""
	soup = BeautifulSoup(html, "html5lib")
	for tag in soup(list(_DECOMPOSE_TAGS)):
		tag.decompose()
	return soup.body.decode_contents() if soup.body else soup.decode_contents()


def sanitize_rich(html: str) -> str:
	"""Return ``html`` reduced to inert, class-styled structural markup safe to
	hand an unauthenticated wkhtmltopdf render.

	Two layers: a BeautifulSoup ``decompose`` pre-pass that deletes every
	fetch-capable / active tag and its subtree, then ``nh3.clean`` with a tight
	tag/attribute/url-scheme allowlist. Inline ``style`` is disallowed outright
	(classes-only) - see the module docstring for why the ``style`` attribute
	cannot be made airtight via ``filter_style_properties``. No ``<img>``,
	``on*`` handler, ``id``, ``javascript:``/``data:``/``file:`` url, or event
	attribute can survive this.
	"""
	stripped = _strip_unsafe_tags(html)
	return nh3.clean(
		stripped,
		tags=_ALLOWED_TAGS,
		attributes=_ALLOWED_ATTRIBUTES,
		url_schemes=_URL_SCHEMES,
	)
