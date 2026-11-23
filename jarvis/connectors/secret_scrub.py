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
	r"|access_token|id_token|refresh_token|token|api_key|apikey|client_secret|password)=)([^&\s\"'<>\\]+)"
)

# A JSON string value under a secret-named key (``"access_token": "..."``), also when the
# JSON itself sits escaped inside a JSON string (``\\"access_token\\":\\"...\\"``). The
# source_guard assignment rule misses both: the closing quote of the key sits before the colon.
_JSON_PAIR_RES = (
	re.compile(r'(")([A-Za-z0-9_\- ]{1,40})"(\s*:\s*)"((?:[^"\\]|\\.)+)"'),
	re.compile(r'(\\")([A-Za-z0-9_\- ]{1,40})\\"(\s*:\s*)\\"((?:[^"\\]|\\(?!"))+)\\"'),
)

# A private key block is found with two linear searches, not one lazy ``.*?`` pattern:
# an answer of many BEGIN lines and no END would make that pattern quadratic.
_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

# Well-known token shapes, each with a fixed prefix.
_TOKEN_RES = (
	re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub
	re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
	re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),  # Stripe
	re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
	re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
	re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),  # JWT
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
	{"page", "next", "prev", "previous", "continuation", "cursor", "sync", "pagination", "resume"}
)
# ``authorization_url``, ``api_key_id``: a name ending in one of these is a locator, not the secret.
_LOCATOR_WORDS = frozenset({"url", "uri", "link", "id"})
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
	out, count = _mask_pem_blocks(str(text))
	out, n = redact_secrets(out)
	count += n
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

	for pattern in _JSON_PAIR_RES:
		out = pattern.sub(mask, out)
	return out, count + masked


def scrub_value(obj):
	"""``obj`` scrubbed: see :func:`scrub_counted`."""
	return scrub_counted(obj)[0]


def scrub_counted(obj) -> tuple[object, int]:
	"""``(scrubbed, replacement_count)``. Walks dicts, lists and tuples and applies
	``scrub_text`` to every string, keys included. A string under a field NAMED like a
	credential is masked whole (the same rule the text path applies to ``"password":
	"..."``), so a structured answer and its text copy are masked alike. Returns a new
	structure; other values pass through."""
	if isinstance(obj, str):
		return scrub_text(obj)
	if isinstance(obj, dict):
		out, count = {}, 0
		for key, value in obj.items():
			new_key, n = scrub_counted(key)
			if isinstance(value, str) and value and value != REDACTION and _is_secret_field(key):
				new_value, n_value = REDACTION, 1
			else:
				new_value, n_value = scrub_counted(value)
			out[new_key] = new_value
			count += n + n_value
		return out, count
	if isinstance(obj, (list, tuple)):
		pairs = [scrub_counted(v) for v in obj]
		return [v for v, _ in pairs], sum(n for _, n in pairs)
	return obj, 0


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
	quote, key, colon, value = match.groups()
	if value == REDACTION or not _is_secret_field(key):
		return match.group(0)
	return f"{quote}{key}{quote}{colon}{quote}{REDACTION}{quote}"


def _is_secret_field(name) -> bool:
	"""A credential-named field whose value is the secret itself: not a locator
	(``authorization_url``, ``api_key_id``) and not a resume cursor (``next_page_token``)."""
	words = _words(name)
	if not is_secret_key(name) or words[-1] in _LOCATOR_WORDS:
		return False
	return not _CURSOR_WORDS & set(words)


def _mask_pem_blocks(text: str) -> tuple[str, int]:
	"""Every private key block masked, header to footer. A block whose footer is not in
	the text (the text was cut) masks the rest of the text: it is all key material."""
	if "PRIVATE KEY-----" not in text:
		return text, 0
	parts, pos, count = [], 0, 0
	while begin := _PEM_BEGIN_RE.search(text, pos):
		parts += [text[pos : begin.start()], REDACTION]
		count += 1
		end = _PEM_END_RE.search(text, begin.end())
		if end is None:
			return "".join(parts), count
		pos = end.end()
	parts.append(text[pos:])
	return "".join(parts), count


def _words(name) -> list[str]:
	spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
	spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
	return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]
