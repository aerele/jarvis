"""Agent event parsing + realtime publish wrapper.

agent emits WebSocket events with shapes like:
  stream=lifecycle  data={phase: start|end|error, ...}
  stream=item       data={kind: tool, phase: start|end, name, toolCallId, status}
  stream=item       data={kind: preamble, phase, itemId, progressText}  (step update)
  stream=tool       data={phase: start|update|result, name, toolCallId}  (tool-events cap)
  stream=assistant  data={text: <cumulative>, delta: <incremental>}

This module normalizes those into a flat dict the worker can act on, and
provides a thin wrapper around frappe.publish_realtime so the channel name
("jarvis:event") lives in one place.
"""

from __future__ import annotations

from typing import Any

import frappe

from jarvis.chat import egress_rules

CHANNEL = "jarvis:event"


def parse_event(payload: dict[str, Any], *, redact: bool = True) -> dict[str, Any] | None:
	"""Normalize an agent WS frame to a flat dict, or return None to drop it.

	``redact=False`` skips every ``egress_rules.redact`` call and returns the RAW
	text instead: the relay mux's reader thread (``relay_mux.py``) has no
	``frappe.local`` site, so it parses with ``redact=False`` and the pump applies
	``egress_rules`` itself, on its own thread, in ``RelayMux._apply`` /
	``_finalize_terminal``, right before the text reaches a lane callback. The
	direct (non-mux) relay path keeps the default ``True`` - it already runs with
	a live Frappe connection, so redacting here, once, at parse time, is correct
	and unchanged."""
	stream = payload.get("stream")
	data = payload.get("data")
	if not isinstance(data, dict):
		data = {}

	def _redact(text: str) -> str:
		return egress_rules.redact(text) if redact else text

	if stream == "lifecycle":
		out: dict[str, Any] = {"kind": "lifecycle", "phase": data.get("phase")}
		if data.get("error"):
			out["error"] = _redact(data["error"])
		return out

	if stream == "item":
		if data.get("kind") == "preamble":
			# The ChatGPT-subscription harness sends the model's "what I'm doing"
			# updates as their own item, separate from the reply text. It becomes
			# the chat's live step line (see jarvis.chat.steps).
			text = _redact(str(data.get("progressText") or "")).strip()
			return {"kind": "step", "text": text} if text else None
		if data.get("kind") != "tool":
			return None
		out = {
			"kind": "tool",
			"phase": data.get("phase"),
			"tool_name": data.get("name"),
			"tool_call_id": data.get("toolCallId"),
		}
		if data.get("status"):
			out["status"] = data["status"]
		# agent's item events carry a human title it derives itself from
		# the tool name + an arg summary (buildToolItemTitle ->
		# inferToolMetaFromArgs, e.g. "get_list Sales Invoice"). Pass it
		# through so the chat's live status line can say WHAT is being
		# fetched without the bench parsing raw args.
		if data.get("title"):
			out["tool_title"] = _redact(data["title"])
		return out

	if stream == "tool":
		# Only reaches us because the connect advertises "tool-events". Tool
		# rows still come from the item stream; a start here only marks where
		# a tool call began (the Claude-subscription runtime sends no item
		# frames), so the relay can find step text before it.
		return {"kind": "tool_boundary"} if data.get("phase") == "start" else None

	if stream == "assistant":
		# Redact the live stream (both transports funnel through here). Silent — no
		# tripwire per frame; the once-per-turn tripwire fires at the final-text and
		# recovery extractors. `text` is cumulative, so a brand token split across
		# deltas is contiguous here and matches.
		return {
			"kind": "assistant",
			"text": _redact(data.get("text", "")),
			"delta": _redact(data.get("delta", "")),
		}

	if stream == "compaction":
		# The runtime brackets an automatic (threshold or overflow) compaction
		# with this stream. Mapped so the chat can show "reorganising" while the
		# run is still alive; nothing else changes on the terminal path.
		phase = str(data.get("phase") or "")
		return {
			"kind": "compaction",
			"phase": "start" if phase in ("start", "before") else "end",
			"completed": bool(data.get("completed")),
		}

	return None


def publish_to_user(user: str, payload: dict[str, Any]) -> None:
	"""Broadcast a payload to a single user's socketio channel."""
	message = payload
	# Terminal events drive global unread/toast routing after the originating
	# builder may have been hidden or switched to another thread. Attach the
	# durable dashboard namespace once per terminal (not per streaming token), so
	# those background completions can never fall back to a main-chat /c/:id URL.
	if (
		payload.get("kind") in {"run:end", "run:error"}
		and payload.get("conversation_id")
		and "origin_page" not in payload
	):
		try:
			origin = (
				frappe.db.get_value("Jarvis Conversation", payload["conversation_id"], "origin_page") or ""
			)
			if origin == "dashboards":
				message = {**payload, "origin_page": origin}
		except Exception:
			# Realtime publication is best-effort and must not become a second
			# failure path if the conversation was concurrently removed.
			pass
	frappe.publish_realtime(CHANNEL, message, user=user)
