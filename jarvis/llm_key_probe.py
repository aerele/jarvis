"""Pre-save "Test" probe for one API-key LLM pool model row (Settings -> AI
models -> Edit -> API key -> Test), BEFORE the customer clicks Save.

Motivated by a live case: a syntactically valid GLM/Z.ai key whose z.ai
account had zero balance saved cleanly and only failed AFTER save, with a
bare "Not working" chip and no reason - the customer could not tell a bad key
from an unpaid account. z.ai's own error is precise::

    {"error":{"code":"1113","message":"Insufficient balance or no resource
    package. Please recharge."}}

This probe surfaces exactly that message instead of swallowing it.

Shape: synchronous, no persistence, gated, does live HTTP, and NEVER raises
for a failed check - it always returns a structured
``{"ok": bool, "verdict": str, "checks": [{"check", "ok", "detail"}, ...]}``.

NOT the same thing as ``PUT /v1/containers/{name}/llm-pool``
(``jarvis.admin_client.post_update_llm_pool``): that is a MUTATING apply on
the fleet-agent that rotates secrets, rewrites the tenant's openclaw.json and
can restart the container (10-30s chat outage). This probe never writes
Jarvis Settings, never calls admin_client, and never touches the fleet or a
container - it is a single side-effect-free HTTP round-trip straight from
THIS bench to the provider's own API, using the provider/base_url/model the
customer has typed into the panel so far (nothing here is persisted, and
nothing here may call the mutating pool apply).

STORED KEYS (#679): the key is the one exception to "only what is typed".
``test_llm_api_key`` will, on explicit request, resolve the saved key for the
row's provider server-side and probe with it, so that changing ONLY a base
URL can be tested by a customer who does not have the key to hand - which is
precisely when a test is worth most. The key is read through
``pool_serialize.stored_api_keys_by_provider``, the same helper
``save_llm_pool`` merges with, so Test can never attest to a credential Save
would not send. It is used in-process and never returned to the browser.

CAVEAT - read before trusting a green check: live tenant chat is actually
served from INSIDE the tenant's bifrost container, not from this bench. For
a public provider (OpenAI/Groq/Z.ai/...) the two networks agree, so a pass
here is a real signal. For a provider whose endpoint is only reachable from
inside the container (ollama/vllm on localhost, or a customer's own private
network), this probe cannot confirm reachability from here - see
``LOCAL_PROVIDER_IDS`` and the ``local_endpoint`` flag the caller should
render as a disclaimer, never a guarantee. ``test_llm_api_key`` always
attaches a ``caveat`` string for this reason, whether or not the provider is
tagged local.

THREE VERDICTS, NOT TWO (#680). That caveat used to be the only hedge, it sat
underneath a red "Test failed.", and it was routinely disbelieved: a base_url
only the container can resolve (``http://host.docker.internal:9000/openai/v1``)
answers HTTP 200 from inside the container and "Could not resolve host" from
here, so the customer read the red banner and did not click Save. A bench that
never got a byte back has learned NOTHING about the credential or the endpoint,
only about its own network, so calling that "failed" was false. ``verdict``
therefore carries three values::

    pass       - the provider answered 2xx. Real signal.
    fail       - a definitive rejection: the provider answered non-2xx, or the
                 input/URL is unusable. The customer must fix something.
    unverified - this bench could not reach the endpoint at all (DNS, an
                 address the SSRF guard refuses, or a dead socket). Says
                 nothing either way; render neutrally, never as a failure.

``ok`` stays strictly ``verdict == "pass"``, so every existing consumer gating
on it keeps today's strictness - notably the SPA's onboarding "Start chatting"
gate, which requires a passing probe before it lets a freshly typed key
through. An endpoint this bench merely could not reach must not unlock a gate
that a real pass unlocks. The classification comes from
``LinkFetchError.kind``, set at the raise site, never from matching message
text.

The honest alternative - probing from inside the container, the network chat
really uses - was investigated and rejected for now, because no such path
exists from this plane. ``jarvis_fleet_agent.proxy_probe`` already does the
right thing (it execs a request inside the tenant's container and even
distinguishes an inconclusive result from a definitive one), but it runs only
inside the MUTATING ``PUT /v1/containers/{name}/llm-pool`` apply: after
secrets are written and the container restarted, which is exactly what a
pre-save Test must not do. Exposing it standalone needs a new fleet-agent
route, an admin relay (this bench holds no fleet credentials), and wiring
here. Three repos, worth doing, but not as a side effect of fixing a banner.

SECURITY: ``base_url`` is customer-supplied, so this is an SSRF vector
exactly like ``jarvis.chat.link_fetch``'s Personalise link fetch (a prior
security audit flagged SSRF as an open risk in this app) - this module reuses
that guard via ``link_fetch.request_pinned`` rather than re-implementing it.
The api_key is NEVER echoed back: not in a returned check detail, not in a
raised exception, not in ``frappe.log_error``. Provider error bodies are
scrubbed for a literal key match and capped in length before they reach the
response.
"""

from __future__ import annotations

import json

import frappe

from jarvis.chat import link_fetch
from jarvis.jarvis.pool_serialize import normalize_provider, stored_api_keys_by_provider
from jarvis.permissions import require_jarvis_admin

_TIMEOUT_S = 15
_MAX_BODY_BYTES = 65536
_MAX_DETAIL_LEN = 400

# Canonical provider ids (jarvis.jarvis.pool_serialize.normalize_provider's
# vocabulary) whose usual endpoint only makes sense reached from INSIDE the
# tenant's bifrost container (localhost / a customer LAN), never from this
# bench - see the module docstring's CAVEAT. The Test button still runs (a
# customer CAN point "vllm"/"ollama" at a real public URL), but the result
# carries local_endpoint=True so the caller renders a disclaimer, and a guard
# rejection gets a locality-aware message instead of a bare "blocked".
LOCAL_PROVIDER_IDS = {"ollama", "vllm"}

# Wire-protocol grouping for building the probe request. Everything not
# explicitly Anthropic/Gemini speaks the OpenAI chat/completions shape - this
# is also true of Z.ai/GLM, which normalize_provider maps to "openai_compat"
# (jarvis.jarvis.pool_serialize._PROVIDER_ALIASES), and is exactly today's
# motivating case.
_ANTHROPIC_IDS = {"anthropic"}
_GEMINI_IDS = {"gemini"}

# The three values of the result's `verdict` - see the module docstring.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNVERIFIED = "unverified"

# What to tell the customer for each way the bench can fail to reach an
# endpoint. Each one is a statement about THIS network, never about the key:
# the same endpoint may well answer from inside the container. Keyed by
# link_fetch's raise-site classification, so a reworded exception message
# cannot silently reclassify a result.
_UNREACHABLE_DETAIL = {
	link_fetch.ERR_UNRESOLVED: (
		"This bench could not resolve that hostname, so nothing was sent. "
		"An endpoint only your Jarvis container can resolve looks exactly like "
		"this from here, and so does a typo in the base URL."
	),
	link_fetch.ERR_BLOCKED_ADDRESS: (
		"That address is private, and this bench does not connect to private "
		"addresses. Your Jarvis container can, so an endpoint on your own "
		"network can only be confirmed from inside it."
	),
	link_fetch.ERR_CONNECT_FAILED: (
		"This bench could not open a connection to that endpoint, so nothing "
		"was sent. It may still be reachable from inside your Jarvis container, "
		"which is where chat runs."
	),
}


def _check(name: str, ok: bool, detail: str) -> dict:
	return {"check": name, "ok": bool(ok), "detail": detail}


def _provider_kind(provider_id: str) -> str:
	if provider_id in _ANTHROPIC_IDS:
		return "anthropic"
	if provider_id in _GEMINI_IDS:
		return "gemini"
	return "openai"


def _scrub(text: str, api_key: str) -> str:
	"""Cap length and strip a literal api_key match, so a provider that
	echoes the credential back in an error body (some do, on a malformed
	auth header) never leaks it into the UI or a log."""
	t = (text or "").strip()
	if api_key:
		t = t.replace(api_key, "***")
	if len(t) > _MAX_DETAIL_LEN:
		t = t[:_MAX_DETAIL_LEN] + "...(truncated)"
	return t


def _extract_provider_message(body: bytes) -> str:
	"""Best-effort pull of a human-readable message out of a provider's JSON
	error body. OpenAI-, Anthropic- and Gemini-shaped errors all nest under
	"error" (an object carrying "message", occasionally a bare string) -
	this is exactly the z.ai shape that motivated this module::

	    {"error":{"code":"1113","message":"Insufficient balance or no
	    resource package. Please recharge."}}

	Falls back to the raw decoded body when the shape doesn't match, and to
	a fixed string when the body isn't decodable text at all. Never raises.
	"""
	try:
		text = body.decode("utf-8", errors="replace")
	except Exception:
		return "(response body could not be decoded)"
	try:
		data = json.loads(text)
	except (ValueError, TypeError):
		return text
	if isinstance(data, dict):
		err = data.get("error")
		if isinstance(err, dict):
			msg = err.get("message") or err.get("code") or ""
			if msg:
				return str(msg)
		elif isinstance(err, str) and err:
			return err
		if data.get("message"):
			return str(data["message"])
	return text


def _build_request(kind: str, base_url: str, model: str, api_key: str) -> dict:
	"""Build ``{"url", "headers", "json_body"}`` for a minimal 1-token
	completion against ``kind``'s wire protocol. The key always rides in a
	HEADER (never a URL query param, including Gemini's ``x-goog-api-key``),
	so it can never end up logged as part of a URL."""
	base = (base_url or "").rstrip("/")
	if kind == "anthropic":
		return {
			"url": f"{base}/v1/messages",
			"headers": {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
			"json_body": {
				"model": model,
				"max_tokens": 1,
				"messages": [{"role": "user", "content": "hi"}],
			},
		}
	if kind == "gemini":
		return {
			"url": f"{base}/v1beta/models/{model}:generateContent",
			"headers": {"x-goog-api-key": api_key},
			"json_body": {
				"contents": [{"parts": [{"text": "hi"}]}],
				"generationConfig": {"maxOutputTokens": 1},
			},
		}
	# OpenAI-compatible (openai, mistral, groq, together, deepseek, moonshot,
	# xai, openrouter, openai_compat, ollama, vllm, and Z.ai/GLM via
	# openai_compat) - all speak POST {base}/chat/completions, Bearer auth.
	return {
		"url": f"{base}/chat/completions",
		"headers": {"Authorization": f"Bearer {api_key}"},
		"json_body": {
			"model": model,
			"messages": [{"role": "user", "content": "hi"}],
			"max_tokens": 1,
			"stream": False,
		},
	}


def probe_api_key(provider: str, model: str, api_key: str, base_url: str = "") -> dict:
	"""Live, side-effect-free check of one provider/base_url/model/api_key
	combination. NEVER raises - every failure mode (missing input, an
	SSRF-blocked endpoint, a network error, a provider rejection) comes back
	as a failed check with a human-readable, key-scrubbed detail.

	Returns ``{"ok": bool, "verdict": str, "checks": [...], "provider":
	<canonical id>, "local_endpoint": bool}``, where ``ok`` is exactly
	``verdict == "pass"``. See the module docstring for the three verdicts and
	why an unreachable endpoint is NOT a failure (#680), for the CAVEAT (probed
	from the bench, not the tenant's container) and for the SECURITY notes (SSRF
	guard via link_fetch, key scrubbing)."""
	checks: list[dict] = []
	provider_id = normalize_provider(provider)
	is_local = provider_id in LOCAL_PROVIDER_IDS

	model = (model or "").strip()
	api_key = (api_key or "").strip()
	base = (base_url or "").strip()

	def _done(verdict: str) -> dict:
		return {
			"ok": verdict == VERDICT_PASS,
			"verdict": verdict,
			"checks": checks,
			"provider": provider_id,
			"local_endpoint": is_local,
		}

	# Missing input is a genuine fail, not "unverified": there is something
	# concrete for the customer to fix, and nothing was left uncertain.
	if not model:
		checks.append(_check("input", False, "Enter a model id before testing."))
		return _done(VERDICT_FAIL)
	if not api_key:
		checks.append(_check("input", False, "Enter an API key before testing."))
		return _done(VERDICT_FAIL)
	if not base:
		checks.append(_check("input", False, "Enter a base URL before testing."))
		return _done(VERDICT_FAIL)

	kind = _provider_kind(provider_id)
	req = _build_request(kind, base, model, api_key)

	try:
		status, _headers, body = link_fetch.request_pinned(
			req["url"],
			method="POST",
			headers=req["headers"],
			json_body=req["json_body"],
			timeout=_TIMEOUT_S,
			max_bytes=_MAX_BODY_BYTES,
		)
	except link_fetch.LinkFetchError as exc:
		reason = _scrub(str(exc), api_key)
		kind = getattr(exc, "kind", link_fetch.ERR_RESPONSE)
		# Never got a byte back. That is a fact about this bench's network and
		# nothing else, so it must not be dressed up as a verdict on the key or
		# the endpoint - see #680 and the module docstring's three verdicts.
		if kind in link_fetch.UNREACHABLE_KINDS:
			checks.append(_check("probe_request", False, _UNREACHABLE_DETAIL[kind]))
			return _done(VERDICT_UNVERIFIED)
		checks.append(_check("probe_request", False, reason))
		return _done(VERDICT_FAIL)

	if 200 <= status < 300:
		checks.append(
			_check(
				"probe_request",
				True,
				f"{provider_id or 'The provider'} accepted a 1-token test request (HTTP {status}).",
			)
		)
		return _done(VERDICT_PASS)

	# `body` came back from whatever the customer-supplied base_url points to - a
	# hostile or merely malformed response must never turn "the test failed" into
	# an unhandled 500 (breaking probe_api_key's documented "NEVER raises"
	# contract). _extract_provider_message already guards the failure modes it
	# knows about (bad JSON, undecodable bytes); this catches anything else
	# (e.g. a pathological structure blowing the interpreter's recursion limit)
	# so a bad response body degrades to a generic message instead of a crash.
	try:
		message = _scrub(_extract_provider_message(body), api_key)
	except Exception:
		message = ""
	checks.append(
		_check(
			"probe_request",
			False,
			f"HTTP {status}: {message}" if message else f"HTTP {status} with no error detail.",
		)
	)
	return _done(VERDICT_FAIL)


def _stored_api_key(provider: str) -> str:
	"""The saved key ``save_llm_pool`` would merge into a row on this provider,
	decrypted, or "" when there is none.

	Deliberately keyed on provider alone and deliberately NOT a lookup of its
	own: it is the same ``stored_api_keys_by_provider`` snapshot the save path
	merges from (see that helper for why per-row identity is not available and
	would not help). Probing anything else would let a green Test attest to a
	credential Save then declines to send.

	Never raises. ``_get_password`` propagates a genuine decryption failure,
	which on the save path is right (better to fail than to wipe a credential
	with a blank), but here it would turn a Test click into a 500. Degrading to
	"" instead lands the customer on the honest "no saved key" check below."""
	try:
		settings = frappe.get_single("Jarvis Settings")
		return stored_api_keys_by_provider(settings.get("models")).get(normalize_provider(provider), "")
	except Exception:
		frappe.log_error(title="llm_key_probe: stored key lookup failed")
		return ""


@frappe.whitelist()
def test_llm_api_key(
	provider: str, model: str, api_key: str = "", base_url: str = "", use_stored_key: int = 0
) -> dict:
	"""UI 'Test' button on an API-key LLM pool model row, run BEFORE save.

	Jarvis Admin / System Manager - the same gate ``jarvis.onboarding.
	save_llm_pool`` already enforces for this exact panel (the Edit panel's
	``editable`` prop is ``isSM || is_jarvis_admin``), so anyone who can edit
	a row can also test it.

	Persists nothing and never touches the fleet/container; see the module
	docstring for why this must never call the mutating ``/llm-pool`` apply.

	``use_stored_key`` opts into probing the SAVED key for ``provider`` instead
	of a typed one (#679), so that a customer who changes only the base URL can
	test it without digging out a credential they pasted months ago - the case
	where testing before saving is worth the most. A typed ``api_key`` always
	wins, matching the save-path merge, so a stale flag from an old client
	cannot override what the customer just entered. This grants no new reach:
	the same role can already Save, which sends that same stored key to any
	base URL it likes. The resolved key stays server-side, is scrubbed out of
	provider error bodies like any other, and is never part of the response."""
	require_jarvis_admin()
	api_key = (api_key or "").strip()
	if not api_key and frappe.utils.cint(use_stored_key):
		api_key = _stored_api_key(provider)
		if not api_key:
			# The row claimed a saved key and there is none under this provider:
			# it was removed, or the provider was switched since the panel loaded.
			# Say which, rather than the generic "enter an API key".
			return {
				"ok": False,
				"verdict": VERDICT_FAIL,
				"checks": [
					_check(
						"input",
						False,
						"No saved key found for this provider. Enter the API key to test it.",
					)
				],
				"provider": normalize_provider(provider),
				"local_endpoint": normalize_provider(provider) in LOCAL_PROVIDER_IDS,
				"caveat": "",
			}
	result = probe_api_key(provider, model, api_key, base_url)
	result["caveat"] = _caveat_for(result)
	return result


def _caveat_for(result: dict) -> str:
	"""The small print under the Test result. It has to say something different
	once the verdict is ``unverified``: the old text ("tested from the bench's
	network") describes a test that in that case never actually happened."""
	if result.get("verdict") == VERDICT_UNVERIFIED:
		return (
			"Nothing reached the provider, so this is not a verdict on your key. "
			"Live chat runs from inside your Jarvis container, which reaches "
			"private and container-only endpoints that this bench cannot. If the "
			"URL is right for the container, saving is still the way to apply it."
		)
	if result.get("local_endpoint"):
		return (
			"Tested from the bench, not from your Jarvis container. Local/private "
			"endpoints (ollama, vllm) can only be confirmed from inside the container."
		)
	return (
		"Tested from the bench's network. Live chat runs from inside your Jarvis "
		"container - for a public provider these agree."
	)
