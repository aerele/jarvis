"""One-shot vision extraction through the tenant's agent plugin.

Document Intake sends a few page images plus a JSON schema to the plugin's
``POST /jarvis/vision-task`` route and gets back one parsed JSON object. The
transport, size cap and error mapping are the ``llm-task`` client's, reused so
a 404 (older plugin without the route) and a refusal behave the same way.
"""

from __future__ import annotations

import frappe

from jarvis.chat import plugin_routes
from jarvis.triggers import llm_task_client as lt

LLMTaskError = lt.LLMTaskError
LLMTaskNotAvailableError = lt.LLMTaskNotAvailableError


def _vision_wording(message: str) -> str:
	"""The shared helpers speak of ``llm-task``; this route is the vision task."""
	return message.replace("llm-task", "vision task")


def vision_task_complete(
	prompt: str,
	schema: dict,
	images: list[dict],
	timeout: int = 90,
	max_tokens: int = 4000,
	model_ref: dict | None = None,
) -> dict:
	"""Extract structured data from ``images`` following ``schema``; return the parsed object.

	``images`` items are ``{"mime", "data_b64", ...}``; only those two keys are sent (the
	route rejects nothing extra, but file names have no business leaving the site).
	A reply of ``{"error": "NO_IMAGE"}`` is returned as-is: the caller decides what it means.
	Raises ``LLMTaskNotAvailableError`` on a 404 (plugin without the route), ``LLMTaskError``
	on anything else (transport, refusal, gateway error, non-object reply).
	"""
	args = {
		"prompt": prompt,
		"schema": schema,
		"images": [{"mime": i["mime"], "data_b64": i["data_b64"]} for i in images],
		"maxTokens": max_tokens,
		"timeoutMs": timeout * 1000,
	}
	if model_ref:
		args["provider"] = model_ref["provider"]
		args["model"] = model_ref["model"]
	try:
		return _run_vision_task(args, timeout)
	except LLMTaskNotAvailableError as e:
		raise LLMTaskNotAvailableError(_vision_wording(str(e))) from e
	except LLMTaskError as e:
		raise LLMTaskError(_vision_wording(str(e))) from e


def _run_vision_task(args: dict, timeout: int) -> dict:
	status, raw = lt._call_route(
		plugin_routes.VISION_TASK_PATH, {"args": args}, timeout + lt._TIMEOUT_BUFFER_S
	)

	if status in (401, 403):
		raise LLMTaskError("vision task request was refused by the agent plugin")
	# Before parsing: an older plugin answers an unknown route with a non-JSON 404.
	if status == 404:
		raise LLMTaskNotAvailableError(lt._gateway_error_message(status, raw))

	try:
		payload = frappe.parse_json(raw.decode("utf-8")) if raw else {}
	except Exception as e:
		raise LLMTaskError("vision task returned a non-JSON response") from e
	if not isinstance(payload, dict):
		raise LLMTaskError("vision task returned an unexpected response shape")
	if not payload.get("ok"):
		lt._raise_for_gateway_error(status, payload)

	reply = lt._extract_object_reply(payload)
	if not isinstance(reply, dict):
		raise LLMTaskError("vision task did not return a JSON object")
	return reply
