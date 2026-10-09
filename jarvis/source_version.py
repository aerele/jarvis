"""Best-effort source revision details for this Jarvis checkout."""

import re
import subprocess
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_SECONDS = 2
_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
_QUERIES = {
	"branch": ("branch", "--show-current"),
	"tag": ("describe", "--tags", "--exact-match", "--match", "v[0-9]*", "HEAD"),
}

# Only answers Git actually gave are kept; an unknown value is asked again on the next call.
_known: dict[str, str] = {}


def source_details() -> dict[str, str | None]:
	"""Return the checked-out branch and exact ``vN.N.N`` release tag.

	Three outcomes per value, and the difference matters to the control plane: a non-empty
	string is the revision, an empty string means Git answered and there is none (detached,
	untagged, not a Git checkout), and ``None`` means Git did not answer in time, so the value
	is unknown and must not be reported as blank. Nothing is ever inferred from ``__version__``,
	so support never sees a made-up revision.
	"""
	details: dict[str, str | None] = {}
	for key, args in _QUERIES.items():
		if key not in _known:
			value = _git_value(*args)
			if value is None:
				details[key] = None
				continue
			_known[key] = value
		details[key] = _known[key]
	return details


def reset_cache() -> None:
	_known.clear()


def _git_value(*args: str) -> str | None:
	try:
		# Fixed git argv, no shell, bounded.
		result = subprocess.run(  # nosemgrep: frappe-subprocess-exec
			["git", "-C", str(_REPOSITORY_ROOT), *args],
			capture_output=True,
			text=True,
			timeout=_GIT_TIMEOUT_SECONDS,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if result.returncode:
		return ""
	value = result.stdout.strip()
	if args[0] == "describe" and not _RELEASE_TAG.match(value):
		return ""
	return value
