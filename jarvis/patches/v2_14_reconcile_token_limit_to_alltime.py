"""Bump every existing per-user cap by that user's current usage, so switching
``Jarvis User Settings.monthly_token_limit`` from a monthly-resetting cap to an
all-time cap (jarvis.chat.policy._over_total_limit) cannot instantly, permanently
lock out an existing user on deploy.

Before this change the field was compared against ``month_tokens`` (reset every
month); after it, it is compared against ``total_tokens`` (cumulative, never
reset - see policy.py). A user already past their old monthly-sized cap in
lifetime usage would otherwise fail ``used >= limit`` on their very next send,
forever, with no month rollover left to save them.

The admin's original number encoded intended HEADROOM, not an absolute
lifetime ceiling - a "100k/month" cap meant "block after 100k more than
whatever came before". Preserving that intent means adding today's already-
accrued usage on top of the existing cap: a 100k cap on a user already at 500k
total becomes 600k (existing cap + intended headroom), so nobody already
compliant is retroactively over, and nobody is silently made unlimited (0
stays out of scope - the WHERE guard below only touches positive caps).

Only rows with ``monthly_token_limit > 0`` are touched; a 0 (unlimited) row has
nothing to reconcile. Deliberately NOT idempotent against a manual re-run (a
second pass would double-add), but that is not a concern for a normal
migrate - the patch log runs this exactly once.
"""

import frappe

SETTINGS = "Jarvis User Settings"


def execute() -> None:
	if not frappe.db.has_column(SETTINGS, "monthly_token_limit") or not frappe.db.has_column(
		SETTINGS, "total_tokens"
	):
		return

	(affected,) = frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM `tab{SETTINGS}`
		WHERE monthly_token_limit > 0
		"""
	)[0]

	frappe.db.sql(
		f"""
		UPDATE `tab{SETTINGS}`
		SET monthly_token_limit = monthly_token_limit + total_tokens
		WHERE monthly_token_limit > 0
		"""
	)
	frappe.db.commit()

	frappe.logger("jarvis").info(
		f"v2_14 token-limit reconciliation: bumped {affected} Jarvis User Settings "
		f"row(s) with a positive cap by their current total_tokens, ahead of the "
		f"monthly -> all-time cap switch"
	)
