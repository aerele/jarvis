"""Matching a Holiday row description to a design occasion.

Pure: no database, no site config. The caller resolves the row's date and the company
country; this module only answers "which occasion, if any, does this text name".

Steps: normalise, mask near-miss phrases, compare every alias against every window of
the same token count (typo-tolerant, whole words only), apply the date guards, and give
up on a tie between two different designs.
"""

from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache

from jarvis.holiday_touch.names import ALIASES, DATE_GUARDS, NEAR_MISSES, OCCASIONS

_MAX_CHARS = 300
_MAX_RAW_CHARS = 5000  # bounds every regex pass; a Holiday description is a user-edited LONGTEXT
_TAGS = re.compile(r"<[^<>]*>")
_NON_ALNUM = re.compile(r"[\W_]+")


def normalise(text) -> list[str]:
	"""One function for row text, seed lists and admin names: strip html, unescape, fold
	compatibility forms (NFKC: full-width letters, ligatures; accents are kept), drop the
	invisible format characters (zero-width space and joiners, soft hyphen, direction marks:
	Unicode category Cf) so they join a word instead of splitting it, lowercase, turn
	everything that is not a letter or digit into a space, cap at 300 chars (a word the cap
	would cut in half is dropped, so the cut cannot invent a match)."""
	if not text or not isinstance(text, str):
		return []
	plain = _fold(html.unescape(_TAGS.sub(" ", _raw_cut(text)))).lower()
	flat = _NON_ALNUM.sub(" ", plain).strip()
	tokens = flat[:_MAX_CHARS].split()
	if len(flat) > _MAX_CHARS and flat[_MAX_CHARS] != " ":
		tokens.pop()
	return tokens


def _fold(text: str) -> str:
	return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")


def _raw_cut(text: str) -> str:
	"""The first 5000 chars. A tag the cut left open (a last ``<`` after the last ``>``) is
	dropped whole, or its attribute text would be read as words (linear, no regex)."""
	raw = text[:_MAX_RAW_CHARS]
	if len(text) > _MAX_RAW_CHARS:
		start = raw.rfind("<")
		if start > raw.rfind(">"):
			raw = raw[:start]
	return raw


def match_occasion(description, on_date, country, extra=None) -> str | None:
	"""The occasion key ("diwali", "republic", ...) the description names on ``on_date``
	for a company in ``country``, or None.

	``extra`` is the admin overlay ``{"aliases": {occasion: [names]}, "near_misses": [names]}``
	(validated elsewhere; here it is only normalised and merged for this call).
	"""
	tokens = normalise(description)
	if not tokens:
		return None
	# By design ``country`` is the COMPANY's country (see ``resolve``): the India-only dates
	# (Republic Day, Independence Day) are for companies in India.
	is_india = (country or "").strip().lower() == "india"
	return _match(" ".join(tokens), on_date.month, on_date.day, is_india, _extra_key(extra))


def _extra_key(extra):
	"""A hashable, normalised form of the admin overlay, so the memo keys on content."""
	if not isinstance(extra, dict):
		return ((), ())
	aliases = []
	raw = extra.get("aliases")
	for occasion, names in raw.items() if isinstance(raw, dict) else ():
		if occasion not in OCCASIONS or not isinstance(names, (list, tuple)):
			continue
		for name in names:
			tokens = tuple(normalise(name))
			if tokens:
				aliases.append((occasion, tokens))
	raw = extra.get("near_misses")
	near_misses = [
		tokens
		for tokens in (tuple(normalise(n)) for n in (raw if isinstance(raw, (list, tuple)) else ()))
		if tokens
	]
	return tuple(sorted(set(aliases))), tuple(sorted(set(near_misses)))


@lru_cache(maxsize=64)
def _tables(extra_key):
	"""(alias forms, near-miss phrases) with the overlay merged in.

	Alias forms are (occasion, token count, letters-only form, allowed edits). Near-misses
	are longest first so "eid e milad" is removed whole instead of leaving "eid" behind."""
	aliases = [(occasion, tuple(normalise(name))) for occasion, names in ALIASES.items() for name in names]
	aliases += list(extra_key[0])
	forms = []
	for occasion, tokens in dict.fromkeys(aliases):
		letters = "".join(tokens)
		forms.append((occasion, len(tokens), letters, _allowed_edits(len(letters))))
	near_misses = {tuple(normalise(n)) for n in NEAR_MISSES} | set(extra_key[1])
	return tuple(forms), tuple(sorted(near_misses, key=lambda t: (-len(t), t)))


def _allowed_edits(letters: int) -> int:
	if letters <= 5:
		return 0
	return 1 if letters <= 8 else 2


@lru_cache(maxsize=2048)
def _match(text, month, day, is_india, extra_key):
	forms, near_misses = _tables(extra_key)
	segments = _mask(text.split(), near_misses)
	found = set()
	for occasion, count, letters, edits in forms:
		if occasion not in found and _present(segments, count, letters, edits):
			found.add(occasion)
	found = {o for o in found if _guard_ok(o, month, day, is_india)}
	if len({OCCASIONS[o] for o in found}) != 1:
		return None  # nothing, or two different designs (a tie)
	return sorted(found)[0]


def _mask(tokens, near_misses):
	"""Split ``tokens`` into runs, dropping every near-miss span. A removed span is a hard
	boundary, so tokens on its two sides are never joined into a window."""
	segments, run, i = [], [], 0
	while i < len(tokens):
		hit = next((nm for nm in near_misses if tuple(tokens[i : i + len(nm)]) == nm), None)
		if hit:
			segments.append(run)
			run, i = [], i + len(hit)
		else:
			run.append(tokens[i])
			i += 1
	segments.append(run)
	return [s for s in segments if s]


def _present(segments, count, letters, edits):
	"""Does an alias with ``count`` tokens appear in any segment, as exactly ``count``
	whole tokens, compared with the spaces stripped (so "x mas" meets "xmas")?"""
	for tokens in segments:
		for i in range(len(tokens) - count + 1):
			window = "".join(tokens[i : i + count])
			if abs(len(window) - len(letters)) <= edits and _within(window, letters, edits):
				return True
	return False


def _guard_ok(occasion, month, day, is_india) -> bool:
	guard = DATE_GUARDS.get(occasion)
	if guard is None:
		return True
	g_month, g_day, india_only = guard
	return (month, day) == (g_month, g_day) and (is_india or not india_only)


def _within(a: str, b: str, limit: int) -> bool:
	"""Damerau-Levenshtein (optimal string alignment) distance of ``a`` and ``b`` is at
	most ``limit``. Gives up as soon as a whole row exceeds the limit."""
	if a == b:
		return True
	if limit == 0:
		return False
	prev2, prev = None, list(range(len(b) + 1))
	for i in range(1, len(a) + 1):
		row = [i] + [0] * len(b)
		for j in range(1, len(b) + 1):
			cost = 0 if a[i - 1] == b[j - 1] else 1
			row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost)
			if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
				row[j] = min(row[j], prev2[j - 2] + 1)
		if min(row) > limit:
			return False
		prev2, prev = prev, row
	return prev[-1] <= limit
