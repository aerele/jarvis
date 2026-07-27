"""Data-loss guards for the Custom App Learning source path (CX5-4).

Reading a customer's custom-app source and shipping it to an external model is
the one place Jarvis deliberately sends customer CODE off the bench. The
containment/symlink guards in ``app_source`` stop the delegate reading files
OUTSIDE the app tree; these guards address what is INSIDE it:

  * ``redact_secrets`` — hard-coded credentials do live in real Frappe apps
    (a ``site_config`` fragment checked in, an API key in a helper, a private key
    fixture). Every served file is redacted BEFORE it is fenced, so a committed
    secret is not handed to the model provider verbatim.
  * ``is_sensitive_path`` — files whose very NAME advertises credentials
    (``*secret*``, ``*credential*``, ``*password*``, ``.env``, ssh keys) are not
    served at all and never appear in a manifest. ``.pem``/``.key`` are already
    outside ``EXT_ALLOWLIST``.
  * ``outbound_violations`` — the reverse direction. Wiki pages the delegate
    composes are attacker-influenceable (the source it read is untrusted data), so
    a page body that carries the run's own session bearer, a Jarvis session header,
    or a credential-shaped value is REFUSED rather than published to the Org wiki.

Deliberately CONSERVATIVE: the assignment matcher only fires on a QUOTED value of
at least ``_MIN_SECRET_LEN`` base64/hex-ish characters, so ordinary code
(``password = get_password()``, ``api_key=self.key``) is never mangled. The cost
of a miss is a redaction that did not happen; the cost of over-matching is
corrupted learning material.
"""

from __future__ import annotations

import re

REDACTION = "[REDACTED-SECRET]"

# Shortest value we will treat as a credential. Below this, base64/hex-ish text is
# far more likely to be an identifier than a key.
_MIN_SECRET_LEN = 20

# A whole PEM private-key block, header to footer.
_PEM_RE = re.compile(
	r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
	re.DOTALL,
)
# AWS long-term (AKIA) + temporary (ASIA) access key ids — a fixed, unambiguous shape.
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# ``Authorization: Bearer <token>`` / ``Bearer <token>`` — redact the token only.
_BEARER_RE = re.compile(
	r"(?i)(bearer\s+)([A-Za-z0-9._\-+/=]{%d,})" % _MIN_SECRET_LEN,
)
# ``password = "…"`` / ``"api_key": "…"`` — QUOTED values only (see module docstring).
_ASSIGN_RE = re.compile(
	r"(?i)"
	r"\b(passwd|password|secret|secret[_-]?key|api[_-]?key|apikey|access[_-]?key"
	r"|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key"
	r"|client[_-]?secret|encryption[_-]?key|authorization)\b"
	r"(\s*[:=]>?\s*)"
	r"(['\"])([A-Za-z0-9+/=._\-]{%d,})\3" % _MIN_SECRET_LEN
)

# Basenames that advertise credentials. ``.pem`` / ``.key`` never reach here (they
# are outside app_source.EXT_ALLOWLIST); these are the allowed-extension names that
# should still never be served.
_SENSITIVE_BASENAME_RE = re.compile(
	r"(?i)(secret|credential|password|passwd|id_rsa|id_dsa|id_ecdsa|id_ed25519)|^\.env"
)

# The per-run bearer's transport header. A page body quoting it means the delegate
# has been steered into echoing its own credentials into durable, org-wide content.
_SESSION_HEADER_RE = re.compile(r"(?i)x-jarvis-session")


def redact_secrets(text: str) -> tuple[str, int]:
	"""``(redacted_text, replacement_count)``.

	Replaces the VALUE of anything credential-shaped with ``[REDACTED-SECRET]``,
	leaving the surrounding code intact so the material is still learnable."""
	if not text:
		return text or "", 0
	count = 0

	def _whole(_m):
		nonlocal count
		count += 1
		return REDACTION

	def _tail(m):
		nonlocal count
		count += 1
		return f"{m.group(1)}{REDACTION}"

	def _assign(m):
		nonlocal count
		count += 1
		return f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTION}{m.group(3)}"

	out = _PEM_RE.sub(_whole, text)
	out = _AWS_KEY_RE.sub(_whole, out)
	out = _BEARER_RE.sub(_tail, out)
	out = _ASSIGN_RE.sub(_assign, out)
	return out, count


def is_sensitive_path(rel_path: str) -> bool:
	"""True iff this path's BASENAME advertises credentials and must never be
	served or listed (case-insensitive)."""
	base = str(rel_path or "").replace("\\", "/").rsplit("/", 1)[-1]
	return bool(base) and bool(_SENSITIVE_BASENAME_RE.search(base))


def outbound_violations(text: str, session_key: str | None = None) -> list[str]:
	"""Labels for anything in ``text`` that must NOT leave the run in durable,
	org-visible content. Empty list = clean.

	Checked: the run's own ``session_key`` (the delegate's bearer — quoting it in a
	wiki page would publish a live credential), the ``X-Jarvis-Session`` header
	shape, and any value ``redact_secrets`` classes as a credential."""
	body = text or ""
	out: list[str] = []
	key = (session_key or "").strip()
	if key and key in body:
		out.append("run session key")
	if _SESSION_HEADER_RE.search(body):
		out.append("jarvis session header")
	if redact_secrets(body)[1]:
		out.append("credential-shaped value")
	return out
