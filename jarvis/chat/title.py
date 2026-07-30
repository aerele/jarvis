"""Conversation auto-titling.

Instead of using the raw first message as the conversation title, we generate
a concise, summarised title after the first *substantive* turn — the way
ChatGPT and openclaw do it. This mirrors openclaw's own ``generateThreadTitle``
(extensions/discord/src/monitor/thread-title.ts):

  - feed the opening user message (capped at ~600 chars) to the model with a
    tight "give me a 3-6 word title, nothing else" instruction;
  - take the first non-empty line of the reply;
  - strip wrapping quotes / ``**bold**`` / ``__underline__`` / code fences;
  - cap the length.

openclaw runs this through a cheap "simple completion" model. We don't have a
separate completion surface in managed mode (device-paired WS only), so we run
a throwaway agent turn on its own session_key — it never touches the visible
conversation. Best-effort throughout: any failure falls back to a cleaned-up
first message (openclaw's ``deriveSessionTitle`` fallback) and never breaks the
chat turn that triggered it.
"""

from __future__ import annotations

import re

import frappe

from jarvis.chat.events import publish_to_user

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

# openclaw uses DERIVED_TITLE_MAX_LEN = 60; match it.
_TITLE_MAX_LEN = 60
# openclaw caps the title *source* at 600 chars before sending to the model.
_SOURCE_MAX_CHARS = 600

# A turn whose opening message is just a greeting shouldn't define the title —
# wait for the actual prompt. Matched after lowercasing + stripping punctuation
# and a trailing "jarvis"/"there"/"bot".
_GREETINGS = {
	"hi",
	"hello",
	"hey",
	"heya",
	"hiya",
	"yo",
	"sup",
	"howdy",
	"greetings",
	"gm",
	"good morning",
	"good afternoon",
	"good evening",
	"good day",
	"hi there",
	"hello there",
	"hey there",
	"ola",
	"hola",
	"namaste",
	"morning",
	"evening",
	"thanks",
	"thank you",
	"ok",
	"okay",
	"test",
}

# Mirrors openclaw's SYSTEM_PROMPT for thread titles, adapted to a single
# user-message agent turn (our managed gateway runs the persona agent, so we
# spell out "no tools / only the title" explicitly).
_TITLE_PROMPT = (
	"Generate a concise title of 3 to 6 words that summarises what this "
	"conversation is about, based on the user's opening message below.\n"
	"Return ONLY the title text — no surrounding quotes, no markdown, no "
	"trailing punctuation, no preamble, and do not call any tools.\n\n"
	"Opening message:\n{msg}"
)


def _clean(text: str | None) -> str:
	return (text or "").strip()


def _is_greeting(text: str) -> bool:
	"""True when ``text`` is just a greeting (so it shouldn't seed the title)."""
	t = text.lower().strip()
	# Drop trailing punctuation/emoji-ish noise and a trailing addressee.
	t = re.sub(r"[!.?,~\s]+$", "", t)
	t = re.sub(r"\b(jarvis|there|bot|buddy)\b", "", t).strip()
	t = re.sub(r"\s+", " ", t)
	if not t:
		return True
	return t in _GREETINGS


def normalize_title(raw: str | None) -> str:
	"""openclaw-style normalisation: first meaningful line, unwrapped, capped."""
	if not raw:
		return ""
	first_line = ""
	for line in raw.replace("\r", "").split("\n"):
		trimmed = line.strip()
		if not trimmed:
			continue
		if not first_line and trimmed.startswith("```"):
			continue  # skip an opening code fence
		first_line = trimmed
		break
	current, previous = first_line.strip(), None
	while current and current != previous:
		previous = current
		current = re.sub(r'^["\'`]+|["\'`]+$', "", current).strip()
		current = re.sub(r"^\*\*(.+)\*\*$", r"\1", current).strip()
		current = re.sub(r"^__(.+)__$", r"\1", current).strip()
		current = re.sub(r"^#+\s*", "", current).strip()  # leading markdown heading
	current = current.rstrip(".").strip()
	return current[:_TITLE_MAX_LEN]


def derive_title(text: str) -> str:
	"""Deterministic fallback (openclaw ``deriveSessionTitle``): the first line
	of the opening message, unwrapped + capped. Used only when LLM generation
	fails, so the chat never gets stuck on "New chat"."""
	return normalize_title(_clean(text)) or _clean(text)[:_TITLE_MAX_LEN]


def _first_substantive_user_message(conversation_id: str) -> str | None:
	"""The earliest user message that isn't a bare greeting, or None if the
	conversation so far is greetings only (title later, once a real prompt lands)."""
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation_id, "role": "user"},
		fields=["content"],
		order_by="seq asc",
		limit_page_length=6,
	)
	for r in rows:
		c = _clean(r.get("content"))
		# Strip the trailing "📎 name" attachment marker send_message appends.
		c = re.sub(r"\n*📎.*$", "", c).strip()
		if c and not _is_greeting(c):
			return c
	return None


def _generate_via_gateway(gateway_url, source_text, *, model, provider) -> str:
	"""Run a silent throwaway agent turn to summarise the opening message into a
	title. Returns "" on any failure (caller falls back to derive_title)."""
	from jarvis.chat import openclaw_session_pool
	from jarvis.chat.openclaw_client import oneshot_run_id
	from jarvis.chat.session_lifecycle import reclaim_throwaway_session

	prompt = _TITLE_PROMPT.format(msg=source_text[:_SOURCE_MAX_CHARS])
	text = ""
	# openclaw rejects sessions.create with a label that's already in use, so
	# the label MUST be unique per call — a fixed "jarvis-title" works the first
	# time then fails ("label already in use") and silently falls back to the
	# raw message. A random suffix keeps each throwaway title session distinct.
	label = f"jarvis-title-{frappe.generate_hash(length=10)}"
	try:
		with openclaw_session_pool.checkout(gateway_url) as sess:
			skey = sess.create_session(label=label)
			try:
				# Titling deliberately pins a cheap model instead of the pool
				# primary, which costs this run its failover chain (#531). The
				# prefixed run id is what keeps openclaw's resulting
				# ``next=none`` from reading like a dead chain.
				for ev in sess.stream_agent_turn(
					skey,
					prompt,
					oneshot_run_id("title", skey, model=model, provider=provider),
					model=model,
					provider=provider,
				):
					if ev.get("kind") == "assistant" and ev.get("text"):
						text = ev["text"]
			finally:
				# Reclaim the throwaway on the SAME pooled connection, turn
				# succeeded or not. Without this every auto-titled chat leaks a
				# session that only the budget-capped orphan sweep could reclaim,
				# and that sweep could never keep up.
				#
				# NOT a bare sessions.delete (issue #525): reaching this line does
				# NOT mean the run is over. stream_agent_turn returns on the
				# lifecycle-end frame while openclaw is still finalising the
				# session file, and RAISES on every error path with the run still
				# going server side - so deleting here renamed the session file
				# out from under a live run, which openclaw answers by either
				# re-creating the file (a fresh orphan) or killing the run with
				# EmbeddedAttemptSessionTakeoverError. reclaim_throwaway_session
				# waits for the gateway to stop reporting an active run, and
				# leaves anything still busy to the orphan sweep.
				#
				# Failure is swallowed in there: `text` is already captured, and
				# losing the title over failed cleanup would be the worse bug -
				# the sweep still collects jarvis-title-* as a backstop.
				reclaim_throwaway_session(sess, skey, logger_name="jarvis.chat.title")
	except Exception:
		frappe.log_error(
			title="auto-title: gateway generation failed",
			message=frappe.get_traceback(),
		)
		return ""
	return normalize_title(text)


def enqueue_autotitle(conversation_id: str, user: str) -> None:
	"""Defer title generation to the SHORT queue (2026-07 latency plan,
	Phase 1.2) so the long-queue chat worker is freed as soon as the turn
	ends instead of running a 2-8s title LLM turn inline. Cheap still-unnamed
	gate here so already-titled conversations never spawn a pointless job.
	"""
	title = frappe.db.get_value(CONV, conversation_id, "title")
	if title and title != "New chat":
		return  # user renamed it, or we already titled it
	frappe.enqueue(
		"jarvis.chat.title.autotitle_job",
		queue="short",
		conversation_id=conversation_id,
		user=user,
	)


def autotitle_job(conversation_id: str, user: str) -> None:
	"""Short-queue job body for the deferred auto-title. Re-resolves
	settings / gateway / model itself (nothing heavyweight is serialized
	through the queue). Best-effort like the old inline path: any failure is
	logged, never affects the finished turn.
	"""
	try:
		from jarvis.chat.turn_handler import _resolve_model_and_provider

		settings = frappe.get_single("Jarvis Settings")
		gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
		if not gateway_url:
			return
		if not frappe.db.exists(CONV, conversation_id):
			return  # deleted between enqueue and run — benign race, not an error
		conv = frappe.get_doc(CONV, conversation_id)
		model, provider = _resolve_model_and_provider(conv)
		maybe_autotitle(
			conversation_id,
			user,
			gateway_url=gateway_url,
			model=model,
			provider=provider,
		)
	except Exception:
		frappe.log_error(
			title="auto-title job failed",
			message=frappe.get_traceback(),
		)


def maybe_autotitle(conversation_id: str, user: str, *, gateway_url, model, provider) -> None:
	"""Generate + set a concise title for a still-unnamed conversation.

	No-op when the conversation already has a title (renamed by the user or
	already auto-titled) or when nothing but greetings has been sent yet.
	Publishes a ``conversation:renamed`` event so the sidebar updates live.
	"""
	title = frappe.db.get_value(CONV, conversation_id, "title")
	if title and title != "New chat":
		return  # user renamed it, or we already titled it

	source = _first_substantive_user_message(conversation_id)
	if not source:
		return  # greetings only so far — title on the next real prompt

	new_title = _generate_via_gateway(
		gateway_url,
		source,
		model=model,
		provider=provider,
	) or derive_title(source)
	if not new_title or new_title == title:
		return

	frappe.db.set_value(CONV, conversation_id, "title", new_title)
	frappe.db.commit()
	publish_to_user(
		user,
		{
			"kind": "conversation:renamed",
			"conversation_id": conversation_id,
			"title": new_title,
		},
	)
