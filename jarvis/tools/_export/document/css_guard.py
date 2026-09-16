"""Validators for USER-authored PDF-template style values.

Predefined templates ship trusted css; a CUSTOM template's values are
attacker-controlled and flow RAW into a ``<style>`` block (``theme.component_css``)
and an inline ``style=`` attribute (``export_document`` accent bar). wkhtmltopdf
resolves ``url()`` server-side, so an unvalidated value like
``#fff;} body{background:url(http://evil)}`` is a CSS-injection / SSRF vector.

These validators are the gate applied BOTH at persistence (the doctype controller)
and at the render-merge (defense in depth): a colour must be plain 3/6-digit hex,
a font must name a known role. Pure module (no ``import frappe``) so it is
unit-testable and shared by the controller and the merge.
"""

from __future__ import annotations

import re

# 3- or 6-digit hex only. 8-digit (alpha) is CSS4 and unreliable in wkhtmltopdf;
# named colours / rgb()/hsl()/url() are all rejected — the whole point is that no
# raw expression can reach the stylesheet.
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

FONT_ROLES = ("sans", "serif")


class CssValueError(ValueError):
	"""A user-supplied style value is not in the allowed shape."""


def validate_hex_color(value: str | None, *, field: str) -> str:
	"""Return the trimmed hex colour, or "" when blank. Raise ``CssValueError`` for
	anything that is not a bare 3/6-digit hex colour."""
	v = (value or "").strip()
	if not v:
		return ""
	if not _HEX.match(v):
		raise CssValueError(f"{field} must be a hex colour like #2e7d6b (got {value!r}).")
	return v


def validate_font_role(value: str | None, *, field: str) -> str:
	"""Return the normalized font role ("sans"/"serif"), or "" when blank. Raise
	``CssValueError`` for anything else (the role maps to a fixed FONT_STACK)."""
	v = (value or "").strip().lower()
	if not v:
		return ""
	if v not in FONT_ROLES:
		raise CssValueError(f"{field} must be one of {', '.join(FONT_ROLES)} (got {value!r}).")
	return v
