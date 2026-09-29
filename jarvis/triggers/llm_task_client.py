"""Client for the tenant's agent gateway ``llm-task`` tool.

jarvis-admin-v2#597: LLM triggers used ``jarvis.chat.voice.openrouter_complete``
(the voice/STT OpenRouter key), which most tenants never configure - every
trigger ended as a Failed activity, "Speech-to-text is not configured on this
site." The gateway already runs completions with NO tools on the tenant's own
configured model via ``POST {http base of Jarvis Settings.agent_url}/tools/invoke``
(same bearer as ``jarvis.chat.canvas`` / ``jarvis.chat.generated_media``), so
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
response body is read bounded/streamed. The bearer token is never logged or
included in a raised error.
"""

from __future__ import annotations

import uuid

import frappe

from jarvis.chat.canvas import _http_base

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


def _agent_gateway_creds() -> tuple[str, str]:
	"""``(http_base, bearer_token)`` for this site's agent gateway, or
	``("", "")`` when either half is unset."""
	settings = frappe.get_cached_doc("Jarvis Settings")
	base = _http_base(getattr(settings, "agent_url", "") or "")
	token = (settings.get_password("agent_token", raise_exception=False) or "").strip()
	return base, token


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


def llm_task_complete(prompt: str, input_payload: str, timeout: int = 60) -> str:
	"""Run one llm-task completion on the tenant's own agent gateway + model.

	``input_payload`` is sent as-is as the tool's ``input`` arg (the caller has
	already fenced/serialized it - never re-parsed here). ``timeoutMs`` and
	``maxTokens`` are passed through to the tool for parity with the old
	openrouter_complete path (60s / 1000 tokens by default); a ``schema`` of
	``{"type": "string"}`` makes the runtime validate the parsed JSON is a
	plain string, backing up the JSON-string-literal instruction in the
	prompt. ``provider``/``model`` are deliberately never sent - the tenant's
	own configured model is the whole point, and there is no path for a
	caller to override it. Returns the reply text. Raises
	``LLMTaskNotAvailableError`` when the gateway doesn't expose the tool;
	``LLMTaskError`` on any other failure (unreachable gateway, no
	agent_url/token configured, timeout, oversize or malformed response).
	Never logs the bearer token.
	"""
	import requests

	base, token = _agent_gateway_creds()
	if not base or not token:
		raise LLMTaskError("agent gateway is not configured on this site")

	body = {
		"name": "llm-task",
		"args": {
			"prompt": prompt,
			"input": input_payload,
			"schema": {"type": "string"},
			"maxTokens": _MAX_TOKENS,
			"timeoutMs": timeout * 1000,
		},
		"sessionKey": f"agent:main:trigger-{uuid.uuid4().hex}",
		"agentId": "main",
	}
	try:
		# allow_redirects=False: the route only ever answers 200/4xx/5xx, so a
		# 3xx would mean a compromised gateway trying to pivot the bench at an
		# internal URL (same reasoning as jarvis.chat.generated_media.fetch_media).
		with requests.post(
			f"{base}/tools/invoke",
			json=body,
			headers={"Authorization": f"Bearer {token}"},
			timeout=(_CONNECT_TIMEOUT_S, timeout + _TIMEOUT_BUFFER_S),
			stream=True,
			allow_redirects=False,
		) as response:
			status = response.status_code
			raw = _read_bounded(response)
	except LLMTaskError:
		raise
	except requests.Timeout as e:
		raise LLMTaskError("llm-task request timed out") from e
	except Exception as e:
		raise LLMTaskError(f"llm-task request failed: {e}") from e

	# Checked BEFORE parsing: an older runtime that doesn't even know the
	# ``/tools/invoke`` route can 404 with a plain-text/HTML body, not the
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
	return _extract_text(payload)
