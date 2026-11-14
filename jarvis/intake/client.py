"""One-shot vision extraction through the tenant's agent plugin.

Document Intake sends a few page images plus a JSON schema to the plugin's
``POST /jarvis/vision-task`` route and gets back one parsed JSON object. The
transport, size cap and error mapping are the ``llm-task`` client's, reused so
a 404 (older plugin without the route) and a refusal behave the same way.
"""

import frappe

from jarvis.chat import plugin_routes
from jarvis.intake import limits
from jarvis.triggers import llm_task_client as lt

LLMTaskError = lt.LLMTaskError
LLMTaskNotAvailableError = lt.LLMTaskNotAvailableError


def reached_model(e: Exception) -> bool:
	"""True when the request may have reached the model, so it is billable. False ONLY for a
	failure before the request left the site (copy check, no agent configured, a connect
	failure, a refused request over the limits) and for a 404 or a 401/403 (an older plugin
	without the route, or the plugin refusing the call before the model ran). Anything after the request was sent, or whose state is unknown, counts: failing
	open here would let junk uploads dodge the daily limits."""
	if isinstance(e, LLMTaskNotAvailableError):
		return False
	return bool(getattr(e, "reached_model", True))


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
	_check_limits(images)
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
		reworded = LLMTaskError(_vision_wording(str(e)))
		reworded.reached_model = reached_model(e)
		raise reworded from e


def _check_limits(images: list[dict]) -> None:
	"""Refuse a request the plugin would refuse, before any bytes leave the site."""
	if not images:
		raise lt.not_sent(LLMTaskError("vision task needs at least one image"))
	if len(images) > limits.MAX_IMAGES:
		raise lt.not_sent(LLMTaskError(f"vision task takes at most {limits.MAX_IMAGES} images"))
	# base64 grows the bytes by 4/3.
	if any(len(i["data_b64"]) > limits.IMAGE_BYTES * 4 // 3 + 4 for i in images):
		raise lt.not_sent(LLMTaskError(f"vision task images must be at most {limits.IMAGE_BYTES} bytes each"))


def _run_vision_task(args: dict, timeout: int) -> dict:
	# Waits longer than the plugin's own 105 s limit, so its 504 reaches the row.
	wait = max(timeout + lt._TIMEOUT_BUFFER_S, limits.SITE_WAIT_S)
	status, raw = lt._call_route(plugin_routes.VISION_TASK_PATH, {"args": args}, wait)

	if status in (401, 403):
		# Refused before the model ran (bad key, a session the plugin will not vouch for):
		# nothing to bill, like a 404.
		raise lt.not_sent(LLMTaskError("vision task request was refused by the agent plugin"))
	# Before parsing: an older plugin answers an unknown route with a non-JSON 404.
	if status == 404:
		raise LLMTaskNotAvailableError(lt._gateway_error_message(status, raw))

	return _parse_reply(status, raw)


def _parse_reply(status: int, raw: bytes) -> dict:
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
