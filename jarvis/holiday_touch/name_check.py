"""Check the extra holiday names the control plane sends, before they are stored.

The bench does not trust admin: only the nine design keys count, a name is 3-60 characters
after ``match.normalise``, and there are at most 200 names in all. ``birthday`` is a design
but never a holiday row, so it takes no aliases (admin refuses it too). A bad entry is dropped
and counted (``NameCheck.dropped``); a value of the wrong shape is ``None`` (malformed).
Names come back normalised, sorted and unique, so equal input always stores equal text.

Pure: no database, no logging. ``policy`` decides what to store and report.
"""

from __future__ import annotations

from jarvis.holiday_touch.match import normalise
from jarvis.holiday_touch.names import DESIGNS

ALIAS_DESIGNS = tuple(d for d in DESIGNS if d != "birthday")
MAX_NAMES = 200
MIN_CHARS = 3
MAX_CHARS = 60


class NameCheck:
	def __init__(self):
		self.dropped = 0

	def aliases(self, value) -> dict | None:
		"""``{design: [names]}`` cleaned; None when ``value`` is not a dict."""
		if not isinstance(value, dict):
			return None
		found = {}
		for design, names in value.items():
			if design not in ALIAS_DESIGNS or not isinstance(names, list):
				self.dropped += 1
				continue
			if cleaned := self._names(names):
				found[design] = cleaned
		return {d: found[d] for d in ALIAS_DESIGNS if d in found}

	def near_misses(self, value) -> list | None:
		"""The cleaned name list; None when ``value`` is not a list."""
		return self._names(value) if isinstance(value, list) else None

	def fit(self, names: dict) -> dict:
		"""``names`` cut to ``MAX_NAMES`` in all: aliases first (design order), then near-misses."""
		room, aliases = MAX_NAMES, {}
		for design, found in names["aliases"].items():
			if room > 0:
				aliases[design] = found[:room]
			room -= len(found)
		near_misses = names["near_misses"][: max(room, 0)]
		self.dropped += _count(names) - _count({"aliases": aliases, "near_misses": near_misses})
		return {"aliases": aliases, "near_misses": near_misses}

	def _names(self, names: list) -> list:
		self.dropped += max(0, len(names) - MAX_NAMES)  # an oversize list is never walked whole
		kept = set()
		for name in names[:MAX_NAMES]:
			tidy = " ".join(normalise(name))
			if MIN_CHARS <= len(tidy) <= MAX_CHARS:
				kept.add(tidy)
			else:
				self.dropped += 1
		return sorted(kept)


def _count(names: dict) -> int:
	return sum(len(v) for v in names["aliases"].values()) + len(names["near_misses"])
