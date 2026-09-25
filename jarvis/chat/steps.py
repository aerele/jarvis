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
