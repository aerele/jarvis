"""WP-2 chat-concurrency measurement harness (Stage A: legacy baseline).

NEW FILES ONLY — this package edits no product code. It measures the CURRENT
(legacy, worker-per-turn) chat transport against a deterministic local WS
gateway that reproduces the openclaw protocol subset the bench uses (S2), so
the owner's six production criteria (C1–C6, BUILD-DIRECTIVE §1) get a clean
baseline the Relay Pump must later beat.

Layers of fidelity (stated up front, and again in every report):

  * Transport / relay / batching — REAL bench code. The harness drives the
    real ``jarvis.chat.openclaw_client.OpenclawSession`` (real handshake,
    ``chat_send``, ``relay_turn_events``) over a real socket into
    ``fake_gateway.FakeGateway``, and feeds the frames through the real
    ``jarvis.chat.turn_handler._AssistantContentBatcher`` +
    ``_handle_event`` (real DB writes + real ``publish_to_user`` fan-out).
    Every millisecond of streaming, batching and DB-commit cadence is real.

  * Concurrency / worker pool — SIMULATED (a bounded thread executor), NOT
    real RQ. The legacy model is worker-per-turn: each worker is held for a
    whole turn. A size-2 pool therefore reproduces the exact 2-background-
    worker starvation the pump removes. This is the ``2-worker config
    simulated`` the brief calls for. Caveats live in the baseline report.

Nothing here mutates durable bench state. The admission flag is overridden
per-process only; ``ensure_paired`` is stubbed process-wide; the gateway URL
is passed directly to the transport (Jarvis Settings is never written).
"""

from __future__ import annotations

import os

BENCH_ROOT = "/home/vignesh/frappe-bench"
SITES_PATH = os.path.join(BENCH_ROOT, "sites")
DEFAULT_SITE = "patterntest.localhost"

# Fixture user the harness owns (mirrors the WP-0 admission-test fixture user
# pattern so cleanup is symmetrical and site-wide-shard-safe).
HARNESS_USER = "jarvis-harness@example.com"

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
PUMP = "Jarvis Relay Pump"

_frappe_ready = False


def bootstrap(site: str = DEFAULT_SITE):
	"""Init + connect frappe for a standalone harness script (idempotent).

	Standalone scripts must run with the sites directory resolvable; we chdir
	into it so ``frappe.init`` finds the site and writes its logs under the
	site path (matching how ``bench execute`` behaves).
	"""
	global _frappe_ready
	import frappe

	if _frappe_ready and getattr(frappe.local, "site", None) == site:
		return frappe
	os.chdir(SITES_PATH)
	frappe.init(site=site)
	frappe.connect()
	_frappe_ready = True
	return frappe
