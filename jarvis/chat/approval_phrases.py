"""Recognises a typed approval of a parked confirmation card.

The write-safety gate parks a mutating tool call and waits for a human. Clicking
Confirm is one way to give that go-ahead; saying it is the other, because in a
chat window the natural reply to "shall I create this supplier?" is "go ahead",
not a hunt for a button.

This module decides ONLY whether a message is that go-ahead. It grants nothing:
the caller still has to find exactly one parked card owned by this user in this
conversation, and the confirmation still runs through the same owner-bound,
single-use consume the button uses.

The matching rule is deliberately blunt: the WHOLE message, once normalised,
must equal one of a short fixed list. Substring matching would be a security
regression on the one gate that stands between the model and a real ERP write.
"Yes, but change the quantity to 5" contains "yes" and is emphatically not an
approval of what is on the card, so it has to reach the model as an ordinary
message. When in doubt this returns False and the user still has the button.

Pure and import-light so the rule is unit-testable without a site.
"""

from __future__ import annotations

import re

# Whole-message approvals. Short, unambiguous, and each one a complete reply on
# its own. Anything that needs a qualifier to make sense ("fine", "sounds good
# but") is deliberately absent.
APPROVAL_PHRASES = frozenset(
	{
		"confirm",
		"confirmed",
		"yes",
		"yes please",
		"y",
		"go ahead",
		"go",
		"proceed",
		"approve",
		"approved",
		"do it",
		"ok",
		"okay",
		"sure",
	}
)

# A typed approval is a handful of characters. The cap is a cheap second guard:
# a long message is a message, whatever words it starts with.
MAX_APPROVAL_LEN = 24

_TRAILING_PUNCT = re.compile(r"[\s.!,]+$")
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
	"""Lowercase, collapse whitespace, drop trailing punctuation.

	Only trailing punctuation is stripped. A message with INTERNAL punctuation
	("yes, but wait") keeps it and therefore cannot match, which is the point.
	A question mark is not stripped either: "confirm?" is a question about the
	card, not an approval of it.
	"""
	return _TRAILING_PUNCT.sub("", _WHITESPACE.sub(" ", (text or "").strip().lower()))


def is_approval(text: str) -> bool:
	"""True when the whole message is an unambiguous go-ahead."""
	if not text:
		return False
	if len(text) > MAX_APPROVAL_LEN:
		return False
	return normalise(text) in APPROVAL_PHRASES


# ── several cards at once ──────────────────────────────────────────────────
#
# With more than one card parked, a bare "go ahead" approves ALL of them. That
# is a deliberate product decision: a user who lines up three writes and says go
# ahead means all three, and making them click three times is the friction this
# whole feature exists to remove.
#
# Saying so explicitly is supported too ("confirm all"), and so is approving
# only some of them by the number shown on each card ("confirm 1 and 3"), which
# is the escape hatch for when the answer really is "these but not that one".
# Numbers are used rather than descriptions because a card's number is on screen
# and exact, whereas matching "the supplier one" against a summary is guesswork,
# and guesswork is what this gate exists to prevent.

# The verbs that can carry a selection. Kept separate from APPROVAL_PHRASES:
# these are only meaningful with an object ("confirm 2"), never alone.
_SELECT_VERB = r"(?:confirm|approve|do|run|yes to|go ahead with|proceed with)"

# "confirm all", "yes to all", "approve all of them", "confirm both".
_ALL_RE = re.compile(rf"^(?:{_SELECT_VERB}|yes|ok|okay)?\s*(?:all(?: of them)?|both)$")

# "confirm 1", "yes to 2 and 3", "approve 1, 3", "do 1 & 2".
_SELECT_RE = re.compile(rf"^{_SELECT_VERB}\s+(\d+(?:\s*(?:,|and|&)\s*\d+)*)$")
_NUMBER_RE = re.compile(r"\d+")

# A selection can name every card, so the length cap has to grow with the list.
MAX_SELECTION_LEN = 80


def looks_like_approval(text: str) -> bool:
	"""Cheap pre-filter: could this message possibly be an approval?

	Almost every message is not, and the real answer needs the parked-card list,
	which costs a Redis read. This rejects the obvious no on length alone before
	any I/O happens. It is deliberately generous: a false yes here just means the
	precise check runs, while ``parse_approval`` remains the only authority.
	"""
	return bool(text) and len(text.strip()) <= MAX_SELECTION_LEN


def parse_approval(text: str, count: int) -> list[int] | None:
	"""Which of ``count`` parked cards this message approves.

	Returns a sorted list of 0-based indexes, or None when the message is not an
	approval at all and should reach the model as ordinary text.

	The indexes are positions in the caller's ordered card list, so the caller
	MUST order it the same way the user sees it. An out-of-range number returns
	None rather than a best guess: "confirm 4" against three cards is a
	misunderstanding, and running three writes on the strength of it would be
	exactly the wrong recovery.
	"""
	if not text or count < 1:
		return None
	raw = (text or "").strip()
	if len(raw) > MAX_SELECTION_LEN:
		return None
	norm = normalise(raw)

	# A plain go-ahead: the single card, or every card when several are parked.
	if len(raw) <= MAX_APPROVAL_LEN and norm in APPROVAL_PHRASES:
		return list(range(count))
	if _ALL_RE.match(norm):
		return list(range(count))

	m = _SELECT_RE.match(norm)
	if not m:
		return None
	picked = sorted({int(n) for n in _NUMBER_RE.findall(m.group(1))})
	if not picked or picked[0] < 1 or picked[-1] > count:
		return None
	return [n - 1 for n in picked]
