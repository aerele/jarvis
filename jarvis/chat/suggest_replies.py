"""Suggested replies: the user's likely next messages, offered as chips under a finished reply.

Modelled on Frappe Flow's suggested replies (frappe/frappe, flow/harness/src/flow_harness/
session/suggest_replies.py and SuggestedReplies.vue): after a turn ends well, ONE extra model
call asks "what might the user ask next?" and gets back one to three short follow-ups as JSON;
the chat shows them under the latest reply until the user says something, and a tap sends one.

How this differs from Flow, and why:

  - **A throwaway session, not the chat's own.** Flow re-runs the chat's transcript with one
    more user message, reusing its prompt cache. Jarvis's agent keeps the chat's session in
    the OpenClaw container, and a turn on it would land in the agent's history, so this runs
    the same kind of silent one-shot turn auto-titling and the new-chat suggestions use
    (``jarvis.chat.title._generate_via_gateway``), on its own session, with the end of the
    conversation (a bounded excerpt of the visible user / assistant text) in the prompt.
  - **Skipped where a one-shot is costly or redundant**: on the Claude plan a one-shot starts
    its own claude-cli process (``one_shot_runs_on_claude_plan``, as titles skip it); a reply
    that asks its own question (a ``jarvis-ask`` card) already offers the answers; a parked
    confirmation card is the next step; a shared chat is not the owner's alone.
  - **Off the request path**: a finalize effect enqueues ``job`` on the short queue, so the
    turn ends at once and the chips land a few seconds later over realtime
    (``message:replies``). A reply the user has already answered gets nothing.

Stored on the reply row (``Jarvis Chat Message.suggested_replies``) so a reload shows them.
Best-effort throughout: any failure leaves the reply without chips and never touches the turn.
"""

from __future__ import annotations

import json
import re
import time

import frappe

from jarvis.chat.events import publish_to_user

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
SETTING = "suggest_replies_enabled"
EVENT = "message:replies"

#: One to three, as Flow: only the ones the user would really pick.
MIN_REPLIES, MAX_REPLIES = 1, 3
#: A chip is a message the user sends as is: short enough to read at a glance.
MAX_REPLY_CHARS = 200
#: The end of the conversation the model sees: the newest messages, newest last.
EXCERPT_MESSAGES = 6
EXCERPT_USER_CHARS = 800
EXCERPT_REPLY_CHARS = 3000
EXCERPT_TOTAL_CHARS = 9000

PROMPT = (
	"Below is the end of a chat between a user and Jarvis, an assistant inside their ERP "
	"(ERPNext). What might the user ask Jarvis next?\n"
	"Reply with only a JSON object and call no tools: "
	'{{"replies": [one to three short follow-ups the user would send in this chat], '
	'"multi_select": false}}.\n'
	"Write each in the user's words and language, as a request to Jarvis, under 15 words. "
	"Each is a next step they would likely want or, when Jarvis's last reply asked them "
	"something, a likely answer. Give only the ones they would really pick, even if that is "
	'just one, and never filler like "No, that\'s it" or "Thanks". Set multi_select to true '
	'when they may pick several together. Reply {{"replies": []}} when no next step is clear.'
	"\n\nThe chat:\n{transcript}"
)

_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
# reasoning some hosts leave in the text, often with braces of its own
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_ASK_FENCE = "```jarvis-ask"


# --------------------------------------------------------------------------- #
# Parsing (strict: a bad answer is no chips, never a wrong chip)
# --------------------------------------------------------------------------- #
def parse_replies(text: str | None) -> dict | None:
	"""``{"replies": [...], "multi_select": bool}`` from the model's answer, or None."""
	match = _OBJECT_RE.search(_THINK_RE.sub("", text or ""))
	try:
		data = json.loads(match.group()) if match else None
	except ValueError:
		return None
	if not isinstance(data, dict) or not isinstance(data.get("replies"), list):
		return None
	replies = [" ".join(reply.split()) for reply in data["replies"] if isinstance(reply, str)]
	if (
		not MIN_REPLIES <= len(replies) <= MAX_REPLIES
		or "" in replies
		or len(set(replies)) < len(replies)
		or any(len(reply) > MAX_REPLY_CHARS for reply in replies)
	):
		return None
	return {"replies": replies, "multi_select": data.get("multi_select") is True}


# --------------------------------------------------------------------------- #
# Material
# --------------------------------------------------------------------------- #
def _visible_rows(conversation: str, limit: int) -> list[dict]:
	"""The newest user / assistant rows the chat shows, newest LAST."""
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": ["in", ["user", "assistant"]], "hidden": 0},
		fields=["name", "role", "content", "error", "stopped", "streaming"],
		order_by="seq desc",
		limit_page_length=limit,
	)
	return list(reversed(rows))


def transcript(conversation: str) -> str:
	"""The end of the conversation as "User: ... / Jarvis: ..." lines, bounded."""
	parts = []
	for row in _visible_rows(conversation, EXCERPT_MESSAGES):
		text = " ".join((row.get("content") or "").split())
		if not text:
			continue
		if row["role"] == "user":
			parts.append("User: " + text[:EXCERPT_USER_CHARS])
		else:
			parts.append("Jarvis: " + text[:EXCERPT_REPLY_CHARS])
	out = "\n\n".join(parts)
	return out[-EXCERPT_TOTAL_CHARS:]


def enabled() -> bool:
	"""The Jarvis Settings switch. On by default: an existing site that never saved its
	settings since the field arrived has no stored value, which reads as on."""
	# Read the raw Singles row: get_single_value casts a missing Check to 0.
	try:
		rows = frappe.db.sql(
			"select `value` from `tabSingles` where `doctype` = %s and `field` = %s limit 1",
			("Jarvis Settings", SETTING),
		)
	except Exception:
		return False
	return True if not rows or rows[0][0] is None else bool(frappe.utils.cint(rows[0][0]))


def skip_reason(conversation: str, message: str, owner: str | None) -> str | None:
	"""Why ``message`` gets no suggestions, or None. Re-checked by the job just before it
	stores, since the user may have moved on while the model answered."""
	if not enabled():
		return "disabled"
	from jarvis.chat.collaboration import is_collaborative

	if is_collaborative(conversation):
		return "shared"
	latest = _visible_rows(conversation, 1)
	if not latest or latest[-1]["name"] != message:
		return "not_latest"
	row = latest[-1]
	if row["role"] != "assistant" or row.get("error") or row.get("stopped") or row.get("streaming"):
		return "not_a_finished_reply"
	content = row.get("content") or ""
	if not content.strip():
		return "empty"
	if _ASK_FENCE in content:
		return "asks_its_own_question"
	if owner:
		from jarvis.chat import pending_confirm

		if pending_confirm.list_items_for_owner(owner, conversation):
			return "pending_card"
	return None


# --------------------------------------------------------------------------- #
# The finalize effect and its job
# --------------------------------------------------------------------------- #
def enqueue(conversation: str, message: str | None, owner: str | None) -> None:
	"""Finalize effect body: queue the job when this reply may get suggestions. Cheap gates
	only (no model call on the finalize path)."""
	if not message or not owner or not enabled():
		return
	frappe.enqueue(
		"jarvis.chat.suggest_replies.job",
		queue="short",
		conversation=conversation,
		message=message,
		owner=owner,
		enqueue_after_commit=True,
	)


def job(conversation: str, message: str, owner: str) -> None:
	"""Short-queue job: one silent one-shot turn -> store on the reply -> publish. Never raises."""
	try:
		if skip_reason(conversation, message, owner):
			return
		from jarvis.chat.turn_handler import _resolve_model_and_provider, one_shot_runs_on_claude_plan

		settings = frappe.get_single("Jarvis Settings")
		gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
		if not gateway_url:
			return
		model, provider = _resolve_model_and_provider(frappe.get_doc(CONV, conversation))
		if one_shot_runs_on_claude_plan(settings, model, provider):
			return
		text = transcript(conversation)
		if not text:
			return
		suggested = parse_replies(_generate_via_gateway(gateway_url, text, model=model, provider=provider))
		if not suggested:
			return
		# The model took seconds: the chat may have moved on. Read the present, then store.
		from jarvis.chat import txn

		txn.fresh_snapshot()
		if skip_reason(conversation, message, owner):
			return
		frappe.db.set_value(MSG, message, "suggested_replies", json.dumps(suggested), update_modified=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist before publish
		publish_to_user(
			owner,
			{"kind": EVENT, "conversation_id": conversation, "message_id": message, **suggested},
		)
	except Exception:
		frappe.log_error(title="suggested replies job failed", message=frappe.get_traceback())


def _generate_via_gateway(gateway_url, source: str, *, model, provider) -> str:
	"""One silent throwaway turn -> the model's raw answer, "" on any failure.

	Mirrors jarvis.chat.title._generate_via_gateway (unique session label, reclaim on the
	same pooled connection) - see there for why the throwaway is reclaimed, not deleted."""
	from jarvis.chat import agent_session_pool
	from jarvis.chat.agent_client import oneshot_run_id
	from jarvis.chat.session_lifecycle import reclaim_throwaway_session
	from jarvis.chat.title import _auth_fault_detail, _is_auth_fault, _log_pinned_lane_auth_fault

	prompt = PROMPT.format(transcript=source)
	text = ""
	lifecycle_error: str | None = None
	label = f"jarvis-replies-{frappe.generate_hash(length=10)}"
	try:
		with agent_session_pool.checkout(gateway_url) as sess:
			skey = sess.create_session(label=label)
			fired_at = time.time()
			run_ended = False
			try:
				for ev in sess.stream_agent_turn(
					skey,
					prompt,
					oneshot_run_id("replies", skey, model=model, provider=provider),
					model=model,
					provider=provider,
				):
					if ev.get("kind") == "assistant" and ev.get("text"):
						text = ev["text"]
					elif ev.get("kind") == "lifecycle" and ev.get("phase") == "error" and ev.get("error"):
						lifecycle_error = str(ev["error"])
				run_ended = True
			finally:
				reclaim_throwaway_session(
					sess,
					skey,
					logger_name="jarvis.chat.suggest_replies",
					fired_at=None if run_ended else fired_at,
				)
	except Exception as e:
		detail = _auth_fault_detail(e)
		if detail:
			_log_pinned_lane_auth_fault("replies", detail, model=model, provider=provider)
		frappe.log_error(title="suggested replies: gateway generation failed", message=frappe.get_traceback())
		return ""
	if not text and _is_auth_fault(lifecycle_error):
		_log_pinned_lane_auth_fault("replies", lifecycle_error, model=model, provider=provider)
	return text
