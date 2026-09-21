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


def is_sweep_all(text: str) -> bool:
	"""True when the message means 'confirm EVERYTHING currently parked' - a plain
	go-ahead ("yes", "go ahead") or an explicit all ("confirm all", "both") - as
	opposed to a numbered pick ("confirm 2"). Used by the server-truth fallback: when
	the client showed no card tokens (the cards never rendered), only a sweep-all can
	safely resolve against the server's parked set, because a NUMBER cannot be mapped
	to cards the user never saw. Mirrors ``parse_approval``'s all-branches, minus the
	count (which the fallback doesn't have)."""
	if not text:
		return False
	raw = text.strip()
	if len(raw) > MAX_SELECTION_LEN:
		return False
	norm = normalise(raw)
	if len(raw) <= MAX_APPROVAL_LEN and norm in APPROVAL_PHRASES:
		return True
	return bool(_ALL_RE.match(norm))


# ── request-scoped "confirm all" arming vocabulary (design Layer B) ──────────
#
# Arming the request-scoped bulk approval must be DELIBERATE - only an EXPLICIT
# blanket approval ("confirm all" / "do everything"), never a bare go-ahead ("yes",
# "ok", "go ahead"), and never an incidental phrase buried in prose. Two shapes:
#   * is_explicit_confirm_all  - the WHOLE message is a blanket approval (the typed
#     path: the user typed it over parked cards). A bare "yes" sweeps the visible
#     cards but does NOT arm the request's future fan-out.
#   * contains_confirm_all_directive - a COMPOUND message that bundles a request with
#     the directive as its TRAILING clause ("create 3 todos and submit them, confirm
#     all"). The upfront path, before the turn's writes hit the gate.
# Both are ONLY for ARMING - never the security gate (is_approval / parse_approval
# stay whole-message equality). A false positive only arms a per-request auto-run the
# BRAKE still cards and the next human message resets, but the anchoring below keeps
# it off ordinary prose ("confirm all the details are right", "undo everything").

# Blanket-approval phrases NOT already covered by _ALL_RE (which handles "all"/"both"
# forms). Deliberately excludes bare APPROVAL_PHRASES ("yes"/"ok"/"go").
_EXPLICIT_ALL_EXTRA = frozenset(
	{
		"do everything",
		"do it all",
		"do them all",
		"confirm everything",
		"approve everything",
		"confirm them all",
	}
)


def is_explicit_confirm_all(text: str) -> bool:
	"""True when the WHOLE (normalised) message is an EXPLICIT blanket approval -
	"confirm all", "do everything", "all", "both", "approve all of them", ... - as
	opposed to a bare go-ahead ("yes"/"ok"/"go ahead") or a numbered pick. ONLY this
	arms the request-scoped bulk approval on the typed path; a bare go-ahead still
	sweeps the visible cards (is_sweep_all) but must NOT pre-authorize the request's
	future fan-out (design Layer B: the arming directive is "confirm all"/"do
	everything", not any yes)."""
	if not text:
		return False
	norm = normalise(text)
	return bool(norm) and (bool(_ALL_RE.match(norm)) or norm in _EXPLICIT_ALL_EXTRA)


# Negations / qualifiers that flip or scope the directive -> do NOT arm.
_DIRECTIVE_NEGATIONS = (
	"don't",
	"do not",
	"dont",
	"never",
	"cannot",
	"can't",
	"cant",
	"shouldn't",
	"should not",
	"must not",
	"won't",
	"will not",
	"without confirm",
	"no need",
)
_DIRECTIVE_EXCEPTIONS = ("except", "but not", "excluding", "other than", "apart from")
# The explicit directives as a TRAILING, word-boundary-anchored clause. Word-boundary
# (^ or a space/clause-delimiter before the phrase) keeps "undo everything" / "redo it
# all" from matching; the trailing `$` keeps "do everything needed to ship it" and
# "confirm all the details are right" from matching (the directive is not at the end).
_TRAILING_DIRECTIVE_RE = re.compile(
	r"(?:^|[\s,.;:])"
	r"(?:confirm all(?: of them| of these)?|approve all(?: of them)?|"
	r"do everything|do it all|do them all|confirm everything|approve everything|"
	r"confirm them all)$"
)
# A bundled directive rides a request, which is short-to-medium; cap the WHOLE message
# so a pasted email / document with an incidental phrase can never arm.
MAX_DIRECTIVE_LEN = 240


def contains_confirm_all_directive(text: str) -> bool:
	"""True when a COMPOUND message bundles a request with an explicit confirm-all
	directive as its TRAILING clause ("create 3 todos and submit them, confirm all").
	Used ONLY to ARM the request-scoped bulk approval upfront - NEVER the security gate.

	Deliberately blunt but SAFE, unlike a raw substring test (which this module's own
	docstring forbids on the gate):
	  * the directive must be a TRAILING, word-boundary-anchored clause, so
	    "undo everything I just did", "do everything needed to ship it", and
	    "confirm all the details are right" do NOT match;
	  * a BARE whole-message directive returns False here (the typed sweep path owns
	    that; with nothing parked there is nothing to scope);
	  * length-capped (a pasted email/document never arms on an incidental phrase);
	  * bailed on a question, a negation ("never"/"cannot"/...), or an exception
	    qualifier ("except"/"but not"/...).
	A false positive would only arm a per-request auto-run the BRAKE still cards and the
	next human message resets - but the anchoring keeps it off ordinary prose."""
	if not text or "?" in text:
		return False
	if len(text) > MAX_DIRECTIVE_LEN:
		return False
	norm = normalise(text)
	if not norm:
		return False
	if any(neg in norm for neg in _DIRECTIVE_NEGATIONS):
		return False
	if any(exc in norm for exc in _DIRECTIVE_EXCEPTIONS):
		return False
	if is_explicit_confirm_all(norm):
		return False  # a bare directive - the typed path's job; nothing to bundle-arm
	return bool(_TRAILING_DIRECTIVE_RE.search(norm))


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
