"""Best-effort source revision details for this Jarvis checkout."""

import re
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_KEYS = ("branch", "tag")

# Only answers Git actually gave are kept; an unknown value is asked again on the next call.
_known: dict[str, str] = {}


def source_details() -> dict[str, str | None]:
	"""Return the checked-out branch and exact ``vN.N.N`` release tag.

	Three outcomes per value, and the difference matters to the control plane: a non-empty
	string is the revision, an empty string means the checkout has none (detached,
	untagged, not a Git checkout), and ``None`` means the checkout could not be read, so the
	value is unknown and must not be reported as blank. Nothing is ever inferred from ``__version__``,
	so support never sees a made-up revision.
	"""
	details: dict[str, str | None] = {}
	for key in _KEYS:
		if key not in _known:
			value = _read_value(key)
			if value is None:
				details[key] = None
				continue
			_known[key] = value
		details[key] = _known[key]
	return details


def reset_cache() -> None:
	_known.clear()


def _read_value(key: str) -> str | None:
	"""``key`` read from the checkout with GitPython (a Frappe dependency). GitDB reads
	refs and objects in Python, so no git process is started."""
	try:
		import git
	except ImportError:  # GitPython refuses to import without a usable git executable
		return None
	try:
		repo = git.Repo(_REPOSITORY_ROOT, odbt=git.GitDB)
		if key == "branch":
			return "" if repo.head.is_detached else repo.head.reference.name
		return _release_tag(repo)
	except (git.InvalidGitRepositoryError, git.NoSuchPathError):
		return ""
	except Exception:
		return None


def _release_tag(repo) -> str:
	"""The highest ``vN.N.N`` tag on HEAD, or "" (also for a repository with no commits)."""
	if not repo.head.is_valid():
		return ""
	head = repo.head.commit.hexsha
	tags = []
	for tag in repo.tags:
		match = _RELEASE_TAG.match(tag.name)
		if match and tag.commit.hexsha == head:
			tags.append((tuple(map(int, match.groups())), tag.name))
	return max(tags)[1] if tags else ""
