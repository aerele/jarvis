"""Shared text helpers for turning the agent's composed text into the format a
destination expects.

The agent composes bodies as PLAIN TEXT with ``\\n`` newlines (persona contract:
"plain text, \\n newlines, no Markdown"). Several destinations - an email
Communication's ``content``, a Comment's ``content`` - are HTML fields rendered
by a mail/timeline client, where whitespace collapses. Written raw, a multi-line
body arrives as one run-on paragraph. Convert at that boundary with
``plaintext_to_html``.

Use it ONLY for an HTML-rendered surface. Do NOT run it over a value bound for a
plain-text storage field (Data / Small Text / a doc field the user edits on a
card) - there a literal ``<br>`` would be wrong.
"""

from __future__ import annotations

from frappe.utils import escape_html


def plaintext_to_html(text: str | None) -> str:
	"""Escape ``text`` and turn its ``\\n`` newlines into ``<br>`` so paragraphs
	and blank lines survive delivery to an HTML surface instead of collapsing
	into one run-on block.

	Escaping is not optional: a stray ``<`` / ``&`` in plain text - an email
	address like ``<a@b.com>``, a DocType name like ``<Sales Order>``, an
	``a < b`` comparison - would otherwise be swallowed by the client as an
	unknown tag. The ``<br>`` round-trips back to a newline in the plain-text
	alternative a mail client generates from the HTML.
	"""
	return escape_html(text or "").replace("\n", "<br>\n")
