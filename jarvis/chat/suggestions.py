"""Dynamic new-chat prompt suggestions, synthesised from the user's own chat titles.

The empty chat screen used to show four hard-coded starter cards, which said the
same thing to a brand-new workspace and to someone six months in. These are
generated from what the person actually works on.

Shape of the feature, and why each part is the way it is:

  - **Titles only.** The model sees conversation TITLES, never message bodies.
    Titles are already short summaries (jarvis.chat.title writes them), so they
    are both the cheapest and the least sensitive thing that still carries what
    the user does.
  - **One cheap oneshot turn**, on the same throwaway-session path auto-titling
    uses (``jarvis.chat.title._generate_via_gateway``): managed mode has no
    separate completion surface, so a silent agent turn on its own session key is
    the cheap call. ~25 titles in, three lines out.
  - **Never on the request path.** The endpoint returns the cached value and
    enqueues a refresh; the page renders instantly whether or not a job runs.
  - **Refresh is gated on ACTIVITY, not just a clock** (``needs_refresh``): an
    idle user costs nothing at all, and an active one is refreshed at most once
    every ``REFRESH_AFTER_DAYS``. A bare timer would have been the worst of both.

Best-effort throughout: every failure path leaves the previous suggestions in
place and returns quietly. An empty-state decoration must never break the chat.
"""

from __future__ import annotations

import json
import time

import frappe

from jarvis.chat.title import _clean, _is_greeting

CONV = "Jarvis Conversation"
MSG_DT = "Jarvis Chat Message"
USETT = "Jarvis User Settings"

#: How far back to look for material. Older work is not what the user is doing now.
LOOKBACK_DAYS = 30
#: Most titles ever sent to the model (newest first) - the token bound.
MAX_TITLES = 25
#: A conversation with fewer USER/ASSISTANT messages than this is a one-off
#: ("Simple Addition Answer"), not a piece of work worth suggesting more of.
#: Tool rows are excluded from the count on purpose - they outnumber prose in a
#: real conversation, so counting them let a single question that happened to
#: call two tools pass as a substantive thread.
MIN_MESSAGES = 3
#: Floor between two syntheses for one user. With the activity gate below, this
#: caps an active user at ~10 cheap calls a month and an idle one at zero.
REFRESH_AFTER_DAYS = 3
#: How many suggestions to ask for (and to keep). Four fills the two-column card
#: grid exactly, which is why it is not three.
WANT = 4
#: Bounds on what we store / hand back, so a chatty model cannot bloat the row.
MAX_SUGGESTION_CHARS = 90
MAX_TITLE_CHARS = 32

_PROMPT = (
	"Below are the titles of recent chats a user had with an ERP assistant.\n"
	"Suggest exactly {n} things they would plausibly want to do next.\n"
	"Format each as:  Label | prompt\n"
	"  Label  = 2 or 3 words naming the area, e.g. Timesheet report\n"
	"  prompt = a natural first-person request under 10 words\n"
	"Base them on the themes in these titles. One per line. No numbering, no "
	"quotes, no markdown, no preamble; do not call any tools.\n\n"
	"Recent chat titles:\n{titles}"
)


# --------------------------------------------------------------------------- #
# Material: which titles are worth feeding the model
# --------------------------------------------------------------------------- #
def eligible_titles(user: str, *, limit: int = MAX_TITLES) -> list[str]:
	"""The user's recent, substantive, de-duplicated chat titles (newest first).

	Filters out exactly the noise the feature must not learn from: still-unnamed
	chats, greeting chats, one-off questions, and the same title repeated (a
	sidebar full of "System Behaviour Overview" should count once, not five
	times, or it drowns out everything else in the prompt).
	"""
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -LOOKBACK_DAYS)
	rows = frappe.db.sql(
		f"""
		SELECT c.title,
		       (SELECT COUNT(*) FROM `tab{MSG_DT}` m
		         WHERE m.conversation = c.name AND m.role IN ('user', 'assistant'))
		           AS message_count
		FROM `tab{CONV}` c
		WHERE c.owner = %s AND c.status = 'Active' AND c.last_active_at >= %s
		ORDER BY c.last_active_at DESC
		""",
		(user, cutoff),
		as_dict=True,
	)
	out: list[str] = []
	seen: set[str] = set()
	for r in rows:
		title = _clean(r.get("title"))
		if not _is_usable_title(title):
			continue
		if int(r.get("message_count") or 0) < MIN_MESSAGES:
			continue
		key = title.lower()
		if key in seen:
			continue
		seen.add(key)
		out.append(title)
		if len(out) >= limit:
			break
	return out


def _is_usable_title(title: str) -> bool:
	"""A title worth learning from: named, not a greeting, not trivially short."""
	if not title or title == "New chat":
		return False
	if _is_greeting(title):
		return False
	# A one-word title carries no theme ("Greeting", "Test").
	return len(title.split()) >= 2


# --------------------------------------------------------------------------- #
# Refresh policy
# --------------------------------------------------------------------------- #
def needs_refresh(user: str) -> bool:
	"""Is it worth spending a model call on this user right now?

	True only when the cache is stale AND the user has actually chatted since it
	was written. The activity half is what makes an idle workspace free: without
	it, a plain timer would re-synthesise the same titles forever.
	"""
	stamp = frappe.db.get_value(USETT, {"user": user}, "prompt_suggestions_at")
	if not stamp:
		return True  # never attempted
	# The STAMP alone gates, never the stored value: an attempt that produced
	# nothing (no eligible titles yet, or a gateway blip) still stamps, so a user
	# with no history cannot spawn a job on every single page load.
	stamped = frappe.utils.get_datetime(stamp)
	age_days = frappe.utils.time_diff(frappe.utils.now_datetime(), stamped).total_seconds() / 86400
	if age_days < REFRESH_AFTER_DAYS:
		return False
	return _has_chatted_since(user, stamped)


def _has_chatted_since(user: str, stamped) -> bool:
	return bool(frappe.db.exists(CONV, {"owner": user, "status": "Active", "last_active_at": (">", stamped)}))


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def enqueue_refresh(user: str) -> None:
	"""Queue a synthesis on the SHORT lane (same lane auto-titling uses) so the
	caller's request returns immediately. Gate first so a hot empty screen cannot
	spawn a job per page load."""
	if not needs_refresh(user):
		return
	frappe.enqueue("jarvis.chat.suggestions.refresh_job", queue="short", user=user)


def refresh_job(user: str) -> None:
	"""Short-queue body. Re-resolves everything itself; never raises."""
	try:
		if not needs_refresh(user):
			return  # another job won the race
		titles = eligible_titles(user)
		if not titles:
			# Nothing to learn from yet. STAMP anyway so the gate backs off for a
			# few days: without this, a workspace with no named history would queue
			# a job every time its empty chat screen opened.
			touch(user)
			return
		settings = frappe.get_single("Jarvis Settings")
		gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
		if not gateway_url:
			touch(user)
			return
		model, provider = _resolve_model_provider(user)
		lines = _generate_via_gateway(gateway_url, titles, model=model, provider=provider)
		if lines:
			store(user, lines)
		else:
			# Generation failed or parsed to nothing. Keep whatever the user already
			# had (a transient gateway blip must not blank a good strip) but stamp,
			# so a broken gateway is retried in days rather than hammered.
			touch(user)
	except Exception:
		frappe.log_error(title="prompt suggestions: refresh failed", message=frappe.get_traceback())


def _resolve_model_provider(user: str):
	"""Reuse the turn handler's resolution against the user's newest conversation.
	There is always one here: eligible_titles() already proved it."""
	from jarvis.chat.turn_handler import _resolve_model_and_provider

	name = frappe.db.get_value(
		CONV, {"owner": user, "status": "Active"}, "name", order_by="last_active_at desc"
	)
	return _resolve_model_and_provider(frappe.get_doc(CONV, name))


def _generate_via_gateway(gateway_url, titles: list[str], *, model, provider) -> list[str]:
	"""One silent throwaway turn -> up to WANT suggestion lines. [] on any failure.

	Mirrors jarvis.chat.title._generate_via_gateway, including the unique session
	label and the reclaim-on-the-same-connection cleanup - see that function for
	why the throwaway session must be reclaimed rather than deleted outright.
	"""
	from jarvis.chat import agent_session_pool
	from jarvis.chat.agent_client import oneshot_run_id
	from jarvis.chat.session_lifecycle import reclaim_throwaway_session

	prompt = _PROMPT.format(n=WANT, titles="\n".join(titles))
	text = ""
	label = f"jarvis-suggest-{frappe.generate_hash(length=10)}"
	try:
		with agent_session_pool.checkout(gateway_url) as sess:
			skey = sess.create_session(label=label)
			fired_at = time.time()
			run_ended = False
			try:
				for ev in sess.stream_agent_turn(
					skey,
					prompt,
					oneshot_run_id("suggest", skey, model=model, provider=provider),
					model=model,
					provider=provider,
				):
					if ev.get("kind") == "assistant" and ev.get("text"):
						text = ev["text"]
				run_ended = True
			finally:
				reclaim_throwaway_session(
					sess,
					skey,
					logger_name="jarvis.chat.suggestions",
					fired_at=None if run_ended else fired_at,
				)
	except Exception:
		frappe.log_error(
			title="prompt suggestions: gateway generation failed",
			message=frappe.get_traceback(),
		)
		return []
	return parse_lines(text)


def parse_lines(text: str | None) -> list[dict]:
	"""Model reply -> ``[{"title", "prompt"}]``.

	Tolerates the shapes a model reaches for anyway (numbering, bullets, quotes)
	rather than trusting the instruction, and tolerates a MISSING label: a line
	with no ``|`` becomes a prompt with an empty title, which the card renders as
	a single line rather than dropping a usable suggestion.
	"""
	out: list[dict] = []
	for raw in (text or "").splitlines():
		line = _strip_ornament(_clean(raw))
		if not line:
			continue
		title, _, prompt = line.partition("|")
		if not prompt:
			title, prompt = "", line
		title = _strip_ornament(title)[:MAX_TITLE_CHARS]
		prompt = _strip_ornament(prompt)[:MAX_SUGGESTION_CHARS]
		# A one-word "prompt" is a stray heading, not something to send.
		if len(prompt.split()) < 2:
			continue
		out.append({"title": title, "prompt": prompt})
		if len(out) >= WANT:
			break
	return out


def _strip_ornament(s: str) -> str:
	"""Drop bullets, "1." / "1)" numbering and wrapping quotes."""
	line = (s or "").strip().lstrip("-*•").strip()
	while line[:1].isdigit():
		rest = line.lstrip("0123456789")
		if rest[:1] not in (".", ")"):
			break  # a genuine leading number ("2026 revenue"), not numbering
		line = rest[1:].strip()
	return line.strip().strip('"').strip("'").strip()


def touch(user: str) -> None:
	"""Record an ATTEMPT without changing the suggestions. This is what makes the
	refresh gate back off after a run that had nothing to say."""
	from jarvis.chat import usage

	doc = usage.get_or_create_user_settings(user)
	doc.db_set("prompt_suggestions_at", frappe.utils.now_datetime(), update_modified=False)
	frappe.db.commit()


def store(user: str, items: list[dict]) -> None:
	"""Persist the cache + its stamp. db_set-style writes so the settings row's
	modified time (and the gate revision that keys off it) does not churn.
	Accepts plain strings too, so a caller (or a test) can pass either shape."""
	from jarvis.chat import usage

	rows = [{"title": "", "prompt": i} if isinstance(i, str) else i for i in items]
	doc = usage.get_or_create_user_settings(user)
	doc.db_set("prompt_suggestions", json.dumps(rows[:WANT]), update_modified=False)
	doc.db_set("prompt_suggestions_at", frappe.utils.now_datetime(), update_modified=False)
	frappe.db.commit()


def read(user: str) -> list[dict]:
	"""Cached suggestions as ``[{"title", "prompt"}]``, or [] - never raises.

	Normalises on the way out so a cache written by the earlier plain-string
	version still renders (as a prompt with no label) instead of blanking the
	strip until the next refresh.
	"""
	try:
		parsed = json.loads(frappe.db.get_value(USETT, {"user": user}, "prompt_suggestions") or "[]")
		if not isinstance(parsed, list):
			return []
		out: list[dict] = []
		for item in parsed:
			if isinstance(item, str):
				item = {"title": "", "prompt": item}
			if not isinstance(item, dict):
				continue
			prompt = str(item.get("prompt") or "")[:MAX_SUGGESTION_CHARS]
			if not prompt:
				continue
			out.append({"title": str(item.get("title") or "")[:MAX_TITLE_CHARS], "prompt": prompt})
		return out[:WANT]
	except Exception:
		return []
