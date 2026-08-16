"""Sanitize agent-composed rich HTML before it reaches the rich-PDF renderer.

This is the security core of the rich-PDF document engine. ``content`` (plus
header/footer/watermark) on the rich path is effectively LLM-controlled, and the
renderer that consumes it (wkhtmltopdf) will FETCH any ``<img src>`` /
``<link href>`` / SVG ``<image href>`` it finds at render time, server-side. So
the output of this module must contain no fetch-capable tag and no active markup
at all - only inert, class-styled structural HTML.

WHY a pure module (imports ONLY ``nh3``, never ``frappe`` or ``bs4``): the
sanitizer is the one place a mistake becomes an SSRF/XSS hole, so it is kept
importable and unit-testable without a running bench - the attack corpus runs in
plain CI, not only inside a site. Depending on a single, Rust-based HTML parser
(``nh3``/``ammonia``, on ``html5ever``) also means untrusted input is parsed
ONCE, by the same engine that sanitizes it - a second, different parser ahead of
it (the previous BeautifulSoup/html5lib pre-pass) is both a parser-differential
mutation-XSS surface and, in pure Python, quadratic on adversarial nesting depth
(a 200k-char deeply-nested fragment parsed for ~80s, defeating the render's
wall-clock budget). ``nh3`` does the same tree construction natively in ~2s.

WHY ``nh3.clean`` directly, NOT ``frappe.utils.sanitize_html``: that helper only
exposes ``disallowed_tags``; its defaults *allow* ``id``/``name``/``background``/
``data``/``poster``/``src``/``href`` + all ~726 style properties + broad url
schemes, and it early-returns the input UNSANITIZED when the payload looks like
JSON or contains no tags. None of that is acceptable here, and none of it is
configurable through that signature. We call ``nh3.clean`` with an explicit,
tight allowlist instead.

WHY ``clean_content_tags`` (not a manual decompose pre-pass): nh3's default for a
disallowed tag is to strip the tag but KEEP its inner text, so
``<script>alert(1)</script>`` would otherwise survive as the visible text
``alert(1)``. ``clean_content_tags`` tells nh3 to remove the tag together with
its subtree - natively, in the same single pass as the allowlist. This is the
exact parameter ``frappe.utils.html_utils.clean_html``/``clean_email_html`` use
for the same purpose (frappe's older ``clean_script_and_style``, a
BeautifulSoup-decompose pass, is deprecated in favour of it).

WHY inline ``style`` is DISALLOWED entirely (classes-only): nh3's
``filter_style_properties`` is a property-NAME allowlist ONLY. It does not reject
``url(...)`` values and it re-forms CSS escapes, so a permitted property is not a
safe property. The only airtight option is to forbid the ``style`` attribute on
agent content and drive every visual through THEME_CSS classes. Tool-generated
components (CSS bar charts, etc.) are spliced in AFTER sanitize, so they never
need agent-supplied inline style.
"""

import nh3

# Removed OUTRIGHT (tag + subtree) via nh3's ``clean_content_tags`` rather than
# left to the allowlist (which would keep inner text). Every one of these can
# trigger an out-of-band fetch when wkhtmltopdf renders (img/svg/iframe/object/
# embed/audio/video/source/link/base), execute (script), inject styling/url()
# (style), carry a metadata refresh (meta), or submit/collect (form/input/
# button). The raw-text / escapable / metadata elements (noscript/template/xmp/
# plaintext/textarea/title) are here too so their contents are discarded rather
# than surviving as inert-but-visible escaped junk in a document meant to read
# as a clean report.
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
	"noscript",
	"template",
	"xmp",
	"plaintext",
	"textarea",
	"title",
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

_DECOMPOSE_SET = set(_DECOMPOSE_TAGS)

# --- Letterhead gate ---------------------------------------------------------
# A Letter Head is operator config, but "trusted" must be ENFORCED, not assumed:
# the render is unauthenticated and wkhtmltopdf fetches any remote resource it
# finds. So a resolved letterhead (after its same-site logos are already inlined
# to permission-checked base64 data: URIs by furniture._inline_letterhead_images)
# passes through this gate, which is sanitize_rich PLUS a single concession: an
# <img> is allowed, but ONLY with a ``data:`` src.
#   * ``url_schemes={"data"}`` strips every http/https/file/etc. image src, no
#     matter how it is written (no-quote, alt-truncation, <image> alias, srcset).
#   * ``url_relative="deny"`` strips protocol-relative (``//host``) and relative
#     (``/files/x``) srcs too, which url_schemes alone does NOT catch.
#   * ``<a>`` is dropped (a letterhead needs no live links, and keeping it would
#     force http(s) back into url_schemes, re-opening the image hole).
# Result: the only image that can survive is the inlined data: logo; <link>,
# <style>, <image>/<svg>, <iframe>, <input type=image>, style="url(...)", etc.
# are all removed. Verified against a bypass corpus.
_LETTERHEAD_TAGS = (_ALLOWED_TAGS - {"a"}) | {"img"}
_LETTERHEAD_ATTRIBUTES = {
	"*": {"class"},
	"img": {"src", "alt", "class", "width", "height"},
	"th": {"colspan", "rowspan", "scope"},
	"td": {"colspan", "rowspan", "scope"},
}
_LETTERHEAD_URL_SCHEMES = {"data"}
_LETTERHEAD_DECOMPOSE = _DECOMPOSE_SET - {"img"}


def sanitize_rich(html: str) -> str:
	"""Return ``html`` reduced to inert, class-styled structural markup safe to
	hand an unauthenticated wkhtmltopdf render.

	One pass: ``nh3.clean`` with a tight tag/attribute/url-scheme allowlist and
	``clean_content_tags`` (removes every fetch-capable / active / raw-text tag
	together with its subtree, so a ``<script>``'s payload is deleted, not kept as
	visible text). Inline ``style`` is disallowed outright (classes-only) - see the
	module docstring for why the ``style`` attribute cannot be made airtight via
	``filter_style_properties``. No ``<img>``, ``on*`` handler, ``id``,
	``javascript:``/``data:``/``file:`` url, or event attribute can survive this.
	"""
	return nh3.clean(
		html,
		tags=_ALLOWED_TAGS,
		attributes=_ALLOWED_ATTRIBUTES,
		url_schemes=_URL_SCHEMES,
		clean_content_tags=_DECOMPOSE_SET,
	)


def sanitize_letterhead(html: str) -> str:
	"""Return trusted-but-enforced letterhead HTML safe for the render.

	Same airtight base as ``sanitize_rich`` with ONE concession: an ``<img>`` is
	allowed, but only with a ``data:`` src (a permission-checked base64 logo that
	``furniture._inline_letterhead_images`` has already inlined). ``url_schemes``
	restricted to ``data`` strips every remote/file image src regardless of syntax;
	``url_relative="deny"`` additionally strips protocol-relative (``//host``) and
	relative srcs that ``url_schemes`` alone would let through; ``<a>`` is dropped.
	Everything else fetch-capable (``<link>``/``<style>``/``<image>``/``<svg>``/
	``<iframe>``/``<input type=image>``/``style="url(...)"`` ...) is removed. So no
	server-side fetch (SSRF) can be provoked through a letterhead - see the
	module's letterhead-gate note."""
	return nh3.clean(
		html,
		tags=_LETTERHEAD_TAGS,
		attributes=_LETTERHEAD_ATTRIBUTES,
		url_schemes=_LETTERHEAD_URL_SCHEMES,
		url_relative="deny",
		clean_content_tags=_LETTERHEAD_DECOMPOSE,
	)
