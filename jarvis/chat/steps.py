"""Step text: what the model says right before a lookup.

While a turn runs, the chat shows the latest step as one live line, and the line
disappears when the answer lands. The runtime delivers step text in two shapes:

- the ChatGPT-subscription harness sends it as a separate update that never
  reaches the final reply;
- every other connection (API keys, Claude subscription) streams it as ordinary
  reply text right before a tool call, so it also ends up in the final reply.

For the second shape ``relay_mux`` treats the text written since the last tool
call as a step only when ``is_step`` says it is one short sentence, hides it
from the live reply with ``remove_steps``, and cleans the saved reply with
``strip_steps``. Anything longer stays in the reply, as it did before.
"""

from __future__ import annotations

import re

from jarvis.chat.learned_api import _excerpt

# One line in the chat's activity row, and the longest text counted as a step.
MAX_STEP_CHARS = 160

# Longest text a RUNTIME-FLAGGED preamble (events.parse_event's kind=="step",
# the "item/kind=preamble" frame) may be and still count as a step arriving
# early in the reply (R4 owner decision, default 320). Looser than
# MAX_STEP_CHARS/is_step: the runtime already told us this text is narration,
# so a two-sentence preamble is accepted, only structural text is rejected
# (see is_preamble_step). HIDDEN LIVE ONLY: it is offered on the step line and
# taken out of the live reply the same as any step, but it is KEPT IN THE
# SAVED REPLY unless it is ALSO a one-sentence step (is_step, #1435's existing
# rule) - relay_mux._finalize_terminal filters lane.steps down to is_step
# entries before strip_steps runs (C1, code review: is_preamble_step's 320-char
# runtime-flag-alone acceptance has no safety net against silently dropping a
# real multi-sentence answer opening from the saved reply). 0 rejects every
# preamble, which restores today's pre-feature behaviour: a flagged preamble
# that is not also is_step is then treated as not-a-step by relay_mux
# (unconditionally offered, never hidden from the reply early). Keep in
# lockstep with MAX_CANDIDATE_CHARS in frontend/src/lib/liveTurn.js.
MAX_PREAMBLE_STEP_CHARS = 320

_TABLE_RULE = re.compile(r"^[\s|:\-]+$")
_LEADING_MARK = re.compile(r"^(?:#{1,6}\s+|[-*+]\s+|>\s*|\d+[.)]\s+)")
_INLINE_MARK = re.compile(r"\*\*|__|`")
# A sentence end followed by more text: "Done. Now" or "Done? Next".
_INNER_SENTENCE_END = re.compile(r"[.!?]\s+\S")


def display_line(text: str) -> str:
	"""Turn raw step text into the one line the chat shows.

	Takes the last prose line (a ChatGPT update can run to a few lines), drops
	markdown marks and truncates. Returns "" when there is no prose to show.
	"""
	lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
	prose = [line for line in lines if not line.startswith("|") and not _TABLE_RULE.match(line)]
	if not prose:
		return ""
	return _excerpt(_INLINE_MARK.sub("", _LEADING_MARK.sub("", prose[-1])), MAX_STEP_CHARS)


def is_step(text: str) -> bool:
	"""True for one short sentence of narration ("I'll count them now.").

	Deliberately narrow: two sentences, a table, a list or a long line may be
	part of the answer (an answer written before a card or chart tool call), so
	they stay in the reply.
	"""
	text = (text or "").strip()
	if not text or len(text) > MAX_STEP_CHARS or "\n" in text:
		return False
	if text.startswith(("|", "#", ">", "```")) or _LEADING_MARK.match(text):
		return False
	return not _INNER_SENTENCE_END.search(text)


def is_preamble_step(text: str) -> bool:
	"""True for prose a runtime-flagged preamble may hide as a step early.

	Looser than ``is_step``: the runtime already flagged this text as
	narration (a separate ``item/kind=preamble`` frame), so a multi-sentence
	update is accepted up to ``MAX_PREAMBLE_STEP_CHARS``. Still rejects
	anything structural (a newline, or a table/list/heading/code start) - that
	is answer shape, never narration, whatever the runtime called it.

	Governs the LIVE hide only (relay_mux._record_preamble_step): a True here
	is hidden from the live reply and offered on the step line, but is kept in
	the SAVED reply unless it is also a one-sentence step (``is_step``) - see
	MAX_PREAMBLE_STEP_CHARS's comment and relay_mux._finalize_terminal (C1).
	"""
	text = (text or "").strip()
	if not text or len(text) > MAX_PREAMBLE_STEP_CHARS or "\n" in text:
		return False
	if text.startswith(("|", "#", ">", "```")) or _LEADING_MARK.match(text):
		return False
	return True


def remove_steps(text: str, steps: list[str]) -> str:
	"""``text`` with each recorded step cut out, searched in order.

	A step that is not found is skipped, so text that never carried the steps
	(an API-key model's fresh per-call segment) comes back unchanged.
	"""
	out, pos = text or "", 0
	for step in steps:
		at = out.find(step, pos) if step else -1
		if at == -1:
			continue
		out = out[:at].rstrip() + ("\n\n" if out[:at].strip() else "") + out[at + len(step) :].lstrip()
		pos = at
	return out.strip() if out is not text else out


def collapse_steps(steps: list[str]) -> list[str]:
	"""Recorded steps with every superseded prefix replaced, order kept.

	The runtime can send a preamble again as it grows ("I checked the ledger."
	then "I checked the ledger and found stale entries."). The relay keeps only
	the longer one, but the pump's cache appends both, and handing both to
	``remove_steps`` cuts the short one first, so the long one no longer matches
	and its tail is left dangling in the reply. A longer step takes the place
	of the shorter one it extends; exact repeats and blanks are dropped.
	"""
	out: list[str] = []
	for step in steps or []:
		if not step or step in out:
			continue
		for i, kept in enumerate(out):
			if step.startswith(kept):
				out[i] = step
				break
		else:
			out.append(step)
	return out


def strip_steps(final: str | None, steps: list[str]) -> str | None:
	"""The saved reply: ``final`` minus its step text.

	Keeps ``final`` whole when the steps are not in it (the ChatGPT harness) or
	when what would remain is shorter than what was removed: then the text
	before the last lookup was most likely the answer itself (a short reply
	before a card) and must not be lost.
	"""
	if not final or not steps:
		return final
	answer = remove_steps(final, steps)
	removed = sum(len(step) for step in steps if step and step in final)
	if not answer or len(answer) < removed:
		return final
	return answer


# C2 (code review): join_segments must never insert a break that could split
# a token/URL/word. A boundary is only safe when the text right before it
# looks like the END of something (sentence punctuation, a closing quote/
# paren, or a closing markdown bold/italic mark) AND the text right after it
# looks like the START of something new (an uppercase letter, a digit, or a
# markdown block start). Neither is a proof the boundary is a real sentence
# break, but a mid-word/mid-URL boundary (lowercase both sides, e.g. "...goin"
# / "g-right-here...") satisfies neither and is left alone.
_JOIN_BOUNDARY_END_CHARS = ".!?:\"')"
_JOIN_BOUNDARY_START_CHARS = "|#-*`"
_CODE_FENCE = "```"


def _is_safe_join_boundary(before: str, after_char: str) -> bool:
	if before.endswith("**") or before.endswith("_"):
		ok_before = True
	else:
		ok_before = bool(before) and before[-1] in _JOIN_BOUNDARY_END_CHARS
	if not ok_before:
		return False
	return after_char.isupper() or after_char.isdigit() or after_char in _JOIN_BOUNDARY_START_CHARS


def join_segments(text: str | None, tails: list[str]) -> str | None:
	"""``text`` with "\\n\\n" inserted after each recorded segment tail (R6).

	An API-key model's runtime glues one call's reply straight onto the next
	with no separator (see steps.py module docstring / DEEPSEEK fixtures), so
	a boundary that was never a step (more than one sentence, stays in the
	reply) reads as one glued sentence: "...addresses.I couldn't find...".
	``tails`` is the last few characters of each earlier segment
	(relay_mux._reply_delta records one whenever a new segment starts), and
	each is searched for IN ORDER, like ``remove_steps`` - a tail that is not
	found, whose next character is already whitespace (the runtime already
	separated that boundary itself), whose position is not a safe boundary
	(``_is_safe_join_boundary``, C2 - never split a token/URL/word), or that
	sits inside an open code fence (an odd number of "```" before it), is
	left alone.

	``None``/empty ``text`` is returned unchanged (never turned into ""): a
	turn with no message content (e.g. a benign empty final) must stay
	distinguishable from one that streamed empty text.
	"""
	if not text or not tails:
		return text
	out, pos = text, 0
	for tail in tails:
		if not tail:
			continue
		at = out.find(tail, pos)
		if at == -1:
			continue
		cut = at + len(tail)
		if (
			cut < len(out)
			and not out[cut].isspace()
			and out[:cut].count(_CODE_FENCE) % 2 == 0
			and _is_safe_join_boundary(out[:cut], out[cut])
		):
			out = out[:cut] + "\n\n" + out[cut:]
			cut += 2
		pos = cut
	return out
