"""Persist codex-generated images into ERP Files + the message canvas.

agent's ``imagegen`` skill writes images onto the container's disk under
``codex-home/generated_images/`` but neither streams them on the WS turn nor
serves them over HTTP, so the bench can't catch them the way it catches canvas
artifacts (see ``jarvis/chat/canvas.py``). Instead, after a turn that used
``imagegen``, the worker pulls any newly-produced images through the fleet agent
(``admin_client.get_generated_media``), saves each as a private Frappe File on
the assistant message, and appends them to the message's ``canvas`` JSON so the
SPA renders them inline (click to zoom) - the same handoff as every other
artifact.
"""

from __future__ import annotations

import base64
import os

import frappe

from jarvis import admin_client

MSG = "Jarvis Chat Message"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Clock-skew buffer: the bench's turn-start epoch vs the container host's file
# mtime can differ slightly in prod (NTP-synced, sub-second). 60s is generous;
# the per-conversation filename dedup below is the real safety net.
_SKEW_BUFFER_MS = 60_000


def _existing_codex_filenames(conversation_id: str) -> set[str]:
	"""Codex source filenames already persisted on this conversation, read from
	the ``source`` key we stamp on each persisted canvas item - so re-running the
	per-turn fetch (the buffer can overlap a fast prior turn) never double-saves.
	"""
	seen: set[str] = set()
	for canvas_json in frappe.get_all(MSG, filters={"conversation": conversation_id}, pluck="canvas"):
		if not canvas_json:
			continue
		try:
			for item in frappe.parse_json(canvas_json) or []:
				src = isinstance(item, dict) and item.get("source")
				if src:
					seen.add(src)
		except Exception:
			continue
	return seen


def _safe_filename(codex_name: str) -> str:
	base, ext = os.path.splitext(codex_name)
	if ext.lower() not in _IMAGE_EXTS:
		ext = ".png"
	return "jarvis-image-" + (base[-10:] or "out") + ext


def persist_generated_images(assistant_msg_name: str, conversation_id: str, turn_start_ms: int) -> list[dict]:
	"""Fetch images produced this turn, save them as private Files on the
	assistant message, append to its ``canvas`` JSON, and return the new canvas
	items (so the caller can publish a ``canvas`` event). Best-effort: returns
	``[]`` and never raises on a fetch/decode failure."""
	from frappe.utils.file_manager import save_file

	try:
		media = admin_client.get_generated_media(since_ms=max(0, int(turn_start_ms) - _SKEW_BUFFER_MS))
	except Exception:
		frappe.log_error(
			title="chat worker: get_generated_media failed",
			message=frappe.get_traceback(),
		)
		return []
	if not media:
		return []

	seen = _existing_codex_filenames(conversation_id)
	new_items: list[dict] = []
	for m in media:
		fn = m.get("filename")
		b64 = m.get("b64")
		if not fn or not b64 or fn in seen:
			continue
		try:
			content = base64.b64decode(b64)
		except Exception:
			continue
		try:
			f = save_file(_safe_filename(fn), content, MSG, assistant_msg_name, is_private=1)
		except Exception:
			frappe.log_error(
				title="chat worker: save generated image failed",
				message=frappe.get_traceback(),
			)
			continue
		new_items.append(
			{
				"name": f.file_url,
				"title": "Generated image",
				"type": "image",
				"file_url": f.file_url,
				"source": fn,  # codex filename - used for dedup on later turns
			}
		)
		seen.add(fn)

	if new_items:
		existing = frappe.db.get_value(MSG, assistant_msg_name, "canvas")
		items = (frappe.parse_json(existing) if existing else []) or []
		items.extend(new_items)
		frappe.db.set_value(MSG, assistant_msg_name, "canvas", frappe.as_json(items))
		frappe.db.commit()
	return new_items
