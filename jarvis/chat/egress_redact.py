"""Generic outbound-text redactor for the chat egress path.

Scrubs a CONFIGURED set of patterns out of the agent's reply before it reaches
the customer: brand-family matches become the tenant's whitelabel name, others
are dropped. This module INTENTIONALLY carries no patterns of its own - the rules
are supplied by the caller (sourced from control-plane config in a later slice),
so it names nothing and reveals nothing even when the app is read publicly.

It runs on the chat delivery hot path, so it is TOTAL and FAIL-OPEN: any internal
error returns the input unchanged - it never raises and never drops a reply.

Patterns are OPERATOR-authored (control-plane config), not customer/attacker
input, so they are trusted to be simple and anchored. ``compile_rules`` still
rejects uncompilable, empty-matchable (would insert at every position), and
unknown-mode rules defensively. A catastrophic-backtracking (ReDoS) pattern would
still hang - the stdlib ``re`` has no timeout - so keep configured patterns
literal/anchored; that residual is accepted given patterns are operator-controlled."""

from __future__ import annotations

import re

#: Fallback replacement when the tenant has no whitelabel name (or it is unsafe).
_DEFAULT_REPLACEMENT = "Jarvis"

#: Rule modes.
MODE_NAME = "name"  # replace the match with the whitelabel name
MODE_REMOVE = "remove"  # drop the match entirely

#: Bound on the redaction fixpoint (below): re-run the rules until the text stops
#: changing, but never more than this many passes so a pathological rule set can't
#: spin. A vetted rule set stabilises in 1-2 passes.
_MAX_PASSES = 5


def compile_rules(raw_rules) -> list:
	"""Compile ``[(pattern_str, mode), ...]`` into ``[(compiled_regex, mode), ...]``.

	A rule is SKIPPED (never fatal, so one bad config entry can't disable the whole
	redactor) when its pattern does not compile, when its mode is unknown, or when
	it can match the EMPTY string (which would otherwise insert the replacement at
	every position and corrupt the reply)."""
	compiled = []
	for entry in raw_rules or []:
		try:
			pattern, mode = entry
			if mode not in (MODE_NAME, MODE_REMOVE):
				continue
			rx = re.compile(pattern, re.IGNORECASE)
			if rx.search(""):  # empty-matchable -> would splice the name everywhere
				continue
			compiled.append((rx, mode))
		except Exception:
			continue
	return compiled


def redact_egress(text, rules, replacement=None):
	"""Apply ``rules`` (``[(compiled_regex, mode), ...]``) to ``text``.

	Returns ``(redacted_text, hit)`` where ``hit`` is True iff the text actually
	changed (the caller's tripwire reads ``hit``; computed by comparison, not by a
	match count, so a replace-with-same-value never fires a spurious tripwire).

	TOTAL / FAIL-OPEN: any error returns ``(text, False)`` unchanged - a redactor
	bug must never break message delivery on the hot path. The match is inserted via
	a CALLABLE replacement, so a ``replacement`` carrying regex-replacement
	metacharacters (``\\1``, ``\\g<0>``, a lone backslash) is written LITERALLY. The
	rules are re-run to a bounded fixpoint because a REMOVE rule can splice text into
	a NEW match an earlier rule would have caught - one pass is not enough to
	guarantee no brand token survives, and re-running is what makes
	``redact(redact(x)) == redact(x)``. A whitelabel ``replacement`` that itself
	trips a rule is swapped for a rule-safe default so it can't compound."""
	try:
		if not isinstance(text, str) or not text or not rules:
			return (text, False)
		name = str(replacement or "").strip() or _DEFAULT_REPLACEMENT
		if any(compiled.search(name) for compiled, _mode in rules):
			name = _DEFAULT_REPLACEMENT
		out = text
		for _ in range(_MAX_PASSES):
			before = out
			for compiled, mode in rules:
				repl = "" if mode == MODE_REMOVE else name
				out = compiled.sub(lambda _m, r=repl: r, out)
			if out == before:
				break
		return (out, out != text)
	except Exception:
		return (text, False)
