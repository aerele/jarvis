"""WP-1d — the short prepare job (Relay Pump).

The RQ prepare job (D1 Stage 1, owners #16–#26) that runs BEFORE the pump
dispatches a turn: it drives ``queued -> preparing -> ready`` and hands the pump a
fully-assembled prompt + a live openclaw session via the Turn's
``dispatch_payload``. Enqueued by the pump at promote (``dispatch_prepare`` seam),
deduped on ``jarvis-prepare::<run_id>`` — a still-``queued`` reserved turn may be
re-offered each slice, and the idempotent ``claim_preparing`` CAS + the dedupe
make that a no-op.

Ruled owners folded in here (WP-D / D1, RULINGS-PA):
  * #16 ``chat_asks.resolve_on_user_message`` at user-message intake (best-effort);
  * #17 assistant placeholder insert (R-1) — seq allocated UNDER the conversation
    lock (canonical rank 2), then linked onto the Turn;
  * #19/#20/#21 prompt assembly + agent-notes drain + attachments — REUSED verbatim
    from ``turn_handler.assemble_prompt`` (no duplication), run under
    ``impersonate(chat_user)`` so the File-read permission moat is the SENDER's, not
    the prepare worker's;
  * #22 session bootstrap via a SHORT-LIVED pool checkout (R-9) with the
    session-row-committed-BEFORE-first-callback ordering (jarvis/api.py:55);
  * #23 model patch; #24 watermark capture.
  * ``run:start`` is NOT published here — it is the PUMP's fenced publish at
    dispatch (R-1). ``agent_notes.clear`` is NOT done here — it fires on the pump's
    observed ack (R-2); prepare only stashes the drained ids for the pump.

On success: ``preparing -> ready`` then ``ensure_pump`` + wake so the pump picks it
up. On an ``OpenclawUnreachableError`` bootstrap failure: ``preparing -> errored``
(release credit) + a fenced ``run:error`` (no retry — matches legacy). Never
blocks the pump (it is its own RQ job).
"""

from __future__ import annotations

import json
import time

import frappe

from jarvis._session import impersonate
from jarvis.chat import turn_state as ts
from jarvis.exceptions import OpenclawUnreachableError

TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"


def run_prepare(run_id: str, relay_target_id: str | None = None) -> dict:
	"""Prepare one reserved ``queued`` turn for dispatch. Idempotent + re-offer-safe;
	returns a small status dict. Never raises out (a prepare crash must not kill the
	pump hop; the deadline watchdog reclaims a stuck ``preparing`` turn)."""
	turn = frappe.db.get_value(
		TURN,
		run_id,
		["state", "version", "conversation", "seed_message"],
		as_dict=True,
	)
	if not turn:
		return {"ok": False, "reason": "no turn"}
	if turn["state"] != "queued":
		# Already claimed (this prepare was re-offered) or advanced/cancelled.
		return {"ok": True, "skipped": turn["state"]}

	conversation = turn["conversation"]
	relay_target_id = relay_target_id or frappe.db.get_value(TURN, run_id, "relay_target_id")

	# (D2 #2) queued -> preparing FIRST: NULLs the reservation expiry so a slow
	# prepare (22s/4-page PDF vision) is never reclaimed mid-flight (OAR-5). Requires
	# the held credit (reserved=1, granted at promote). A lost CAS = another prepare
	# won / the turn moved; no-op.
	if not ts.claim_preparing(run_id, int(turn["version"])):
		return {"ok": True, "skipped": "claim_lost"}
	frappe.db.commit()
	version = int(turn["version"]) + 1

	conv = frappe.get_doc(CONV, conversation)
	owner = conv.owner
	chat_user = frappe.db.get_value(MSG, turn["seed_message"], "owner") or owner

	# (#16) user-message intake: answer any Pending chat-sourced approval. Best-effort.
	try:
		from jarvis.chat import chat_asks

		chat_asks.resolve_on_user_message(conversation)
	except Exception:
		frappe.log_error(title="prepare.chat_asks_resolve", message=frappe.get_traceback())

	# (#17) assistant placeholder — seq UNDER the conversation lock (R-1), then link
	# via an ACTOR-fenced CAS (CDX-7): the attach proves this prepare still holds the
	# preparing claim (state='preparing' AND version=V). A prepare that paused past a
	# watchdog reclaim / cancel loses the CAS and must NOT attach a streaming
	# placeholder to a re-queued/cancelled row — it cleans its own orphan + aborts.
	assistant_msg = _create_placeholder_locked(conversation)
	if not ts.attach_placeholder(run_id, version, assistant_msg):
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "attach_lost"}
	frappe.db.commit()

	turn_start_ms = int(time.time() * 1000)

	# (#19/#20/#21) prompt assembly REUSED from the legacy path (byte-identical),
	# under impersonate(chat_user) so File-read permission checks are the sender's.
	from jarvis.chat import turn_handler

	context = _load_context(run_id)
	attachments = _load_attachments(run_id)
	try:
		with impersonate(chat_user):
			ap = turn_handler.assemble_prompt(
				conv,
				message_id=turn["seed_message"],
				conversation_id=conversation,
				context=context,
				attachments=attachments,
				user=owner,
			)
	except Exception:
		# Assembly is all best-effort reads; a hard failure here is unexpected. Error
		# the turn (release credit) rather than dispatch a broken prompt.
		frappe.log_error(title="prepare.assemble", message=frappe.get_traceback())
		_prepare_error(run_id, version, assistant_msg, conversation, owner, "Could not prepare the message.")
		return {"ok": False, "reason": "assemble_failed"}

	# (#22/#23/#24) session bootstrap + model patch + watermark on a SHORT-LIVED
	# pooled connection (R-9). Managed only (the pump is managed; §10.9).
	settings = ap.settings
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		# Defensive: pump mode is managed-only; a self-host turn must never reach the
		# pump. Error rather than mis-dispatch.
		_prepare_error(
			run_id, version, assistant_msg, conversation, owner, "Self-host turns do not use the pump."
		)
		return {"ok": False, "reason": "self_host"}

	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	effective_model, oauth_provider_id = turn_handler._session_model_for(conv)
	managed_attachments = turn_handler._to_managed_attachments(ap.vision_parts) if ap.vision_parts else None

	if not conv.session_key:
		# Cold-start UX: tell the user we're waking the assistant (matches legacy).
		try:
			ts.publish_fenced(
				owner,
				"run:status",
				conversation_id=conversation,
				run_id=run_id,
				message_id=assistant_msg,
				status="waking",
			)
		except Exception:
			pass

	from jarvis.chat import openclaw_session_pool

	try:
		with openclaw_session_pool.checkout(gateway_url) as sess:
			session_key = conv.session_key
			if not session_key:
				# (#22) create the session on THIS pooled connection and persist the
				# Jarvis Chat Session row + CONV.session_key BEFORE the first tool
				# callback can fire (the plugin sessionKey->user moat, jarvis/api.py:55).
				from jarvis.chat.api import _ensure_session_key

				session_key = _ensure_session_key(chat_user, sess=sess)
				frappe.db.set_value(CONV, conversation, "session_key", session_key)
				frappe.db.commit()
			# (#23) model patch (stateful — openclaw remembers across turns).
			if effective_model:
				model_ref = f"{oauth_provider_id}/{effective_model}" if oauth_provider_id else effective_model
				try:
					sess.set_session_model(session_key, model_ref)
				except OpenclawUnreachableError:
					raise
				except Exception:
					frappe.log_error(title="prepare.model_patch", message=frappe.get_traceback())
			# (#24) watermark BEFORE the send: recovery must never stamp a prior
			# turn's answer onto this row. Best-effort.
			try:
				wm_msgs = sess.get_session_messages(session_key, limit=5)
				watermark = max(
					(((m or {}).get("__openclaw") or {}).get("seq", 0) for m in wm_msgs),
					default=0,
				)
				if watermark:
					frappe.db.set_value(
						MSG, assistant_msg, "openclaw_seq_watermark", watermark, update_modified=False
					)
					frappe.db.commit()
			except Exception:
				frappe.log_error(title="prepare.watermark", message=frappe.get_traceback())
	except OpenclawUnreachableError as exc:
		# Pre-dispatch unreachable = a real, retriable error (the run never started).
		_prepare_error(run_id, version, assistant_msg, conversation, owner, str(exc), exc=exc)
		return {"ok": False, "reason": "unreachable"}

	# The prepare->pump handoff contract (WP-1c): the pump reads session_key +
	# message (+ thinking/attachments) from dispatch_payload; drained_note_ids let
	# the pump clear agent-notes on its observed ack (R-2); chat_user + turn_start_ms
	# feed finalize (wiki nudge / generated-image scoping).
	payload = {
		"session_key": conv.session_key or session_key,
		"message": ap.user_message,
		"thinking": (conv.thinking_override or "").strip() or None,
		"attachments": managed_attachments,
		"drained_note_ids": ap.drained_ids,
		"chat_user": chat_user,
		"turn_start_ms": turn_start_ms,
		"context": context,
		"attachments_raw": attachments,
	}
	# (CDX-7) store the handoff payload through an ACTOR-fenced CAS: a prepare that
	# lost the preparing claim discards its payload (never stamping dispatch state onto
	# a recovered/re-queued/cancelled row) + cleans its orphan placeholder + aborts.
	if not ts.store_dispatch_payload(run_id, version, json.dumps(payload)):
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "payload_lost"}
	frappe.db.commit()

	# (D2 #3) preparing -> ready.
	if not ts.mark_ready(run_id, version):
		# Lost (a watchdog reclaimed a slow prepare, or a cancel raced). The stale
		# state wins; do not dispatch — clean the orphan placeholder this prepare made.
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "ready_lost"}
	frappe.db.commit()

	# Wake the pump to dispatch this ready turn (PRIMARY path; watchdog backstops).
	from jarvis.chat import pump

	pump.ensure_pump(relay_target_id)
	pump.lpush_wake(relay_target_id, run_id)
	return {"ok": True, "ready": True}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _create_placeholder_locked(conversation: str) -> str:
	"""Insert the streaming assistant placeholder with its seq allocated UNDER the
	conversation FOR UPDATE lock (R-1 / canonical rank 2), so it never collides with
	a concurrent out-of-band tool receipt on the same conversation. Commit-first so
	the lock is the first statement (REPEATABLE-READ discipline)."""
	frappe.db.commit()
	ts._lock_conversation(conversation)
	try:
		seq = (
			frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conversation})[
				0
			][0]
			or 0
		) + 1
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": "assistant",
				"content": "",
				"streaming": 1,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name
	finally:
		ts.reset_lock_tracking()


def _discard_orphan_placeholder(assistant_msg: str | None) -> None:
	"""CDX-7: a prepare that LOST its actor claim (a watchdog reclaim / cancel bumped
	the version between the claim and the attach/payload/ready CAS) must not leave the
	streaming placeholder it just created dangling — nobody references it (run:start is
	pump-owned at dispatch and never fired), so DELETE it. Best-effort; a delete
	failure at worst leaves a hidden orphan the stale-scan/watchdog can still sweep."""
	if not assistant_msg:
		return
	try:
		frappe.db.rollback()  # drop any uncommitted work from the losing CAS
		frappe.delete_doc(MSG, assistant_msg, ignore_permissions=True, force=True)
		frappe.db.commit()
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		# Fall back to marking it inert (streaming off) so it never renders a spinner.
		try:
			frappe.db.set_value(MSG, assistant_msg, {"streaming": 0, "hidden": 1}, update_modified=False)
			frappe.db.commit()
		except Exception:
			frappe.log_error(title="prepare.discard_orphan", message=frappe.get_traceback())


def _prepare_error(run_id, version, assistant_msg, conversation, owner, error, *, exc=None) -> None:
	"""preparing -> errored (release credit) + mark the placeholder errored + fenced
	run:error. Best-effort; mirrors legacy's pre-ack error surface (changed_data=False
	— the run never started)."""
	try:
		if assistant_msg:
			frappe.db.set_value(MSG, assistant_msg, {"streaming": 0, "error": (error or "")[:1000]})
		if ts.prepare_errored(run_id, version, error=error):
			frappe.db.commit()
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
	try:
		from jarvis.chat.turn_handler import _classify_error

		code = _classify_error(error, exc)
	except Exception:
		code = "internal"
	if owner:
		try:
			ts.publish_fenced(
				owner,
				"run:error",
				conversation_id=conversation,
				run_id=run_id,
				message_id=assistant_msg,
				error=error,
				code=code,
				changed_data=False,
			)
		except Exception:
			pass


def _load_dispatch_raw(run_id: str) -> dict:
	raw = frappe.db.get_value(TURN, run_id, "dispatch_payload")
	if not raw:
		return {}
	try:
		parsed = json.loads(raw)
		return parsed if isinstance(parsed, dict) else {}
	except Exception:
		return {}


def _load_context(run_id: str):
	return _load_dispatch_raw(run_id).get("context")


def _load_attachments(run_id: str):
	"""The ORIGINAL client attachment dicts ({file_url, file_name}).

	accept_or_queue stores them under ``attachments`` for a queued turn. Once
	prepare rewrites ``dispatch_payload`` with the pump handoff, the originals live
	under ``attachments_raw`` (the ``attachments`` key then holds the MANAGED vision
	shape for the pump's chat.send) — so a re-prepare after recovery still assembles
	with the real files. ``attachments_raw`` wins when present."""
	dp = _load_dispatch_raw(run_id)
	if "attachments_raw" in dp:
		return dp.get("attachments_raw")
	return dp.get("attachments")
