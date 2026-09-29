"""Reuse complete font metadata without sharing Matplotlib's writable locks.

Each worker gets a private cache directory. Only a bounded, complete JSON font
list from a successful worker is retained as immutable process-local bytes.
An interrupted worker cannot leave a lock or partial file for the next request.
No chart, label, document or image data is retained here.
"""

import contextlib
import json
import re
from pathlib import Path

_MAX_BYTES = 2_000_000
_FONT_CACHE: tuple[str, bytes] | None = None


def _valid(name: str, payload: bytes) -> bool:
	match = re.fullmatch(r"fontlist-v([0-9]{1,4})\.json", name)
	if not match or len(payload) > _MAX_BYTES:
		return False
	try:
		data = json.loads(payload)
	except (ValueError, UnicodeError, RecursionError):
		return False
	return (
		isinstance(data, dict)
		and data.get("_version") == int(match.group(1))
		and data.get("__class__") == "FontManager"
		and isinstance(data.get("ttflist"), list)
		and isinstance(data.get("afmlist"), list)
	)


def seed_font_cache(directory: Path) -> None:
	"""Copy only validated JSON bytes; no shared configuration or lock files."""
	snapshot = _FONT_CACHE
	if snapshot and _valid(*snapshot):
		with contextlib.suppress(OSError):
			(directory / snapshot[0]).write_bytes(snapshot[1])


def remember_font_cache(directory: Path) -> None:
	"""Called only after the worker exits successfully. Publishing one immutable
	tuple is atomic; concurrent successful workers may publish equivalent data.
	Invalid/oversized metadata never replaces a previously usable snapshot.
	"""
	global _FONT_CACHE
	for path in directory.glob("fontlist-v*.json"):
		if path.is_symlink():
			continue
		try:
			with path.open("rb") as stream:
				payload = stream.read(_MAX_BYTES + 1)
		except OSError:
			continue
		if _valid(path.name, payload):
			_FONT_CACHE = (path.name, payload)
			return
