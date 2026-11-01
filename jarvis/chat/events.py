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


def parse_event(
	payload: dict[str, Any], *, redact: bool = True, collaboration: bool = False
) -> dict[str, Any] | None:
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
		# The run's raw reply as the runtime settled it, on the ``end`` frame. Kept RAW (never
		# redacted, never shown): it is read only to detect ``MEDIA:`` paths. openclaw strips a
		# ``MEDIA:`` line from the streamed text AND from the chat final (verified on 2026.9.3 and
		# 2026.10.6), so the final-text detection never sees it; this is the only frame that still
		# carries the line.
		reply = data.get("terminalReply")
		if data.get("phase") == "end" and isinstance(reply, dict) and isinstance(reply.get("text"), str):
			out["terminal_reply_raw"] = reply["text"]
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
		if data.get("phase") != "start":
			return None
		out = {"kind": "tool_boundary"}
		if collaboration:
			out.update(tool_name=data.get("name"), tool_call_id=data.get("toolCallId"))
		return out

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
	conversation = payload.get("conversation_id") or payload.get("conversation")
	if conversation:
		from jarvis.chat.collaboration_realtime import is_shared, notify

		if is_shared(conversation):
			notify(user, conversation, payload.get("kind"))
			return
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


def accumulate_assistant_text(previous: str, event: dict) -> str:
	"""Fold one parsed ``assistant`` event into the reply text so far.

	Both stream shapes are handled: openclaw 2026.9.x sends a cumulative ``text`` on every frame
	(plus ``delta``), while 2026.10.x sends ``text`` only on the first frame (and on ``replace``
	frames) and the rest as ``delta`` with ``text=""``. Reading only ``text`` therefore kept the
	FIRST CHUNK of every one-shot reply on 10.x (one-word auto-titles, suggestions never stored,
	found 2026-10-22 on the openclaw compat stack). A non-empty ``text`` is authoritative; an empty
	one means "append the delta"."""
	text = event.get("text") or ""
	if text:
		return text
	return previous + (event.get("delta") or "")
