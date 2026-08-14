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
# One customer clicking through onboarding fires ONE billed probe; a reload
# or double-entry inside the TTL reuses the verdict instead of re-billing.
_PROBE_CACHE_KEY = "jarvis.preflight.probe_verdict"
_PROBE_CACHE_TTL_S = 30

# Provider quota/exhaustion vocabulary, checked BEFORE the auth patterns so
# "insufficient credit" style messages land on the non-blocking side. The
# auth side reuses chat/title.py's _AUTH_FAULT_RE (single source, jarvis#738)
# rather than growing a second, drifting copy.
_RATE_LIMIT_RE = re.compile(
	r"\b429\b"
	r"|rate.?limit|usage.?limit(?:_reached)?|too.?many.?requests"
	r"|quota|overloaded|exhausted"
	r"|insufficient|credit|billing",
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
	  "usable":  {"state": "ok|rate_limit|auth|unreachable|unknown",
	              "detail": <provider text, trimmed>, "source": ...},
	}

	Plain dict like ``account.is_ready_for_chat`` (no ok-envelope): this
	endpoint cannot refuse - every failure shape inside it degrades to an
	unchecked/unknown row by design. Same gate as the wizard's endpoints."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	integration = _integration_item()
	usable = _usable_item(settings)
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
	except admin_client.AdminValidationError as e:
		if admin_client.is_method_not_found(e):
			return unchecked
		return unchecked
	except Exception:
		return unchecked
	tri = data.get("tri_state") or {}
	plugin = _tri(tri.get("plugin"))
	persona = _tri(tri.get("persona"))
	if plugin != "ok" or persona != "ok":
		# Escalate once to the deep probe: the static one cannot see whether
		# the plugin actually registered its tools, so a not-ok cheap answer
		# is a suspicion, not a verdict. Best-effort - on failure the cheap
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
	value = (value or "").lower()
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
	return {"state": "unknown", "detail": message[:300], "source": "key_probe"}


def _probe_direct_subscription(settings) -> dict:
	"""ONE bounded real turn through the tenant's own container - the only
	honest usability check the direct-subscription leg has (its Test button
	says so in as many words). Billed, hence the cache; throwaway session,
	deleted afterward; hard wall-clock budget so the wizard never hangs on a
	wedged upstream."""
	cache = frappe.cache()
	cached = cache.get_value(_PROBE_CACHE_KEY)
	if isinstance(cached, dict) and cached.get("state"):
		return cached
	# Deliberate reuse of the chat pipeline's own private helpers - single
	# sources for the gateway URL, the model/provider pin, and the auth
	# vocabulary, so the probe can never drift from what a real first turn
	# would do (the prewarm module makes the same choice).
	from jarvis.chat.agent_client import AgentSession, AgentUnreachableError, oneshot_run_id
	from jarvis.chat.prewarm import _gateway_ws_url
	from jarvis.chat.turn_handler import _resolve_model_and_provider

	gateway_url = _gateway_ws_url(settings)
	if not gateway_url:
		return {"state": "unreachable", "detail": "", "source": "live_probe"}
	model, provider = _resolve_model_and_provider(frappe._dict(model_override=""))
	verdict = {"state": "unknown", "detail": ""}
	sess = None
	throwaway = None
	try:
		sess = AgentSession.connect(gateway_url)
		throwaway = sess.create_session(label=f"jarvis-preflight-{uuid.uuid4().hex[:8]}")
		deadline = time.monotonic() + _PROBE_BUDGET_S
		error_text = ""
		saw_text = False
		for event in sess.stream_agent_turn(
			throwaway,
			_PROBE_PROMPT,
			oneshot_run_id("preflight", uuid.uuid4().hex, model=model, provider=provider),
			model=model or None,
			provider=provider,
		):
			kind = event.get("kind")
			if kind == "assistant" and (event.get("delta") or event.get("text")):
				saw_text = True
				break
			if kind == "lifecycle" and event.get("phase") == "error":
				error_text = str(event.get("error") or "")
				break
			if kind == "relay:error":
				error_text = str(event.get("error") or "")
				break
			if kind == "lifecycle" and event.get("phase") == "end":
				break
			if time.monotonic() > deadline:
				break
		if saw_text:
			verdict = {"state": "ok", "detail": ""}
		elif error_text:
			verdict = _classify_probe_error(error_text)
	except AgentUnreachableError as e:
		verdict = {"state": "unreachable", "detail": str(e)[:300]}
	except Exception:
		verdict = {"state": "unknown", "detail": ""}
	finally:
		if sess is not None:
			if throwaway:
				try:
					sess.delete_session(throwaway)
				except Exception:
					pass  # orphan; the gateway's own sweep collects it
			try:
				sess.close()
			except Exception:
				pass
	verdict["source"] = "live_probe"
	cache.set_value(_PROBE_CACHE_KEY, verdict, expires_in_sec=_PROBE_CACHE_TTL_S)
	return verdict


def _classify_probe_error(text: str) -> dict:
	"""Rate-limit BEFORE auth: quota messages routinely contain words like
	"credit"/"insufficient" and must land on the NON-blocking side (the
	issue's core constraint). Unclassifiable text is "unknown", which also
	never blocks."""
	from jarvis.chat.title import _AUTH_FAULT_RE

	detail = (text or "")[:300]
	if _RATE_LIMIT_RE.search(text or ""):
		return {"state": "rate_limit", "detail": detail}
	if _AUTH_FAULT_RE.search(text or ""):
		return {"state": "auth", "detail": detail}
	return {"state": "unknown", "detail": detail}
