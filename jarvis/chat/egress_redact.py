"""Generic outbound-text redactor for the chat egress path.

Scrubs a CONFIGURED set of patterns out of the agent's reply before it reaches the
customer: every match is DROPPED (removed). This module INTENTIONALLY carries no
patterns of its own - the rules are supplied by the caller (sourced from
control-plane config), so it names nothing and reveals nothing even when the app is
read publicly. (An earlier version also supported a "rename the match to the
tenant's whitelabel name" mode; a staging red-team showed renaming MISLABELS rather
than protects - a "<brand> <version>" banner became "<whitelabel> <version>", so the
version leaked, just relabelled - so the redactor is now REMOVE-ONLY. The
conversational brand case
is owned by the persona; see egress_rules / the control-plane _egress_patterns.)

It runs on the chat delivery hot path, so it is TOTAL and FAIL-OPEN: any internal
error returns the input unchanged - it never raises and never drops a reply.

Patterns are OPERATOR-authored (control-plane config), not customer/attacker input,
so they are trusted to be simple and anchored. ``compile_rules`` still rejects
uncompilable, empty-matchable (would match at every position), and non-remove rules
defensively. A catastrophic-backtracking (ReDoS) pattern would still hang - the
stdlib ``re`` has no timeout - so keep configured patterns literal/anchored; that
residual is accepted given patterns are operator-controlled."""

from __future__ import annotations

import re

#: The only rule mode: drop the match entirely. A rule whose mode is anything else
#: is skipped by compile_rules - the redactor never renames.
MODE_REMOVE = "remove"

#: A non-empty, name-free placeholder delivered when redaction would ENTIRELY empty
#: a non-empty message (see the INVARIANT in redact_egress). Never a tenant name -
#: this module names nothing.
_COLLAPSED_PLACEHOLDER = "…"

#: Bound on the redaction fixpoint (below): re-run the rules until the text stops
#: changing, but never more than this many passes so a pathological rule set can't
#: spin. A vetted rule set stabilises in 1-2 passes.
_MAX_PASSES = 5


def compile_rules(raw_rules) -> list:
	"""Compile ``[(pattern_str, mode), ...]`` into ``[compiled_regex, ...]``.

	A rule is SKIPPED (never fatal, so one bad config entry can't disable the whole
	redactor) when its pattern does not compile, when its mode is not ``remove`` (the
	redactor never renames, so a non-remove rule is dropped rather than applied), or
	when it can match the EMPTY string (which would otherwise match at every position
	and corrupt the reply). The empty-match check catches literal zero-width patterns
	(e.g. ``a*``) but not a pure lookaround (``(?<=x)``); those are accepted under the
	operator-authored trust model (patterns are code-reviewed control-plane config,
	never customer input)."""
	compiled = []
	# Tolerate a non-list raw_rules (a malformed cache blob) without raising here —
	# a truthy non-iterable would otherwise blow up the whole compile.
	for entry in raw_rules if isinstance(raw_rules, (list, tuple)) else []:
		try:
			pattern, mode = entry
			if mode != MODE_REMOVE:  # remove-only; a non-remove rule is skipped, never applied
				continue
			rx = re.compile(pattern, re.IGNORECASE)
			if rx.search(""):  # empty-matchable -> would match at every position
				continue
			compiled.append(rx)
		except Exception:
			continue
	return compiled


def redact_egress(text, rules):
	"""Apply ``rules`` (``[compiled_regex, ...]``) to ``text``, DROPPING every match.

	Returns ``(redacted_text, hit)`` where ``hit`` is True iff the text actually
	changed (the caller's tripwire reads ``hit``; computed by comparison, not by a
	match count, so an unchanged text never fires a spurious tripwire). INVARIANT: a
	non-empty input never returns empty — content that reduces entirely to nothing
	falls back to a name-free placeholder (see below), so a delivered message is
	never blanked by redaction.

	TOTAL / FAIL-OPEN: any error returns ``(text, False)`` unchanged - a redactor bug
	must never break message delivery on the hot path. The rules are re-run to a
	bounded fixpoint because dropping a match can splice text into a NEW match an
	earlier rule would have caught - one pass is not enough to guarantee no token
	survives, and re-running is what makes ``redact(redact(x)) == redact(x)``."""
	try:
		if not isinstance(text, str) or not text or not rules:
			return (text, False)
		out = text
		for _ in range(_MAX_PASSES):
			before = out
			for compiled in rules:
				out = compiled.sub("", out)
			if out == before:
				break
		# INVARIANT: a non-empty input never collapses to nothing. A reply / error /
		# recovered text that was ENTIRELY removable tokens (a lone glyph, a bare brand
		# URL) would otherwise blank the chat card and, on the recovery path, be read as
		# "no output yet" and hang the turn to a false timeout. Substitute a name-free
		# placeholder so the message stays non-empty.
		if text.strip() and not out.strip():
			out = _COLLAPSED_PLACEHOLDER
		return (out, out != text)
	except Exception:
		return (text, False)
