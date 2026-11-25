"""RFC 7636 PKCE: code verifier + S256 challenge. Pure, no I/O, no imports
from the rest of this package.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

# The only challenge method OAuth 2.1 (and this client) supports. "plain" is
# not implemented - it exists in the RFC only for constrained clients that
# cannot compute SHA-256, which does not describe us.
METHOD = "S256"

_MIN_LEN = 43
_MAX_LEN = 128


def new_verifier() -> str:
	"""A code verifier: 43-128 characters from the RFC 7636 unreserved set
	(``secrets.token_urlsafe`` only ever emits ``[A-Za-z0-9_-]``, a subset of
	it). ``token_urlsafe(64)`` yields ~86 characters - already inside the
	43-128 window - the trim/pad below is a defensive belt for any future
	change to the generation length, not a path exercised today."""
	verifier = secrets.token_urlsafe(64)[:_MAX_LEN]
	while len(verifier) < _MIN_LEN:
		verifier += secrets.token_urlsafe(8)
	return verifier[:_MAX_LEN]


def challenge(verifier: str) -> str:
	"""``S256`` challenge: base64url(sha256(verifier)) with no padding."""
	digest = hashlib.sha256(verifier.encode("ascii")).digest()
	return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
