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
(jarvis_admin_api_key present) AND carries the marker its own leg is gated on -
the same evidence is_ready_for_chat itself opens chat with, per
account._llm_apply_confirmed - was chat-ready before this deploy and must stay
so. The marker is stamped from that leg's own timestamp, not from now(): it is a
claim about the past, and back-dating it keeps the daily refresh honest.

Grandfather ONLY from evidence of a CONFIRMED apply (review P0-07). Two of the
three sync markers qualify: llm_pool_synced_at and llm_direct_synced_at are
stamped on an admin-confirmed apply. The third, llm_oauth_connected_at, is
DELIBERATELY EXCLUDED: it is stamped when the OAuth grant completes and the
auth-profile blob is PUSHED (jarvis/oauth/api.py) - a push, not a confirmation
that the container ever served the workspace. A workspace whose only evidence is
an OAuth push may never have become usable, and manufacturing an established
claim for it would let it fail OPEN through an outage it should fail closed on.
Such a workspace stays unconfirmed until its next live Ready earns the marker
honestly.

NOT stamped: a workspace with no admin key (never signed up), or one with no
CONFIRMED apply marker (nothing an admin ever verified reached its container).
Those are the onboarding-stage cohort by definition, and this patch must not
manufacture history for them - their marker sets on their first real Ready.

The backfilled marker is bound to the workspace's CURRENT authority (admin
principal + container + generation) via the same anchor the live gate stamps, so
a later reset/reconnect/generation change ends the grandfathered claim exactly as
it would a freshly-earned one.

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
	# Confirmed-apply evidence ONLY: the pool marker for a pool (incl. a BYO
	# api-key pool) or the direct apply marker otherwise. llm_oauth_connected_at is
	# intentionally NOT consulted (see the module docstring): a push is not a
	# confirmation.
	confirmed = settings.get("llm_pool_synced_at") or settings.get("llm_direct_synced_at")
	if not confirmed:
		return
	frappe.db.set_single_value("Jarvis Settings", "chat_was_ready_at", confirmed)
	# Bind the grandfathered claim to the current authority so it is fenced the same
	# way a live-earned one is (account._has_been_chat_ready). Best-effort: an
	# unbound legacy marker still honours the presence rule, so a failure here only
	# forgoes the mechanical fence, never the grandfather itself.
	try:
		from jarvis import account

		if frappe.get_meta("Jarvis Settings").get_field(account._READY_ANCHOR_FIELD):
			raw = account._settings_raw(account._GATE_STATE_FIELDS)
			frappe.db.set_single_value(
				"Jarvis Settings", account._READY_ANCHOR_FIELD, account._authority_anchor(raw)
			)
	except Exception:
		pass
