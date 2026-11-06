"""DATA-safe text for what is stored, shown to a reviewer, or quoted back into a prompt
(jarvis-admin-v2#793).

Three tools, from loosest to strictest:

* ``plain_line``: the uncapped core. It strips markup and control characters, collapses
  whitespace and turns backticks into quotes. It leaves brackets and quotes alone because
  ``held_sheets._fix_text`` needs frappe's ``[Item, X]: stock_uom`` intact, so
  ``filebox_skills._safe`` (its only other caller) is just ``plain_line`` plus a cap.
* ``safe_line``: ``plain_line`` plus the neutralizing a quoted line needs, so it cannot forge a
  ``[Context:]`` or ``[System]`` label, close a quoted value, or lose an email address to
  ``strip_html``. For text the agent or a reviewer wrote that is only CONTEXT.
* ``decision_value``: a reviewer's decision, which is the PAYLOAD (a recipient list, a part
  number, an address), so it is quoted EXACTLY as one JSON string instead of being reshaped.
"""

from __future__ import annotations

import json
import re
import unicodedata

import frappe

# One lead-in length for the whole feature: the agent's words stored with a chat ask
# (``chat_asks.ask_lead_in``) and quoted back into the decision message (``approvals_api``).
LEAD_IN_CAP = 400
# The longest decision quoted into a prompt, measured on the cleaned text. A recipient list of
# a few dozen addresses fits whole; anything longer says so (``DECISION_TRUNCATED``).
DECISION_RAW_CAP = 2000
DECISION_TRUNCATED = " (answer truncated; ask the user for the rest)"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def plain_line(text) -> str:
	"""``text`` as one line: markup stripped, control characters turned into spaces, whitespace
	collapsed, backticks turned into ``'``. No cap, and brackets and quotes are untouched."""
	text = _CONTROL.sub(" ", frappe.utils.strip_html(str(text or "")))
	return " ".join(text.split()).replace("`", "'")


# Frappe's strip_html deletes any span from "<" to the next ">" (an email address in angle
# brackets, or "a < b ... c > d"), so the angle brackets are mapped BEFORE plain_line sees them.
_ANGLES = str.maketrans({"<": "‹", ">": "›"})
# Square brackets would forge "[Context:]" / "[System]" labels; a double quote would close a
# quoted value. Each maps to a look-alike that reads the same to a human. An em-dash (U+2014)
# is not allowed in agent-facing text at all (lint.sh fails on it in persona content), so it
# maps to a plain hyphen.
_EM_DASH = chr(0x2014)  # spelled as a code point: the branding gate greps the literal
_LOOKALIKES = str.maketrans({"[": "［", "]": "］", '"': "”", _EM_DASH: "-"})
# Unicode format characters (category Cf: bidi overrides U+202A-202E, isolates U+2066-2069,
# zero-width space, BOM...) are invisible and can reorder what a reviewer reads, so they are
# dropped. The zero-width joiner and non-joiner are kept: they shape Indic conjuncts and emoji
# sequences and cannot reorder anything.
_KEEP_FORMAT = frozenset((chr(0x200C), chr(0x200D)))  # ZWNJ, ZWJ
_C1 = frozenset(chr(c) for c in range(0x80, 0xA0))


def safe_line(text, cap: int) -> str:
	"""``text`` as one line of plain prose, at most ``cap`` chars, cut on a word boundary with
	an ellipsis. Never raises on ``None`` or a non-string."""
	text = "".join(
		" " if ch in _C1 else ch  # C1 controls (U+0080-U+009F): not in plain_line, _safe stays as is
		for ch in str(text or "")
		if ch in _KEEP_FORMAT or unicodedata.category(ch) != "Cf"
	)
	# Map "<" and ">" BEFORE plain_line: its strip_html deletes everything from "<" to the next ">".
	text = plain_line(text.translate(_ANGLES))
	text = text.translate(_LOOKALIKES)
	if len(text) <= cap:
		return text
	head = text[: cap - 1]
	space = head.rfind(" ")
	# Back up to the last whole word, unless that would throw away most of the line (an
	# unbroken token, or no space until very late): then hard-cut at the cap.
	if not text[cap - 1].isspace() and space >= cap // 2:
		head = head[:space]
	return head.rstrip() + "…"


_LINE_BREAKS = {0x2028: "\n", 0x2029: "\n", 0x0D: None}  # LS, PS -> newline; CR dropped


def decision_value(text, verdict: str) -> str:
	"""A reviewer's decision, exact, as ONE JSON string (``json.dumps``, not ASCII-escaped).

	The decision is the payload the agent acts on, so nothing is reworded: quotes, brackets,
	angle brackets and non-Latin text arrive as typed. Newlines become ``\\n`` inside the
	quotes, so no line of the message around it can be forged. Only what cannot be seen or
	cannot be safely carried is removed: format (Cf) characters except ZWNJ and ZWJ, C0 and C1
	controls except newline and tab, and lone surrogates. An em-dash maps to a hyphen (no
	em-dash in agent-facing text). Over ``DECISION_RAW_CAP`` cleaned chars it is cut and says so
	after the closing quote. Empty once cleaned, it is ``verdict`` (unquoted, so it cannot be
	mistaken for typed text)."""
	text = str(text or "").replace("\r\n", "\n").translate(_LINE_BREAKS).replace(_EM_DASH, "-")
	text = "".join(
		ch
		for ch in text
		if ch in "\n\t" or ch in _KEEP_FORMAT or unicodedata.category(ch) not in ("Cf", "Cc", "Cs")
	).strip()
	if not text:
		return verdict
	cut = len(text) > DECISION_RAW_CAP
	quoted = json.dumps(text[:DECISION_RAW_CAP], ensure_ascii=False)
	return quoted + DECISION_TRUNCATED if cut else quoted
