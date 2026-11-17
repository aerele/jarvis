"""Keep agent runtime directives out of the text that jarvis-app sends.

Plan: .claude/workflow/plans/2026-11-10-fq8-jarvis-app-directive-filter.md (rev 2, F-Q8).

The agent runtime (9.3) reads inline directives from the whole message body, as an
authorized sender. A tenant who types ``/model openai/x --runtime codex`` could
move the session to a runtime that cannot redact source data. The same text can
arrive inside content that jarvis adds: an attachment, shared history, a
referenced document. This module has two rules:

* Typed text (the user's own message, and a ``/compact`` hint) that carries a
  ``/model`` token is refused. Nothing goes to the gateway.
* Added content keeps its meaning. Only the ``/`` of a ``/model`` token becomes
  U+2215 (division slash), so the host cannot parse it.

One token rule, ``MODEL_TOKEN``, has no lookbehind: the host removes earlier
directive spans before it reads ``/model``, so a token glued to an earlier
directive (``/think:/model``) becomes a directive after that removal. The
differential test (``jarvis/tests/test_directive_filter.py`` with its fixture)
proves the rule against the pinned host parser.

The checked bytes are the sent bytes: ``canonical`` mirrors the host's own
input sanitizer, and the guard returns the canonical text.
"""

from __future__ import annotations

import re
import unicodedata

import frappe

REFUSAL_TEXT = "Model and runtime changes are not available in chat."
NUL_TEXT = "The message contains a character that cannot be sent. Remove it and try again."

# The whitespace of the host's ECMAScript ``\s`` plus U+0085. Never Python ``\s``:
# Python does not count U+FEFF as whitespace, and the host does.
WS_BODY = "\t\n\v\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff\u0085"
WS = "[" + WS_BODY + "]"

# No lookbehind (c1 B1): a superset of every position where the host can see
# ``/model`` after it removes earlier spans. ``re.I`` is a superset of the host's
# ASCII-only fold.
MODEL_TOKEN = re.compile(r"/model(?=\Z|$|" + WS + r"|:)", re.I)
RUNTIME_OPTION = re.compile(r"--runtime|runtime=|harness=", re.I)
SLASH_TOKEN = re.compile(r"/[^" + WS_BODY + r"/]+")

# What the host's sanitizer removes (it rejects U+0000 instead).
_CONTROL = re.compile("[\u0001-\u0008\u000b\u000c\u000e-\u001f\u007f]")
DIVISION_SLASH = "\u2215"


class DirectiveRefused(Exception):
	"""Raised by ``canonical`` for typed text the host would reject (U+0000)."""

	def __init__(self, kind: str):
		super().__init__(kind)
		self.kind = kind


class DirectiveRefusal(frappe.ValidationError):
	"""The user-facing refusal. Callers that show a run error catch this class."""


# The strip can put a base character next to a combining mark, so one pass of "NFC, then
# strip" is not a fixed point. Two passes always are (NFC never adds a control character);
# the bound is a guard, not a tuning knob.
_CANONICAL_ROUNDS = 4


def canonical(text: str) -> str:
	"""The text as the host reads it: NFC, CR LF and CR as LF, control characters
	removed, repeated until the text no longer changes. So ``canonical(canonical(x))`` is
	``canonical(x)``, and the host's own sanitizer is the identity on the sent text.
	U+0000 raises ``DirectiveRefused(kind="nul")``; text that is not stable after
	``_CANONICAL_ROUNDS`` rounds raises ``DirectiveRefused(kind="unstable")``."""
	for _ in range(_CANONICAL_ROUNDS):
		step = unicodedata.normalize("NFC", text)
		step = step.replace("\r\n", "\n").replace("\r", "\n")
		if "\x00" in step:
			raise DirectiveRefused("nul")
		step = _CONTROL.sub("", step)
		if step == text:
			return text
		text = step
	raise DirectiveRefused("unstable")


def canonical_added(text: str) -> str:
	"""``canonical`` for content that jarvis added: U+0000 is removed, not refused."""
	return canonical(text.replace("\x00", ""))


def typed_carries_directive(text: str) -> bool:
	"""True when canonical typed text holds a ``/model`` token anywhere (FQ8-D1), or
	a slash token together with a runtime option (the alias guard)."""
	if MODEL_TOKEN.search(text):
		return True
	return bool(SLASH_TOKEN.search(text) and RUNTIME_OPTION.search(text))


def neutralize(message: str) -> str:
	"""Replace the ``/`` of every ``/model`` token with U+2215. Other text is unchanged."""
	return MODEL_TOKEN.sub(lambda m: DIVISION_SLASH + m.group(0)[1:], message)


def _refuse(text: str) -> None:
	frappe.throw(text, DirectiveRefusal)


def clean_added(text: str) -> str:
	"""Content that jarvis added, made safe: canonical, with every ``/model`` token neutralized.
	Text that has no stable canonical form is refused with ``NUL_TEXT`` (fail closed)."""
	try:
		return neutralize(canonical_added(text))
	except DirectiveRefused:
		_refuse(NUL_TEXT)


def refuse_typed(text: str) -> str:
	"""The canonical form of typed text, or a ``DirectiveRefusal`` (``NUL_TEXT`` for
	U+0000, ``REFUSAL_TEXT`` for a directive)."""
	try:
		canon = canonical(text)
	except DirectiveRefused:
		_refuse(NUL_TEXT)
	if typed_carries_directive(canon):
		_refuse(REFUSAL_TEXT)
	return canon


def assert_clean(message: str) -> str:
	"""The post-check on a finished message: canonical already, and no ``/model``
	token. No rewrite, so a byte budget that was fitted earlier does not move."""
	try:
		clean = canonical(message) == message
	except DirectiveRefused as exc:
		_refuse(NUL_TEXT if exc.kind == "unstable" else REFUSAL_TEXT)
	if not clean or MODEL_TOKEN.search(message):
		_refuse(REFUSAL_TEXT)
	return message


def guard_turn_message(typed: str, message: str) -> str:
	"""The text to send for one user turn: refuse typed directives, neutralize the
	rest, and verify. ``message`` is the whole assembled prompt, ``typed`` the
	user's own words inside it."""
	refuse_typed(typed)
	return assert_clean(clean_added(message))
