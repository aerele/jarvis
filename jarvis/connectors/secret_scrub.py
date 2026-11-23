"""Credential scrubbing for third-party text that reaches the agent, a log or the
transcript. NO frappe import.

Judges VALUES by shape, never by field name: a Cloudflare KV ``key`` or an
``issue_key`` is harmless, an AWS key id inside a presigned URL is not.
:func:`is_secret_key` is the matching name test (whole words, not substrings), for
callers that mask a value by its key. Every pattern is shape-anchored; there is
deliberately no generic "long string" rule, so ordinary ids are never mangled."""

from __future__ import annotations

import re
from itertools import pairwise

from jarvis.learning.source_guard import REDACTION, redact_secrets

# Query parameters that carry a credential or a signature (presigned and SAS URLs,
# OAuth redirects). The name stays, the value goes. ``\\u0026`` is a JSON-escaped ``&``.
_URL_PARAM_RE = re.compile(
	r"(?i)((?:[?&;]|\\u0026)(?:x-amz-credential|x-amz-signature|x-amz-security-token"
	r"|x-goog-credential|x-goog-signature|awsaccesskeyid|signature|sig"
	r"|access_token|id_token|refresh_token)=)([^&\s\"'<>\\]+)"
)

# A JSON string value under a secret-named key (``"access_token": "..."``). The
# source_guard assignment rule misses it: the closing quote of the key sits before the colon.
_JSON_PAIR_RE = re.compile(r'"([A-Za-z0-9_\- ]{1,40})"(\s*:\s*)"((?:[^"\\]|\\.){8,})"')

# Well-known token shapes, each with a fixed prefix.
_TOKEN_RES = (
	re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub
	re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
	re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),  # Stripe
	re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
	re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
	re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),  # JWT
)

_SECRET_WORDS = frozenset(
	{
		"password",
		"passwd",
		"pwd",
		"secret",
		"token",
		"credential",
		"credentials",
		"authorization",
		"bearer",
		"apikey",
		"cookie",
	}
)
# A ``token`` the agent needs to carry on (a cursor), not a credential.
_CURSOR_WORDS = frozenset(
	{"page", "next", "prev", "previous", "continuation", "cursor", "sync", "pagination"}
)
_SECRET_PAIRS = frozenset(
	{
		("api", "key"),
		("access", "key"),
		("secret", "key"),
		("private", "key"),
		("client", "secret"),
		("session", "token"),
		("encryption", "key"),
		("signing", "key"),
	}
)


def scrub_text(text) -> tuple[str, int]:
	"""``(scrubbed_text, replacement_count)``: every credential-shaped value in
	``text`` becomes ``[REDACTED-SECRET]``. Never raises."""
	if not text:
		return "", 0
	out, count = redact_secrets(str(text))
	out, n = _URL_PARAM_RE.subn(lambda m: m.group(1) + REDACTION, out)
	count += n
	for pattern in _TOKEN_RES:
		out, n = pattern.subn(REDACTION, out)
		count += n
	masked = 0

	def mask(match) -> str:
		nonlocal masked
		replaced = _mask_secret_pair(match)
		masked += replaced != match.group(0)
		return replaced

	out = _JSON_PAIR_RE.sub(mask, out)
	return out, count + masked


def scrub_value(obj):
	"""``obj`` (dicts, lists, tuples) with ``scrub_text`` applied to every string,
	keys included. Returns a new structure; other values pass through."""
	if isinstance(obj, str):
		return scrub_text(obj)[0]
	if isinstance(obj, dict):
		return {scrub_value(k): scrub_value(v) for k, v in obj.items()}
	if isinstance(obj, (list, tuple)):
		return [scrub_value(v) for v in obj]
	return obj


def is_secret_key(name) -> bool:
	"""True when the field NAME advertises a credential. The name is split into words
	(snake, kebab, camelCase). One word is secret if it is in ``_SECRET_WORDS``; two
	neighbouring words are secret if they are in ``_SECRET_PAIRS``. A bare ``key`` is
	not (``issue_key``, ``teamKey`` and a KV ``key`` are ids)."""
	words = _words(name)
	if any(w in _SECRET_WORDS for w in words):
		return True
	return any(pair in _SECRET_PAIRS for pair in pairwise(words))


def _mask_secret_pair(match) -> str:
	key, colon, value = match.groups()
	if not is_secret_key(key) or value == REDACTION or _CURSOR_WORDS & set(_words(key)):
		return match.group(0)
	return f'"{key}"{colon}"{REDACTION}"'


def _words(name) -> list[str]:
	spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
	spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
	return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]
