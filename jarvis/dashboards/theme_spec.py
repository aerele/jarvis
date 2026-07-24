"""Load the curated dashboard theme specs
(``jarvis/dashboards/themes/<key>/theme.json``).

Pure + frappe-free so the validator can be unit-tested standalone. The
label<->key mapping mirrors ``frontend/src/lib/dashboardThemes.js`` (the DocType
stores the capitalized label; the specs are keyed lowercase). ``theme.json``'s
``render_tokens`` mirror the ``--jd-*`` custom properties that file's ``VARS``
emits — a node sync-guard test fails loudly if the two drift.
"""

from __future__ import annotations

import json
import pathlib
from functools import lru_cache

_THEMES_DIR = pathlib.Path(__file__).resolve().parent / "themes"

# Curated theme keys (lowercase). "custom" is the bespoke escape hatch — it has
# NO theme.json (no design spec); the validator applies only the safety bans.
CURATED_KEYS = ("jarvis", "insight", "claude", "graphite")
CUSTOM_KEY = "custom"
DEFAULT_KEY = "jarvis"


@lru_cache(maxsize=None)
def load_theme(key: str) -> dict | None:
	"""Return the parsed ``theme.json`` for a lowercase key, or ``None`` when
	there is no spec (the bespoke ``custom`` theme, or an unknown key)."""
	k = (key or "").strip().lower()
	if k not in CURATED_KEYS:
		return None
	try:
		return json.loads((_THEMES_DIR / k / "theme.json").read_text())
	except (OSError, ValueError):
		return None


def theme_key(label: str) -> str:
	"""Map a DocType theme label (or key) to the lowercase spec key. Unknown ->
	the default ('jarvis'); ``custom`` is preserved (it has no spec)."""
	k = (label or "").strip().lower()
	if k == CUSTOM_KEY:
		return CUSTOM_KEY
	return k if k in CURATED_KEYS else DEFAULT_KEY
