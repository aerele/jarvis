"""Frappe realtime handlers for jarvis - the Path B chat subscriber.

Frappe's Python realtime process (frappe/realtime/server.py) discovers and
imports ``<app>/realtime/handlers.py`` from every installed app at startup
via ``discover_app_handlers()``. The realtime process is the **only** Frappe
process that imports this module; gunicorn / RQ workers / the scheduler
never reach this code.

What this module does on import:

- If the bench has ``socketio_backend: "python"`` set in
  ``common_site_config.json``, spawn a long-lived gevent greenlet that
  subscribes to ``jarvis:chat:send:<site>`` on Redis pub/sub and dispatches
  each message to ``jarvis.chat.turn_handler.handle_chat_send`` in its own
  greenlet (so many concurrent turns share the one realtime process). The
  outer greenlet wraps the subscribe loop in a watchdog: any uncaught
  exception inside the loop is logged and the loop is respawned after a
  short backoff so a single bad turn does not silently disable chat for
  the whole bench.

- If the bench is on the default Node socketio backend (or any value other
  than ``"python"``), this module is still imported but the subscriber is
  **never spawned**. The Path B code path is dormant. Chat continues to go
  through ``jarvis.chat.api.send_message -> frappe.enqueue ->
  jarvis.chat.worker.run_agent_turn`` exactly as before. There is no
  feature-flag toggle inside jarvis to flip; the ``socketio_backend``
  config value is the single source of truth.

Context discipline (Stage A findings, 2026-07-03): the realtime process
imports this module with NO site context, and gevent greenlets get a fresh
``frappe.local`` - so nothing here may assume ``frappe.conf`` /
``frappe.cache()`` / ``frappe.log_error`` are usable outside an explicit
context.

- Import/loop-level plumbing reads ``common_site_config.json`` directly and
  talks to Redis through a raw client; failures log to the module logger
  (the realtime process's stdout), never crash the server (a raised import
  error would take down ALL realtime, Desk included - the registry
  surfaces import errors loudly by design).
- Each dispatched turn runs inside frappe's own
  ``frappe.realtime.context.frappe_context(site, user)``: init -> forced
  PyMySQL driver (mysqlclient's C socket would stall the gevent hub;
  frappe enforces this per-context) -> connect -> commit/rollback ->
  destroy. Turns run as Administrator, mirroring the RQ worker's
  background-job semantics; ``handle_chat_send`` derives the actual chat
  owner from the conversation row.

The matching publisher lives at ``jarvis.chat.dispatch.publish_chat_send``;
both sides use the same site-scoped channel format (pinned by a test
against dispatch._channel).
"""

from __future__ import annotations

import json
import logging
import os
import time

import frappe

_logger = logging.getLogger("jarvis.realtime")

_SUBSCRIBER_SPAWNED = False

# How long the watchdog sleeps before restarting the subscribe loop after an
# unhandled exception. Short enough that chat recovers on the next turn, long
# enough that a hard-failure tight loop does not spam logs.
_WATCHDOG_BACKOFF_S = 5

# Site-scoped channel format. Must match jarvis.chat.dispatch._channel.
_CHANNEL_TEMPLATE = "jarvis:chat:send:{site}"


def _read_common_config() -> dict:
	"""common_site_config.json, read directly off disk.

	The realtime server chdirs into ``sites/`` before importing handlers,
	so the bare filename is the normal hit; the ``sites/`` variant covers
	being imported from the bench root (tests, tooling). Never raises -
	config-file problems must not take down the realtime process."""
	for path in ("common_site_config.json", os.path.join("sites", "common_site_config.json")):
		try:
			with open(path) as fh:
				data = json.load(fh)
			if isinstance(data, dict):
				return data
		except Exception:
			continue
	return {}


def _conf_get(key: str) -> str:
	"""A config value from frappe.conf when a context exists (tests, request
	code), else from common_site_config.json (the realtime process's
	context-free import/loop phases)."""
	try:
		val = frappe.conf.get(key)
		if val:
			return str(val)
	except Exception:
		pass
	return str(_read_common_config().get(key) or "")


def _python_backend_active() -> bool:
	"""True iff the bench has opted into the Python socketio backend."""
	return _conf_get("socketio_backend").strip().lower() == "python"


def _site_name() -> str:
	"""Site for channel scoping: the open context's site when there is one,
	else ``default_site`` from common config. Empty string when neither
	resolves - the caller decides how loudly to fail (an empty channel
	name would silently drop every turn)."""
	site = getattr(frappe.local, "site", None)
	if site:
		return site
	return _conf_get("default_site")


def _redis_url() -> str:
	"""The cache Redis URL - the same instance frappe.cache() (and therefore
	the publisher in jarvis.chat.dispatch) uses."""
	return _conf_get("redis_cache") or "redis://127.0.0.1:13000"


def _turn_context(site: str):
	"""One turn's Frappe context: frappe's own realtime context manager
	(init -> forced PyMySQL -> connect -> set_user -> commit/rollback ->
	destroy). Indirection point so tests can substitute a null context."""
	from frappe.realtime.context import frappe_context

	return frappe_context(site, "Administrator")


def _run_one(raw: bytes | str, site: str) -> None:
	"""Decode one pub/sub message and run it through the shared turn
	handler inside a full per-turn site context. All handler/payload
	failures are logged (frappe.log_error - we have a context by then) and
	swallowed so one bad turn cannot take down the subscribe loop;
	context-bootstrap failures fall back to the module logger."""
	try:
		with _turn_context(site):
			# Late import: keeps the heavy chat-turn module out of realtime
			# startup when the Python backend is not active.
			from jarvis.chat.turn_handler import handle_chat_send

			try:
				body = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
				payload = json.loads(body)
				if not isinstance(payload, dict):
					raise ValueError(f"expected dict payload, got {type(payload).__name__}")
				handle_chat_send(payload)
			except Exception:
				frappe.log_error(
					title="jarvis chat subscriber: handler failed",
					message=frappe.get_traceback(),
				)
	except Exception:
		_logger.exception("jarvis chat subscriber: turn context failed for site %s", site)


def _subscribe_loop_once(site: str) -> None:
	"""Open one Redis pub/sub subscription and dispatch every message
	until the connection drops. Context-free by design: raw Redis client
	(frappe.cache() needs a site context this process does not have), and
	each message gets its own greenlet + its own site context inside
	_run_one. The watchdog restarts this on failure; this function itself
	never retries."""
	import gevent
	import redis

	channel = _CHANNEL_TEMPLATE.format(site=site)
	conn = redis.Redis.from_url(_redis_url())
	pubsub = conn.pubsub()
	pubsub.subscribe(channel)
	_logger.info("jarvis chat subscriber: listening on %s", channel)
	for message in pubsub.listen():
		if message.get("type") != "message":
			continue
		gevent.spawn(_run_one, message.get("data"), site)


def _watchdog_loop(site: str) -> None:
	"""Forever-loop wrapper: run the subscribe loop, and on any unhandled
	exception log it and restart after a short backoff. The realtime
	process supervisor will only restart THIS file's import, not the
	module-level subscribe greenlet, so a crashed loop without this
	watchdog would silently disable chat until the next bench restart."""
	while True:
		try:
			_subscribe_loop_once(site)
		except Exception:
			_logger.exception("jarvis chat subscriber: subscribe loop crashed")
		# Backoff before reconnect. Use time.sleep (gevent monkey-patches it)
		# rather than gevent.sleep so this module stays import-time safe
		# under both backends; the realtime process has monkey.patch_all()
		# called before discover_app_handlers, so time.sleep yields the hub.
		time.sleep(_WATCHDOG_BACKOFF_S)


def maybe_start_chat_subscriber() -> bool:
	"""Spawn the chat-send subscriber if the Python socketio backend is
	active. Idempotent (a second call is a no-op). Returns True iff a new
	greenlet was spawned by this call (mostly useful for tests; production
	code calls it for the side effect).

	Resilient by design: a python-backend bench that cannot resolve its
	site logs an ERROR and skips the spawn (chat dispatch would be broken,
	visibly, in the realtime log) instead of raising - a raise here would
	crash the whole realtime server, Desk realtime included."""
	global _SUBSCRIBER_SPAWNED
	if _SUBSCRIBER_SPAWNED:
		return False
	if not _python_backend_active():
		return False
	site = _site_name()
	if not site:
		_logger.error(
			"jarvis chat subscriber: socketio_backend=python but no site "
			"resolvable (set default_site in common_site_config.json); "
			"chat dispatch will NOT be consumed"
		)
		return False
	import gevent

	gevent.spawn(_watchdog_loop, site)
	_SUBSCRIBER_SPAWNED = True
	_logger.info(
		"jarvis chat subscriber: watchdog greenlet spawned for %s (socketio_backend=python)",
		site,
	)
	return True


# Fire on module import. Frappe's realtime process imports this module via
# discover_app_handlers() during boot, after gevent.monkey.patch_all() has
# already run, so spawning a greenlet here is safe. Never let a bootstrap
# problem escape - the registry propagates import errors, which would take
# down ALL realtime for the bench, not just chat.
try:
	maybe_start_chat_subscriber()
except Exception:
	_logger.exception("jarvis chat subscriber: startup failed")
