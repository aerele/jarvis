"""Step text: what the model says right before a lookup.

While a turn runs, the chat shows the latest step as one live line, and the line
disappears when the answer lands. The runtime delivers step text in two shapes:

- the ChatGPT-subscription harness sends it as a separate update that never
  reaches the final reply;
- every other connection (API keys, Claude subscription) streams it as ordinary
  reply text right before a tool call, so it also ends up in the final reply.

``relay_mux`` records the second kind as it streams and calls ``strip_steps`` on
the final reply so the saved answer holds only the answer.
"""

from __future__ import annotations

import re

# One line in the chat's activity row.
MAX_STEP_CHARS = 160

_TABLE_RULE = re.compile(r"^[\s|:\-]+$")
_LEADING_MARK = re.compile(r"^(?:#{1,6}\s+|[-*+]\s+|>\s*|\d+[.)]\s+)")
_INLINE_MARK = re.compile(r"\*\*|__|`")


def display_line(text: str) -> str:
	"""Turn raw step text into the one line the chat shows.

	Takes the last prose line (a model that drafted a table and then stopped to
	look something up ends on the sentence that says so), drops markdown marks
	and truncates. Returns "" when there is no prose to show.
	"""
	lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
	prose = [line for line in lines if not line.startswith("|") and not _TABLE_RULE.match(line)]
	if not prose:
		return ""
	line = _INLINE_MARK.sub("", _LEADING_MARK.sub("", prose[-1])).strip()
	if len(line) > MAX_STEP_CHARS:
		line = line[: MAX_STEP_CHARS - 1].rstrip() + "…"
	return line


def visible_text(text: str, steps: list[str]) -> str:
	"""``text`` with recorded steps removed from its front, in order, while each
	one is an exact prefix. May be "" (the text so far was all steps).

	Covers both streaming shapes: the Claude CLI's one growing text starts with
	its steps, while an API-key model's fresh per-call segment does not, so it
	comes back unchanged.
	"""
	text = text or ""
	rest = text
	for step in steps:
		body = rest.lstrip()
		if not step or not body.startswith(step):
			break
		rest = body[len(step) :]
	return text if rest is text else rest.lstrip()


def strip_steps(final: str | None, steps: list[str]) -> str | None:
	"""The saved reply: ``final`` minus its leading step text.

	A reply that does not carry the steps (the ChatGPT-subscription harness
	never does) comes back unchanged. If removing them would leave nothing, the
	text written before the last tool call was the answer itself, so the reply
	is kept whole.
	"""
	if not final or not steps:
		return final
	return visible_text(final, steps) or final
