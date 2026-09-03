"""Versions relative to the installed one, for release-notice tests.

A notice is "active" only while its version is above ``jarvis.__version__``.
Tests that hard-code ``0.0.2`` as "newer" hold on develop (``0.0.1``) and break
on the stable lines (``16.x`` / ``15.x``), so derive the test versions instead.
"""

from jarvis import __version__
from jarvis.release_notice import _version


def above(by: int = 1) -> str:
	"""A version `by` patch levels above the installed one."""
	major, minor, patch = _version(__version__)
	return f"{major}.{minor}.{patch + by}"


NEWER = above(1)  # a release this bench has not reached
NEWER_2 = above(2)
