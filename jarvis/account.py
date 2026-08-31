"""Customer-side wrappers for the SPA billing page (/jarvis/billing).

Written originally for the /app/jarvis-account Desk page, which has been
retired. The endpoints were never Desk-specific and moved to the SPA unchanged;
only Razorpay Checkout had to be reimplemented there (frontend/src/lib/
useRazorpay.js).

Thin shims over admin_client (so the browser never holds admin api_key /
api_secret). Errors are normalized via the shared ``_surface`` helper from
onboarding so admin ValidationErrors arrive as clean ``frappe.throw`` toasts.

The page also reuses these existing onboarding endpoints directly under
their published names - no duplicates:

  - jarvis.onboarding.save_llm_creds  (LLM section save)
  - jarvis.onboarding.renew           (renew / reactivate / resume CTAs)
  - jarvis.onboarding.finish_payment  (post-Razorpay confirm)
"""

import hashlib

import frappe

from jarvis import admin_client, compat, onboarding_contract, release_notice
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)
from jarvis.jarvis.pool_serialize import compute_pool_mode, pool_primary_model
from jarvis.onboarding import _surface
from jarvis.permissions import require_jarvis_access, require_jarvis_admin

SETTINGS = "Jarvis Settings"

# R2-H4 chat-readiness gate, shared by boot, is_ready_for_chat and the send
# entitlement check. Only "Ready" is cached, so suspension/renewal is still seen
# promptly.
#
# The cache is keyed by a CONFIG REVISION (see _gate_revision) and the TTL is a
# belt, not the mechanism. A flat site-wide key with a 2-minute TTL meant a
# customer who had just saved a broken LLM config could be told "Ready" for two
# more minutes on the strength of the verdict their PREVIOUS config earned - the
# save itself busted nothing. 30s bounds how long an ADMIN-side change (a
# suspension, a container restart) can stay hidden; the revision covers every
# change this bench makes itself, immediately.
_CHAT_GATE_CACHE_KEY = "jarvis:chat_readiness_gate"
_CHAT_GATE_CACHE_TTL_S = 30

# Every LOCAL input that can change what admin answers about readiness. A change
# to any of them invalidates the cached verdict outright, because the verdict was
# about the previous configuration. Kept as raw stored values (see _settings_raw)
# so the hash is stable and cheap.
#
# These are the same fields is_ready_for_chat and _llm_apply_confirmed gate on,
# plus the ones that decide WHICH container answers (agent_url) and what the last
# apply did (last_sync_status / last_subscription_status) - a save flips the
# status to "pending:" before anything else moves, so the revision changes on the
# save itself rather than only when the async apply lands.
_GATE_REVISION_FIELDS = (
	"agent_url",
	"llm_auth_mode",
	"llm_provider",
	"llm_model",
	"proxy_active",
	"routing_mode",
	"preset",
	"last_sync_status",
	"last_subscription_status",
	"llm_pool_synced_at",
	"llm_direct_synced_at",
	"llm_oauth_connected_at",
	# The tenant authority this bench is bound to (Plan 04 generation contract).
	# A move/repair that changes it must not keep serving the verdict the
	# PREVIOUS authority earned (P1-09). Read raw: the column may not exist yet
	# on a bench that predates the generation consumer, in which case _settings_raw
	# simply omits it and the digest is unchanged - forward-compatible by
	# construction.
	"tenant_authority_generation",
)

# Durable "admin has said Ready about this workspace at least once" marker. It is
# what separates an ESTABLISHED workspace (whose chat must survive a control-plane
# outage) from an onboarding-stage one (which must not be told it is ready when
# nobody could confirm it). See _admin_unreachable_verdict.
_READY_MARKER_FIELD = "chat_was_ready_at"
# Re-stamped at most this often. The gate only ever asks "is it set", so the
# freshness is for operators reading the field; writing on every Ready would put a
# DB write on every uncached page load for no gate value.
_READY_MARKER_REFRESH_S = 86400

# The authority the established claim is BOUND to (P0-06 / review §8.6). A digest
# of (admin principal, container URL, tenant authority generation) captured when
# the marker was last stamped. _has_been_chat_ready requires it to still match the
# CURRENT authority before failing open, so a reset / reconnect / principal or
# container replacement / generation change ends the claim mechanically - the
# explicit clears in _disconnect_agent_transport and write_connection are belt to
# this suspenders, not the only fence.
_READY_ANCHOR_FIELD = "chat_ready_authority"

# jarvis C2 time-box (review finding 1): the field stamped at APPLY-ENQUEUE time
# by jarvis_settings.py's two sync funnels (_enqueue_pool_sync,
# _on_update_single_model_legacy). _provisioning_verdict below reads it to bound
# how long the soft llm_applying reason can ride: without a bound, a stuck
# first-apply (fleet-agent down, never converges, never writes a terminal
# "failed:" status) would stay soft indefinitely with no way out. Deliberately
# NOT in _GATE_REVISION_FIELDS: last_sync_status already flips to "pending:" on
# the same save that stamps this, so the cached verdict is already busted by
# the time this field would matter.
_APPLYING_TIMESTAMP_FIELD = "last_sync_requested_at"
# Generous on purpose: long enough that a genuinely slow but converging apply
# (container restart, admin round-trips) never flips an established workspace
# to the hard setup-wizard reason out from under a customer still watching it
# finish, short enough that a stuck apply degrades to the pre-PR hard reason
# within one sitting rather than staying soft indefinitely.
_APPLYING_SOFT_WINDOW_S = 15 * 60

_GATE_STATE_FIELDS = (
	*_GATE_REVISION_FIELDS,
	_READY_MARKER_FIELD,
	_READY_ANCHOR_FIELD,
	_APPLYING_TIMESTAMP_FIELD,
	"jarvis_admin_api_key",
)

# Not-ready code for "we could not confirm this workspace is ready" - as opposed
# to container_provisioning / subscription_suspended, which are verdicts admin
# actually rendered. Callers may retry it; nothing about it is permanent.
_UNCONFIRMED_REASON = "readiness_unconfirmed"
_UNCONFIRMED_DETAIL = (
	"We couldn't confirm your workspace is ready yet. This usually clears in a moment - please retry."
)
# Short enough that "retryable" stays true in practice (the wizard's poll is 2.5s
# and a recovered control plane must be seen within a beat or two), long enough
# that an outage does not turn one wizard into 30 admin round-trips a minute.
# Applies to THIS code alone: a real not-ready verdict is never cached.
_UNCONFIRMED_CACHE_TTL_S = 5

# A customer that has NEVER paid, as named by admin's structured refusal code
# (jarvis_admin_v2 auth contract). This is the machine signal the never-paid gate
# reads: a stable code on the 403 envelope, propagated onto AdminAuthError.code by
# admin_client, so the decision no longer depends on parsing a human sentence that
# a hardened control plane may omit entirely (P0-05 / review §8.4).
#
# An ALLOWLIST: Cancelled, an empty body, a proxy's own 403 and anything
# unrecognised all fall through to the soft verdict. Being wrong in the hard
# direction locks a customer out of chat AND /billing; being wrong in the soft
# direction shows a banner one step too late.
#
# CUSTOMER_NOT_PAID is admin's concrete structured code
# (jarvis_admin_v2.api._responses.CustomerNotPaidError, status 403), raised for a
# Pending Payment / Pending Verification / any non-Active-non-Suspended customer on
# the wrapped endpoints and get_connection. Distinct from
# TENANT_AUTHORITY_REPAIR_REQUIRED, which is NOT never-paid (it is a repair state)
# and is deliberately absent here.
_NEVER_PAID_CODES = frozenset({"CUSTOMER_NOT_PAID"})

# TEMPORARY prose fallback for an OLD admin that answers the never-paid 403 with a
# human sentence and no structured code. REMOVE once every control plane in the
# fleet emits _NEVER_PAID_CODES (tracked with the Plan 04/05 admin cutover): a
# hardened admin already sends exc_type alone with no sentence, so this branch is
# best-effort compatibility, not the mechanism.
_NEVER_PAID_403_MARKERS = (
	"customer status: pending payment",
	"customer status: pending verification",
	"not a jarvis customer",
)


def _settings_raw(fields: tuple[str, ...]) -> dict:
	"""Raw ``tabSingles`` values for ``fields`` - exactly as stored, uncast.

	Deliberately NOT ``frappe.db.get_value`` / ``get_single_value`` on the Single:
	both cast by fieldtype, and casting an EMPTY Datetime single runs it through
	``get_datetime``, which returns ``now_datetime()`` for None. Every Datetime
	this function reads is used either as "has this ever been stamped" or as "has
	this changed since the cached verdict", and that coercion breaks both: an
	unstamped marker would read as truthy, and a never-applied config would hash
	differently on every single call. Same trap the v1_10 / v2_00 backfill
	patches document from the other direction (``datetime(1, 1, 1)``).

	Uncached on purpose: this decides whether a cached readiness verdict may still
	be served, so it must see a write the moment it lands.

	``jarvis_admin_api_key`` is among the fields callers ask for; the column holds
	only the mask (jarvis/_password_utils.py), and it is tested for PRESENCE here
	and never returned to a caller or logged.
	"""
	try:
		rows = frappe.db.sql(
			"""select `field`, `value` from `tabSingles` where doctype = %s and `field` in %s""",
			(SETTINGS, tuple(fields)),
		)
	except Exception:
		return {}
	return {f: v for f, v in rows}


def _is_never_paid_403(err) -> bool:
	"""Does this admin rejection carry admin's OWN evidence that the customer has
	never paid? Anything less is not evidence.

	Reads the STRUCTURED code first (admin_client tags AdminAuthError.code from the
	403 envelope's ``error.code``): a machine signal survives a hardened control
	plane that returns ``exc_type`` alone with no human sentence, which is exactly
	the shape the old prose match missed (P0-05). The prose markers remain only as
	a temporary fallback for an admin that predates the code contract - see
	_NEVER_PAID_403_MARKERS.
	"""
	if getattr(err, "status_code", None) != 403:
		return False
	code = (getattr(err, "code", "") or "").strip().upper()
	if code:
		return code in _NEVER_PAID_CODES
	# Old-admin compatibility: no structured code, fall back to the sentence.
	message = str(err or "").strip().lower()
	return any(marker in message for marker in _NEVER_PAID_403_MARKERS)


def _gate_revision(raw: dict) -> str:
	"""Short digest of the local readiness inputs - the cached verdict's identity.

	A miss costs one admin round-trip, so the digest may be conservative (an
	unrelated re-save that rewrites the same values keeps the entry) but must
	never be stale: any changed value must produce a different key.
	"""
	joined = "|".join(f"{f}={raw.get(f) or ''}" for f in _GATE_REVISION_FIELDS)
	return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _authority_anchor(raw: dict) -> str:
	"""Digest of the AUTHORITY the established claim is bound to (review §8.6).

	Every input is a LOCAL, offline-readable field, because this is recomputed on
	the fail-open path when admin cannot be asked at all:

	  - the admin principal: a DIGEST of the REAL jarvis_admin_api_key (review F6).
	    The raw ``tabSingles`` value the caller passes carries only this Password
	    field's MASK ("**********"), which is a CONSTANT - joining it made the
	    principal term inert, so a reconnect to a different principal on the same
	    container + generation produced an IDENTICAL anchor (a fence that did not
	    fence). We fetch and hash the decrypted credential from __Auth instead (a
	    local read, so still offline-safe on the fail-open path); the real key is
	    never stored - only its one-way digest rides into the anchor.
	  - agent_url: the container. A workspace reset clears it; a container
	    replacement changes it.
	  - tenant_authority_generation: the Plan 04 generation. A move/repair bumps
	    it (empty on a bench that predates the consumer - the digest is then
	    stable across the two remaining inputs, which is correct).

	A change to any of them means the workspace admin confirmed Ready is no longer
	the one in front of us, so the claim must not carry across it.
	"""
	from frappe.utils.password import get_decrypted_password

	# The REAL admin credential (from __Auth), digested - NOT the masked column value
	# the raw tabSingles read carries. get_decrypted_password is a local DB read, so
	# it is safe on the fail-open path. Empty (un-onboarded) -> empty principal term.
	real_key = (
		get_decrypted_password(SETTINGS, SETTINGS, "jarvis_admin_api_key", raise_exception=False) or ""
	).strip()
	principal = hashlib.sha256(real_key.encode("utf-8")).hexdigest() if real_key else ""
	joined = "|".join(
		(
			principal,
			(raw.get("agent_url") or "").strip(),
			str(raw.get("tenant_authority_generation") or ""),
		)
	)
	return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _has_been_chat_ready(raw: dict) -> bool:
	"""Is this an ESTABLISHED workspace - a stored control-plane connection AND an
	explicit Ready verdict, for the SAME authority we are looking at now?

	Three halves matter. The marker + admin key say "admin confirmed this
	workspace Ready once and signup is still in place". The authority anchor is the
	fence the marker alone was missing (review P0-06): a bare timestamp kept a
	workspace whose transport was torn down (reset), or whose account was
	reconnected elsewhere, in the protected cohort - precisely the workspace whose
	chat cannot work. Binding the claim to (principal, container, generation) ends
	it the moment any of those move, whether or not the writer that moved them also
	remembered to clear the marker.
	"""
	meta = frappe.get_meta(SETTINGS)
	if not meta.get_field(_READY_MARKER_FIELD):
		# Code running ahead of its migration: without the marker field NO
		# workspace can prove it was ever Ready, so fail CLOSED rather than promote
		# every site by default (P0-07 - a missing field must not manufacture
		# established status). The documented deploy order (migrate before restart)
		# makes this window nil; it self-heals the moment `bench migrate` runs.
		return False
	if not (bool(raw.get(_READY_MARKER_FIELD)) and bool((raw.get("jarvis_admin_api_key") or "").strip())):
		return False
	if meta.get_field(_READY_ANCHOR_FIELD):
		stored = (raw.get(_READY_ANCHOR_FIELD) or "").strip()
		if stored:
			# The mechanical fence: the claim survives only while its bound
			# authority is still the current one.
			return stored == _authority_anchor(raw)
		# A marker with no stored anchor is a legacy/backfilled claim from before
		# the fence existed. Honour the presence rule for it - the explicit clears
		# on reset/reconnect still end it - so a pre-fence established site is not
		# ejected wholesale on upgrade.
	return True


def _marker_is_fresh(current) -> bool:
	"""Is the stored marker recent enough to leave alone? An unset or unreadable
	stamp is NOT fresh - rewriting it is how a corrupted value heals."""
	if not current:
		return False
	try:
		return frappe.utils.time_diff_in_seconds(frappe.utils.now(), current) < _READY_MARKER_REFRESH_S
	except Exception:
		return False


def _apply_age_seconds(requested_at):
	"""Seconds elapsed since ``last_sync_requested_at``, or ``None`` when there is
	no usable timestamp. ``None`` covers both a workspace that never enqueued an
	apply (null/unset - predates the field, or simply none pending) and an
	unparseable value: this runs on the hot is_ready_for_chat path and must never
	turn a bad value into a 500, so a parse failure is swallowed to ``None`` rather
	than raised. The single parse+diff path for the soft-window decision, so the
	"recent" and "stale" boundaries (in ``_provisioning_verdict``) cannot drift
	apart or be computed inconsistently - the reason a bare ``None`` sentinel is
	returned rather than two separate predicate helpers (jarvis#825 review)."""
	if not requested_at:
		return None
	try:
		return frappe.utils.time_diff_in_seconds(frappe.utils.now(), requested_at)
	except Exception:
		return None


def _write_is_durable() -> bool:
	"""Would a write made right now survive the end of this request?

	Frappe commits only for an UNSAFE method and rolls everything else back
	(frappe/app.py ``sync_database``, frappe/auth.py ``UNSAFE_HTTP_METHODS``), so a
	marker written during the desk's boot GET is discarded anyway - and writing it
	regardless would burn a row-lock on Singles on every page load of every user
	for nothing. The paths that matter are POSTs (the SPA's readiness call, a chat
	send) and background jobs, which is where the marker actually becomes durable.
	"""
	request = getattr(frappe.local, "request", None)
	if request is None:
		return True  # background job, CLI, test: no request to be rolled back
	return (getattr(request, "method", "") or "").upper() in ("POST", "PUT", "DELETE", "PATCH")


def _mark_chat_ready(raw: dict) -> None:
	"""Record that admin has EXPLICITLY confirmed this workspace Ready, bound to the
	authority that confirmation was about.

	Called only on an explicit ``chat_readiness == "Ready"`` (never on the
	v1-tolerant missing-key path - review P0-07): the marker means "admin said
	Ready", and a response that never said it must not mint that proof.

	Two writes, both best-effort and quiet (readiness is a read path and must not
	fail because a marker could not be written), both ``update_modified=False`` so
	the gate's revision does not churn on its own bookkeeping:

	  - the timestamp, refreshed at most daily (the gate only reads "is it set");
	  - the authority anchor, so the claim is bound to this (principal, container,
	    generation) and ends when any of them move. Re-stamped whenever the
	    timestamp is - a rebind onto the current authority is exactly what an
	    established-through-a-generation-change workspace needs.
	"""
	try:
		if not _write_is_durable():
			return
		if _marker_is_fresh(raw.get(_READY_MARKER_FIELD)):
			# The timestamp is current, but the authority may have moved since it
			# was stamped (a legacy marker carries no anchor, or a rebind is due).
			# Keep the anchor in step without rewriting the timestamp.
			_rebind_anchor(raw)
			return
		frappe.db.set_value(
			SETTINGS, SETTINGS, _READY_MARKER_FIELD, frappe.utils.now(), update_modified=False
		)
		_rebind_anchor(raw)
	except Exception:
		pass


def _rebind_anchor(raw: dict) -> None:
	"""Point the stored authority anchor at the CURRENT authority. Its own
	try/except so a missing column (bench mid-migration) never breaks the gate."""
	try:
		if not frappe.get_meta(SETTINGS).get_field(_READY_ANCHOR_FIELD):
			return
		anchor = _authority_anchor(raw)
		if (raw.get(_READY_ANCHOR_FIELD) or "").strip() != anchor:
			frappe.db.set_value(SETTINGS, SETTINGS, _READY_ANCHOR_FIELD, anchor, update_modified=False)
	except Exception:
		pass


def _admin_unreachable_verdict(raw: dict, diag_code: str = "", retryable: bool | None = None) -> dict:
	"""Admin could not be asked. Which way is failing WRONG?

	For an ESTABLISHED workspace (see _has_been_chat_ready) the container is
	almost certainly still serving the config it was already serving, and the
	control plane is not in the chat path at all - blocking there would take chat
	away from a working customer over an outage they are not even affected by.
	Fail OPEN, as this gate always has.

	For a workspace that has NEVER been confirmed ready, the same shrug is what
	sends a half-onboarded customer into a chat that cannot answer: nothing has
	ever proven a container is serving them, and "probably fine" is not a thing
	anybody knows. Fail CLOSED with a RETRYABLE code - not a verdict admin
	rendered, just an admission that nobody could confirm one - so the wizard
	keeps polling instead of declaring setup finished.

	The closed branch is cached for _UNCONFIRMED_CACHE_TTL_S (the gate does the
	caching, since it owns the key) - long enough to stop a 2.5s poll turning one
	outage into an admin round-trip per beat, short enough that "retryable" is not
	a lie. A verdict admin actually RENDERED is still never cached.
	"""
	if _has_been_chat_ready(raw):
		return {"ready": True, "reason": None, "billing_notice": {}}
	verdict = {
		"ready": False,
		"reason": _UNCONFIRMED_REASON,
		"retryable": True if retryable is None else retryable,
		"detail": _UNCONFIRMED_DETAIL,
		"billing_notice": {},
	}
	if diag_code:
		# P1-08: keep the diagnostic CLASS the broad catch would otherwise flatten,
		# so the SPA's recovery copy can tell an admin transport outage (retry) from
		# an authorization denial (needs the customer to act) from an unexpected
		# contract shape (support). The code is a fixed, safe token - never admin's
		# raw exception text. It is carried on the cached entry too, so a polling
		# wizard gets the same verdict on every beat, not one shape then another.
		verdict["diag_code"] = diag_code
	return verdict


# Fixed, customer-safe diagnostic tokens for a readiness_unconfirmed verdict, and
# whether the customer's own action could change the answer (retryable) or not.
# The token is what the SPA branches its recovery copy on; the raw exception text
# never crosses this boundary (it can carry admin internals). An unrecognised
# failure stays retryable - the same optimistic default the rest of this module
# uses for a control plane it has not classified.
def _diagnostic_class(err) -> tuple[str, bool]:
	from jarvis.exceptions import (
		AdminAuthError,
		AdminRateLimitedError,
		AdminUnreachableError,
		AdminValidationError,
	)

	if isinstance(err, AdminAuthError):
		# 401 is a token problem (re-mintable, retryable); 403 is an authorization
		# denial the same principal will keep hitting until the account changes.
		if getattr(err, "status_code", None) == 403:
			return "admin_forbidden", False
		return "admin_auth", True
	if isinstance(err, AdminRateLimitedError):
		return "admin_rate_limited", True
	if isinstance(err, AdminValidationError):
		# A structured 4xx business/contract error - not a transient outage. An old
		# admin whose contract this bench no longer speaks lands here.
		return "admin_contract", False
	if isinstance(err, AdminUnreachableError):
		return "admin_unreachable", True
	return "admin_error", True


def _admin_chat_gate() -> dict:
	"""Last managed ready-gate: ask admin whether the customer's container is
	actually provisioned enough to serve chat. v1-tolerant.

	Called only AFTER the local signup + LLM-credential checks have passed, at
	the managed ready-exits of ``is_ready_for_chat`` — it is the final gate.

	Returns ``{"ready": True, "reason": None}`` UNLESS admin is reachable AND
	reports a ``chat_readiness`` != ``"Ready"``, in which case
	``{"ready": False, "reason": <code>, "detail": <admin's sentence>}``. The
	code is ``"authority_repair_required"`` for ``SupportRequired`` (a paged
	incident, no self-service action), ``"reconnect_required"`` for
	``ReconnectRequired`` (slice 4b: reconnect the provider), ``"subscription_suspended"``
	for ``Suspended`` (renew), ``"container_unavailable"`` for ``Unavailable``
	(jarvis#885: admin's health cron confirmed the container is dead/unhealthy -
	an outage, not a wait) and ``"container_provisioning"`` otherwise (wait) -
	each kept distinct so a customer isn't told to wait for a container that won't
	come back, or to keep spinning on a strand that only a reconnect can clear.

	- v1-tolerance: an ABSENT ``chat_readiness`` key (v1 admin, or a v2 that
	  doesn't surface it) means the control plane has no opinion → allow.
	- Resilience: a ``get_connection`` failure (unreachable / auth / timeout) is
	  answered by cohort - fail open for an established workspace, fail closed
	  with a retryable code for one that has never been confirmed ready. See
	  ``_admin_unreachable_verdict``. A verdict admin RENDERED is never cached, so
	  a transient block clears on the very next load; the unconfirmed verdict gets
	  a few seconds so a polling wizard cannot amplify an outage.
	"""
	raw = _settings_raw(_GATE_STATE_FIELDS)
	cache = frappe.cache()
	cache_key = f"{_CHAT_GATE_CACHE_KEY}:{_gate_revision(raw)}"
	cached = compat.cache_get_memoized(cache_key)
	if cached:
		if isinstance(cached, dict) and cached.get("unconfirmed"):
			return _admin_unreachable_verdict(
				raw, diag_code=cached.get("diag_code") or "", retryable=cached.get("retryable")
			)
		# The billing banner rides the cached verdict. Caching a bare flag would
		# hide an expiring/grace notice for the whole TTL on every ready load.
		# Tolerate the pre-upgrade shape (a bare 1) rather than re-asking admin.
		notice = cached.get("notice") if isinstance(cached, dict) else {}
		return {"ready": True, "reason": None, "billing_notice": notice or {}}
	try:
		conn = admin_client.get_connection(timeout_s=8) or {}
	except Exception as err:
		# A site whose account was reconnected elsewhere fails auth here forever, so
		# failing open sends it into a chat that cannot work. Ask the one question it
		# can still ask before deciding.
		moved = _site_replacement()
		if moved.get("replaced"):
			return {"ready": False, "reason": "site_replaced", "replaced_notice": moved, "billing_notice": {}}
		diag_code, retryable = _diagnostic_class(err)
		verdict = _admin_unreachable_verdict(raw, diag_code=diag_code, retryable=retryable)
		if verdict.get("reason") == _UNCONFIRMED_REASON:
			# Same revision key as the positive verdict, so a save drops this too -
			# a customer who fixes their config is never held behind an outage's
			# leftovers. Only the outage FACT + its diagnostic class are cached (never
			# the cohort decision), so the cohort is re-evaluated even inside the
			# window and a polling wizard gets a stable verdict shape.
			cache.set_value(
				cache_key,
				{"unconfirmed": True, "diag_code": diag_code, "retryable": retryable},
				expires_in_sec=_UNCONFIRMED_CACHE_TTL_S,
			)
		return verdict
	# Refresh the locally-mirrored release notice on this gate's cadence so an
	# active user sees an activate/clear without waiting for the daily sync.
	release_notice.persist(conn.get("release_notice") or {})
	notice = conn.get("billing_notice") or {}
	if "chat_readiness" in conn and conn["chat_readiness"] != "Ready":
		# Authority-repair incident (review plan 04 P0-6): admin's strict resolver
		# found the customer's serving row ambiguous or the pointer invalid, so
		# there is NO single container we may serve. Admin already returned the
		# safe TENANT_AUTHORITY_REPAIR_REQUIRED envelope (chat_readiness =
		# "SupportRequired", every self-service action withdrawn) and paged ops.
		# The bench must surface that as its own honest not-ready state: NOT
		# "container_provisioning" (which invites the customer to keep waiting for
		# a container that isn't coming) and NOT "subscription_suspended" (which
		# offers a Renew that cannot help and could double-charge). The admin's
		# own sentence - "your payment is safe, please don't retry" - rides
		# ``detail`` and is the customer-facing copy.
		if conn["chat_readiness"] == "SupportRequired":
			return {
				"ready": False,
				"reason": "authority_repair_required",
				"billing_notice": {},
				"detail": conn.get("chat_readiness_reason") or "",
			}
		# Slice 4b (C10b): an aged onboarding OAuth strand whose subscription connect
		# never landed. The customer cannot self-heal by waiting - only by reconnecting
		# their provider - so this MUST NOT bucket into "container_provisioning" below
		# (which the onboarding UI renders as the endless "bringing your setup online"
		# spinner). Its own reason surfaces the terminal-STOP-with-Reconnect card;
		# admin owns the wording via ``chat_readiness_reason``. billing_notice stays
		# empty like SupportRequired: the action is a reconnect, not a billing banner.
		if conn["chat_readiness"] == "ReconnectRequired":
			return {
				"ready": False,
				"reason": "reconnect_required",
				"billing_notice": {},
				"detail": conn.get("chat_readiness_reason") or "",
			}
		# admin-v2's health cron marks a container "Unavailable" once it is
		# confirmed dead/unhealthy - a DIFFERENT fact than "Provisioning"
		# (the two states are mutually exclusive). Bucketing this into the
		# generic "container_provisioning" fallback below tells a customer
		# whose workspace just died that it is "coming online", which is
		# false and stalls them on a spinner that never resolves. Its own
		# reason lets the frontend render an honest outage message instead.
		if conn["chat_readiness"] == "Unavailable":
			return {
				"ready": False,
				"reason": "container_unavailable",
				"billing_notice": {},
				"detail": conn.get("chat_readiness_reason") or "",
			}
		suspended = conn["chat_readiness"] == "Suspended"
		return {
			"ready": False,
			"reason": "subscription_suspended" if suspended else "container_provisioning",
			# Carried even when blocked: "detail" is the ADMIN wording, and a
			# member who cannot renew needs a different call to action.
			"billing_notice": notice,
			# Admin owns the wording (jarvis_admin_v2.billing.entitlement) so the
			# two sides can't drift into different explanations. A v1/older admin
			# sends no reason; the SPA falls back to its own copy.
			"detail": conn.get("chat_readiness_reason") or "",
		}
	# Reachable + (Ready, or v1-absent) → allow and cache the positive verdict.
	# But only an EXPLICIT Ready earns the established marker (review P0-07): the
	# v1-tolerant missing-key path allows this page load without minting proof admin
	# never supplied - a workspace whose control plane has never once said "Ready"
	# must not be promoted into the cohort whose chat survives an outage.
	if conn.get("chat_readiness") == "Ready":
		_mark_chat_ready(raw)
	cache.set_value(cache_key, {"notice": notice}, expires_in_sec=_CHAT_GATE_CACHE_TTL_S)
	return {"ready": True, "reason": None, "billing_notice": notice}


@frappe.whitelist()
def is_onboarded() -> dict:
	"""True iff Jarvis Settings holds an admin api_key. The wizard's
	completion-card branch and the account page's redirect guard share this.

	Pool-pending customers (paid but no tenant yet) still count as onboarded -
	they've completed signup; the agent_url just hasn't been wired up. The
	account page handles that state via tenant_status: pending.
	"""
	settings = frappe.get_single("Jarvis Settings")
	api_key = (
		settings.get_password(
			"jarvis_admin_api_key",
			raise_exception=False,
		)
		or ""
	).strip()
	return {"onboarded": bool(api_key)}


_REPLACED_CACHE_KEY = "jarvis:site_replacement"
_REPLACED_CACHE_TTL_S = 300


def _site_replacement() -> dict:
	"""Cached {replaced, at, moved_to}. Cached both ways: a replaced site would
	otherwise ask on every gate miss, and an unreplaced one on every admin blip."""
	cache = frappe.cache()
	hit = cache.get_value(_REPLACED_CACHE_KEY, expires=True)
	if isinstance(hit, dict):
		return hit
	try:
		out = admin_client.site_replacement() or {}
	except Exception:
		out = {}
	verdict = {
		"replaced": bool(out.get("replaced")),
		"at": out.get("at") or "",
		"moved_to": out.get("moved_to") or "",
	}
	cache.set_value(_REPLACED_CACHE_KEY, verdict, expires_in_sec=_REPLACED_CACHE_TTL_S)
	return verdict


@frappe.whitelist()
def is_ready_for_chat() -> dict:
	"""Pre-flight check used by /jarvis-chat's page load to decide whether to
	render the chat surface or redirect the customer to /jarvis-onboarding.

	Stricter than ``is_onboarded`` - signup (admin api_key) AND a usable LLM
	credential for the active ``llm_auth_mode`` must be in place. A pool
	tenant mid-RE-save still counts as ready (the container keeps serving
	its previous config), but a pool whose FIRST apply never succeeded does
	not.

	Returns ``{ready: bool, reason: str | None}`` where ``reason`` is one of:

	- ``"signup"`` - jarvis_admin_api_key is empty (customer hasn't completed
	  the wizard's signup step).
	- ``"llm_credentials"`` - signup done, but LLM creds for the active
	  auth mode are missing. api_key mode needs llm_api_key + llm_provider +
	  llm_model; subscription / oauth modes need llm_oauth_connected_at
	  (the timestamp set when the oauth grant completes). Soft: banner, not
	  the wizard gate — the workspace was established and stays reachable.
	- ``"llm_setup"`` - creds missing AND this workspace never completed
	  onboarding: no LLM config ever confirmed and the subscription never
	  went Active (e.g. a failed-payment signup). Hard: routes back to the
	  wizard — chat cannot work and there is no history to protect.
	- ``"llm_pool_provisioning"`` - a pool is configured (pool mode) but
	  no sync has ever applied it to the container (first sync pending or
	  failed).
	- ``"llm_applying"`` - the same still-converging window as
	  ``llm_pool_provisioning`` / ``llm_provisioning``, but for a workspace
	  that has been chat-ready before (``_has_been_chat_ready``) - e.g. an
	  established, chatting customer whose FIRST pool leg is mid-apply after
	  adding a model in Settings. Soft, like ``llm_credentials``: a reload
	  during this window must keep the customer in the app, not send them
	  back to the setup wizard (jarvis C2). Never returned for a workspace
	  that has never been confirmed ready - that stays on the hard
	  ``llm_pool_provisioning`` / ``llm_provisioning`` reason unchanged. Also
	  TIME-BOXED (review finding 1): only while ``last_sync_requested_at`` is
	  present and within ``_APPLYING_SOFT_WINDOW_S`` of now - a stuck apply
	  that never converges and never writes a terminal status ages out of the
	  soft window and falls back to the hard reason, so it cannot stay soft
	  forever. The container may briefly be unavailable while a first pool
	  transition bounces it (10-30s); this reason is only about keeping the
	  customer IN the app during that window, not a claim that chat keeps
	  answering uninterrupted.
	- ``"llm_apply_stuck"`` - the established-workspace apply that ``llm_applying``
	  covered has aged past ``_APPLYING_SOFT_WINDOW_S`` without ever converging or
	  writing a terminal ``"failed:"`` status (fleet-agent down, a sync that hangs).
	  Before jarvis#825 this silently fell back to the hard
	  ``llm_pool_provisioning`` / ``llm_provisioning`` reason, bouncing an
	  established customer to the setup wizard with no explanation. Now it is its
	  own honest, retryable state: the readiness surfaces render "your last AI
	  update didn't finish" and offer Jarvis Admins a Retry that calls
	  ``jarvis.onboarding.resync_llm`` (probe-first, throttled), which restamps
	  ``last_sync_requested_at`` and flips the reason back to ``llm_applying``.
	  Only reachable for a workspace that HAS an apply on record; a never-applied
	  one keeps the hard reason.
	- ``"llm_rejected"`` - the pool/api_key/subscription config's FIRST sync ended
	  in a terminal ``last_sync_status`` of ``"failed: ..."`` AND admin's own
	  refusal is what produced it (an unusable spec, a validation error, a
	  subscription needing re-authentication) - re-submitting the SAME config
	  would just be refused again. ``detail`` carries the recorded reason
	  verbatim (jarvis#757; see ``_rejected_sync_verdict``). A terminal
	  ``"failed: ..."`` from a transient cause instead (admin unreachable,
	  rate-limited, a lock timeout, an unclassified error) is deliberately
	  NOT this reason - retrying could clear it, so it falls through to the
	  same ``llm_pool_provisioning``/``llm_provisioning`` verdict a
	  still-converging sync gets, which is what lets the wizard keep polling
	  and, at its ceiling, offer Retry (jarvis#757 review, gap 1).
	- ``"container_provisioning"`` - all local checks passed, but admin reports
	  the container isn't chat-ready yet (chat_readiness != "Ready"). Set only by
	  the final ``_admin_chat_gate`` at the managed ready-exits; fail-open and
	  v1-tolerant (see ``_admin_chat_gate``).
	- ``"readiness_unconfirmed"`` - admin could not be asked AND this workspace
	  has never been confirmed ready, so nothing knows whether a container is
	  serving it. Carries ``retryable: True``: it is the absence of a verdict,
	  not one. An ESTABLISHED workspace does not get this - the same failure
	  leaves it ready (see ``_admin_unreachable_verdict``).
	- ``"authority_repair_required"`` - admin reports an authority-repair incident
	  (chat_readiness == "SupportRequired"): the customer's serving container is
	  ambiguous/invalid and support has been paged. A safe blocked state - the
	  customer must NOT retry payment or reconnect; ``detail`` carries admin's own
	  reassurance (see ``_admin_chat_gate``, review plan 04 P0-6).
	- ``None`` when ``ready`` is True.

	Also carries two non-blocking worker-health fields, merged in AFTER the
	verdict above and never able to change ``ready``/``reason``:

	- ``worker_warning`` (bool) - the background worker lane looks degraded
	  (``jarvis.chat.pump.chat_worker_status()['degraded']``). Soft: banner only.
	- ``worker_blocked`` (bool) - workers confidently read as zero
	  (``...['blocked']``). Still non-blocking here - the hard block on actually
	  SENDING a chat message lives in chat/policy.py, not in this readiness read.

	Fails safe: any exception probing worker health leaves both fields False
	rather than raising or affecting the verdict above.
	"""
	verdict = _ready_verdict()
	try:
		from jarvis.chat.pump import chat_worker_status

		w = chat_worker_status()
		verdict["worker_warning"] = bool(w.get("degraded"))
		verdict["worker_blocked"] = bool(w.get("blocked"))
	except Exception:
		verdict.setdefault("worker_warning", False)
		verdict.setdefault("worker_blocked", False)
	return verdict


def _ready_verdict() -> dict:
	"""Verdict body of ``is_ready_for_chat``; reason codes documented there."""
	settings = frappe.get_single("Jarvis Settings")

	admin_api_key = (
		settings.get_password(
			"jarvis_admin_api_key",
			raise_exception=False,
		)
		or ""
	).strip()
	if not admin_api_key:
		return {"ready": False, "reason": "signup"}

	# Pool mode: being a pool is config INTENT, derived and committed at
	# save time BEFORE the async pool sync runs - it does not prove the
	# container ever received the pool. Gate on evidence of a successful
	# apply instead: llm_pool_synced_at, stamped by the pool-sync job on
	# every "ok" (tenants provisioned before the field existed are
	# backfilled by patch v1_10). A pool that has EVER applied stays ready
	# through a later re-save's transient pending/failed - the container
	# keeps serving its previous config. A fresh tenant whose FIRST sync
	# is still pending or failed is NOT ready: sending them to chat
	# guarantees failing turns while onboarding still shows "provisioning"
	# (JARVIS-2026-07-08 split-brain).
	#
	# Deliberately NOT a last_sync_status check: that field is shared with
	# the single-model sync, so a stale legacy "ok (reload via admin)" from
	# a queued creds job could falsely open the gate for a never-applied
	# pool.
	#
	# Keyed on pool MODE, not the narrower proxy_active: a BYO api-key pool has
	# no sidecar but still syncs through /llm-pool and stamps llm_pool_synced_at,
	# so gating it on the direct marker would strand it forever.
	#
	# An unstamped marker means "this bench never RECORDED an apply", which is not
	# the same as "no apply happened" - the sync job can hand off to the scheduled
	# reconcile without stamping. So both provisioning exits below ask admin once
	# before declaring the workspace not ready (_confirm_apply_via_admin, #576);
	# the reason strings still mean exactly what they say, they are now just also
	# backed by the control plane declining to confirm.
	if compute_pool_mode(settings):
		if getattr(settings, "llm_pool_synced_at", None) or _confirm_apply_via_admin(settings, is_pool=True):
			return _admin_chat_gate()
		return _not_ready_verdict(settings, _settings_raw(_GATE_STATE_FIELDS), "llm_pool_provisioning")

	auth_mode = (getattr(settings, "llm_auth_mode", "") or "api_key").strip()

	if auth_mode == "api_key":
		llm_key = (
			settings.get_password(
				"llm_api_key",
				raise_exception=False,
			)
			or ""
		).strip()
		provider = (getattr(settings, "llm_provider", "") or "").strip()
		model = (getattr(settings, "llm_model", "") or "").strip()
		if not (llm_key and provider and model):
			return _llm_missing_verdict(settings)
		# Local key/provider/model presence is config INTENT (committed at save,
		# before the async admin apply runs) — it does NOT prove the container ever
		# received the creds. Gate on evidence of a CONFIRMED apply instead
		# (round-4 review R4-P0-6 / P1-10): llm_direct_synced_at is stamped only on
		# admin status="applied". A direct tenant that has EVER confirmed stays
		# ready through a later re-save's transient "applying" (the container keeps
		# serving its previous key); a FRESH tenant whose first apply is still
		# pending/failed is NOT ready — opening chat there guarantees failing turns
		# while onboarding still shows "applying". Legacy direct tenants are
		# backfilled by patch v2_00_backfill_llm_direct_synced_at.
		if not getattr(settings, "llm_direct_synced_at", None) and not _confirm_apply_via_admin(
			settings, is_pool=False
		):
			return _not_ready_verdict(settings, _settings_raw(_GATE_STATE_FIELDS), "llm_provisioning")
	elif auth_mode == "subscription":
		# Unified models[]-table subscription on the DIRECT leg (jarvis#715 step
		# 3): no cliproxy sidecar, so - like api_key direct above - this gates on
		# a CONFIRMED /llm-creds apply (llm_direct_synced_at), NOT
		# llm_oauth_connected_at. That marker belongs to the legacy flat-field
		# oauth flow only: save_llm_pool unconditionally clears it on every
		# models[]-table save (see its comment), so gating here on it would
		# never open chat for this leg.
		#
		# The marker short-circuits BOTH inner checks below, same as the plain
		# `not getattr(...) and not _confirm_apply_via_admin(...)` shape used
		# everywhere else in this function - an established tenant never pays a
		# has_configured_subscription_model call, let alone an admin round-trip.
		if not getattr(settings, "llm_direct_synced_at", None):
			# jarvis#755 review: a tenant that never configured a subscription at
			# all used to fall straight into the generic llm_provisioning
			# verdict below, identical to one mid-apply. Distinguish "nothing to
			# confirm" from "confirmation pending" first, mirroring the
			# api_key/oauth branches' own missing-verdict exit above.
			from jarvis.jarvis.pool_serialize import has_configured_subscription_model

			if not has_configured_subscription_model(settings):
				return _llm_missing_verdict(settings)
			if not _confirm_apply_via_admin(settings, is_pool=False):
				return _not_ready_verdict(settings, _settings_raw(_GATE_STATE_FIELDS), "llm_provisioning")
	elif auth_mode == "oauth":
		# The legacy flat-field direct-oauth flow: llm_oauth_connected_at is
		# set (read-only) when the oauth grant completes and the admin
		# pushes the auth-profile blob to the container.
		if not getattr(settings, "llm_oauth_connected_at", None):
			return _llm_missing_verdict(settings)
	else:
		# Unknown auth_mode - treat as misconfigured; the wizard owns it.
		return {"ready": False, "reason": "llm_credentials"}

	return _admin_chat_gate()


# Damping for _confirm_apply_via_admin. Only a NEGATIVE outcome is cached: a
# confirmation stamps the durable marker, which ends the question for good.
#
# Needed because the two not-ready exits below are polled hard. OnboardingView's
# legacy readiness wait runs a 2.5s probe loop after a Connect, and the desk
# banner + widget probe on their own schedules, so an unproven tenant would put
# one admin round-trip on every one of those ticks. The budget is now
# leg-dependent (onboarding.save_llm_pool.readiness_budget_s): 75s for a
# single-restart api_key/oauth apply, 300s for the dual-restart subscription leg
# (jarvis#715 step 3). This 10s damp still costs at most one extra poll before a
# convergence is noticed, and cuts the round-trips a poll generates by roughly two
# thirds; on the widened 300s subscription poll that is ~30 damped round-trips per
# connect instead of ~7, which is acceptable and priced in here so the next reader
# does not rediscover it.
_APPLY_CONFIRM_MISS_KEY = "jarvis:apply_confirm_miss"
_APPLY_CONFIRM_MISS_TTL_S = 10


def _confirm_apply_via_admin(settings, *, is_pool: bool) -> bool:
	"""Last chance to confirm an apply this bench never recorded: ask admin.

	The two callers sit at ``is_ready_for_chat``'s provisioning exits, reached
	when the durable evidence-of-apply marker is unstamped. Those exits are what
	renders "Chat may not work yet / applying your LLM configuration", and until
	now they returned that verdict WITHOUT ever asking the control plane — while
	the neighbouring proven-tenant branch asks it directly via
	``_admin_chat_gate``. So a tenant whose apply converged after the sync job's
	in-band poll gave up was told chat would not work while the container was
	demonstrably serving turns, and nothing could correct it but the ``*/5``
	``reconcile_pending_llm_sync`` tick (jarvis #576). The surface that reports
	the problem can now also clear it.

	No new way to become ready: this reuses ``_admin_chat_readiness`` and
	``_stamp_converged_ok``, the exact pair the scheduled reconcile and the
	onboarding poller's ``_reconcile_pending_applying`` already use. Admin gates
	``chat_readiness`` "Ready" on ``applied_version >= desired_version``, so it
	never reports Ready from intent — the evidentiary bar is unchanged.

	Deliberately does NOT commit, unlike ``_reconcile_pending_applying``. That
	one runs only from the onboarding poller; this runs from ``boot.py`` too, on
	a desk-boot GET, and injecting a commit into boot would make this gate commit
	whatever else that request happens to be carrying. The SPA, the desk widget
	and the banner all reach ``is_ready_for_chat`` through frappe-ui's ``call``,
	which POSTs, so the stamp lands durably where it matters; on a GET it rolls
	back and the next probe simply re-confirms. Correct either way — persistence
	is the optimisation, not the mechanism.

	Fails closed and never raises: an unreachable admin, a non-Ready verdict or a
	failed write all leave the provisioning verdict standing.
	"""
	try:
		cache = frappe.cache()
		if cache.get_value(_APPLY_CONFIRM_MISS_KEY, expires=True):
			return False
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
			_admin_chat_readiness,
			_stamp_converged_ok,
		)

		state, _reason = _admin_chat_readiness()
		if state != "Ready":
			cache.set_value(_APPLY_CONFIRM_MISS_KEY, 1, expires_in_sec=_APPLY_CONFIRM_MISS_TTL_S)
			return False
		# Stamps the marker for this leg AND clears last_sync_status to a
		# converged "ok" — which is the second face of #576, the Settings pane
		# still showing "Applying to your agent" off that same field.
		_stamp_converged_ok(settings, is_pool=is_pool)
		return True
	except Exception:
		return False


# jarvis#757 review, gap 1: a terminal ``"failed: ..."`` status is not one fact,
# it is at least two, and the first shipped ``_rejected_sync_verdict`` treated
# them as the same:
#
# - genuinely REJECTED: admin looked at this exact config and refused it (an
#   unusable spec, a validation error, a subscription that needs
#   re-authentication). Re-submitting the identical config will be refused
#   again - there is nothing to wait for, and the wizard must say so and send
#   the customer back to the form, with no Retry.
# - merely FAILED, transiently: admin was unreachable, rate-limited, a sync
#   lock timed out, or something unclassified blew up. The config itself may
#   be perfectly fine - a retry (the customer's own Retry click, or the next
#   sync a re-save enqueues) could clear it outright. Telling this customer
#   "that connection was rejected" and refusing to let them retry is simply
#   false, and it is the false claim gap 1 of the #757 review exists to stop.
#
# ``last_sync_status`` is one free-text field with no second field to carry
# this distinction, and jarvis_settings.py's ``_sync_via_admin`` / ``sync_pool_now``
# do not hand this function the exception that produced it - only the flattened
# string, written long before (a prior poll, possibly a prior process). So the
# only signal available is the literal text those two functions write, and this
# allowlist leans on it deliberately, NOT on admin's own free-form prose (matching
# an admin-authored sentence is exactly what broke the failed-payment resume -
# see ``AdminValidationError``'s docstring in jarvis/exceptions.py): every
# terminal write in those two functions uses one of a small, closed set of
# literal tokens right after ``"failed:"``, tokens THIS codebase wrote, not
# admin's. Two of those shapes are structurally guaranteed, not guessed: both
# ``_admin_rejection_reason`` and ``_admin_customer_facing_reason`` (jarvis_settings.py)
# check their own output starts with "Your " before using it, and they are the
# ONLY writers of the bare ``f"failed: {reason}"`` shape (no other token in
# front of the reason) - so ``"failed: Your "`` reliably means one of those two
# ran, and both only run for a genuine, admin-decided refusal.
#
# This is an ALLOWLIST, not a denylist: an unrecognised "failed:" shape (a
# status format a future change adds without updating this list) stays on the
# side that was already correct before jarvis#757 existed - falls through as
# "still provisioning", retryable - rather than a newly-invented "rejected"
# telling a customer their working connection was refused. The coupling this
# creates is real: change one of the literal prefixes in jarvis_settings.py
# without updating this list and a case silently reclassifies. The fix that
# removes the coupling is a dedicated field (e.g. a Check
# ``last_sync_is_rejection`` stamped by jarvis_settings.py at each of these
# same call sites) so this function reads a fact instead of re-deriving one
# from text shape - that needs a doctype change and a migration, which this
# fix deliberately avoids; see the #757 gap-1 report for the tradeoff.
def _is_genuine_rejection_status(status: str) -> bool:
	if status.startswith("failed: validation: "):
		# AdminValidationError (jarvis_settings.py's _sync_via_admin /
		# sync_pool_now): the pushed payload itself was invalid (e.g. a
		# missing/malformed oauth_blob) - the customer's config is the problem.
		return True
	if status == "failed: subscription needs re-authentication (blocked)":
		# The pool leg's "blocked" apply status: only a fresh re-auth (which
		# happens on this same editable form) can ever clear it.
		return True
	if status.startswith("failed: Your "):
		# _admin_rejection_reason / _admin_customer_facing_reason: a config
		# admin was REACHED and PERMANENTLY refused (AdminRejectedError, or the
		# pool leg's structured-rejection AdminUnreachableError branch).
		return True
	return False


def _rejected_sync_verdict(settings) -> dict | None:
	"""jarvis#757 (gap-1 hardening): a terminal ``last_sync_status`` of
	``"failed: ..."`` is not the same fact as "still provisioning", but it is
	also not ONE fact either - see ``_is_genuine_rejection_status`` above for
	the split and why it is drawn where it is.

	``is_ready_for_chat``'s three provisioning exits (pool, api_key, subscription)
	each answer "nothing has confirmed this leg applied yet" with the SAME generic
	reason whether the last attempt is genuinely still converging (``last_sync_status``
	is ``"pending: ..."`` or unset), transiently failed (unreachable, rate-limited,
	a lock timeout - a retry could clear it), or admin permanently refused it. Only
	the last of those three is a rejection: waiting AND retrying are both useless
	against it, so the Connect step's readiness wait stops rather than grinding to
	its five-minute ceiling over a config admin had already, explicitly, turned
	down. The other two stay on the generic provisioning reason, unchanged, so the
	wizard keeps polling and a genuinely stuck one still reaches its own Retry at
	the ceiling - a transient failure must never be told "rejected, no retry".

	Returns the ``{"ready": False, "reason": "llm_rejected", "detail": ...}`` verdict
	only for a genuine rejection (``_is_genuine_rejection_status``), or ``None``
	otherwise - a blank/``"pending: ..."`` status, an unrecognised "failed:" shape,
	or a known-transient one all fall through to the caller's own generic
	provisioning reason unchanged.
	"""
	status = (settings.get("last_sync_status") or "").strip()
	if not status.startswith("failed:") or not _is_genuine_rejection_status(status):
		return None
	detail = status[len("failed:") :].strip()
	return {
		"ready": False,
		"reason": "llm_rejected",
		"detail": detail or "Your AI configuration was rejected.",
	}


# jarvis#760: is_ready_for_chat's three provisioning exits (pool, api_key,
# subscription) each answered "nothing has confirmed this leg applied yet" by
# inlining the SAME `_rejected_sync_verdict(settings) or _provisioning_verdict(...)`
# expression - and #760's own review found that a new reason added at one site
# and missed at another silently reopened the gate. One helper, called at all
# three sites, closes that trap: there is now exactly one place to update when
# either half of the combinator changes.
def _not_ready_verdict(settings, raw: dict, hard_reason: str) -> dict:
	"""Single combinator for is_ready_for_chat's three still-converging exits.

	``_rejected_sync_verdict`` takes priority: a genuine, admin-decided
	rejection is never softened, whether or not the workspace is established -
	re-submitting the identical config would just be refused again, so there is
	nothing to wait for. Only when that returns ``None`` does the still-
	converging ``_provisioning_verdict`` get a say between the soft
	``llm_applying`` reason and ``hard_reason``.

	``raw`` is fetched ONCE by the caller (``is_ready_for_chat``) rather than
	here, so the happy path (an already-applied leg, the overwhelming majority
	of calls) never pays the extra ``tabSingles`` read - only these not-ready
	exits need it at all, and each call site takes exactly one branch.
	"""
	return _rejected_sync_verdict(settings) or _provisioning_verdict(raw, hard_reason)


# jarvis C2: a reload during the mid-apply window must not show an established,
# chatting customer the full-screen setup poster. The gate string alone cannot
# tell "never onboarded" from "onboarded, first pool/direct leg mid-apply" - both
# reach here with the SAME unstamped evidence marker - so the only signal that
# can tell them apart is _has_been_chat_ready's durable, authority-bound proof.
#
# TIME-BOXED (review finding 1): softening to llm_applying also requires
# last_sync_requested_at to be present and recent (_apply_age_seconds below
# _APPLYING_SOFT_WINDOW_S). Without a bound, a STUCK first apply - the
# fleet-agent down, an apply that never converges and never writes a terminal
# "failed:" status - would stay soft indefinitely with no way out: the reload
# it was built to fix would instead trap the customer in a chat that can never
# actually work, forever, with no wizard to fall back to. Aging out of the
# window degrades to the hard reason, which is exactly the pre-PR behaviour -
# a stuck apply was always routed to the setup wizard before this fix existed.
def _provisioning_verdict(raw: dict, hard_reason: str) -> dict:
	"""Not-ready verdict for is_ready_for_chat's three still-converging exits
	(pool, api_key, subscription), downgraded to the soft ``llm_applying``
	reason for a workspace that has been chat-ready before AND whose apply was
	requested recently enough to still be plausibly converging.

	Takes ``raw`` from the caller (see ``_not_ready_verdict``) rather than
	fetching it itself, so a single call to ``is_ready_for_chat`` never issues
	more than one ``_settings_raw`` read for this decision.

	A FRESH tenant - no durable marker, or one whose authority anchor no
	longer matches the current (principal, container, generation) after a
	reset/reconnect - keeps the unchanged hard reason: it must still route to
	the setup wizard, because chat genuinely cannot work there and there is no
	history to protect. Likewise an ESTABLISHED tenant whose apply was
	requested too long ago (or never recorded a request at all) keeps the hard
	reason too - see the module comment above.
	"""
	if not _has_been_chat_ready(raw):
		return {"ready": False, "reason": hard_reason}
	# One parse+diff for both boundaries (see _apply_age_seconds). None = no apply
	# on record (never enqueued, or unparseable): a workspace with nothing to be
	# mid-apply OR stuck on, so it keeps the hard reason.
	age = _apply_age_seconds(raw.get(_APPLYING_TIMESTAMP_FIELD))
	if age is None:
		return {"ready": False, "reason": hard_reason}
	if age < _APPLYING_SOFT_WINDOW_S:
		# Requested recently enough to still be plausibly converging: soft.
		return {"ready": False, "reason": "llm_applying"}
	# An apply WAS requested but has aged out of the soft window without
	# converging: a stuck apply (jarvis#825). Surface an honest, retryable state
	# instead of silently routing an established, chatting customer to the setup
	# wizard.
	return {"ready": False, "reason": "llm_apply_stuck"}


def _llm_missing_verdict(settings) -> dict:
	"""LLM creds absent for the active mode: wizard or banner?

	An ESTABLISHED workspace gets the soft ``llm_credentials`` banner — any LLM
	config ever confirmed (a synced/connected marker survives creds expiry), or
	an Active subscription (the workspace-reset "disconnect AI model
	connections" option clears every marker; its owner reconnects via
	Settings -> AI models, and the wizard would dead-end them at signup's
	duplicate guard). Never lock such a workspace away from chat + history over
	a recoverable credential problem.

	A workspace that has NEVER had a working LLM and whose subscription never
	went Active is still MID-ONBOARDING (e.g. a failed-payment signup, refresh):
	``llm_setup`` hard-gates back to the wizard — chat cannot work there, and
	the half-created signup resumes via start_signup's authenticated fallback.
	Subscription state comes from admin; fail OPEN to the soft banner when it
	is unknown/unreachable — EXCEPT when admin was reached and refused this site
	outright (see the 403 branch)."""
	never_synced = not (
		getattr(settings, "llm_direct_synced_at", None)
		or getattr(settings, "llm_pool_synced_at", None)
		or getattr(settings, "llm_oauth_connected_at", None)
	)
	if not never_synced:
		return {"ready": False, "reason": "llm_credentials"}
	try:
		from jarvis import admin_client

		sub_status = (admin_client.get_connection(timeout_s=8) or {}).get("subscription_status") or ""
	except AdminAuthError as e:
		# A 403 is admin REACHED and refusing this site's principal. That refusal is
		# how the llm_setup gate below came to be UNREACHABLE for the cohort it was
		# written for: a Pending Payment customer 403s at
		# jarvis_admin_v2.api._auth.current_customer (allow_pending False) instead of
		# answering with a subscription_status, so they fell through to the soft
		# banner and landed in the chat app with a "no AI connected" note rather than
		# back in the wizard that can finish their payment.
		#
		# ONLY the never-paid shapes hard-gate, and only when admin's own words say
		# so - see _is_never_paid_403. Every other 403 (Cancelled, a bodyless
		# proxy/WAF rejection, anything unrecognised) stays SOFT, because the
		# renew/suspension banner owns those states and the wizard would dead-end
		# them at signup's duplicate guard. Suspended cannot arrive here at all:
		# Jarvis Customer propagates Suspended to User.enabled=0
		# (jarvis_admin_v2/.../jarvis_customer.py:81), so that customer's bench is
		# rejected by Frappe auth with a 401 long before current_customer runs.
		if _is_never_paid_403(e):
			return {"ready": False, "reason": "llm_setup"}
		sub_status = ""
	except Exception:
		sub_status = ""
	# Hard-gate ONLY the never-paid shapes. Active is established (the revoke
	# case); Suspended/Cancelled stay SOFT too — the renew/suspension banner
	# path owns them, and the wizard would dead-end them at the dedup guard.
	if sub_status in ("none", "Pending Payment", "Pending Verification"):
		return {"ready": False, "reason": "llm_setup"}
	return {"ready": False, "reason": "llm_credentials"}


@frappe.whitelist()
def get_llm_apply_operation(operation_id: str) -> dict:
	"""Read-only status of a durable LLM-apply operation (plan-05 D2), for the SPA's
	single Start-chatting controller to follow one operation to a terminal state.

	A thin System-Manager-gated shim over admin's read-only endpoint (see
	admin_client.get_llm_apply_operation): it never spends the apply rate bucket, so
	the controller polls it with backoff. The bench holds no operation state of its
	own - admin owns the operation's truth - so this forwards the opaque id and
	surfaces admin's §8.4 status verbatim. Errors arrive as clean frappe.throw
	toasts via the shared _surface helper; the SPA client seam
	(frontend/src/lib/llmOperation.js) treats a transport failure as "keep polling",
	not a verdict.

	Side effect (plan-05 D2 paired follow-up): admin moved the fleet push OFF the
	synchronous save (admin #193), so ``save_llm_pool`` no longer sees an inline
	apply result to stamp the AI-models per-model verdicts from. Admin now carries
	the fleet's probe verdicts on the CONVERGED operation status, so this fold them
	into the same bench settings cache the settings panel already reads - see
	``_persist_operation_probe_verdicts``. That makes this poll a (bounded) writer,
	so it must be reached over POST (frappe-ui's ``call`` always POSTs) for the
	write to be durable; a read that never converges writes nothing.
	"""
	require_jarvis_admin()
	status = _surface(admin_client.get_llm_apply_operation, operation_id)
	_persist_operation_probe_verdicts(status)
	return status


def _persist_operation_probe_verdicts(status: dict) -> None:
	"""Fold the fleet's probe verdicts off a converged operation status into the
	bench settings cache the AI-models panel reads (``last_subscription_status`` /
	``last_sync_warnings`` / ``last_model_statuses`` via ``get_llm_config`` ->
	LlmPoolEditor). Plan-05 D2 paired follow-up: admin #193 moved the fleet push off
	the synchronous save, so these verdicts arrive on the operation status once the
	apply has CONVERGED rather than on the save's (now push-less) return - without
	this, the panel would blank after a save with nothing to repopulate it.

	Present ONLY once converged; ABSENT (still applying, or an old admin) leaves the
	prior verdicts intact - never blanks them. An ``unchanged`` no-op apply ran no
	probe, so its "unchecked" / ``[]`` must not discard the last real verdict (same
	rule as ``jarvis_settings._stamp_pool_applied_ok``). A no-op when the values
	already match, so a hot poll does not churn the Singles row.

	NOTE: the exact field shape (subscription_status: str, warnings: list,
	model_statuses: list, unchanged: bool) mirrors the fleet-agent verdict contract
	the async worker already persists; it must match the shape admin's operation
	status projection emits (admin plan-05 branch). Never coerces a mismatch."""
	if not isinstance(status, dict):
		return
	# Not converged yet (still applying) / an old admin: no probe fields -> leave the
	# last real verdict untouched rather than blanking the panel.
	if not any(k in status for k in ("subscription_status", "warnings", "model_statuses")):
		return
	# A byte-identical no-op apply ran no probe (contract 1.10 unchanged=true): its
	# "unchecked"/[] would discard the last real verdict. Keep the prior values.
	if status.get("unchanged"):
		return
	fields = {
		"last_subscription_status": str(status.get("subscription_status") or ""),
		"last_sync_warnings": frappe.as_json(status.get("warnings") or []),
		"last_model_statuses": frappe.as_json(status.get("model_statuses") or []),
	}
	current = (
		frappe.db.get_value("Jarvis Settings", "Jarvis Settings", list(fields.keys()), as_dict=True) or {}
	)
	if all((current.get(k) or "") == v for k, v in fields.items()):
		return  # unchanged from what is stored - do not churn the Singles row per poll
	frappe.db.set_value("Jarvis Settings", "Jarvis Settings", fields, update_modified=False)


@frappe.whitelist()
def get_llm_usage() -> dict:
	"""Real LLM usage for the Monitor tab (System-Manager only, spec 7).

	Two tenants report real $/token cost: a proxied (Bifrost) tenant, curated
	from admin below, and a DIRECT api-key tenant, computed locally in
	``_direct_llm_usage`` from this bench's own per-model token counters
	against the admin-maintained catalog's prices - there is no Bifrost ledger
	for it to read. Every other shape (subscription, oauth - no per-tenant
	$/token exists for either) short-circuits to the empty ``applicable:False``
	shape with no admin round-trip."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	if not getattr(settings, "proxy_active", 0):
		# Blank/unset llm_auth_mode coerces to "api_key" — the field is reqd with
		# that default, and the two other reads in this file (account.py:688, :1511)
		# treat a blank the same way, so a legacy row with no auth mode must still
		# get its BYO-key cost, not fall through to the empty shape.
		if (getattr(settings, "llm_auth_mode", "") or "api_key").strip() == "api_key":
			return _direct_llm_usage()
		return {
			"applicable": False,
			"period": None,
			"tokens_in": 0,
			"tokens_out": 0,
			"cost_usd": 0.0,
			"per_model": [],
			"used_vs_limit": {"used_usd": 0.0, "limit_usd": None},
		}
	data = _surface(admin_client.get_llm_usage) or {}
	data["applicable"] = True
	return data


def _direct_llm_usage() -> dict:
	"""Real cost for a DIRECT (BYO api-key, no Bifrost sidecar) tenant: this
	month's tenant-wide per-model token totals, priced off the admin
	catalog's ``input_price_per_1m_usd`` / ``output_price_per_1m_usd``.

	Returns the SAME shape ``get_llm_usage`` returns for a proxied tenant, so
	the Billing/Metering chart renders it with no frontend change - see
	``frontend/src/charts/usageCharts.js`` for the ``per_model`` row contract
	(``{model, provider, tokens_in, tokens_out, cost_usd}``) this must match."""
	from jarvis.chat import pricing, usage

	month = usage.current_month_key()
	per_model = []
	tokens_in_total = 0
	tokens_out_total = 0
	cost_total = 0.0
	for row in usage.tenant_wide_per_model_tokens(month):
		model = row.get("model")
		tokens_in = int(row.get("in_") or 0)
		tokens_out = int(row.get("out_") or 0)
		price_in, price_out = pricing.price_for_model(model)
		cost = (tokens_in / 1_000_000) * price_in + (tokens_out / 1_000_000) * price_out
		per_model.append(
			{
				"model": model,
				"provider": "",
				"tokens_in": tokens_in,
				"tokens_out": tokens_out,
				"cost_usd": round(cost, 6),
			}
		)
		tokens_in_total += tokens_in
		tokens_out_total += tokens_out
		cost_total += cost
	cost_total = round(cost_total, 6)
	return {
		"applicable": True,
		"period": month,
		"tokens_in": tokens_in_total,
		"tokens_out": tokens_out_total,
		"cost_usd": cost_total,
		"per_model": per_model,
		"used_vs_limit": {"used_usd": cost_total, "limit_usd": None},
	}


@frappe.whitelist()
def get_llm_connection_status() -> dict:
	"""Connection card for Settings, General: how this workspace's LLM config is
	SHAPED (``pool_mode`` / ``proxy_active`` / ``model_count`` / ``routing_mode``)
	and whether it is actually SERVING (``health``). Never returns token material.
	System-Manager only.

	``health`` is decided here, from what the bench already knows, and it is the
	only field the status badge may render. Admin's ``auth_profile_present`` is
	still passed through as ``auth_present`` because support wants the raw claim,
	but it is NOT a verdict about the workspace. A live 4-model pool whose two
	chat subscriptions cliproxy had loaded and was answering turns with still came
	back ``auth_profile_present: false, profile_ids: []``, and trusting that
	boolean painted a red "Not connected" over a workspace whose chat
	demonstrably worked (#561). Why admin sees no profiles is a control-plane bug
	of its own; the point here is that the answer never described a pool in the
	first place, so no value of it should decide this badge.

	That is the same misleading state that was already fixed once for DIRECT
	tenants, by short-circuiting them before the admin round-trip: the raw admin
	payload's leftover fields (a stale/default default_model with
	auth_profile_present false) made the SPA render "Not connected" for a direct
	tenant whose chat verifiably worked. The proxy branch kept trusting the
	boolean, so the identical state came back for pools.

	The honest local signal is the one ``is_ready_for_chat`` already gates chat
	on: the durable marker the fleet stamps when it CONFIRMS an apply. Tying the
	badge to it means the badge and the chat gate cannot disagree - a workspace
	chat let the customer into is green because the same evidence opened both, and
	a workspace whose config never reached its container is red for the same
	reason chat refuses it. See ``_llm_health``.

	Tenants with no Bifrost/cliproxy sidecar (proxy_active=0, which includes a BYO
	api-key pool) still short-circuit before the admin round-trip, mirroring
	get_llm_usage above - there is no proxy auth profile to report.

	``disconnected`` is a state of its own, and it has to be computed FIRST. The
	DIRECT short-circuit below returns before any admin round-trip, so a tenant
	whose connection was torn down (jarvis.onboarding.disconnect_llm) would
	otherwise fall into it and report a healthy-looking config while chat is
	dead. It is derived here rather than added to ``chat_readiness``: that field
	is a shared admin/bench contract with exactly four values, and the bench
	already knows its own config is empty without asking anybody."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	pool_mode = compute_pool_mode(settings)
	# Shape, not health. The SPA needs these to name the topology honestly: a
	# single Model/Provider/Auth-mode triple describes one credential, and a
	# failover pool is not one credential. pool_mode and proxy_active answer
	# DIFFERENT questions and must not be blurred - see compute_proxy_active.
	shape = {
		"pool_mode": pool_mode,
		"model_count": len([m for m in (settings.get("models") or []) if m.enabled]),
		"routing_mode": settings.get("routing_mode") or "",
		"sync_status": settings.get("last_sync_status") or "",
	}
	if not _has_llm_config(settings):
		return {
			**shape,
			"proxy_active": False,
			"disconnected": True,
			"health": "down",
			"attention_reason": "",
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": "",
		}
	if not getattr(settings, "proxy_active", 0):
		health, attention_reason = _llm_health(settings, pool_mode)
		return {
			**shape,
			"proxy_active": False,
			"disconnected": False,
			"health": health,
			"attention_reason": attention_reason,
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": settings.get("llm_model") or "",
		}
	raw = _surface(admin_client.post_llm_auth_status) or {}
	data = raw.get("data", raw) or {}
	health, attention_reason = _llm_health(settings, pool_mode)
	return {
		**shape,
		"proxy_active": True,
		"disconnected": False,
		"health": health,
		"attention_reason": attention_reason,
		"auth_present": bool(data.get("auth_profile_present")),
		"oauth_expires_at": data.get("openai_profile_expires_ms"),
		"profile_ids": data.get("profile_ids", []),
		# Admin answers this with the Bifrost virtual endpoint
		# ("openai_compat/jarvis-pool"), which is not a model the customer picked
		# and not one they would recognise. The bench knows which member the
		# container actually runs first, so prefer that and keep admin's value as
		# the fallback for a shape pool_primary_model cannot read.
		"default_model": pool_primary_model(settings) or data.get("default_model", ""),
	}


# The complete set of values ``get_llm_connection_health`` may ever return. It
# is the SPA's badge vocabulary minus "disconnected" (see below), and it is
# named here so the member contract is one readable list rather than something a
# reader has to reconstruct from branches.
MEMBER_HEALTH_STATES = ("ok", "applying", "attention", "down")


@frappe.whitelist()
def get_llm_connection_health() -> dict:
	"""The Settings Status badge for a workspace MEMBER: is this workspace's
	assistant working right now? Returns exactly one field, ``state``, one of
	:data:`MEMBER_HEALTH_STATES`. Nothing else, ever.

	This exists because the badge used to be a hardcoded green "Connected" for
	anyone who was not an admin (jarvis#711). ``get_llm_connection_status`` above
	is ``require_jarvis_admin``, so a member could not fetch a verdict at all and
	the SPA rendered a placeholder that read as an answer: a member saw green
	while chat was failing, while a save was still applying, and while the
	workspace was disconnected. It was not stale, it was a constant.

	WHAT A MEMBER MAY AND MAY NOT LEARN
	-----------------------------------
	A member gets a HEALTH verdict and no description of the workspace. Not the
	pool shape, model count or routing mode; not model names, provider names or
	base URLs; not profile or account ids; not whether a key or subscription is
	present; not billing state; and no error text from a provider or from a
	failed turn. The admin payload's every other field is deliberately absent
	rather than blanked, so a future field added there cannot ride along here.

	No branch calls admin. ``get_llm_connection_status`` round-trips to
	``post_llm_auth_status`` for a proxied tenant, and doing that here would put
	``proxy_active`` - a topology fact - into the response LATENCY even though it
	is absent from the body. Every input below is a local read, so the work is
	the same shape whatever the workspace runs.

	THE DISCLOSURE DECISION (jarvis#711, settled)
	---------------------------------------------
	``_llm_health`` returns ``(health, attention_reason)``, and folds three
	distinct causes into ``attention``: a failed apply (``sync_failed``), a
	failed chat turn (``turn_error``, added for #678) and a chat subscription the
	fleet's probe rejected (``subscription_unverified``, split out for #714).

	``health`` on its own carries no shape. It says whether the assistant is
	serving, which is a workspace-level statement true of an api-key tenant and a
	subscription tenant alike. ``attention_reason`` is different:
	``subscription_unverified`` can only ever arise on a workspace that
	authenticates with a CHAT SUBSCRIPTION rather than an api key, so handing it
	to a member tells them which kind of credential this workspace runs on. That
	is pool shape, and shape is exactly what a member may not have.

	The decision: the member gets ``health`` and no cause. ``attention_reason``
	is dropped here, not upstream, and it is OMITTED rather than mapped:

	* Mapping is the trap. Any member-visible value reachable only from
	  ``subscription_unverified`` recovers it exactly, and even a lossy mapping
	  narrows the credential kind by elimination. Omission is the only version
	  with nothing left to recover.
	* All three causes therefore produce the byte-identical response ``{"state":
	  "attention"}``. There is no cause-specific label, no extra state and no
	  extra field on any branch, so the response cannot be decomposed.
	* The alternative - dropping the subscription cause from the member verdict
	  so that workspace reads ``ok`` - was rejected twice over. It reintroduces
	  the false green #711 is about, and "this workspace never reports attention
	  from that cause" is itself a shape oracle for anyone who can compare a
	  broken workspace against a working one.

	Timing cannot recover the cause either. The only variance inside
	``_llm_health`` is one versus two ``limit 1`` reads of the newest completed
	chat message (``sync_failed`` returns before either; ``turn_error`` does one;
	``subscription_unverified`` does two), on the same local table, with no
	network call on any branch. That is well under the noise floor of an HTTP
	round trip, and it is the same code path the admin endpoint already runs.

	Nothing above changes ``_llm_health`` itself, the values of
	``attention_reason``, or what an admin sees (jarvis#713/#714 own that area).
	This function is a member-visibility filter placed in front of it.

	WHY ``disconnected`` IS NOT A MEMBER STATE
	------------------------------------------
	A workspace with no credential at all reports ``down`` here, exactly like a
	workspace whose config never reached its container. The admin payload keeps
	them apart because an admin can act on the difference. For a member,
	``disconnected`` would say "this workspace currently holds no AI credential",
	which is credential-presence disclosure, and a member can do nothing with it
	that ``down`` does not already tell them: the assistant is not going to
	answer, ask an admin. Collapsing the two also keeps the member vocabulary at
	four values, so no state is unique to one cause.

	GATE
	----
	``require_jarvis_access``, not ``require_jarvis_admin``: any authenticated
	System User of this workspace holding a Jarvis access role. It refuses Guest
	directly, because ``is_system_user`` rejects Guest by name before roles are
	even consulted, and refuses Website/portal users for the same reason chat
	refuses them.

	That in-body call is THE guard, and it is deliberately not left to the
	framework. ``@frappe.whitelist()`` does NOT wrap the function - the decorator
	only adds it to Frappe's ``whitelisted`` set - so nothing refuses a Guest
	until the request dispatcher separately calls ``frappe.is_whitelisted``,
	which throws for a Guest invoking a method not registered ``allow_guest``.
	That check therefore exists only on the HTTP path: a direct Python call
	reaches this body with no framework gate at all. Anyone tempted to drop the
	line below as redundant should read those two functions first.

	``get_llm_connection_status`` is untouched by this: its guard is unchanged
	and so is every field it returns to an admin.
	"""
	require_jarvis_access()
	settings = frappe.get_single("Jarvis Settings")
	if not _has_llm_config(settings):
		return {"state": "down"}
	state, _reason = _llm_health(settings, compute_pool_mode(settings))
	# Belt and braces: _llm_health's contract is the same four values, so this
	# only fires if that function grows a fifth. A member must get a known state
	# or the most conservative one, never a value the SPA has no mapping for -
	# an unmapped state is how a badge falls back to its default, and that
	# default was the green this issue is about.
	return {"state": state if state in MEMBER_HEALTH_STATES else "down"}


def _llm_health(settings, pool_mode: bool) -> tuple:
	"""``(health, attention_reason)`` for a workspace that HAS a credential
	(``_has_llm_config`` has already said so). ``health`` is one of ``ok`` /
	``applying`` / ``attention`` / ``down``. ``attention_reason`` is one of
	``sync_failed`` / ``turn_error`` / ``subscription_unverified`` when
	``health`` is ``attention``, else ``""`` - the SPA's Status hint (#714)
	picks its copy off this instead of a single sentence that claimed every
	cause was a failed chat message, which was not always true.

	Every input is local. Nothing here asks admin, because the one thing admin was
	asked - "does a cliproxy auth profile exist" - turned out not to describe a
	pool at all (see get_llm_connection_status).

	  applying  - a save is in flight. The container is still on its previous
	              config, so neither "fine" nor "broken" is true yet.
	  down      - the container has NEVER confirmed this workspace's config, so it
	              is not serving it. This is the same evidence is_ready_for_chat
	              gates chat on, which is what keeps the badge and the gate from
	              contradicting each other.
	  attention - the container IS serving, but something downstream is wrong:
	              the last apply failed, the latest completed turn errored, or the
	              fleet's own probe reports the chat subscription rejecting
	              requests with no completed turn since to contradict it. All
	              three are workspace-level verdicts. Per-MODEL verdicts
	              deliberately stay out: AI models shows them per row, and one
	              dead member of a healthy failover chain is what failover is for.
	  ok        - serving, the last apply came back clean, and the last turn that
	              finished did not error.

	``ok`` still does NOT mean the provider was reached just now. Nothing here
	probes an endpoint, and it should not pretend to: the two open defects about
	the existing test path (#679, #680) are what a real reachability verdict has
	to be built on. What changed for #678 is only that a workspace whose turns are
	visibly failing can no longer read green.

	The status prefixes match @/lib/syncStatus's, which is the SPA's one
	translator for the same field, so the badge and the sync line cannot disagree
	about which of the three an audit string means.

	sync_failed does not self-clear on a later successful turn (jarvis#714), and
	that is deliberate, not a gap this function leaves open: a turn succeeding
	proves the container is still serving whatever it applied LAST, never that a
	failed apply since landed. Clearing "sync_failed" off local chat evidence
	would be the badge lying about the one thing #713's sync path is the actual
	fix for - see jarvis_settings.py's sync path, which this function does not
	touch. subscription_unverified is different: that probe result is a
	workspace-wide snapshot with no causal link to which config is currently
	applied, so a completed turn succeeding since is first-hand proof the
	subscription that turn used is fine now, and is allowed to demote it - the
	same "fresh local evidence over a stale remote snapshot" reasoning #678
	already established for turn_error below.
	"""
	status = (settings.get("last_sync_status") or "").strip().lower()
	if status.startswith("pending"):
		return "applying", ""
	if not _llm_apply_confirmed(settings, pool_mode):
		return "down", ""
	if status.startswith("failed"):
		return "attention", "sync_failed"
	# A confirmed apply says the config REACHED the container, never that the
	# provider answers. An api-key model pointed at a base URL nothing serves
	# applies perfectly and then fails every turn, which is how a green badge
	# came to sit over a dead endpoint (#678). The turns themselves are the only
	# local evidence that anything actually responded.
	if _last_turn_errored():
		return "attention", "turn_error"
	# The fleet's own pool-wide subscription probe. Only an explicit rejection
	# counts: "unchecked" (and a no-op apply, which runs no probe at all) means
	# nobody looked, which is not evidence of a problem. A completed turn that
	# succeeded SINCE that probe ran is fresher, stronger evidence than a
	# snapshot only a future apply would otherwise refresh (jarvis#714) - see
	# _last_turn_succeeded.
	if (settings.get("last_subscription_status") or "") == "unverified" and not _last_turn_succeeded():
		return "attention", "subscription_unverified"
	return "ok", ""


def _latest_completed_turn_error():
	"""``None`` when this workspace has no completed assistant turn yet, else
	that turn's ``error`` field (``""`` for a clean finish). The one query
	``_last_turn_errored`` and ``_last_turn_succeeded`` both read, so the
	filters - the part TestLastTurnErrored's docstring warns silently rots -
	can only drift in one place.

	Completed means not still streaming and not parked for snapshot recovery; a
	recovering turn has not failed yet. Cancellation is excluded for free:
	stopping a turn writes `stopped`, not `error` (turn_handler:1183), so hitting
	stop cannot paint the badge - that false-alarm shape is exactly what #561 was
	about.

	Workspace-wide on purpose (this card is admin-only and describes the
	workspace), and `get_all` is the right call for that since it does not
	filter by owner.
	"""
	rows = frappe.get_all(
		"Jarvis Chat Message",
		filters={"role": "assistant", "streaming": 0, "recovering": 0},
		fields=["error"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return None
	return (rows[0].get("error") or "").strip()


def _last_turn_errored() -> bool:
	"""Did the most recent COMPLETED assistant turn in this workspace fail?

	Deliberately kind-agnostic. `turn_handler._classify` sorts failures into
	unreachable / timeout / provider / gateway, but its own #702 comment records
	that the agent's wording is not trustworthy for that split: "LLM request
	failed: network connection error." was the verbatim text of a turn that
	failed because a paired-device file was mid-rewrite, nothing to do with the
	network. So this reads the FACT that a turn errored, which is reliable, and
	makes no claim about why - "attention", never "down", and the card's copy
	sends the reader to the failed message itself, whose own inline error is the
	only first-hand account, rather than naming a cause we cannot know.

	Self-clearing: only the LATEST completed turn is read, so the next turn that
	succeeds takes the badge back to green with no stamp to reset.
	"""
	return bool(_latest_completed_turn_error())


def _last_turn_succeeded() -> bool:
	"""Did the most recent COMPLETED assistant turn in this workspace finish
	clean? The positive counterpart of ``_last_turn_errored`` - a workspace with
	no completed turn yet reports False here too, same as it does there: absence
	of evidence must not read as proof of success any more than it reads as proof
	of failure.

	Used only to demote a stale ``last_subscription_status`` (see _llm_health):
	that probe is a snapshot from the last APPLY, refreshed only by a future
	apply (jarvis_settings.py's sync path), never by ordinary chat traffic, so
	without this a single old rejection kept the badge on "attention" forever
	regardless of how much chat had worked since (jarvis#714).
	"""
	err = _latest_completed_turn_error()
	return err is not None and err == ""


def _llm_apply_confirmed(settings, pool_mode: bool) -> bool:
	"""Has the fleet CONFIRMED an apply of the leg this workspace syncs through?

	Mirrors ``is_ready_for_chat``'s three legs exactly, and must keep mirroring
	them - the whole value of this signal is that chat and the connection badge
	read the same evidence. Pool marker for a pool (including a BYO api-key pool,
	which has no sidecar but is still pushed through /llm-pool and still stamps
	llm_pool_synced_at); the OAuth connect stamp ONLY for the legacy flat-field
	direct-oauth tenant; the direct apply marker for everything else, including
	a models[]-table subscription now taking the direct leg (jarvis#715 step 3) -
	that leg pushes its own oauth blob and stamps llm_direct_synced_at exactly
	like an api-key direct tenant does, never llm_oauth_connected_at (which
	save_llm_pool unconditionally clears on every models[]-table save, by
	design - see its comment).

	Legacy workspaces on both legs are backfilled by patch (v1_10 for the pool,
	v2_00_backfill_llm_direct_synced_at for direct), so an established tenant does
	not read as never-applied.
	"""
	if pool_mode:
		return bool(settings.get("llm_pool_synced_at"))
	if (settings.get("llm_auth_mode") or "api_key").strip() == "oauth":
		return bool(settings.get("llm_oauth_connected_at"))
	return bool(settings.get("llm_direct_synced_at"))


def _has_llm_config(settings) -> bool:
	"""True when this workspace still has a usable AI CREDENTIAL.

	It asks whether a credential exists, not whether a provider NAME is written
	down, and that distinction is the whole point. llm_provider / llm_model are
	labels that on_update mirrors from models[0]; they are not proof of anything
	on their own, and a partial disconnect can leave them behind.

	The concrete case: jarvis.oauth.api.disconnect (Disconnect chat subscription)
	deliberately clears only the OAuth side, writes last_sync_status
	"disconnected", and LEAVES llm_provider / llm_model in place. Testing the
	labels therefore reported such a workspace as connected while its own sync
	status said "disconnected" - two sources of truth disagreeing, and the
	customer's Connection badge showing a healthy state over a workspace that
	cannot answer a turn. Observed on a real tenant: models[] empty,
	last_sync_status "disconnected", llm_provider still "OpenAI".

	What counts as a credential:
	  * models[] - the pool holds its own keys/accounts. Checked even when every
	    row is DISABLED, because a paused pool still HAS credentials and must not
	    read as disconnected.
	  * proxy_active - DERIVED from the config at save time
	    (compute_proxy_active) and reset to 0 when the config goes away, so a set
	    flag is itself proof a pool exists whatever the mirrors say.
	  * a direct tenant's stored api key, or its live OAuth connection.
	"""
	if settings.get("models") or getattr(settings, "proxy_active", 0):
		return True
	# Direct (non-pool) tenant. The credential lives in the flat fields, so read
	# the credential itself rather than the label beside it.
	if (settings.get("llm_auth_mode") or "") == "oauth":
		return bool(settings.get("llm_oauth_connected_at") or settings.get("llm_oauth_account_email"))
	try:
		return bool(settings.get_password("llm_api_key", raise_exception=False))
	except Exception:
		# Never let a password-store read break the status endpoint: an
		# unreadable secret is not evidence of a connection.
		return False


@frappe.whitelist()
def get_account() -> dict:
	"""Plan + validity + upgrade-eligible plans for the account page.

	System-Manager only, like its siblings above. Until now the only gate was
	the UI: SettingsDialog hides the ACCOUNT & BILLING rail group from non-SM
	users, and the (now retired) jarvis-account desk page carried
	roles=["System Manager"]. Neither stops a direct /api/method call, so any
	authenticated user could read the account's plan, status and validity. The
	SPA /jarvis/billing route deliberately adds no client-side guard of its own
	and leans on this one.
	"""
	require_jarvis_admin()
	return _surface(admin_client.get_account_summary)


@frappe.whitelist()
def preview_upgrade(target_plan: str) -> dict:
	"""Prorated amount for the upgrade modal's per-plan cards.

	Same gate as ``start_upgrade`` below: this is the read half of the same
	billing transaction, reachable only from the SM-only desk page, and it
	spends an admin round-trip per call. Whoever may not upgrade the plan has
	no business pricing the upgrade either.
	"""
	require_jarvis_admin()
	return _surface(admin_client.preview_upgrade, target_plan)


@frappe.whitelist()
def start_upgrade(target_plan: str, provider: str | None = None) -> dict:
	"""Create the prorated order on the sub's gateway (or the ``provider``
	override); the page then opens that gateway's Checkout.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review): initiates a billing transaction tied to the site's admin
	account; non-admin staff shouldn't be able to upgrade the plan.
	"""
	require_jarvis_admin()
	# plan-09 WS8: a token-bearing answer is attested against the bench's OWN
	# configured pay origin so BillingPage can top-level-navigate to the
	# admin-hosted checkout. Behaviour-neutral on a non-token (raw / settled)
	# answer - see onboarding_contract.augment_pay_page.
	return onboarding_contract.augment_pay_page(
		_surface(admin_client.start_upgrade, target_plan, provider=provider)
	)


@frappe.whitelist()
def cancel_plan_at_period_end() -> dict:
	"""Schedule a period-end cancellation of the site's BILLING plan.

	Named plan, not subscription: in this app "subscription" means the LLM
	provider subscription (disconnect_subscription / DirectSubscriptionCard),
	and confusing the two would be expensive.

	Gated on System Manager for the same reason as start_upgrade: it changes
	the billing state of the site's admin account. The gate runs BEFORE the
	admin round-trip so an unauthorized caller never spends a network call.
	"""
	require_jarvis_admin()
	out = _surface(admin_client.cancel_plan_at_period_end)
	_bust_chat_gate()
	return out


@frappe.whitelist()
def resume_plan() -> dict:
	"""Undo a scheduled cancellation (System Manager, as above)."""
	require_jarvis_admin()
	out = _surface(admin_client.resume_plan)
	_bust_chat_gate()
	return out


@frappe.whitelist()
def reauthorize_autopay() -> dict:
	"""Start re-arming auto-renewal; the page then opens a mandate Checkout.

	No chat-gate bust: this only creates a mandate intent, it changes no
	entitlement. confirm_payment is what flips autorenew back on.
	"""
	require_jarvis_admin()
	# plan-09 WS8: attest a token answer so BillingPage navigates to the
	# admin-hosted mandate checkout (behaviour-neutral otherwise).
	return onboarding_contract.augment_pay_page(_surface(admin_client.reauthorize_autopay))


@frappe.whitelist()
def get_billing_payment_state() -> dict:
	"""Where the current billing checkout stands, without asking a gateway.

	A live attempt re-echoes its pay-page token, so a customer whose renew redirect
	died resumes the SAME pay page instead of minting a second one.
	"""
	require_jarvis_admin()
	return onboarding_contract.augment_pay_page(_surface(admin_client.get_billing_payment_state))


@frappe.whitelist()
def check_billing_payment_status() -> dict:
	"""Ask the provider what happened to the current billing payment and converge.

	Busts the chat gate: a verified payment reactivates the plan, and the cached
	verdict would otherwise keep chat paused for a customer who has just paid.
	"""
	require_jarvis_admin()
	try:
		data = admin_client.check_billing_payment_status()
	except (AdminValidationError, AdminAuthError, AdminUnreachableError, AdminRateLimitedError) as e:
		error, status = onboarding_contract.error_object(e)
		return onboarding_contract.failure(error, status)
	out = onboarding_contract.augment_pay_page(data)
	_bust_chat_gate()
	# Explicitly context-LESS: wire(None) would load the persisted SIGNUP context,
	# so a gen-2 billing answer echoed the signup's amount, generation and code.
	# This endpoint reports on the current billing attempt; it has no context of
	# its own to add, and borrowing signup's would be a lie.
	return onboarding_contract.success(out, context={})


@frappe.whitelist()
def preview_downgrade(target_plan: str) -> dict:
	"""Describe a downgrade for the picker. SM-only, same gate as upgrade."""
	require_jarvis_admin()
	return _surface(admin_client.preview_downgrade, target_plan)


@frappe.whitelist()
def start_downgrade(target_plan: str) -> dict:
	"""Schedule a downgrade (next cycle). Monthly autopay returns a mandate
	pay-page token for a ₹0 mandate checkout; Annual just schedules.

	Chat-gate bust: a downgrade never changes entitlement until the boundary,
	so no bust is needed - the container keeps serving the current plan."""
	require_jarvis_admin()
	# plan-09 WS8: a Monthly downgrade returns a mandate token to navigate to;
	# an Annual downgrade just schedules (no token) and passes through untouched.
	return onboarding_contract.augment_pay_page(_surface(admin_client.start_downgrade, target_plan))


@frappe.whitelist()
def cancel_scheduled_downgrade() -> dict:
	"""Revoke a scheduled downgrade (SM-only).

	plan-09 WS8: Monthly re-arms the current plan's mandate, so this can carry a
	pay-page token; augment_pay_page attests it (behaviour-neutral otherwise)."""
	require_jarvis_admin()
	return onboarding_contract.augment_pay_page(_surface(admin_client.cancel_scheduled_downgrade))


def _bust_chat_gate() -> None:
	"""Drop EVERY cached chat-readiness verdict for this site.

	Two kinds of caller. A billing state change (cancel / resume) moves nothing
	the revision covers, so this is the only thing that can drop its entry -
	cancelling does not itself change readiness (entitlement runs to period end),
	but the pane re-reads immediately afterwards and a stale positive verdict
	would be confusing. An LLM save DOES move the revision, so its entry is
	already unreachable; this still runs there because the revision can return to
	a previous value within the TTL (a save that is reverted, an apply that lands
	back on the same status), and a resurrected verdict about a configuration that
	was replaced in between is exactly what the revision exists to prevent.

	Prefix delete, not a single key: the entry to drop is whichever revision is
	live, and after a save that is no longer the one this call would compute.

	DO NOT call this from a hot path. ``delete_keys`` is a Redis KEYS scan of the
	whole keyspace, which is fine for the handful of rare, human-triggered actions
	that call it (a billing change, an LLM save, a disconnect, a reconnect) and is
	not fine per request or per chat turn. A caller that needs invalidation on a
	hot path wants the config revision instead - it costs nothing and is already
	how every routine change is picked up."""
	try:
		frappe.cache().delete_keys(_CHAT_GATE_CACHE_KEY)
	except Exception:
		pass
