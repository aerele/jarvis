"""One-time nonces for the site proof.

Admin hands secrets and pairing material only to the site that proves it holds a
nonce this site issued: fleet runs the plugin CLI in the tenant container, which
POSTs ``jarvis.api.pairing_challenge`` to the bound URL. A copy keeps its own
cache, so the real site answers ``held=false`` for a nonce the copy issued.
"""

import re
import secrets

import frappe

PROOF_SESSION_KEY = "pairing-challenge"
_PREFIX = "jarvis:pairing_proof:"
_TTL_S = 600
_NONCE_RE = re.compile(r"[0-9a-f]{32}")


def issue_nonce() -> str:
	nonce = secrets.token_hex(16)
	frappe.cache().set_value(_PREFIX + nonce, "1", expires_in_sec=_TTL_S)
	return nonce


def consume_nonce(nonce: str) -> bool:
	"""True once per issued nonce; False for unknown, expired or malformed."""
	if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
		return False
	key = _PREFIX + nonce
	held = bool(frappe.cache().get_value(key))
	if held:
		frappe.cache().delete_value(key)
	return held
