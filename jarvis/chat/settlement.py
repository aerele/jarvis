"""WP-1d — the ONE critical settlement transaction (Relay Pump).

Pump-invoked at a terminal frame (``on_terminal``), plus the reconcile path
(settlement-owed turns recovered from the row, R-12) and the cancel sweep. This
is D3 Race 3 realised against the ownership table (WP-D / D1):

  * the final assistant-text PROJECTION + the ``terminal_observed -> finalizing |
    errored | cancelled`` state advance + the conversation-slot release, ALL in
    ONE epoch+version-fenced transaction (nothing blocking — no WS poll, R-4);
  * the turn's REQUIRED ``Jarvis Turn Effect`` rows inserted in that SAME txn so
    finalize has a CLOSED, force-done-bounded owed-enrichment set (OAR-9);
  * a 0-rows LOSS routes through the ONE shared ``lease_lost_exit`` (a takeover
    re-stamped the epoch — D3 S3; the uncommitted S1 message write rolls back);
  * AFTER the winning commit, the AUTHORITATIVE fenced terminal realtime event
    (``run:end`` with ``enrichment_pending`` for a success/abort, SUX-7; or a
    ``run:error`` carrying today's Message.error classification for an error,
    SUX-11), then ``enqueue_finalize`` — enrichment can never block the next turn
    or convert a success to an error.

This module is wired as the pump's ``invoke_settlement`` seam (pump.PumpDeps);
``jarvis.chat.pump._default_invoke_settlement`` lazily delegates here so the
DEFAULT deps settle correctly in production, and tests inject it directly.

HARD INVARIANTS (restated): every turn mutation goes through ``turn_state``; the
settlement txn contains NOTHING blocking; realtime publishes are fenced and fire
only after the winning commit; usage is NOT in this txn (it is a finalize effect
with a (turn_id) idempotency guard, R-4).
"""

from __future__ import annotations

import json

import frappe

from jarvis.chat import turn_state as ts

TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"

# The owed-enrichment set fixed at settlement (OAR-9). A relay:final success owes
# the full set; an errored/cancelled terminal owes the macro-advance + telemetry
# hooks (there is no rich output/title/usage to enrich, and its reply is already
# terminal — finalize NEVER un-settles it). BOTH sets owe terminal_publish (CDX-12 /
# R-5): the idempotent terminal re-publish backstop so a lost settlement terminal
# (run:end / run:error) is redelivered off the durable row, on success AND error.
FINAL_EFFECTS = (
	"terminal_publish",
	"rich_outputs",
	"usage",
	"chat_asks",
	"macro_advance",
	"auto_title",
	"wiki_nudge",
	"telemetry_flush",
)
TERMINAL_EFFECTS = ("terminal_publish", "macro_advance", "telemetry_flush")


def invoke_settlement(
	run_id: str,
	*,
	relay_target_id: str,
	epoch: int,
	version: int,
	terminal_kind: str,
	terminal_payload: dict | None,
	assistant_message: str | None,
	owner: str | None,
	conversation: str,
	deps,
) -> None:
	"""Settle one turn at its terminal (D3 Race 3). Epoch+version-fenced; a stale
	pump's CAS affects 0 rows and exits via ``lease_lost_exit`` (which rolls back
	the uncommitted S1 message write). On a win, publishes the authoritative fenced
	terminal event AFTER commit, then enqueues finalize. Never blocks."""
	row = ts.read_turn(run_id)
	if row is None:
		return
	# Already settled (finalizing/done/errored/cancelled) — a replayed/duplicate
	# terminal. Idempotent no-op (the CAS below would also 0-row, but skip the work).
	if row["state"] not in ("terminal_observed",):
		return
	v = int(row["version"])
	kind = row.get("terminal_kind") or terminal_kind
	am = assistant_message or row.get("assistant_message")
	aborted = bool(row.get("cancel_requested")) and _is_aborted_payload(
		row.get("terminal_kind"), terminal_payload
	)

	if aborted:
		# Clean-stop terminal (D2 #19). The stop is a FLAG, not prose — keep whatever
		# streamed; the SPA renders the marker from `stopped` (matches legacy).
		if am:
			ts._run_cas(f"UPDATE `tab{MSG}` SET stopped=1, streaming=0 WHERE name=%(m)s", {"m": am})
		won = ts.settle_cancelled(run_id, v, epoch)
		if won:
			ts.insert_required_effects(run_id, TERMINAL_EFFECTS)
		pub_kind, pub_extra = "run:end", {"stopped": True}
	elif kind == "relay:error":
		err = _error_text(terminal_payload)
		# Mark errored WITHOUT overwriting the streamed content (matches legacy
		# _mark_errored: streaming=0 + error, content preserved). SUX-11: the code
		# classification travels on the realtime event below.
		if am:
			ts._run_cas(
				f"UPDATE `tab{MSG}` SET streaming=0, error=%(e)s WHERE name=%(m)s",
				{"e": err[:1000], "m": am},
			)
		won = ts.settle_errored(run_id, v, epoch, error=err)
		if won:
			ts.insert_required_effects(run_id, TERMINAL_EFFECTS)
		pub_kind, pub_extra = "run:error", {"error": err, "code": _classify(err)}
	else:
		final_text = _final_text(terminal_payload)
		# S1 final projection (final text beats the batcher tail); always clear
		# streaming even when the terminal carried no text (matches legacy).
		if am:
			if final_text:
				ts._run_cas(
					f"UPDATE `tab{MSG}` SET content=%(c)s, streaming=0 WHERE name=%(m)s",
					{"c": final_text, "m": am},
				)
			else:
				ts._run_cas(f"UPDATE `tab{MSG}` SET streaming=0 WHERE name=%(m)s", {"m": am})
		won = ts.settle_finalizing(run_id, v, epoch, required_effects=FINAL_EFFECTS)
		# SUX-6: was_recovered tells the client a VISIBLE replacement is legitimate
		# (the answer changed via snapshot recovery); on the normal path it is 0 and
		# the client skips re-rendering an identical terminal.
		pub_kind, pub_extra = (
			"run:end",
			{
				"enrichment_pending": True,
				"was_recovered": bool(frappe.db.get_value(TURN, run_id, "was_recovered")),
			},
		)

	# F4: stamp the REAL reply duration where the UI reads it (see _stamp_reply_duration).
	# Runs inside the fenced txn: on a win it commits atomically with the projection; on a
	# 0-rows loss it rolls back with the message write below.
	if won and am:
		_stamp_reply_duration(am, run_id)

	if not won:
		# OARF-3 / §10.11: a 0-rows settlement CAS is NOT unconditionally a lease
		# loss. Re-read the row's epoch to disambiguate:
		#   * epoch NO LONGER matches -> a takeover re-stamped us; route through the
		#     ONE shared lease-loss exit (rollback undoes the uncommitted S1 message
		#     write, stops writing/publishing; the pump's on_terminal wrapper converts
		#     the LeaseLostExit to its shared exit).
		#   * epoch INTACT + version drifted -> a legitimate concurrent actor moved
		#     the row (e.g. the watchdog marked a stuck terminal_observed turn
		#     recovering in the TOCTOU window between read_turn and this CAS). That is
		#     an ordinary optimistic-concurrency loss, NOT a lease incident — rolling
		#     back the S1 write and returning lets reconcile/watchdog own it; killing
		#     the whole hop here would turn a benign drift into an availability
		#     incident (every turn on the shard stalls up to LEASE_TTL_S).
		if _epoch_lost(run_id, epoch):
			ts.lease_lost_exit(run_id)
		else:
			frappe.db.rollback()
		return

	frappe.db.commit()  # slot released; the NEXT turn can be promoted

	# S5 — authoritative fenced terminal event, ONLY after the winning commit. CDX-3:
	# the terminal carries pump_epoch so the client permanently blocks any later
	# lower-epoch straggler (a stale pump's late delta / run:start) for this turn. CDX-12:
	# it also carries a STABLE terminal identity (run_id + epoch + a terminal seq = the
	# durable watermark at the terminal) so the client dedupes a repeat equal-epoch
	# terminal (the finalize backstop re-publish) one-shot — no repeated announcement/reload.
	if owner:
		ts.publish_fenced(
			owner,
			pub_kind,
			conversation_id=conversation,
			run_id=run_id,
			event_seq=int(row.get("last_event_seq") or 0),
			message_id=am,
			pump_epoch=epoch,
			relay_target_id=relay_target_id,
			**pub_extra,
		)

	# S6 — enqueue enrichment (idempotent per (turn, effect_name); force-done at 3).
	deps.enqueue_finalize(run_id, relay_target_id)


# --------------------------------------------------------------------------- #
# terminal-payload helpers (mirror pump.py's so the two agree on shape)
# --------------------------------------------------------------------------- #


def _stamp_reply_duration(assistant_message: str, run_id: str) -> None:
	"""CDX-20 / F4 — the persisted turn-duration fix, on ONE durable baseline shared with the
	live timer. The pump projects the assistant row via raw SQL (``ts._run_cas``) and the
	streaming batcher writes deltas with ``update_modified=False``, so the row's ``modified``
	never advances past the placeholder insert — a reload's duration read 0.0s. The FIX stamps a
	dedicated ``reply_duration_ms`` metric (NOT a rewrite of Frappe's ``creation`` audit field —
	CDX-20) computed on the SAME boundary the live timer uses:

	    run:start (the pump's ``dispatching_at`` — stamped at ready->dispatching, IMMEDIATELY
	    before ``run:start`` is published in _dispatch_one) -> settlement (now, the terminal).

	The SPA's live timer runs from the ``run:start`` event to the ``run:end`` event; the reload
	path reads THIS ``reply_duration_ms`` (ChatView.elapsedOf), so both anchor on run:start↔
	settlement and agree within network tolerance. ``dispatching_at`` is used — NOT
	``first_event_at`` (which the round-3 code used): first_event_at is written only at the
	chat.send ACK (dispatching->streaming), i.e. AFTER run:start, so it undercounts the span the
	user watched tick. ``modified`` IS bumped to now — honest (the row is genuinely finalized in
	this txn), which also fixes the assistant row's hover timestamp — but ``creation`` is left
	untouched. Runs inside the fenced settlement txn (no commit here); a stale-pump loss rolls
	it back with the rest."""
	start = frappe.db.get_value(TURN, run_id, "dispatching_at")
	if not start:
		# A degenerate turn with no dispatch stamp — still bump modified (finalized now) but
		# record no duration; the UI then falls back to its legacy modified-creation read.
		ts._run_cas(
			f"UPDATE `tab{MSG}` SET modified=%(now)s WHERE name=%(m)s",
			{"now": frappe.utils.now(), "m": assistant_message},
		)
		return
	duration_ms = int(max(0, frappe.utils.time_diff_in_seconds(frappe.utils.now(), start) * 1000))
	ts._run_cas(
		f"UPDATE `tab{MSG}` SET reply_duration_ms=%(d)s, modified=%(now)s WHERE name=%(m)s",
		{"d": duration_ms, "now": frappe.utils.now(), "m": assistant_message},
	)


def _epoch_lost(run_id: str, epoch: int) -> bool:
	"""OARF-3 / §10.11: True iff the row's ``pump_epoch`` no longer matches this
	settlement's epoch — the real lease-loss signal that distinguishes a takeover
	(kill the hop) from a benign concurrent-actor version drift (an ordinary
	optimistic-concurrency loss the hop must survive)."""
	e = frappe.db.get_value(TURN, run_id, "pump_epoch")
	return e is None or int(e) != epoch


def _coerce_payload(payload):
	if isinstance(payload, str):
		try:
			return json.loads(payload)
		except Exception:
			return {"text": payload}
	return payload


def _final_text(payload) -> str | None:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		return payload.get("text")
	return None


def _error_text(payload) -> str:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		return payload.get("error") or payload.get("state") or "The run ended with an error."
	return "The run ended with an error."


def _is_aborted_payload(terminal_kind, payload) -> bool:
	payload = _coerce_payload(payload)
	if isinstance(payload, dict):
		if payload.get("aborted") is True:
			return True
		if payload.get("state") == "aborted":
			return True
	return False


def _classify(err_text: str) -> str:
	"""Preserve today's Message.error headline classification (SUX-11)."""
	try:
		from jarvis.chat.turn_handler import _classify_error

		return _classify_error(err_text)
	except Exception:
		return "internal"
