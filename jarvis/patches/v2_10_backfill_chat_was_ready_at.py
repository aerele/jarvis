"""Backfill Jarvis Settings.chat_was_ready_at for workspaces that predate it.

The marker ("admin has confirmed this workspace chat-Ready at least once") is
what tells the readiness gate which way to fail when the control plane cannot be
asked: an ESTABLISHED workspace keeps its chat (the container is still serving
whatever it was already serving), a never-confirmed one fails closed with a
retryable verdict rather than being told it is ready by a shrug.

Every workspace running before this deploy has it empty, and it is only ever
stamped by a live Ready verdict. Without this backfill, a control-plane outage in
the window between migrating and the next successful gate call would fail CLOSED
on working customers - the exact regression the established cohort exists to
prevent.

Grandfather rule (mirrors v1_10 / v2_00): a workspace that is onboarded
(jarvis_admin_api_key present) AND whose LLM config has been CONFIRMED applied at
least once - the same evidence is_ready_for_chat itself gates on, per
account._llm_apply_confirmed - was chat-ready before this deploy and must stay
so. The marker is stamped from that apply's own timestamp, not from now(): it is
a claim about the past, and back-dating it keeps the daily refresh honest.

NOT stamped: a workspace with no admin key (never signed up) or no confirmed
apply on any leg (its container has demonstrably never been serving its config).
Those are the onboarding-stage cohort by definition, and this patch must not
manufacture history for them - their marker sets on their first real Ready.

Reads go through the document API, NOT frappe.db.get_single_value, for the same
empty-Datetime coercion reason documented in v1_10.
"""

import frappe


def execute():
	settings = frappe.get_single("Jarvis Settings")
	if settings.get("chat_was_ready_at"):
		return
	if not (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip():
		return  # never signed up: nothing to grandfather
	# Whichever leg this workspace syncs through - pool marker for a pool (incl. a
	# BYO api-key pool), the OAuth connect stamp for a direct subscription/oauth
	# tenant, the direct apply marker otherwise. Read permissively rather than
	# re-deriving the leg: any ONE confirmed apply is evidence enough that a
	# container was serving this workspace.
	confirmed = (
		settings.get("llm_pool_synced_at")
		or settings.get("llm_direct_synced_at")
		or settings.get("llm_oauth_connected_at")
	)
	if not confirmed:
		return
	frappe.db.set_single_value("Jarvis Settings", "chat_was_ready_at", confirmed)
