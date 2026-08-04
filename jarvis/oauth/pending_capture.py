"""Durable, encrypted, short-lived pending OAuth capture (plan-05 D2, review
P0-04 / §8.2).

A chat-subscription sign-in mints a LIVE provider token. Before this module that
token existed only in the browser (``complete_pool_account_signin`` returned the
raw ``oauth_blob`` to Vue memory), so a refresh, an error-banner Retry, or a crash
lost the only copy Jarvis had - while the provider token stayed live with no
sweeper able to revoke it, and a second sign-in minted another one (review §7.1).

Here the token exchange PERSISTS the blob ENCRYPTED (a Password field -> Frappe
__Auth, never plaintext in the column) and hands the browser only an opaque
``capture_id`` plus safe display metadata. The save that adopts the credential
CONSUMES the capture exactly once (an atomic, owner-gated, lock-taken claim), and
an expiry sweeper REVOKES the provider token where the provider exposes an endpoint
we can call, then erases the ciphertext either way.

The token blob NEVER leaves the server after the exchange: no read endpoint here
returns it, and nothing logs it.
"""

import hashlib
import json
import secrets

import frappe
import requests
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.password import remove_encrypted_password

DT = "Jarvis Pending OAuth Capture"

# How long a minted-but-unsaved credential stays resumable. Long enough to cover a
# reload / phone-sign-in detour / error-Retry, short enough that an abandoned live
# token is revoked (or expired) promptly. Provider access tokens themselves live
# ~1h, so this is the resumability window, not the token's lifetime.
CAPTURE_TTL_MINUTES = 30

# The sweeper gives up (and alerts) after this many transient revoke failures for
# one capture, so a persistently-unreachable provider is bounded, not retried
# forever. The ciphertext is erased once we stop trying.
REVOKE_MAX_ATTEMPTS = 5

_HTTP_TIMEOUT = 10

# Provider token-revocation endpoints we can actually call, keyed by
# agent_provider. ONLY endpoints grounded in the provider's own docs belong
# here; a wrong revoke URL is worse than none. Google exposes the well-known
# RFC-7009 endpoint. The pooled chat-subscription upstreams (openai / xai / kimi)
# publish no revocation endpoint this integration can verify, so their captures
# resolve to ``unsupported``: the short-lived token is left to expire and the
# ciphertext is erased on sweep. Extend this map when a provider's endpoint is
# confirmed.
_REVOKE_ENDPOINTS = {
	"google-gemini-cli": "https://oauth2.googleapis.com/revoke",
}


class CaptureError(frappe.ValidationError):
	"""A capture could not be consumed (missing, not yours, or expired).
	Deliberately opaque so a caller cannot tell "not yours" from "does not
	exist"."""


class CaptureAlreadyConsumed(CaptureError):
	"""This user's capture was already consumed. Distinct from CaptureError so a
	resume (the SPA re-calling save with the same idempotency_key after a lost
	response) can fall back to the blob the first attempt already wrote into the
	saved config, instead of hard-failing - the credential was NOT lost, only
	adopted already."""


class _RevokeTransientError(Exception):
	"""The provider's revoke endpoint failed in a way worth retrying."""


def _subject_hash(subject: str) -> str:
	subject = (subject or "").strip()
	if not subject:
		return ""
	return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def _nonce_hash(nonce: str) -> str:
	nonce = (nonce or "").strip()
	if not nonce:
		return ""
	return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _connection_anchor() -> str:
	"""The workspace's current connection identity (agent_url) - the bench-side
	signal a capture is bound to (F10 / §10.2). A workspace reset / reconnect / tenant
	move changes it, so a capture minted against the old identity is refused at
	consume. Empty during onboarding before the first container is provisioned; an
	empty-bound capture consumed while still empty matches (the normal first-onboard
	path, where the consume itself triggers provisioning)."""
	try:
		return (frappe.db.get_single_value("Jarvis Settings", "agent_url") or "").strip()
	except Exception:
		return ""


def _safe_view(doc_or_row) -> dict:
	"""The ONLY shape a read path returns: opaque id + non-secret display metadata.
	Never the blob, never a token. ``expires_at`` is stringified for the wire."""

	def g(k):
		if hasattr(doc_or_row, "get"):
			return doc_or_row.get(k)
		return getattr(doc_or_row, k, None)

	return {
		"capture_id": g("capture_id") or g("name"),
		"account_ref": g("account_ref") or "",
		"label": g("safe_label") or g("account_email") or "",
		"account_email": g("account_email") or "",
		"provider": g("provider") or "",
		"upstream": g("upstream") or "",
		"provider_subject": g("provider_subject_hash") or "",
		"expires_at": str(g("expires_at") or ""),
	}


def create_capture(
	*,
	provider: str,
	upstream: str,
	agent_provider: str,
	oauth_blob: str,
	account_email: str,
	account_ref: str,
	safe_label: str,
	provider_subject: str = "",
	nonce: str = "",
) -> dict:
	"""Persist a freshly-minted OAuth blob ENCRYPTED and return the safe view.

	Called from the token-exchange endpoints BEFORE they reply, so the blob is
	durable the instant the browser learns the sign-in worked. Folds onto an
	existing un-consumed capture of the SAME account (provider + stable subject)
	for this user rather than minting a second row (review P1-07), reusing that
	row's ``account_ref`` so the pool row still keys on one stable id.

	``oauth_blob`` is the JSON string of the agent blob. It is written to a
	Password field, so it is encrypted at rest and never returned by any read.
	"""
	subject_hash = _subject_hash(provider_subject)
	# Fold same-account recaptures on (provider, upstream, STABLE subject) - never
	# email/label (F4). Scoping the fold to the same provider+upstream makes it
	# impossible for two DIFFERENT providers that happen to share a subject value to
	# collide onto one row and clobber a live token. A capture with no stable subject
	# never folds (subject_hash == "" short-circuits below): a duplicate row is safe;
	# an overwritten unrevocable token is not.
	existing = None
	if subject_hash:
		name = frappe.db.get_value(
			DT,
			{
				"owner_user": frappe.session.user,
				"provider": provider,
				"upstream": upstream,
				"provider_subject_hash": subject_hash,
				"consumed_at": ["is", "not set"],
				"revocation_state": "pending",
			},
			"name",
		)
		if name:
			existing = frappe.get_doc(DT, name)

	if existing is not None:
		existing.encrypted_oauth_blob = oauth_blob
		existing.provider = provider
		existing.upstream = upstream
		existing.agent_provider = agent_provider
		existing.account_email = account_email or existing.account_email
		existing.safe_label = safe_label or existing.safe_label
		existing.bound_agent_url = _connection_anchor()
		existing.nonce_hash = _nonce_hash(nonce)
		existing.expires_at = add_to_date(now_datetime(), minutes=CAPTURE_TTL_MINUTES)
		existing.revocation_state = "pending"
		existing.revocation_attempts = 0
		existing.save(ignore_permissions=True)
		return _safe_view(existing)

	doc = frappe.new_doc(DT)
	doc.capture_id = "oacap_" + secrets.token_hex(16)
	doc.owner_user = frappe.session.user
	doc.provider = provider
	doc.upstream = upstream
	doc.agent_provider = agent_provider
	doc.provider_subject_hash = subject_hash
	doc.safe_label = safe_label or account_email or ""
	doc.account_email = account_email or ""
	doc.account_ref = account_ref
	doc.bound_agent_url = _connection_anchor()
	doc.nonce_hash = _nonce_hash(nonce)
	doc.encrypted_oauth_blob = oauth_blob
	doc.expires_at = add_to_date(now_datetime(), minutes=CAPTURE_TTL_MINUTES)
	doc.revocation_state = "pending"
	doc.revocation_attempts = 0
	doc.insert(ignore_permissions=True)
	return _safe_view(doc)


def list_active(user: str | None = None) -> list[dict]:
	"""Safe views of the user's live (un-consumed, un-expired) captures, for the
	editor to rehydrate on load / Retry so a reload resumes without a second
	sign-in. Never returns a blob."""
	user = user or frappe.session.user
	# ignore_permissions: the DocType grants NO role read (it holds live tokens), so
	# access is only ever through this owner-scoped, whitelisted path - never a raw
	# get_all a peer System Manager could aim at another user's row.
	rows = frappe.get_all(
		DT,
		ignore_permissions=True,
		filters={
			"owner_user": user,
			"consumed_at": ["is", "not set"],
			"expires_at": [">", now_datetime()],
			# Only a still-live capture rehydrates - a cancelled/revoked one (its
			# ciphertext already erased) must never reappear as resumable.
			"revocation_state": "pending",
		},
		fields=[
			"capture_id",
			"account_ref",
			"safe_label",
			"account_email",
			"provider",
			"upstream",
			"provider_subject_hash",
			"expires_at",
		],
		order_by="creation desc",
	)
	return [_safe_view(r) for r in rows]


def consume_capture(capture_id: str) -> str:
	"""Atomically claim a capture ONCE and return its decrypted blob (JSON string).

	The claim is a row-locked, owner-gated check-and-set: ``SELECT ... FOR UPDATE``
	then set ``consumed_at``. Two tabs racing the same id serialize on the row lock
	- the first commits ``consumed_at`` and the second sees it and is refused, so a
	minted token can be adopted at most once. The ``consumed_at`` write and the
	ciphertext erase ride the SAME request transaction as the caller's
	desired-state save (save_llm_pool commits once at the end), so the blob either
	moved into the saved config AND the capture was burned, or neither happened.

	Raises ``CaptureError`` (opaque) when the capture is missing, not this user's,
	expired, or already consumed.
	"""
	capture_id = (capture_id or "").strip()
	if not capture_id:
		raise CaptureError("unknown capture")
	rows = frappe.db.sql(
		"""SELECT name, consumed_at, expires_at, bound_agent_url
		   FROM `tabJarvis Pending OAuth Capture`
		   WHERE capture_id = %s AND owner_user = %s
		   FOR UPDATE""",
		(capture_id, frappe.session.user),
		as_dict=True,
	)
	if not rows:
		raise CaptureError("unknown capture")
	row = rows[0]
	if row.consumed_at:
		raise CaptureAlreadyConsumed("capture already used")
	if get_datetime(row.expires_at) <= now_datetime():
		raise CaptureError("capture expired")
	# Authority/connection fence (F10 / §10.2): a workspace reset / reconnect / tenant
	# move between sign-in and save changed the connection identity, so this capture
	# was minted against a workspace this bench no longer points at - refuse it. (The
	# admin operation carries the authority-generation fence; this is its bench-side
	# complement so a stale credential is not adopted post-move.)
	if (row.bound_agent_url or "") != _connection_anchor():
		raise CaptureError("capture no longer matches this workspace connection")

	from frappe.utils.password import get_decrypted_password

	blob = get_decrypted_password(DT, row.name, "encrypted_oauth_blob", raise_exception=False) or ""
	if not blob:
		# Ciphertext already gone (a prior partial run erased it) - nothing to adopt.
		raise CaptureError("capture already used")
	frappe.db.set_value(
		DT,
		row.name,
		{"consumed_at": now_datetime(), "revocation_state": "consumed"},
		update_modified=False,
	)
	# The container now owns the credential; erase our copy so a consumed capture
	# never leaves a live token at rest on the bench.
	_erase_ciphertext(row.name)
	return blob


def mark_consumed_by_operation(capture_ids: list[str], operation_id: str) -> None:
	"""Record which durable apply operation adopted these captures (audit only),
	after save_llm_pool has the descriptor back. Best-effort - a failure here must
	never undo a completed save."""
	operation_id = (operation_id or "").strip()
	if not operation_id:
		return
	for cid in capture_ids or []:
		name = frappe.db.get_value(DT, {"capture_id": cid, "owner_user": frappe.session.user}, "name")
		if name:
			frappe.db.set_value(DT, name, "consumed_by_operation", operation_id, update_modified=False)


def cancel_capture(capture_id: str) -> dict:
	"""Customer explicitly discarded an unsaved just-connected account: revoke the
	provider token where we can, then erase the ciphertext. Owner-gated."""
	capture_id = (capture_id or "").strip()
	name = frappe.db.get_value(DT, {"capture_id": capture_id, "owner_user": frappe.session.user}, "name")
	if not name:
		raise CaptureError("unknown capture")
	doc = frappe.get_doc(DT, name)
	if doc.consumed_at:
		# Already adopted into a save; nothing minted is still ours to revoke.
		return {"revocation_state": doc.revocation_state or "consumed"}
	state = _revoke_and_erase(doc)
	return {"revocation_state": state}


def _erase_ciphertext(name: str) -> None:
	"""Drop the encrypted blob's __Auth row and blank the masked column so no live
	token remains at rest for this capture."""
	try:
		remove_encrypted_password(DT, name, "encrypted_oauth_blob")
	except Exception:
		# No __Auth row (already erased) - benign.
		pass
	frappe.db.set_value(DT, name, "encrypted_oauth_blob", "", update_modified=False)


def _revoke_token(agent_provider: str, blob_json: str) -> str:
	"""Attempt to revoke the provider token. Returns the terminal revocation_state
	(``revoked`` | ``unsupported``); raises ``_RevokeTransientError`` on a failure
	worth retrying. Never logs the token."""
	endpoint = _REVOKE_ENDPOINTS.get(agent_provider or "")
	if not endpoint:
		return "unsupported"
	try:
		blob = json.loads(blob_json or "{}")
	except (ValueError, TypeError):
		return "unsupported"
	# RFC 7009: revoking a refresh token cascades to its access tokens.
	token = (blob.get("refresh") or blob.get("access") or "").strip()
	if not token:
		return "unsupported"
	try:
		resp = requests.post(endpoint, data={"token": token}, timeout=_HTTP_TIMEOUT)
	except requests.RequestException as e:
		raise _RevokeTransientError(str(e)) from e
	# 200 = revoked. A 400 typically means the token was already invalid/expired -
	# the desired end-state either way, so treat it as done rather than retrying.
	if resp.ok or resp.status_code == 400:
		return "revoked"
	raise _RevokeTransientError(f"revoke endpoint returned {resp.status_code}")


def _revoke_and_erase(doc) -> str:
	"""Revoke (best-effort) then erase, updating revocation_state/attempts. Returns
	the resulting state. Used by both the explicit cancel and the sweeper."""
	from frappe.utils.password import get_decrypted_password

	blob = get_decrypted_password(DT, doc.name, "encrypted_oauth_blob", raise_exception=False) or ""
	attempts = int(doc.revocation_attempts or 0) + 1
	try:
		state = _revoke_token(doc.agent_provider, blob)
	except _RevokeTransientError as e:
		# Transient: keep the ciphertext for a retry until the attempt ceiling, then
		# give up (erase + alert) so a dead provider can't strand a live token here
		# forever or churn the sweeper.
		if attempts >= REVOKE_MAX_ATTEMPTS:
			frappe.db.set_value(
				DT,
				doc.name,
				{"revocation_state": "failed", "revocation_attempts": attempts},
				update_modified=False,
			)
			_erase_ciphertext(doc.name)
			frappe.log_error(
				title="oauth capture: token revoke gave up after retries",
				message=f"capture={doc.name} provider={doc.agent_provider!r} attempts={attempts} err={e!r}",
			)
			return "failed"
		frappe.db.set_value(
			DT,
			doc.name,
			{"revocation_state": "failed", "revocation_attempts": attempts},
			update_modified=False,
		)
		return "failed"
	frappe.db.set_value(
		DT,
		doc.name,
		{"revocation_state": state, "revocation_attempts": attempts},
		update_modified=False,
	)
	_erase_ciphertext(doc.name)
	return state


def sweep_expired(batch: int = 200) -> None:
	"""Scheduled: revoke + erase every expired, un-consumed capture (and retry the
	ones a prior sweep left ``failed`` under the attempt ceiling). Runs hourly.
	Best-effort per row so one bad provider never stalls the batch."""
	names = frappe.get_all(
		DT,
		ignore_permissions=True,
		filters={
			"consumed_at": ["is", "not set"],
			"expires_at": ["<=", now_datetime()],
			"revocation_state": ["in", ("pending", "failed")],
			"revocation_attempts": ["<", REVOKE_MAX_ATTEMPTS],
		},
		pluck="name",
		limit=batch,
	)
	for name in names:
		try:
			doc = frappe.get_doc(DT, name)
			_revoke_and_erase(doc)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="oauth capture sweep: row failed",
				message=f"capture={name}",
			)
