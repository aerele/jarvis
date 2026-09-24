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
import re

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


def _append_canvas_items(assistant_msg_name: str, new_items: list[dict]) -> None:
	"""Append canvas items to a message's ``canvas`` JSON in one read-modify-write.
	Shared by the codex-image and native-media seeders.

	NOTE: the read-append-write is not atomic (accepted pre-existing pattern); both
	callers dedup by ``source`` first, so a finalize-effect retry re-appends nothing.
	MUST run after ``canvas.persist_canvases`` (which overwrites the field wholesale),
	which the worker guarantees by block order."""
	existing = frappe.db.get_value(MSG, assistant_msg_name, "canvas")
	items = (frappe.parse_json(existing) if existing else []) or []
	items.extend(new_items)
	frappe.db.set_value(MSG, assistant_msg_name, "canvas", frappe.as_json(items))
	frappe.db.commit()


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
		_append_canvas_items(assistant_msg_name, new_items)
	return new_items


# --------------------------------------------------------------------------- #
# Native media (``MEDIA:`` marker) consumer.
#
# agent's NATIVE image tool (distinct from the codex ``imagegen`` skill above)
# writes to the agent media store and emits a protocol marker
# ``MEDIA:<abs path>`` on its own line when reply delivery falls back to
# ``automatic`` mode. Unlike codex images, this file IS served by the container
# gateway at ``/__openclaw__/assistant-media?source=<path>`` (the same gateway
# that serves canvas artifacts), so we fetch it directly like ``canvas.py`` —
# no fleet-agent host-pull. The marker is detected + stripped from the reply
# BEFORE egress redaction (so the raw container path never leaks and the file
# is delivered as an inline image). See the Q3 plan.
# --------------------------------------------------------------------------- #

# A ``MEDIA:`` directive = a line that, after leading whitespace, starts with
# ``MEDIA:`` (the agent runtime's contract: ``line.trimStart().toUpperCase().startsWith``).
# ``\s*`` (not ``[ \t]*``) also catches NBSP / ideographic-space indentation like
# the agent runtime's ``trimStart``. The payload is everything after ``MEDIA:``
# (surrounding backticks trimmed). NOT fence-aware: an unbalanced/unterminated code
# fence earlier in the reply must never let a real trailing marker survive — D2
# already favours leak-safety over preserving a fenced example, so every such line
# is consumed.
_MEDIA_PREFIX = re.compile(r"^\s*MEDIA:\s*(.*?)\s*$", re.IGNORECASE)
# Couples to the container HOME (fleet-agent compose). If the agent config dir is
# ever renamed (Q2), update this literal AND the control-plane ``/home/node`` egress
# rule together.
_MEDIA_ROOT = "/home/node/.openclaw/media/"
_MAX_MEDIA_PER_TURN = 8  # mirror canvas._MAX_CANVAS_PER_TURN (bound a hostile many-marker reply)
_MAX_MEDIA_BYTES = 8 * 1024 * 1024  # bound worker memory; the source is LLM-influenced


def _media_lines(text: str):
	"""Yield ``(line_index, payload)`` for each line that is a ``MEDIA:`` directive.
	``payload`` is the text after ``MEDIA:`` with surrounding backticks stripped —
	used by ``detect_media_paths``; ``strip_media_lines`` removes the whole line
	regardless of payload. Shared by both so they can never disagree. Total: a
	non-string input yields nothing (keeps ``redact_final_with_media`` non-raising).

	NOTE: the agent runtime emits exactly one marker per line for delivery, so a
	degenerate two-markers-on-one-line reply collapses into one payload — it is still
	stripped (no leak); at most its (bogus) path fails to fetch."""
	if not isinstance(text, str):
		return
	for i, line in enumerate(text.splitlines()):
		m = _MEDIA_PREFIX.match(line)
		if m:
			yield i, m.group(1).strip().strip("`").strip()


def _valid_media_path(path: str) -> bool:
	"""Root-confined, non-traversal, allowed-image-extension check shared by the
	``MEDIA:`` marker and the embedded-path detectors below."""
	if not path.startswith(_MEDIA_ROOT):
		return False
	rel = path[len(_MEDIA_ROOT) :]
	# ``..`` can't escape the media root (the gateway also realpath-confines),
	# but reject it here so we never even send a traversal path.
	if not rel or rel.startswith("/") or ".." in rel.split("/"):
		return False
	return path.lower().endswith(tuple(_IMAGE_EXTS))


def detect_media_paths(text: str) -> list[str]:
	"""Full absolute container paths for a qualifying (root-confined,
	non-traversal, image-extension) media reference, capped per turn:
	line-anchored ``MEDIA:`` markers FIRST, then embedded-path lines (see
	``_embedded_media_lines`` - the agent runtime's image/video/music tools'
	deferred reply, e.g. ``Attachment: <path>``).

	The gateway resolves the full path against its own media roots, so we pass
	the whole path (not a relpath). External URLs, non-image files, and traversal
	attempts are excluded here — a ``MEDIA:`` line is still stripped from the
	reply regardless (strip-but-don't-fetch, ``strip_media_lines``); an embedded
	one is left untouched (see ``_embedded_media_lines``)."""
	out: list[str] = []
	for _, path in _media_lines(text):
		if _valid_media_path(path):
			out.append(path)
			if len(out) >= _MAX_MEDIA_PER_TURN:
				return out
	for _, path in _embedded_media_lines(text):
		out.append(path)
		if len(out) >= _MAX_MEDIA_PER_TURN:
			break
	return out


def strip_media_lines(text: str) -> str:
	"""Remove EVERY line-anchored ``MEDIA:`` line from the visible reply — handled,
	external, malformed, even a ``MEDIA:``-prefixed prose/fenced line — PLUS any
	line carrying a qualifying embedded media path (see ``_embedded_media_lines``).

	Deliberately broader than the agent runtime for ``MEDIA:`` (which keeps a
	``MEDIA:``-prefixed line whose payload is not a path, and preserves fenced
	examples): we favour leak-safety over that fidelity (Q3 decision D2). Being
	fence-UNAWARE is load-bearing — an unbalanced fence must never leave a real
	trailing marker. An embedded-path line is the OPPOSITE: only stripped when its
	path actually qualifies — it is ordinary prose otherwise (e.g. a path outside
	the media root, a traversal attempt, or a non-image extension), never leak-
	safety-first, since it is not a dedicated protocol marker."""
	if not text:
		return text
	drop = {i for i, _ in _media_lines(text)}
	drop |= {i for i, _ in _embedded_media_lines(text)}
	if not drop:
		return text  # nothing to strip -> return verbatim (no line-ending normalisation)
	kept = [ln for i, ln in enumerate(text.splitlines()) if i not in drop]
	return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# --------------------------------------------------------------------------- #
# Embedded-path media detection (image/video/music tool deferred replies).
#
# The agent runtime's image/video/music generation tools detach into a
# background task and, ~25-30s later, restart the session with a model-authored reply that
# NAMES the generated file's path in free text - "Attachment: <path>",
# `path="<path>"`, or a markdown image `![alt](<path>)` - not a ``MEDIA:``
# marker line (that is a distinct protocol the runtime uses for its own
# "automatic" delivery fallback). The wording is model-authored and unstable;
# the absolute path under the agent media root is the only stable anchor.
# --------------------------------------------------------------------------- #

# A path token: the media root followed by everything up to the first
# whitespace / quote / paren / angle-bracket / comma / semicolon - the usual
# terminators of "Attachment: <path>", 'path="<path>"' and markdown
# "![alt](<path>)". Matched on ANY line, not just a dedicated marker line.
_EMBEDDED_MEDIA_PATH = re.compile(re.escape(_MEDIA_ROOT) + r"[^\s\"'()<>,;]+")


def _embedded_media_lines(text: str):
	"""Yield ``(line_index, path)`` for lines carrying a QUALIFYING embedded
	media path outside a ``MEDIA:`` marker. A line whose only candidate path is
	NOT qualifying (outside the root, a traversal attempt, a non-image
	extension) yields nothing for that line - it is ordinary prose and stays
	completely untouched, never partially stripped. A line already claimed by a
	``MEDIA:`` marker is skipped (that marker already owns the line)."""
	if not isinstance(text, str):
		return
	marker_lines = {i for i, _ in _media_lines(text)}
	for i, line in enumerate(text.splitlines()):
		if i in marker_lines:
			continue
		for m in _EMBEDDED_MEDIA_PATH.finditer(line):
			path = m.group(0)
			if _valid_media_path(path):
				yield i, path
				break  # one image per line is all the tool ever emits


def has_media_marker(text: str) -> bool:
	"""True if the reply has ANY line-anchored ``MEDIA:`` directive — qualifying
	(fetchable image) or not (``.pdf`` / external / traversal). MEDIA:-only —
	callers use this as a leak-safety gate for the dedicated protocol marker;
	see ``has_embedded_media_path`` for the separate, narrower embedded-path
	signal. Broader than ``detect_media_paths``: used to force the stored-
	content overwrite so that no marker survives in stored content even when
	stripping it leaves the reply empty."""
	return next(_media_lines(text), None) is not None


def has_embedded_media_path(text: str) -> bool:
	"""True if the reply has any line ``strip_media_lines`` will remove for a
	QUALIFYING embedded media path (outside a ``MEDIA:`` marker - see
	``_embedded_media_lines``). Kept separate from ``has_media_marker``: an
	embedded path is ordinary prose unless it fully qualifies, never a leak-
	safety-first signal, so a caller must opt in explicitly rather than getting
	it folded into the marker gate."""
	return next(_embedded_media_lines(text), None) is not None


def fetch_media(agent_url: str, token: str, source: str) -> bytes | None:
	"""GET one media file from the container gateway (binary-safe,
	redirect-disabled, size-bounded). Returns the raw bytes, or ``None`` on any
	failure / oversize.

	The host is ALWAYS the tenant's own gateway (``agent_url``); the LLM marker
	supplies only the ``source`` path (URL-encoded so it cannot break out of the
	query). ``allow_redirects=False`` because the route only ever returns 200 or
	4xx/5xx — following a 3xx would let a compromised gateway pivot the bench at
	an internal URL (SSRF), bypassing the ``call_tool`` permission layer."""
	from urllib.parse import quote

	import requests

	from jarvis.chat.canvas import _http_base

	base = _http_base(agent_url)
	if not base or not token:
		return None
	url = f"{base}/__openclaw__/assistant-media?source={quote(source, safe='')}"
	try:
		# 15s (tighter than canvas's 20s): seed_media shares the ~180s finalize hop
		# with the canvas + imagegen fetches, so up to 8 media fetches must not
		# dominate that budget. A healthy same-infra gateway responds sub-second.
		with requests.get(
			url,
			headers={"Authorization": f"Bearer {token}"},
			timeout=15,
			stream=True,
			allow_redirects=False,
		) as r:
			if r.status_code != 200:
				return None
			buf = bytearray()
			for chunk in r.iter_content(64 * 1024):
				buf += chunk
				if len(buf) > _MAX_MEDIA_BYTES:
					return None  # oversize -> drop (the marker line is already stripped)
	except Exception:
		return None
	return bytes(buf) or None


def _pretty_media_title(basename: str) -> str:
	"""Human title from a media filename: drop the extension and a trailing
	``---<uuid>`` the tool appends, turn separators into spaces. Length-capped so a
	hostile long filename can't produce an unbounded canvas-item title."""
	stem = re.sub(r"\.\w+$", "", basename)
	stem = re.sub(r"-{2,}[0-9a-fA-F-]{8,}$", "", stem)
	stem = stem.replace("-", " ").replace("_", " ").strip()
	return (stem.title() or basename)[:80]


def _safe_media_filename(basename: str) -> str:
	"""Bounded, sanitised File name from the marker basename (keep a known image
	extension, else default to ``.png``)."""
	base, ext = os.path.splitext(basename)
	if ext.lower() not in _IMAGE_EXTS:
		ext = ".png"
	base = re.sub(r"[^\w.\-]", "", base)[-40:] or "image"
	return "jarvis-media-" + base + ext


def _media_dedup_key(source: str) -> str:
	"""The canvas-item ``source`` / dedup key for a media path: the path RELATIVE to
	the media root (e.g. ``tool-image-generation/<name>.png``), NEVER the full
	``/home/node/.openclaw/...`` absolute path. The canvas item ships to the client
	(live ``canvas`` event + raw on history reload) even though the frontend never
	renders ``source``, so storing the absolute path would re-leak the runtime brand
	+ container layout the rest of this change strips out. The relative form is
	unique per image and brand/path-free."""
	return source[len(_MEDIA_ROOT) :] if source.startswith(_MEDIA_ROOT) else source


def _existing_media_sources(assistant_msg_name: str) -> set[str]:
	"""Relative ``source`` keys already on THIS message's canvas, so a re-run (e.g. a
	pump effect retry) never double-seeds the same media file."""
	seen: set[str] = set()
	canvas_json = frappe.db.get_value(MSG, assistant_msg_name, "canvas")
	if not canvas_json:
		return seen
	try:
		for item in frappe.parse_json(canvas_json) or []:
			if isinstance(item, dict) and item.get("source"):
				seen.add(item["source"])
	except Exception:
		pass
	return seen


def seed_media(assistant_msg_name: str, agent_url: str, token: str, sources: list[str]) -> list[dict]:
	"""Fetch each media ``source`` (a full container path) from the gateway, save it
	as a private File on the assistant message, append to its ``canvas`` JSON, and
	return the new canvas items (so the caller can publish a ``canvas`` event).

	Best-effort: a failed / oversize fetch is skipped (the raw marker line is already
	stripped upstream, so nothing leaks even when the image is not delivered). The
	stored ``source`` / dedup key is the RELATIVE path (see ``_media_dedup_key``),
	keeping a re-run idempotent without shipping the absolute container path.

	MUST run AFTER ``canvas.persist_canvases`` — that overwrites the ``canvas`` field
	wholesale, whereas this appends; the worker calls the blocks in that order."""
	from frappe.utils.file_manager import save_file

	if not sources:
		return []
	seen = _existing_media_sources(assistant_msg_name)
	new_items: list[dict] = []
	failures = 0
	for source in sources:
		key = _media_dedup_key(source)
		if key in seen:
			continue
		content = fetch_media(agent_url, token, source)  # fetch with the FULL path
		if not content:
			failures += 1
			continue
		basename = source.rsplit("/", 1)[-1]
		try:
			f = save_file(_safe_media_filename(basename), content, MSG, assistant_msg_name, is_private=1)
		except Exception:
			frappe.log_error(
				title="chat worker: save native media failed",
				message=frappe.get_traceback(),
			)
			continue
		new_items.append(
			{
				"name": f.file_url,
				"title": _pretty_media_title(basename),
				"type": "image",
				"file_url": f.file_url,
				"source": key,  # RELATIVE path - dedup key, not brand/path-bearing
			}
		)
		seen.add(key)

	if failures:
		# ONE aggregated signal per turn (not per file) so a silent fleet-wide
		# degradation — e.g. a _MEDIA_ROOT / container-path drift where every marker
		# is detected but every fetch 404s — surfaces in the Error Log instead of
		# relying on a user complaint. Best-effort; never raises.
		frappe.log_error(
			title="chat worker: native media fetch returned no content",
			message=f"{failures} of {len(sources)} media fetch(es) empty for {assistant_msg_name}",
		)
	if new_items:
		_append_canvas_items(assistant_msg_name, new_items)
	return new_items
