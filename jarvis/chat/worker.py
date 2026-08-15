"""RQ job: stream one agent turn from agent to DB + realtime.

Today this is the default chat path: ``jarvis.chat.api.send_message``
enqueues, RQ workers pick up, this function runs. The actual turn
logic lives in ``jarvis.chat.turn_handler.handle_chat_send`` so future
non-RQ executors (the Phase 2 realtime-process subscriber gated on
``socketio_backend == 'python'``) can call the same code without
duplicating it. See
``docs/superpowers/specs/2026-06-24-chat-bridge-architecture-design.md``
for the bigger picture.

Helpers historically defined here (``_AssistantContentBatcher``,
``_handle_event``, ``_resolve_model_and_provider``, etc.) are
re-exported below so existing tests that import them via this module
keep working unchanged. ``publish_to_user`` is re-exported by name so
``mock.patch("jarvis.chat.worker.publish_to_user")`` continues to
patch the turn-handler's outbound publishes (the handler dereferences
through the worker module at call time). New code should import from
``jarvis.chat.turn_handler`` directly.
"""

from __future__ import annotations

# Re-exported patch target. test_chat_worker.py and any other test
# suite that pre-existed the turn_handler split still calls
# ``patch("jarvis.chat.worker.publish_to_user")``; the handler in
# turn_handler.py looks the symbol up on this module at call time, so
# rebinding it here is sufficient to redirect every outbound publish.
from jarvis.chat.events import publish_to_user

# Re-export everything the legacy worker module used to expose. Tests
# (test_chat_worker.py, test_vision_attachments.py) import these by name
# from jarvis.chat.worker and must keep working.
from jarvis.chat.turn_handler import (
	_ASSISTANT_BATCH_INTERVAL_MS,
	_ASSISTANT_BATCH_SIZE,
	_IMAGE_EXT,
	_MAX_INLINE_CHARS,
	_PROVIDER_LABEL_TO_AGENT_ID,
	POOL_VIRTUAL_MODEL,
	_AssistantContentBatcher,
	_create_assistant_placeholder,
	_handle_event,
	_handle_event_inner,
	_mark_errored,
	_prepare_attachments,
	_prepend_doc_context,
	_resolve_model_and_provider,
	_session_model_for,
	_to_managed_attachments,
	_vision_enabled,
	handle_chat_send,
)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

__all__ = [
	"CONV",
	"MSG",
	"POOL_VIRTUAL_MODEL",
	"_ASSISTANT_BATCH_INTERVAL_MS",
	"_ASSISTANT_BATCH_SIZE",
	"_IMAGE_EXT",
	"_MAX_INLINE_CHARS",
	"_PROVIDER_LABEL_TO_AGENT_ID",
	"_AssistantContentBatcher",
	"_create_assistant_placeholder",
	"_handle_event",
	"_handle_event_inner",
	"_mark_errored",
	"_prepare_attachments",
	"_prepend_doc_context",
	"_resolve_model_and_provider",
	"_session_model_for",
	"_to_managed_attachments",
	"_vision_enabled",
	"handle_chat_send",
	"publish_to_user",
	"run_agent_turn",
]


def run_agent_turn(
	conversation_id: str,
	message_id: str,
	run_id: str,
	attachments=None,
	context=None,
	enqueued_at_ms=None,
) -> None:
	"""RQ entry point. Thin shim around ``handle_chat_send``.

	Kept verbatim as the RQ contract so
	``jarvis.chat.api.send_message`` /
	``jarvis.chat.api.retry_message``'s
	``frappe.enqueue("jarvis.chat.worker.run_agent_turn", ...)`` calls
	continue to work unchanged. All real work happens in
	``jarvis.chat.turn_handler.handle_chat_send``.

	``enqueued_at_ms`` (optional): epoch ms stamped by the enqueue site so
	the handler can log queue_wait_ms (latency plan, Phase 0). Deploys that
	add this kwarg must restart workers (RQ workers don't hot-reload).
	"""
	handle_chat_send(
		{
			"conversation_id": conversation_id,
			"message_id": message_id,
			"run_id": run_id,
			"attachments": attachments,
			"context": context,
			"enqueued_at_ms": enqueued_at_ms,
		}
	)
