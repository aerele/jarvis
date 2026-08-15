"""Path B dispatch helper: publish a chat-send event onto Redis pub/sub.

The default chat path (Node socketio backend, the standard Frappe Cloud setup)
keeps using ``frappe.enqueue(...)`` to hand a turn off to an RQ worker; this
helper is the alternate dispatch used when the bench has been switched to the
Python socketio backend (``socketio_backend: "python"`` in
``common_site_config.json``). In that mode an in-process subscriber inside
Frappe's existing Python realtime process picks up the published payload and
runs ``handle_chat_send`` directly via gevent (see
``jarvis.realtime.handlers``).

The two paths live side by side: ``jarvis.chat.api.send_message`` and
``retry_message`` branch on the same config value to choose between them.
This module is only loaded along the Path B branch, so on Node-backend
benches it has no operational effect.

Channel name is site-scoped: ``jarvis:chat:send:<site>``. Site scoping
lets the realtime process (which serves all sites on the bench) keep one
subscription per active site without cross-site bleed.
"""

from __future__ import annotations

import json

import frappe


def _channel(site: str) -> str:
	"""Single source of truth for the pub/sub channel name. Used by the
	publisher here and by the subscriber in ``jarvis.realtime.handlers``;
	any change must land in both call sites."""
	return f"jarvis:chat:send:{site}"


def subscriber_count() -> int:
	"""Live subscriber count on this site's Path B channel (PUBSUB NUMSUB).

	Zero means a publish would be fire-and-forget into nowhere: either the
	config/process mismatch (``socketio_backend: python`` while the Node
	server is the one running - e.g. Frappe Cloud, which pins node in its
	supervisor template) or the Python realtime process is down. The
	dispatcher uses this to fall back to the RQ path instead of stranding
	the turn."""
	res = frappe.cache().pubsub_numsub(_channel(frappe.local.site))
	# redis returns a list of (channel, count) pairs.
	return int(res[0][1]) if res else 0


def publish_chat_send(payload: dict) -> None:
	"""Publish a chat turn onto the site-scoped Path B channel.

	``payload`` mirrors the kwargs ``jarvis.chat.worker.run_agent_turn``
	accepts (conversation_id, message_id, run_id, attachments?, context?);
	the subscriber rehydrates it and calls ``handle_chat_send`` with the
	same dict, so the two dispatch paths produce identical turn behaviour.
	"""
	# frappe.cache() IS a redis.Redis subclass (RedisWrapper) - publish
	# directly. (The previously-assumed .get_redis_connection() accessor
	# does not exist on this frappe; caught by the Stage A live smoke.)
	frappe.cache().publish(_channel(frappe.local.site), json.dumps(payload))
