"""WP-1d — the short prepare job (Relay Pump).

The RQ prepare job (D1 Stage 1, owners #16–#26) that runs BEFORE the pump
dispatches a turn: it drives ``queued -> preparing -> ready`` and hands the pump a
fully-assembled prompt + a live agent session via the Turn's
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
up. On an ``AgentUnreachableError`` bootstrap failure: ``preparing -> errored``
(release credit) + a fenced ``run:error`` (no retry — matches legacy). Never
blocks the pump (it is its own RQ job).
"""

from __future__ import annotations

import json
import time

import frappe

from jarvis._session import impersonate
from jarvis.chat import seq_watermark, txn
from jarvis.chat import turn_state as ts
from jarvis.chat.runtime_profile import get_profile
from jarvis.exceptions import AgentUnreachableError

TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"


def _claim_preparing_unit(run_id: str) -> dict:
	"""Read the turn and attempt the ``queued`` -> ``preparing`` claim CAS as one
	unit, replayed whole by ``txn.replay_on_conflict`` (P6): this job's first read
	fixes a snapshot, and a competing commit on this row (promote, watchdog reclaim,
	a user cancel) between that read and the claim CAS would otherwise crash the job
	with 1020, breaking its "never raises out" contract. Losing the race for real
	still resolves to the existing ``claim_lost``/state-skip outcome - a replay just
	re-reads the present row before deciding."""
	turn = frappe.db.get_value(
		TURN,
		run_id,
		["state", "version", "conversation", "seed_message"],
		as_dict=True,
	)
	if not turn:
		return {"turn": None, "claimed": False, "skipped": None}
	if turn["state"] != "queued":
		# Already claimed (this prepare was re-offered) or advanced/cancelled.
		return {"turn": turn, "claimed": False, "skipped": turn["state"]}
	# (D2 #2) queued -> preparing FIRST: NULLs the reservation expiry so a slow
	# prepare (22s/4-page PDF vision) is never reclaimed mid-flight (OAR-5). Requires
	# the held credit (reserved=1, granted at promote). A lost CAS = another prepare
	# won / the turn moved; no-op.
	if not ts.claim_preparing(run_id, int(turn["version"])):
		return {"turn": turn, "claimed": False, "skipped": "claim_lost"}
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist unit before next step
	return {"turn": turn, "claimed": True, "skipped": None}


def run_prepare(run_id: str, relay_target_id: str | None = None) -> dict:
	"""Prepare one reserved ``queued`` turn for dispatch. Idempotent + re-offer-safe;
	returns a small status dict. Never raises out (a prepare crash must not kill the
	pump hop; the deadline watchdog reclaims a stuck ``preparing`` turn)."""
	try:
		result = txn.replay_on_conflict(
			lambda: _claim_preparing_unit(run_id), label=f"prepare.claim_preparing {run_id}", fresh=True
		)
	except Exception as e:
		# Exhausted the replay (rare: a third writer landed inside the retry window
		# too) - the "never raises out" docstring still holds, so this resolves to
		# the same claim_lost the ordinary lost-CAS path already returns.
		txn.report_lost_race(e, title="prepare.claim_preparing")
		return {"ok": True, "skipped": "claim_lost"}

	turn = result["turn"]
	if turn is None:
		return {"ok": False, "reason": "no turn"}
	if not result["claimed"]:
		return {"ok": True, "skipped": result["skipped"]}

	conversation = turn["conversation"]
	relay_target_id = relay_target_id or frappe.db.get_value(TURN, run_id, "relay_target_id")
	version = int(turn["version"]) + 1

	# Turn -> triggering-message binding (skill "Approve & run", design §3.3, correctness-C1):
	# the Relay Pump is the DEFAULT transport and dispatches turns THROUGH this prepare job,
	# NOT turn_handler.handle_chat_send - so the bind must be installed here too, or the
	# park-time offer gate reads no binding on the default path and the whole feature is
	# inert in prod. Bind THIS turn's seed message under its conversation now, at turn start,
	# AFTER winning the claim (only the running turn binds) and BEFORE any tool call can
	# park. Best-effort: a binding failure must never break the prepare/dispatch pipeline.
	try:
		from jarvis.chat import turn_message_binding

		turn_message_binding.bind_turn_message(conversation, turn["seed_message"])
	except Exception:
		frappe.log_error(title="prepare.bind_turn_message", message=frappe.get_traceback())

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
	#
	# Full-diff review: create + attach is ONE replayable unit (not attach alone),
	# so a snapshot race on the attach CAS discards the placeholder THIS attempt
	# just created (inside the unit, before re-raising) instead of leaving it
	# behind. A replay's fresh ``_create_placeholder_locked`` call then makes a
	# NEW placeholder rather than ending up with two.
	try:
		won, assistant_msg = txn.replay_on_conflict(
			lambda: _attach_placeholder_unit(conversation, run_id, version),
			label=f"prepare.attach_placeholder {run_id}",
			fresh=True,
		)
	except Exception as e:
		# The unit already discarded its own placeholder before re-raising, so
		# there is nothing left here to clean up (unlike the payload/ready sites
		# below, whose placeholder was attached by an EARLIER, already-durable
		# unit and so still needs an explicit discard on this exhaustion path).
		txn.report_lost_race(e, title="prepare.attach_placeholder")
		return {"ok": True, "skipped": "attach_lost"}
	if not won:
		return {"ok": True, "skipped": "attach_lost"}

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
		# #702 review: an explicit "internal" - this is a bug in our own code
		# (assemble_prompt raised), never a gateway/relay signal, so it must
		# not fall through _classify_error's text guess into "gateway".
		_prepare_error(
			run_id,
			version,
			assistant_msg,
			conversation,
			owner,
			"Could not prepare the message.",
			code="internal",
		)
		return {"ok": False, "reason": "assemble_failed"}

	# (#22/#23/#24) session bootstrap + model patch + watermark on a SHORT-LIVED
	# pooled connection (R-9).
	settings = ap.settings
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	patch_session_model, session_model_ref = turn_handler._session_model_patch(conv)
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

	from jarvis.chat import agent_session_pool

	try:
		with agent_session_pool.checkout(gateway_url) as sess:
			session_key = conv.session_key
			if not session_key:
				# (#22) create the session on THIS pooled connection and persist the
				# Jarvis Chat Session row + CONV.session_key BEFORE the first tool
				# callback can fire (the plugin sessionKey->user moat, jarvis/api.py:55).
				from jarvis.chat.api import _ensure_session_key

				# profile=True: the pump pipeline's deferred creation point,
				# reached only for a genuine first turn of interactive chat -
				# a macro/scheduled turn always mints its session eagerly at
				# api._enqueue_turn, before this worker path ever runs.
				session_key = _ensure_session_key(chat_user, sess=sess, profile=True)
				frappe.db.set_value(CONV, conversation, "session_key", session_key)
				frappe.db.commit()
			# (#23) model patch (stateful — agent remembers across turns). A None ref
			# means "reset to the agent default" and MUST still be sent: it is the
			# instruction that walks back a stale pin. See _session_model_patch.
			if patch_session_model:
				try:
					sess.set_session_model(session_key, session_model_ref)
				except AgentUnreachableError:
					raise
				except Exception:
					frappe.log_error(title="prepare.model_patch", message=frappe.get_traceback())
			# (#24) watermark BEFORE the send: recovery must never stamp a prior
			# turn's answer onto this row. Best-effort.
			try:
				wm_msgs = sess.get_session_messages(session_key, limit=5)
				watermark = max(
					(
						((m or {}).get(get_profile().message_metadata_key) or {}).get("seq", 0)
						for m in wm_msgs
					),
					default=0,
				)
				if watermark:
					seq_watermark.stamp_watermark(assistant_msg, watermark)
					frappe.db.commit()
			except Exception:
				frappe.log_error(title="prepare.watermark", message=frappe.get_traceback())
	except AgentUnreachableError as exc:
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
		"thinking": turn_handler._turn_thinking(conv),
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
	#
	# Full-diff review: the whole assemble+gateway-bootstrap phase above (HTTP to
	# the agent) is the "slow part" that can leave this job's snapshot stale
	# relative to a concurrent cancel/watchdog reclaim on the Turn row; wrap the
	# CAS as its own replayable unit, fresh=True closing that window right before
	# the write (same policy as the pump's tool applier / settlement).
	try:
		won = txn.replay_on_conflict(
			lambda: _store_dispatch_payload_unit(run_id, version, json.dumps(payload)),
			label=f"prepare.store_dispatch_payload {run_id}",
			fresh=True,
		)
	except Exception as e:
		txn.report_lost_race(e, title="prepare.store_dispatch_payload")
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "payload_lost"}
	if not won:
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "payload_lost"}

	# (D2 #3) preparing -> ready.
	try:
		won = txn.replay_on_conflict(
			lambda: _mark_ready_unit(run_id, version), label=f"prepare.mark_ready {run_id}", fresh=True
		)
	except Exception as e:
		# Lost (a watchdog reclaimed a slow prepare, or a cancel raced). The stale
		# state wins; do not dispatch — clean the orphan placeholder this prepare made.
		txn.report_lost_race(e, title="prepare.mark_ready")
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "ready_lost"}
	if not won:
		_discard_orphan_placeholder(assistant_msg)
		return {"ok": True, "skipped": "ready_lost"}

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
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- fresh txn before row lock
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
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist unit before next step
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


def _attach_placeholder_unit(conversation: str, run_id: str, version: int) -> tuple[bool, str | None]:
	"""Create the assistant placeholder, then attach it to the Turn, as one
	replayable unit (full-diff review of #1527). ``ts.attach_placeholder``'s CAS
	is the previously-unprotected write: a competing commit on the Turn row
	(a user cancel, a watchdog reclaim) between ``_create_placeholder_locked``'s
	reads and this CAS used to raise ``QueryDeadlockError`` straight out of the
	job. On ANY failure here - a genuine lost actor claim (0 rows, no exception)
	OR a snapshot conflict (caught, re-raised) - THIS attempt's placeholder is
	discarded before returning/re-raising, so ``txn.replay_on_conflict``'s retry,
	which calls ``_create_placeholder_locked`` again, never leaves a second
	placeholder sitting next to the first. Returns ``(won, assistant_msg)``;
	``assistant_msg`` is only meaningful when ``won`` (the caller has nothing to
	clean up on a loss - already discarded here)."""
	assistant_msg = _create_placeholder_locked(conversation)
	try:
		won = ts.attach_placeholder(run_id, version, assistant_msg)
	except Exception:
		_discard_orphan_placeholder(assistant_msg)
		raise
	if not won:
		_discard_orphan_placeholder(assistant_msg)
		return False, None
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist unit before next step
	return True, assistant_msg


def _store_dispatch_payload_unit(run_id: str, version: int, payload_json: str) -> bool:
	"""``ts.store_dispatch_payload``'s CAS as one unit, replayed whole (full-diff
	review): the whole assemble+gateway-bootstrap phase between the attach step
	and this call is the job's "slow part" (HTTP to the agent), so a snapshot
	fixed before it can easily be stale by the time this write runs. A win
	commits; a genuine lost claim (0 rows, no exception) is left for the caller
	to discard the already-durable placeholder - nothing here needs rolling back
	on that path."""
	won = ts.store_dispatch_payload(run_id, version, payload_json)
	if won:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist unit before next step
	return won


def _mark_ready_unit(run_id: str, version: int) -> bool:
	"""``ts.mark_ready``'s CAS as one unit, replayed whole (full-diff review).
	Same shape as ``_store_dispatch_payload_unit``."""
	won = ts.mark_ready(run_id, version)
	if won:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist unit before next step
	return won


def _prepare_error(
	run_id, version, assistant_msg, conversation, owner, error, *, exc=None, code=None
) -> None:
	"""preparing -> errored (release credit) + mark the placeholder errored + fenced
	run:error. Best-effort; mirrors legacy's pre-ack error surface (changed_data=False
	- the run never started).

	`code` lets a caller that already knows the cause skip the text/exc-based
	guess entirely (#702 review): _classify_error's taxonomy is aimed at
	gateway/relay failures, and a local bug in our own prompt-assembly code
	(caught by a bare `except Exception`, no AgentUnreachableError in hand) has
	no gateway/relay signal to classify - guessing from generic text like
	"Could not prepare the message." would otherwise fall into the mid-run
	"gateway" default and tell the customer to just retry a bug that a retry
	will most likely reproduce."""
	errored = False
	try:
		if assistant_msg:
			frappe.db.set_value(MSG, assistant_msg, {"streaming": 0, "error": (error or "")[:1000]})
		if ts.prepare_errored(run_id, version, error=error):
			frappe.db.commit()
			errored = True
			# jarvis#1425 review (scoped re-review, 2026-09-27): preparing is an
			# in-flight state (admission._INFLIGHT_STATES) - this leaves it.
			from jarvis.chat import llm_switch

			llm_switch.apply_if_active(source="prepare._prepare_error")
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
	if errored:
		# No settlement (so no finalize seal) on this edge: seal its File Box sheet here.
		from jarvis.chat import held_sheet_seal

		held_sheet_seal.after_turn(conversation, run_id)
	if not code:
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
