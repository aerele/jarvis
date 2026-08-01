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
``{"ok": bool, "checks": [{"check", "ok", "detail"}, ...]}``.

NOT the same thing as ``PUT /v1/containers/{name}/llm-pool``
(``jarvis.admin_client.post_update_llm_pool``): that is a MUTATING apply on
the fleet-agent that rotates secrets, rewrites the tenant's openclaw.json and
can restart the container (10-30s chat outage). This probe never writes
Jarvis Settings, never calls admin_client, and never touches the fleet or a
container - it is a single side-effect-free HTTP round-trip straight from
THIS bench to the provider's own API, using whatever
provider/base_url/model/api_key the customer has typed into the panel so far
(nothing here is persisted, and nothing here may call the mutating pool
apply).

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
from jarvis.jarvis.pool_serialize import normalize_provider
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

	Returns ``{"ok": bool, "checks": [...], "provider": <canonical id>,
	"local_endpoint": bool}``. See the module docstring for the CAVEAT
	(probed from the bench, not the tenant's container) and the SECURITY
	notes (SSRF guard via link_fetch, key scrubbing)."""
	checks: list[dict] = []
	provider_id = normalize_provider(provider)
	is_local = provider_id in LOCAL_PROVIDER_IDS

	model = (model or "").strip()
	api_key = (api_key or "").strip()
	base = (base_url or "").strip()

	def _done(ok: bool) -> dict:
		return {"ok": ok, "checks": checks, "provider": provider_id, "local_endpoint": is_local}

	if not model:
		checks.append(_check("input", False, "Enter a model id before testing."))
		return _done(False)
	if not api_key:
		checks.append(_check("input", False, "Enter an API key before testing."))
		return _done(False)
	if not base:
		checks.append(_check("input", False, "Enter a base URL before testing."))
		return _done(False)

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
		detail = _scrub(str(exc), api_key)
		if is_local:
			detail = (
				f"Can't reach this endpoint from the bench ({detail}). Local/private "
				"endpoints are only reachable from inside your Jarvis container, so "
				"this can't be verified from here."
			)
		checks.append(_check("probe_request", False, detail))
		return _done(False)

	if 200 <= status < 300:
		checks.append(
			_check(
				"probe_request",
				True,
				f"{provider_id or 'The provider'} accepted a 1-token test request (HTTP {status}).",
			)
		)
		return _done(True)

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
	return _done(False)


@frappe.whitelist()
def test_llm_api_key(provider: str, model: str, api_key: str = "", base_url: str = "") -> dict:
	"""UI 'Test' button on an API-key LLM pool model row, run BEFORE save.

	Jarvis Admin / System Manager - the same gate ``jarvis.onboarding.
	save_llm_pool`` already enforces for this exact panel (the Edit panel's
	``editable`` prop is ``isSM || is_jarvis_admin``), so anyone who can edit
	a row can also test it.

	Operates ONLY on the values passed in (whatever is currently typed into
	the panel) - it never reads or writes the stored, encrypted key on an
	existing row, and never persists anything or touches the fleet/container.
	See the module docstring for why this must never call the mutating
	``/llm-pool`` apply."""
	require_jarvis_admin()
	result = probe_api_key(provider, model, api_key, base_url)
	result["caveat"] = (
		"Tested from the bench, not from your Jarvis container. Local/private "
		"endpoints (ollama, vllm) can only be confirmed from inside the container."
		if result.get("local_endpoint")
		else "Tested from the bench's network. Live chat runs from inside your Jarvis "
		"container - for a public provider these agree."
	)
	return result
