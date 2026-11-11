"""Client for the tenant's agent gateway ``llm-task`` tool.

jarvis-admin-v2#597: LLM triggers used ``jarvis.chat.voice.openrouter_complete``
(the voice/STT OpenRouter key), which most tenants never configure - every
trigger ended as a Failed activity, "Speech-to-text is not configured on this
site." The gateway already runs completions with NO tools on the tenant's own
configured model via the plugin's signed ``POST {http base of Jarvis Settings.agent_url}/jarvis/llm-task``
route (the plugin holds the gateway owner token; this site never does), so
triggers use that instead and only fall back to OpenRouter when the tool is
unavailable (older/unconfigured runtime).

The gateway's ``llm-task`` tool concatenates ``prompt`` and ``JSON.stringify(input)``
into one user message, forces the model to answer with JSON only, and returns
``{"content": [{"type": "text", "text": <json-as-text>}], "details": {"json":
<parsed>, "provider": ..., "model": ...}}``. Callers ask the model for a bare
JSON string literal so ``details.json`` IS the finding text; ``llm_task_complete``
prefers that and only falls back to the ``content`` text block when it isn't a
string.

Same SSRF-safe conventions as ``fetch_media``: the host is ALWAYS the tenant's
own ``agent_url`` (never attacker-influenced), redirects are disabled, and the
response body is read bounded/streamed. The signing key is never logged or
included in a raised error.
"""

from __future__ import annotations

import uuid

import frappe

from jarvis.chat import plugin_routes, session_claims

_CONNECT_TIMEOUT_S = 10
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
_MAX_TOKENS = 1000  # parity with the old openrouter_complete(max_tokens=1000) path
# The HTTP read timeout must exceed the runtime's own timeoutMs (sent in the
# body below) - otherwise the client gives up first and reports a spurious
# "request timed out" for a run the gateway would have finished a moment later.
_TIMEOUT_BUFFER_S = 5


class LLMTaskError(Exception):
	"""Raised for an llm-task invocation failure (network, timeout, oversize,
	malformed response, or any gateway-reported error other than the tool
	being unavailable)."""


class LLMTaskNotAvailableError(LLMTaskError):
	"""Raised when the tenant's agent gateway does not expose the ``llm-task``
	tool - an older runtime, or a container not yet updated/provisioned.
	Callers may fall back to a different completion path on this specific
	error, never on the base ``LLMTaskError``."""


def _agent_url() -> str:
	"""The agent URL from Jarvis Settings, or "" when unset."""
	settings = frappe.get_cached_doc("Jarvis Settings")
	return (getattr(settings, "agent_url", "") or "").strip()


def _read_bounded(response) -> bytes:
	"""Stream the response body, aborting once it exceeds the size cap
	(the gateway is same-infra and answers with a small JSON envelope, so an
	oversized body signals something wrong rather than a legitimate reply)."""
	buf = bytearray()
	for chunk in response.iter_content(64 * 1024):
		buf += chunk
		if len(buf) > _MAX_RESPONSE_BYTES:
			raise LLMTaskError("llm-task response exceeded the size limit")
	return bytes(buf)


def _gateway_error_message(status: int, raw: bytes) -> str:
	"""Best-effort ``error.message`` out of a non-200 body - tolerates a body
	that isn't the expected JSON envelope (an older runtime's 404 may answer
	with plain text/HTML, not the ``{"error": {...}}`` shape)."""
	try:
		payload = frappe.parse_json(raw.decode("utf-8")) if raw else {}
		error = (
			payload.get("error")
			if isinstance(payload, dict) and isinstance(payload.get("error"), dict)
			else {}
		)
		return error.get("message") or f"llm-task invocation failed (HTTP {status})"
	except Exception:
		return f"llm-task invocation failed (HTTP {status})"


def _raise_for_gateway_error(status: int, payload: dict) -> None:
	error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
	message = error.get("message") or f"llm-task invocation failed (HTTP {status})"
	if error.get("type") == "not_found" or "tool not available" in message.lower():
		raise LLMTaskNotAvailableError(message)
	raise LLMTaskError(message)


def _extract_text(payload: dict) -> str:
	result = payload.get("result") or {}
	details = result.get("details") or {}
	json_val = details.get("json")
	if isinstance(json_val, str) and json_val.strip():
		return json_val.strip()
	for block in result.get("content") or []:
		if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
			return str(block["text"]).strip()
	raise LLMTaskError("llm-task returned an empty result")


def _extract_object_reply(payload: dict) -> dict | str:
	"""The reply for an object-contract round (``expect_object``): the parsed
	``details.json`` when it is an object, else the content text block parsed as
	a JSON object, else the plain text (a model that ignored the contract)."""
	result = payload.get("result") or {}
	details = result.get("details") or {}
	json_val = details.get("json")
	if isinstance(json_val, dict):
		return json_val
	for block in result.get("content") or []:
		if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
			try:
				parsed = frappe.parse_json(str(block["text"]).strip())
			except Exception:
				break
			if isinstance(parsed, dict):
				return parsed
			break
	return _extract_text(payload)


def _call_route(path: str, body: dict, read_timeout_s: float) -> tuple[int, bytes]:
	"""POST ``body`` to a plugin route on the tenant's agent and return ``(status, raw body)``.

	Every transport failure is an ``LLMTaskError`` (never carrying the signing key)."""
	import requests

	# Third door to the tenant's agent besides AgentSession.connect: a restored copy
	# must not use its token. LLMTaskError (not the OpenRouter fallback) is the answer.
	from jarvis.chat import site_pin
	from jarvis.exceptions import SiteIsCopyError

	try:
		site_pin.assert_not_copy()
	except SiteIsCopyError as e:
		raise LLMTaskError(str(e)) from e

	agent_url = _agent_url()
	if not agent_url:
		raise LLMTaskError("agent gateway is not configured on this site")

	# The plugin vouches this key with the site before it runs the task, so claim it
	# first: a trigger key has no row until the activity is written.
	session_key = f"agent:main:trigger-{uuid.uuid4().hex}"
	session_claims.claim(session_key)
	try:
		# route_request never follows a redirect: the route only ever answers
		# 200/4xx/5xx, so a 3xx would mean a compromised gateway trying to pivot the
		# bench at an internal URL (same reasoning as generated_media.fetch_media).
		with plugin_routes.route_request(
			agent_url,
			"POST",
			path,
			session_key,
			body,
			read_timeout_s=read_timeout_s,
			stream=True,
		) as response:
			return response.status_code, _read_bounded(response)
	except LLMTaskError:
		raise
	except plugin_routes.PluginRouteError as e:
		raise LLMTaskError(str(e)) from e
	except requests.Timeout as e:
		raise LLMTaskError("llm-task request timed out") from e
	except Exception as e:
		raise LLMTaskError("llm-task request failed") from e


def llm_task_complete(
	prompt: str,
	input_payload: str,
	timeout: int = 60,
	expect_object: bool = False,
	model_ref: dict | None = None,
) -> str | dict:
	"""Run one llm-task completion on the tenant's own agent gateway + model.

	``input_payload`` is sent as-is as the tool's ``input`` arg (the caller has
	already fenced/serialized it - never re-parsed here). ``timeoutMs`` and
	``maxTokens`` are passed through to the tool for parity with the old
	openrouter_complete path (60s / 1000 tokens by default); a ``schema`` of
	``{"type": "string"}`` makes the runtime validate the parsed JSON is a
	plain string, backing up the JSON-string-literal instruction in the
	prompt. ``provider``/``model`` are sent only for a pool failover attempt
	(``model_ref``, see ``llm_task_complete_with_failover``); otherwise the
	tenant's own configured model answers. Returns the reply text. Raises
	``LLMTaskNotAvailableError`` when the gateway doesn't expose the tool;
	``LLMTaskError`` on any other failure (unreachable gateway, no
	agent_url/token configured, timeout, oversize or malformed response).
	Never logs the bearer token.

	``expect_object`` (LLM trigger lookups) asks for a JSON object instead of a
	string literal: the schema becomes ``{"type": ["object", "string"]}`` (an
	object-only schema makes the runtime fail a prose answer with HTTP 500, so
	a string answer must stay valid and become the finding) and the return is
	the parsed object (``details.json`` first, then the content text), or the
	plain text when the model did not answer with one.
	"""
	args = {
		"prompt": prompt,
		"input": input_payload,
		"schema": {"type": ["object", "string"] if expect_object else "string"},
		"maxTokens": _MAX_TOKENS,
		"timeoutMs": timeout * 1000,
	}
	if model_ref:
		# jarvis-admin-v2#826: a pool failover attempt names the exact provider-qualified
		# model; the gateway honours it only where the host allowlists that model.
		args["provider"] = model_ref["provider"]
		args["model"] = model_ref["model"]
	status, raw = _call_route(plugin_routes.LLM_TASK_PATH, {"args": args}, timeout + _TIMEOUT_BUFFER_S)

	# 401/403 are the plugin refusing this site (bad key, or a session it will not
	# vouch for): a fixed error, never a fallback to another completion path.
	if status in (401, 403):
		raise LLMTaskError("llm-task request was refused by the agent plugin")

	# Checked BEFORE parsing: an older runtime that doesn't even know the
	# ``/jarvis/llm-task`` route can 404 with a plain-text/HTML body, not the
	# ``{"ok": false, "error": {...}}`` envelope - that must still resolve to
	# "not available" so callers fall back, not to a generic parse failure.
	if status == 404:
		raise LLMTaskNotAvailableError(_gateway_error_message(status, raw))

	try:
		payload = frappe.parse_json(raw.decode("utf-8")) if raw else {}
	except Exception as e:
		raise LLMTaskError("llm-task returned a non-JSON response") from e
	if not isinstance(payload, dict):
		raise LLMTaskError("llm-task returned an unexpected response shape")

	if not payload.get("ok"):
		_raise_for_gateway_error(status, payload)
	return _extract_object_reply(payload) if expect_object else _extract_text(payload)


# ---- pool failover (jarvis-admin-v2#826) ---------------------------------------------------

# Error classes (jarvis.chat.error_taxonomy codes) that mean "this model's credential cannot
# serve right now", so the next pool model may. Anything else (timeout, bad reply, ...) is not
# cured by another model and is raised as-is.
_FAILOVER_CODES = frozenset({"subscription-expired", "authentication", "billing", "quota"})
# Total completions one trigger evaluation may spend walking the pool.
_MAX_ATTEMPTS = 3


def pool_model_refs() -> list[dict]:
	"""The tenant gateway's chat models as ``[{"provider", "model"}, ...]``, primary first and
	then the fallbacks: the order its own chat failover uses, with the provider-qualified ids
	only the gateway can mint. Raises ``LLMTaskNotAvailableError`` when the plugin predates the
	route, ``LLMTaskError`` on any other failure."""
	status, raw = _call_route(plugin_routes.LLM_TASK_MODELS_PATH, {}, _CONNECT_TIMEOUT_S + _TIMEOUT_BUFFER_S)
	if status == 404:
		raise LLMTaskNotAvailableError(_gateway_error_message(status, raw))
	if status in (401, 403):
		raise LLMTaskError("llm-task request was refused by the agent plugin")
	try:
		payload = frappe.parse_json(raw.decode("utf-8")) if raw else {}
	except Exception as e:
		raise LLMTaskError("llm-task returned a non-JSON response") from e
	if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
		raise LLMTaskError(f"llm-task model list failed (HTTP {status})")
	models = payload.get("models")
	return [
		{"provider": m["provider"], "model": m["model"]}
		for m in (models if isinstance(models, list) else [])
		if isinstance(m, dict)
		and isinstance(m.get("provider"), str)
		and isinstance(m.get("model"), str)
		and m["provider"]
		and m["model"]
	]


def _failover_code(error: Exception) -> str:
	"""The failover-worthy error class of ``error``, or ``""``."""
	from jarvis.chat.error_taxonomy import classify_error_text

	code = classify_error_text(str(error))
	return code if code in _FAILOVER_CODES else ""


def _flagged_upstreams() -> set[str]:
	"""Subscription upstreams the site already knows are signed out. Never raises."""
	try:
		from jarvis import subscription_health

		return {e.get("upstream") for e in subscription_health.expired_entries() if e.get("upstream")}
	except Exception:
		return set()


def _blocked(ref: dict, skipped: set[str]) -> bool:
	"""Whether ``ref`` is served by an upstream in ``skipped``. Never raises."""
	if not skipped:
		return False
	try:
		from jarvis import subscription_health

		return subscription_health.upstream_for_serving(ref["model"], ref["provider"]) in skipped
	except Exception:
		return False


def _pool_refs_or_empty() -> list[dict]:
	"""``pool_model_refs()``, or ``[]`` where the plugin cannot list them (older plugin, refused)."""
	try:
		return pool_model_refs()
	except LLMTaskError:
		return []


def llm_task_complete_with_failover(
	prompt: str, input_payload: str, timeout: int = 60, expect_object: bool = False
) -> str | dict:
	"""``llm_task_complete`` that walks the tenant's model pool when a model's credential is dead.

	The first attempt uses the gateway's own default (the pool primary), unless the site already
	flags the primary's subscription as expired, in which case it starts at the first model whose
	upstream is not flagged. A failure of the credential kind (``_FAILOVER_CODES``) is fed to
	``subscription_health`` (so expiry detection does not depend on chat) and the next model in
	pool order is tried, at most ``_MAX_ATTEMPTS`` completions in all. Any other failure is
	raised as-is.

	Where the walk cannot start (the plugin has no model list, or the host's render does not
	allow a per-call model) the FIRST error is raised: exactly what a trigger did before.
	"""
	from jarvis import subscription_health

	extra = {"expect_object": True} if expect_object else {}
	skipped = _flagged_upstreams()
	ordered: list[dict] | None = None  # pool order after the primary, learned lazily
	use_default = True
	if skipped:
		refs = _pool_refs_or_empty()
		if refs and _blocked(refs[0], skipped):
			ordered = [r for r in refs[1:] if not _blocked(r, skipped)]
			use_default = not ordered
	first_error: LLMTaskError | None = None
	for _ in range(_MAX_ATTEMPTS):
		ref = None
		if use_default:
			use_default = False
		else:
			if ordered is None:
				ordered = _pool_refs_or_empty()[1:]
			ref = next((r for r in ordered if not _blocked(r, skipped)), None)
			if ref is None:
				break
			ordered.remove(ref)
		try:
			return llm_task_complete(
				prompt, input_payload, timeout=timeout, **extra, **({"model_ref": ref} if ref else {})
			)
		except LLMTaskNotAvailableError:
			raise
		except LLMTaskError as e:
			first_error = first_error or e
			code = _failover_code(e)
			if not code:
				raise first_error from None
			subscription_health.note_turn_error(str(e), code)
			from jarvis.chat.error_taxonomy import subscription_upstream_from_error

			upstream = subscription_upstream_from_error(str(e))
			if upstream:
				skipped.add(upstream)
	if first_error is None:
		# Nothing usable left to try (every other model is flagged): let the default speak.
		return llm_task_complete(prompt, input_payload, timeout=timeout, **extra)
	raise first_error
