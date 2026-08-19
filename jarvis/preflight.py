"""Onboarding chat preflight (jarvis#840).

Readiness (jarvis.account.is_ready_for_chat) proves the config APPLIED;
nothing proves the connection is USABLE. The 2026-08-14 e2e wired a fresh
chat subscription perfectly and the FIRST message still failed upstream with
a quota 429, because no check ever exercises the credential. This module is
the wizard's last step before chat: one call answering the checklist items
readiness cannot - plugin wired, persona present, credential actually usable.

Verdict policy (the issue's core constraint): a provider quota/rate-limit is
NON-BLOCKING - shown honestly, then the customer proceeds; only a genuine
credential rejection sends them back to the connect form. Anything this
module cannot determine is "unchecked"/"unknown" and never blocks.

The usable item is leg-discriminated so the live probe is spent only where
no cheaper verdict exists:
- pool tenants reuse the pool apply's own persisted probe verdict
  (last_subscription_status, jarvis_admin_v2#193 classifiers);
- api-key direct reuses the stateless bench-side key probe (jarvis#679);
- ONLY the direct-subscription leg (jarvis#715), which has no check at all
  today, fires one bounded real turn through the tenant's own container
  (the prewarm precedent: throwaway session, deleted afterward). That turn
  is billed against the customer's plan, so it is cached briefly and never
  looped (the prewarm quota incident is the cautionary tale).
"""

import re
import time
import uuid

import frappe

from jarvis import admin_client
from jarvis.permissions import require_jarvis_admin

_PROBE_PROMPT = "Reply with the single word: ok"
_PROBE_BUDGET_S = 20
# One customer clicking through onboarding fires ONE billed probe; a reload or
# double-entry inside the TTL reuses the verdict instead of re-billing. Keyed
# by the jarvis#841 apply fingerprint so a FIXED credential (new blob, new
# fingerprint) always probes fresh and can never be answered by the previous
# credential's refusal. Billed outcomes keep the long TTL; an unreachable
# gateway (a restart blip, nothing billed) retries almost immediately so the
# checklist cannot lie for half a minute about a container that just came back.
_PROBE_CACHE_KEY = "jarvis.preflight.probe_verdict"
_PROBE_CACHE_TTL_S = 30
_PROBE_CACHE_TTL_UNREACHABLE_S = 5
# Every verdict detail shown to the customer is trimmed to this (one place,
# five consumers - PR #848 review).
_DETAIL_MAX_CHARS = 300

# Provider quota/exhaustion vocabulary, checked BEFORE the auth patterns so
# quota prose lands on the non-blocking side. Deliberately CONTEXT-BOUND
# (jarvis#840 review M2): bare tokens like "insufficient"/"credit" also occur
# in genuine credential failures ("Request had insufficient authentication
# scopes", a 403), and the issue's tolerance is asymmetric - wrongly BLOCKING
# is recoverable, wrongly letting a broken credential into chat is the exact
# failure this feature exists to prevent. The auth side reuses
# chat/title.py's _AUTH_FAULT_RE (single source, jarvis#738).
_RATE_LIMIT_RE = re.compile(
	r"\b429\b"
	r"|rate.?limit|usage.?limit|too.?many.?requests"
	r"|\bquota\b|overloaded|resource.?exhausted"
	r"|insufficient[_ -]?(?:quota|credits?|balance|funds)"
	r"|credit.?balance|out.?of.?(?:credits?|messages)"
	r"|billing.?(?:hard.?)?limit",
	re.IGNORECASE,
)


@frappe.whitelist()
def run_chat_preflight() -> dict:
	"""The wizard's pre-chat checklist (jarvis#840). Container health and
	config-applied are NOT re-derived here - the SPA just watched its own
	readiness poll turn green and renders those two rows from that verdict.
	This answers only what readiness cannot:

	{
	  "plugin":  "ok|degraded|broken|unchecked",
	  "persona": "ok|degraded|broken|unchecked",
	  "usable":  {"state": "ok|rate_limit|auth|timeout|unreachable|unknown",
	              "detail": <provider text, trimmed>, "source": ...},
	}

	Latency: the all-green path is one cheap relay + one short live turn. The
	worst non-green path is bounded at roughly two 20s admin round trips (a
	live "degraded" answer pays the deep escalation) plus the 20s probe
	budget; every leg degrades rather than raises.

	Plain dict like ``account.is_ready_for_chat`` (no ok-envelope): this
	endpoint cannot refuse - every failure shape inside it degrades to an
	unchecked/unknown row by design. Same gate as the wizard's endpoints."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	# Outer degrade guard, same shape admin grew in #334: a bug anywhere below
	# must answer as an unchecked/unknown checklist, never a 500 the wizard
	# has to swallow (round-5 review). The permission gate above still raises.
	try:
		integration = _integration_item()
	except Exception:
		integration = {
			"plugin": "unchecked",
			"persona": "unchecked",
			"integration_source": "unavailable",
		}
	try:
		usable = _usable_item(settings)
	except Exception:
		usable = {"state": "unknown", "detail": "", "source": "none"}
	return {**integration, "usable": usable}


def _integration_item() -> dict:
	"""Plugin/persona tri-states via admin's relay of the fleet probe.

	Cheap probe first; anything not "ok" retries once with deep=1 (the
	boot-log truth - the static probe cannot see whether the plugin actually
	registered its tools). EVERY failure shape is "unchecked": an admin
	without the endpoint (deploy window), an unreachable admin, a malformed
	answer. An unchecked row renders as "not checked" and never blocks."""
	unchecked = {"plugin": "unchecked", "persona": "unchecked", "integration_source": "unavailable"}
	try:
		data = admin_client.get_integration_status() or {}
	except Exception:
		# Deploy-window method-not-found, unreachable admin, anything: all the
		# same "unchecked" - there is no second call worth making when the
		# FIRST admin round trip already failed (jarvis#840 review M3).
		return unchecked
	tri = data.get("tri_state") or {}
	plugin = _tri(tri.get("plugin"))
	persona = _tri(tri.get("persona"))
	if data.get("source") == "live" and (plugin != "ok" or persona != "ok"):
		# Escalate once to the deep probe: the static one cannot see whether
		# the plugin actually registered its tools, so a not-ok cheap answer
		# is a suspicion, not a verdict. ONLY on a live not-ok (review M3): a
		# "cached"/"none" answer means the fleet call already failed or there
		# is no container, and the deep retry would pay a second full round
		# trip to meet the same wall. Best-effort - on failure the cheap
		# answer stands.
		try:
			deep = admin_client.get_integration_status(deep=True) or {}
			deep_tri = deep.get("tri_state") or {}
			plugin = _tri(deep_tri.get("plugin"))
			persona = _tri(deep_tri.get("persona"))
			data = deep
		except Exception:
			pass
	return {"plugin": plugin, "persona": persona, "integration_source": data.get("source") or ""}


def _tri(value) -> str:
	# str() first: a type-drifted relay value (bool/int) must degrade to
	# "unchecked", never raise out of the one endpoint whose contract is that
	# it cannot refuse (PR #848 review).
	value = str(value or "").lower()
	return value if value in ("ok", "degraded", "broken") else "unchecked"


def _usable_item(settings) -> dict:
	"""Leg-discriminated usability verdict; see the module docstring."""
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	if compute_pool_mode(settings):
		status = (settings.get("last_subscription_status") or "").strip()
		if status == "verified":
			return {"state": "ok", "detail": "", "source": "pool_probe"}
		# unverified/unchecked/not_applicable: the pool apply's own classifier
		# had no fresh verdict; do not invent one (and do not bill a second
		# probe - the pool Test button exists for an explicit re-check).
		return {"state": "unknown", "detail": "", "source": "pool_probe"}
	mode = settings.llm_auth_mode or ""
	if mode == "api_key":
		return _probe_stored_api_key(settings)
	if mode == "subscription":
		return _probe_direct_subscription(settings)
	return {"state": "unknown", "detail": "", "source": "none"}


def _probe_stored_api_key(settings) -> dict:
	"""Reuse the stateless bench-side key probe (jarvis#679) against the
	STORED key - persists nothing, never touches the container."""
	from jarvis.llm_key_probe import test_llm_api_key

	try:
		res = (
			test_llm_api_key(
				provider=settings.llm_provider or "",
				model=settings.llm_model or "",
				api_key="",
				base_url=settings.llm_base_url or "",
				use_stored_key=1,
			)
			or {}
		)
	except Exception:
		return {"state": "unknown", "detail": "", "source": "key_probe"}
	verdict = res.get("verdict") or ""
	message = str(res.get("message") or "")
	if verdict == "pass":
		return {"state": "ok", "detail": "", "source": "key_probe"}
	if verdict == "fail":
		return {**_classify_probe_error(message), "source": "key_probe"}
	return {"state": "unknown", "detail": message[:_DETAIL_MAX_CHARS], "source": "key_probe"}


def _probe_cache_key(settings) -> str:
	"""Per-config cache key (jarvis#840 review M4): the jarvis#841 apply
	fingerprint changes whenever the credential/config changes, so a fixed
	credential always probes fresh while a reload of the SAME config reuses
	the billed verdict."""
	return f"{_PROBE_CACHE_KEY}:{settings.get('llm_last_apply_fingerprint') or 'nofp'}"


def _drain_probe_stream(sess, session_key: str, run_id: str, model, provider) -> dict:
	"""Consume the turn stream in a worker thread joined on the probe budget
	(jarvis#840 review B2): stream_agent_turn's own deadline is the chat
	turn's 600s and it can block long before its first yield, which would pin
	this whitelisted request's worker for minutes on a wedged upstream. The
	thread is pure WS work (no frappe state beyond an already-created stdlib
	logger). An abandoned worker still OWNS the socket's recv, so the caller
	must close the socket FIRST and issue no further RPC on it (review round-2
	MAJOR 1: a reclaim RPC would race the worker for frames and lose)."""
	import threading

	out = {"saw_text": False, "error_text": "", "exc": None, "done": False}

	def _drain():
		try:
			for event in sess.stream_agent_turn(
				session_key,
				_PROBE_PROMPT,
				run_id,
				model=model or None,
				provider=provider,
			):
				kind = event.get("kind")
				if kind == "assistant" and (event.get("delta") or event.get("text")):
					out["saw_text"] = True
					break
				if kind == "lifecycle" and event.get("phase") == "error":
					out["error_text"] = str(event.get("error") or "")
					break
				if kind == "lifecycle" and event.get("phase") == "end":
					break
		except Exception as e:
			out["exc"] = e
		out["done"] = True

	worker = threading.Thread(target=_drain, daemon=True, name="jarvis-preflight-probe")
	worker.start()
	worker.join(_PROBE_BUDGET_S)
	return out


def _probe_direct_subscription(settings) -> dict:
	"""ONE bounded real turn through the tenant's own container - the only
	honest usability check the direct-subscription leg has (its Test button
	says so in as many words). Billed, hence the cache; throwaway session,
	reclaimed afterward (never bare-deleted: jarvis#525/#535 - deleting under
	a live run re-creates the session as an orphan or kills the run); hard
	wall-clock budget so the wizard never hangs on a wedged upstream.

	Model/provider pin comes from turn_handler's resolver, DELIBERATELY not
	prewarm's simpler one: on this leg the agent-provider id rides the
	account's own blob (jarvis#755) and only turn_handler reads it, so this is
	what the customer's real first turn would use."""
	cache = frappe.cache()
	cache_key = _probe_cache_key(settings)
	cached = cache.get_value(cache_key, expires=True)
	if isinstance(cached, dict) and cached.get("state"):
		return cached
	from jarvis.chat.agent_client import AgentSession, AgentUnreachableError, oneshot_run_id
	from jarvis.chat.prewarm import _gateway_ws_url
	from jarvis.chat.session_lifecycle import reclaim_throwaway_session
	from jarvis.chat.title import _auth_fault_detail
	from jarvis.chat.turn_handler import _resolve_model_and_provider

	gateway_url = _gateway_ws_url(settings)
	if not gateway_url:
		return {"state": "unreachable", "detail": "", "source": "live_probe"}
	# In-flight claim (PR #848 review): the verdict caches only AFTER the probe
	# completes, so a reload/second tab during the up-to-20s window would fire
	# a SECOND billed turn. The claim closes that window; the concurrent caller
	# gets a non-blocking, uncached "still checking" answer and the next
	# preflight (Retry, reload) reads the finished verdict from the cache.
	# Claimed only AFTER the no-gateway early return, which bills nothing and
	# must not leave a 30s "already running" ghost behind (round-5 review).
	inflight_key = f"{cache_key}:inflight"
	if cache.get_value(inflight_key, expires=True):
		return {
			"state": "unknown",
			"detail": "A live check is already running.",
			"source": "live_probe",
		}
	cache.set_value(inflight_key, "1", expires_in_sec=int(_PROBE_BUDGET_S) + 10)
	model, provider = _resolve_model_and_provider(frappe._dict(model_override=""))
	verdict = {"state": "unknown", "detail": ""}
	sess = None
	throwaway = None
	fired_at = None
	stream_finished = False
	try:
		sess = AgentSession.connect(gateway_url)
		throwaway = sess.create_session(label=f"jarvis-preflight-{uuid.uuid4().hex[:8]}")
		fired_at = time.time()
		out = _drain_probe_stream(
			sess,
			throwaway,
			oneshot_run_id("preflight", uuid.uuid4().hex, model=model, provider=provider),
			model,
			provider,
		)
		stream_finished = bool(out["done"])
		if out["saw_text"]:
			verdict = {"state": "ok", "detail": ""}
		elif out["error_text"]:
			verdict = _classify_probe_error(out["error_text"])
		elif out["exc"] is not None:
			verdict = _classify_probe_exception(out["exc"], _auth_fault_detail)
		elif not stream_finished:
			# Budget expired with the run still going: billed, so cache it.
			verdict = {"state": "timeout", "detail": "The live check timed out."}
	except AgentUnreachableError as e:
		verdict = _classify_probe_exception(e, _auth_fault_detail)
	except Exception:
		verdict = {"state": "unknown", "detail": ""}
	finally:
		if sess is not None:
			if throwaway and stream_finished:
				try:
					# fired_at marks "no active run" untrustworthy until the
					# grace elapses, so a not-yet-started run is never deleted
					# out from under itself; a busy one is left for the sweep.
					reclaim_throwaway_session(
						sess, throwaway, logger_name="jarvis.preflight", fired_at=fired_at
					)
				except Exception:
					pass  # orphan; the gateway's own sweep collects it
			# On the abandoned-worker path (stream_finished False) the socket
			# closes FIRST and no reclaim RPC is issued at all: the worker
			# still owns the recv, so a reclaim would race it for frames and
			# burn ~10s to lose (review round-2 MAJOR 1). The hourly sweep
			# collects the throwaway on the 1h throwaway grace - the
			# "jarvis-preflight-" prefix is registered in
			# session_lifecycle._THROWAWAY_LABEL_PREFIXES for exactly this.
			try:
				sess.close()
			except Exception:
				pass
	verdict["source"] = "live_probe"
	ttl = _PROBE_CACHE_TTL_UNREACHABLE_S if verdict["state"] == "unreachable" else _PROBE_CACHE_TTL_S
	cache.set_value(cache_key, verdict, expires_in_sec=ttl)
	cache.delete_value(inflight_key)
	return verdict


def _classify_probe_exception(exc, auth_fault_detail) -> dict:
	"""An auth fault can arrive as a RAISED gateway rejection, not only as
	lifecycle error text (jarvis#840 review M1) - title.py handles both
	surfaces and so must this, or a credential the probe just proved rejected
	would read as a shrug-grade "unreachable" and open chat anyway.

	Rate-limit FIRST, same invariant as _classify_probe_error (review round-2
	MAJOR 2): the auth vocabulary carries a bare "credential" token, and
	proxied upstreams phrase exhaustion as "credentials exhausted" / "no
	healthy credential, rate limited" - quota arriving as a refused RPC must
	still land on the non-blocking side."""
	parts = [str(exc), str(getattr(exc, "code", "") or "")]
	details = getattr(exc, "details", None)
	if isinstance(details, dict):
		parts.extend(str(v) for v in details.values())
	text = " ".join(p for p in parts if p)
	if _RATE_LIMIT_RE.search(text):
		return {"state": "rate_limit", "detail": str(exc)[:_DETAIL_MAX_CHARS]}
	auth_text = None
	try:
		auth_text = auth_fault_detail(exc)
	except Exception:
		auth_text = None
	# The supplement covers BOTH surfaces, same as the title regex it extends:
	# a scope failure phrased without a status code can arrive as a refused
	# RPC too, and missing it here would open chat on a rejected credential
	# (PR #848 review).
	if auth_text or _AUTH_SUPPLEMENT_RE.search(text):
		return {"state": "auth", "detail": str(auth_text or exc)[:_DETAIL_MAX_CHARS]}
	return {"state": "unreachable", "detail": str(exc)[:_DETAIL_MAX_CHARS]}


# Auth vocabulary title.py's #738-scoped regex deliberately lacks but a
# preflight must catch: scope failures phrased without a status code. Kept
# local rather than widening title's regex, whose scope is pinned by #738.
_AUTH_SUPPLEMENT_RE = re.compile(
	r"insufficient authentication|invalid[_ -]?scope",
	re.IGNORECASE,
)


def _classify_probe_error(text: str) -> dict:
	"""Rate-limit BEFORE auth: quota messages routinely contain words like
	"credit"/"insufficient" and must land on the NON-blocking side (the
	issue's core constraint). Unclassifiable text is "unknown", which also
	never blocks."""
	from jarvis.chat.title import _AUTH_FAULT_RE

	detail = (text or "")[:_DETAIL_MAX_CHARS]
	if _RATE_LIMIT_RE.search(text or ""):
		return {"state": "rate_limit", "detail": detail}
	if _AUTH_FAULT_RE.search(text or "") or _AUTH_SUPPLEMENT_RE.search(text or ""):
		return {"state": "auth", "detail": detail}
	return {"state": "unknown", "detail": detail}
