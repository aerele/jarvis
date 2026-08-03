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

from jarvis import admin_client, release_notice
from jarvis.exceptions import AdminAuthError
from jarvis.jarvis.pool_serialize import compute_pool_mode, pool_primary_model
from jarvis.onboarding import _surface
from jarvis.permissions import require_jarvis_admin

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

_GATE_STATE_FIELDS = (
	*_GATE_REVISION_FIELDS,
	_READY_MARKER_FIELD,
	_READY_ANCHOR_FIELD,
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
	code is ``"subscription_suspended"`` for ``Suspended`` (renew) and
	``"container_provisioning"`` otherwise (wait) - kept distinct so a suspended
	customer isn't told to wait for a container that won't come back.

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
	cached = cache.get_value(cache_key)
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
	hit = cache.get_value(_REPLACED_CACHE_KEY)
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
	- ``"container_provisioning"`` - all local checks passed, but admin reports
	  the container isn't chat-ready yet (chat_readiness != "Ready"). Set only by
	  the final ``_admin_chat_gate`` at the managed ready-exits; v1-tolerant (see
	  ``_admin_chat_gate``).
	- ``"readiness_unconfirmed"`` - admin could not be asked AND this workspace
	  has never been confirmed ready, so nothing knows whether a container is
	  serving it. Carries ``retryable: True``: it is the absence of a verdict,
	  not one. An ESTABLISHED workspace does not get this - the same failure
	  leaves it ready (see ``_admin_unreachable_verdict``).
	- ``None`` when ``ready`` is True.
	"""
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
	if compute_pool_mode(settings):
		if getattr(settings, "llm_pool_synced_at", None):
			return _admin_chat_gate()
		return {"ready": False, "reason": "llm_pool_provisioning"}

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
		if not getattr(settings, "llm_direct_synced_at", None):
			return {"ready": False, "reason": "llm_provisioning"}
	elif auth_mode in ("subscription", "oauth"):
		# Both modes use the same local signal: llm_oauth_connected_at is
		# set (read-only) when the oauth grant completes and the admin
		# pushes the auth-profile blob to the container.
		if not getattr(settings, "llm_oauth_connected_at", None):
			return _llm_missing_verdict(settings)
	else:
		# Unknown auth_mode - treat as misconfigured; the wizard owns it.
		return {"ready": False, "reason": "llm_credentials"}

	return _admin_chat_gate()


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
def get_llm_usage() -> dict:
	"""Real, curated Bifrost usage for the Monitor tab (System-Manager only,
	spec 7). Tenants with no Bifrost (proxy_active=0) short-circuit to the empty
	shape — no pointless admin round-trip. That now includes a BYO api-key POOL,
	which the fleet renders openclaw-direct with no sidecar at all."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	if not getattr(settings, "proxy_active", 0):
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
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": "",
		}
	if not getattr(settings, "proxy_active", 0):
		return {
			**shape,
			"proxy_active": False,
			"disconnected": False,
			"health": _llm_health(settings, pool_mode),
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": settings.get("llm_model") or "",
		}
	raw = _surface(admin_client.post_llm_auth_status) or {}
	data = raw.get("data", raw) or {}
	return {
		**shape,
		"proxy_active": True,
		"disconnected": False,
		"health": _llm_health(settings, pool_mode),
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


def _llm_health(settings, pool_mode: bool) -> str:
	"""``ok`` / ``applying`` / ``attention`` / ``down`` for a workspace that HAS a
	credential (``_has_llm_config`` has already said so).

	Every input is local. Nothing here asks admin, because the one thing admin was
	asked - "does a cliproxy auth profile exist" - turned out not to describe a
	pool at all (see get_llm_connection_status).

	  applying  - a save is in flight. The container is still on its previous
	              config, so neither "fine" nor "broken" is true yet.
	  down      - the container has NEVER confirmed this workspace's config, so it
	              is not serving it. This is the same evidence is_ready_for_chat
	              gates chat on, which is what keeps the badge and the gate from
	              contradicting each other.
	  attention - the container IS serving, but the last apply failed or the
	              fleet's own probe reports the chat subscription rejecting
	              requests. Both are workspace-level verdicts. Per-MODEL verdicts
	              deliberately stay out: AI models shows them per row, and one dead
	              member of a healthy failover chain is what failover is for.
	  ok        - serving, and the last apply came back clean.

	The status prefixes match @/lib/syncStatus's, which is the SPA's one
	translator for the same field, so the badge and the sync line cannot disagree
	about which of the three an audit string means.
	"""
	status = (settings.get("last_sync_status") or "").strip().lower()
	if status.startswith("pending"):
		return "applying"
	if not _llm_apply_confirmed(settings, pool_mode):
		return "down"
	if status.startswith("failed"):
		return "attention"
	# The fleet's own pool-wide subscription probe. Only an explicit rejection
	# counts: "unchecked" (and a no-op apply, which runs no probe at all) means
	# nobody looked, which is not evidence of a problem.
	return "attention" if (settings.get("last_subscription_status") or "") == "unverified" else "ok"


def _llm_apply_confirmed(settings, pool_mode: bool) -> bool:
	"""Has the fleet CONFIRMED an apply of the leg this workspace syncs through?

	Mirrors ``is_ready_for_chat``'s three legs exactly, and must keep mirroring
	them - the whole value of this signal is that chat and the connection badge
	read the same evidence. Pool marker for a pool (including a BYO api-key pool,
	which has no sidecar but is still pushed through /llm-pool and still stamps
	llm_pool_synced_at), the OAuth connect stamp for a direct subscription/oauth
	tenant, the direct apply marker otherwise.

	Legacy workspaces on both legs are backfilled by patch (v1_10 for the pool,
	v2_00_backfill_llm_direct_synced_at for direct), so an established tenant does
	not read as never-applied.
	"""
	if pool_mode:
		return bool(settings.get("llm_pool_synced_at"))
	if (settings.get("llm_auth_mode") or "api_key").strip() in ("subscription", "oauth"):
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
	return _surface(admin_client.start_upgrade, target_plan, provider=provider)


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

	No chat-gate bust: this only creates a Razorpay object, it changes no
	entitlement. confirm_payment is what flips autorenew back on.
	"""
	require_jarvis_admin()
	return _surface(admin_client.reauthorize_autopay)


@frappe.whitelist()
def preview_downgrade(target_plan: str) -> dict:
	"""Describe a downgrade for the picker. SM-only, same gate as upgrade."""
	require_jarvis_admin()
	return _surface(admin_client.preview_downgrade, target_plan)


@frappe.whitelist()
def start_downgrade(target_plan: str) -> dict:
	"""Schedule a downgrade (next cycle). Monthly autopay returns a
	subscription id for a ₹0 mandate Checkout; Annual just schedules.

	Chat-gate bust: a downgrade never changes entitlement until the boundary,
	so no bust is needed - the container keeps serving the current plan."""
	require_jarvis_admin()
	return _surface(admin_client.start_downgrade, target_plan)


@frappe.whitelist()
def cancel_scheduled_downgrade() -> dict:
	"""Revoke a scheduled downgrade (SM-only)."""
	require_jarvis_admin()
	return _surface(admin_client.cancel_scheduled_downgrade)


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
