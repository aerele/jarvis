"""The 8 scripted transcripts (fixture data) for the differential harness.

These are the deterministic openclaw run playbacks the fake gateway streams.
Each transcript models one row of the WP-2 matrix:

    success, tool-heavy, confirmation-card, overflow/compaction,
    abort, ack-timeout, ws-drop, recovered

A transcript is a plain dict (JSON-serialisable) with:

  name          str
  description   str
  ack           {"status": "started"} | {"status": "in_flight"} | ...
  ack_behavior  "normal" | "timeout"   (timeout: delay the ack past the
                client ack window so the bench parks for snapshot recovery)
  frames        ordered list of ops the gateway plays AFTER the ack, once the
                run is admitted to the (simulated) main lane:
                  {"op":"assistant","text":<cumulative>,"delta":<incremental>}
                  {"op":"tool_start","name","call_id","title"}
                  {"op":"tool_end","call_id","status"}
                  {"op":"lifecycle_error","error":<str>}   (non-terminal on the
                      bench: "context overflow" reroutes to run:recovering)
                  {"op":"pause","ms":<int>}                (explicit extra dwell)
  terminal      {"kind":"final","text":<str|None>}
                | {"kind":"error","state":"error","errorMessage":<str>}
                | {"kind":"aborted"}                      (also emitted on
                    chat.abort regardless of scripted point)
                | {"kind":"failed_final","stopReason":"error"}
  inject        optional fault: {"drop_after_frame": <idx>}  (WS-drop) |
                {"recover_via":"history","final_text":<str>} (recovered:
                    stream is cut, the durable transcript still holds the
                    full answer for a snapshot-recovery finalize)

``text`` on the terminal final is the authoritative answer
(``_chat_final_text`` joins message.content). The gateway derives the chat
final ``message.content`` block from it.

The transcripts are built in Python (the maintainable source of truth) and can
be exported to ``fixtures/transcripts/*.json`` via ``dump_fixtures()`` for
inspection / diffing.
"""

from __future__ import annotations

import json
import os

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")


def _stream_text(full: str, chunk_words: int = 2) -> list[dict]:
	"""Expand a full answer into cumulative assistant deltas (word-chunked),
	exactly the shape openclaw emits (stream=assistant, cumulative text +
	incremental delta)."""
	words = full.split(" ")
	frames: list[dict] = []
	acc: list[str] = []
	i = 0
	while i < len(words):
		chunk = words[i : i + chunk_words]
		i += chunk_words
		delta = (" " if acc else "") + " ".join(chunk)
		acc.extend(chunk)
		frames.append({"op": "assistant", "text": " ".join(acc), "delta": delta})
	return frames


_SUCCESS_TEXT = (
	"Here is the summary you asked for. The three overdue invoices total "
	"forty two thousand rupees across two customers, and the oldest is "
	"eighteen days past its due date. I would prioritise the Acme invoice "
	"first because it is both the largest and the oldest, then follow up on "
	"the two smaller Globex invoices together in a single reminder."
)

_TOOL_INTRO = "Let me pull the current figures for you."
_TOOL_MID = "Got the invoice list. Now checking the customer balances."
_TOOL_ANSWER = (
	"Across the two customers you have three open invoices worth forty two "
	"thousand rupees. Acme is the largest exposure at thirty thousand."
)

_CONFIRM_TEXT = (
	"I have prepared a payment reminder for the Acme overdue invoice. Please "
	"review the draft below and confirm before I send it."
)

_OVERFLOW_PRE = "Reading the full thread history to answer accurately."
_OVERFLOW_POST = (
	"Thanks for waiting — I compacted the older context and here is the "
	"answer: the reconciliation matches for all but one line, which is off "
	"by ninety rupees due to a rounding difference."
)

_RECOVERED_TEXT = (
	"The month-end close checklist is complete: all journals are posted, the "
	"bank reconciliation ties out, and the trial balance is in balance."
)

_ABORT_PARTIAL = "Starting the export now, this will take a few moments as I gather"


def _build() -> dict[str, dict]:
	t: dict[str, dict] = {}

	# 1. success — a clean answer, no tools.
	t["success"] = {
		"name": "success",
		"description": "Clean single-answer turn, no tools, terminal final.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": _stream_text(_SUCCESS_TEXT),
		"terminal": {"kind": "final", "text": _SUCCESS_TEXT},
	}

	# 2. tool-heavy — assistant + 3 tool round-trips interleaved.
	tool_frames: list[dict] = []
	tool_frames += _stream_text(_TOOL_INTRO)
	tool_frames += [
		{"op": "tool_start", "name": "jarvis__get_list", "call_id": "t1", "title": "get_list Sales Invoice"},
		{"op": "pause", "ms": 180},
		{"op": "tool_end", "call_id": "t1", "status": "completed"},
	]
	tool_frames += _stream_text(_TOOL_MID)
	tool_frames += [
		{"op": "tool_start", "name": "jarvis__query", "call_id": "t2", "title": "query Customer balances"},
		{"op": "pause", "ms": 220},
		{"op": "tool_end", "call_id": "t2", "status": "completed"},
		{"op": "tool_start", "name": "browser", "call_id": "t3", "title": "open dashboard"},
		{"op": "pause", "ms": 150},
		{"op": "tool_end", "call_id": "t3", "status": "completed"},
	]
	tool_frames += _stream_text(_TOOL_ANSWER)
	t["tool-heavy"] = {
		"name": "tool-heavy",
		"description": "Assistant text interleaved with three tool call round-trips (two jarvis__, one built-in browser), terminal final.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": tool_frames,
		"terminal": {"kind": "final", "text": _TOOL_ANSWER},
	}

	# 3. confirmation-card — assistant text + a confirm-gated jarvis tool that
	#    stays "running" (the bench renders the card and awaits the user); the
	#    run ends final with the draft prepared. Used for the storm scenario.
	confirm_frames: list[dict] = []
	confirm_frames += _stream_text(_CONFIRM_TEXT)
	confirm_frames += [
		{
			"op": "tool_start",
			"name": "jarvis__update_doc",
			"call_id": "c1",
			"title": "update_doc Payment Reminder (needs confirm)",
		},
		{"op": "pause", "ms": 120},
	]
	t["confirmation-card"] = {
		"name": "confirmation-card",
		"description": "Assistant proposes a change and opens a confirmation card (jarvis__update_doc, confirm-gated); terminal final leaves the card awaiting the user.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": confirm_frames,
		"terminal": {"kind": "final", "text": _CONFIRM_TEXT},
	}

	# 4. overflow/compaction — a mid-stream context-overflow lifecycle error
	#    (the bench marks run:recovering and KEEPS streaming), a compaction
	#    pause, then the real answer and a terminal final.
	overflow_frames: list[dict] = []
	overflow_frames += _stream_text(_OVERFLOW_PRE)
	overflow_frames += [
		{
			"op": "lifecycle_error",
			"error": "Context overflow: prompt too large for the model; auto-compacting",
		},
		{"op": "pause", "ms": 600},
	]
	overflow_frames += _stream_text(_OVERFLOW_POST)
	t["overflow-compaction"] = {
		"name": "overflow-compaction",
		"description": (
			"Mid-stream context-overflow lifecycle error, a compaction pause, then the answer "
			"streams and terminal final. NOTE: on the MANAGED relay path relay_turn_events drops "
			"lifecycle frames, so the bench sees a longer turn that resolves to final (openclaw "
			"auto-compacts internally); the run:recovering UX is a self-host-path (stream_agent_turn) "
			"behavior. The lifecycle_error frame is retained so the self-host consumer + Stage-B "
			"differential still exercise it."
		),
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": overflow_frames,
		"terminal": {"kind": "final", "text": _OVERFLOW_POST},
	}

	# 5. abort — a partial stream, then the user stops it; the gateway emits an
	#    aborted terminal (also emitted immediately on receiving chat.abort).
	abort_frames = _stream_text(_ABORT_PARTIAL)
	t["abort"] = {
		"name": "abort",
		"description": "Partial stream then a user stop; terminal aborted (also emitted on chat.abort). Drives the stop-click->visible-stop probe.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": abort_frames,
		"terminal": {"kind": "aborted"},
	}

	# 6. ack-timeout — the gateway delays the chat.send response past the
	#    client's ack window; the bench parks for snapshot recovery
	#    (relay:interrupted reason=ack-timeout). openclaw still ran the turn,
	#    so the durable transcript holds the answer.
	t["ack-timeout"] = {
		"name": "ack-timeout",
		"description": "chat.send ack delayed past the client window -> OpenclawUnreachable(ack-timeout) -> bench parks; the transcript still completes server-side.",
		"ack": {"status": "started"},
		"ack_behavior": "timeout",
		"frames": _stream_text(_SUCCESS_TEXT),
		"terminal": {"kind": "final", "text": _SUCCESS_TEXT},
	}

	# 7. ws-drop — the gateway closes the socket mid-stream; the relay yields
	#    relay:interrupted reason=transport and the pool evicts the corpse.
	dropdrop_frames = _stream_text(_RECOVERED_TEXT)
	t["ws-drop"] = {
		"name": "ws-drop",
		"description": "Gateway closes the WS mid-stream -> relay:interrupted reason=transport; pooled connection is evicted.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": dropdrop_frames,
		"terminal": {"kind": "final", "text": _RECOVERED_TEXT},
		"inject": {"drop_after_frame": max(1, len(dropdrop_frames) // 3)},
	}

	# 8. recovered — like ws-drop, but the durable transcript (chat.history /
	#    sessions.get) still holds the full answer, so snapshot recovery can
	#    finalize it. The fixture records the recovery final text so Stage-B
	#    recovery probes can assert the resolved outcome.
	recovered_frames = _stream_text(_RECOVERED_TEXT)
	t["recovered"] = {
		"name": "recovered",
		"description": "Mid-stream drop whose durable transcript still holds the complete answer; snapshot recovery finalizes it (Stage-B recovery fixture).",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": recovered_frames,
		"terminal": {"kind": "final", "text": _RECOVERED_TEXT},
		"inject": {
			"drop_after_frame": max(1, len(recovered_frames) // 2),
			"recover_via": "history",
			"final_text": _RECOVERED_TEXT,
		},
	}

	return t


TRANSCRIPTS: dict[str, dict] = _build()

# The 8 canonical names, in matrix order.
NAMES = [
	"success",
	"tool-heavy",
	"confirmation-card",
	"overflow-compaction",
	"abort",
	"ack-timeout",
	"ws-drop",
	"recovered",
]


def get(name: str) -> dict:
	if name not in TRANSCRIPTS:
		raise KeyError(f"unknown transcript {name!r}; have {sorted(TRANSCRIPTS)}")
	return TRANSCRIPTS[name]


def dump_fixtures(out_dir: str = FIXTURE_DIR) -> list[str]:
	"""Write each transcript to fixtures/transcripts/<name>.json (inspectable
	data). Returns the paths written."""
	os.makedirs(out_dir, exist_ok=True)
	written = []
	for name in NAMES:
		path = os.path.join(out_dir, f"{name}.json")
		with open(path, "w") as fh:
			json.dump(TRANSCRIPTS[name], fh, indent=2)
		written.append(path)
	return written


if __name__ == "__main__":
	for p in dump_fixtures():
		print("wrote", p)
