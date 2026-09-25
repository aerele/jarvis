"""Transition-safe access to the renamed snapshot-recovery watermark column.

The white-label rename moved the legacy chat-message watermark column to
``agent_seq_watermark``. The one-shot v2_10 copy patch only migrates rows that exist
when ``bench migrate`` runs — but the transition has live traffic on both sides of it:

* old-code RQ workers keep WRITING the legacy column until the post-migrate restart —
  a new-code reader that looks only at the new column sees 0 for those rows, and
  min_seq=0 turns the anti-answer-stealing recovery fence OFF (the exact incident the
  watermark exists to prevent);
* a rollback resumes old-code READERS of the legacy column — values written only to
  the new column are invisible to them.

Every write therefore stamps BOTH columns and every read takes ``GREATEST`` of
the pair while the legacy column exists. Normal migrations also reconcile both
columns, including sites whose original rename patch already ran. No worker-drain
gate or manual customer action is required for this compatibility step. Retiring
the old column remains a separate deployment decision.
"""

from __future__ import annotations

import frappe

MSG = "Jarvis Chat Message"
_LEGACY_COL = "openclaw_seq_watermark"


def has_legacy_column() -> bool:
	"""True while the pre-rename column is still present (upgraded site, transition
	release). Fresh installs never grow it. Asks the SERVER directly (SHOW COLUMNS)
	rather than ``get_table_columns`` — that helper is redis/client cached and a
	stale entry here would silently disable the compat layer (bit CI when the test
	suite ALTERs the column in). Memoized per request — schema does not change
	mid-request."""
	cached = getattr(frappe.local, "_jarvis_wm_legacy_col", None)
	if cached is None:
		cached = bool(frappe.db.sql(f"SHOW COLUMNS FROM `tab{MSG}` WHERE Field = %(c)s", {"c": _LEGACY_COL}))
		frappe.local._jarvis_wm_legacy_col = cached
	return cached


def reconcile_watermarks() -> None:
	"""Idempotently preserve the effective fence in both columns on migrate.

	Do not drop the legacy column: an old worker can still write after this
	statement, and a rollback still needs its values. Runtime dual writes and
	GREATEST reads cover that overlap. Fresh sites have no legacy column.
	"""
	# Migration may have changed the schema after this request's first lookup.
	frappe.local._jarvis_wm_legacy_col = None
	if not has_legacy_column():
		return
	effective = f"GREATEST(agent_seq_watermark, {_LEGACY_COL})"
	frappe.db.sql(
		f"""
		UPDATE `tab{MSG}`
		SET agent_seq_watermark = {effective}, {_LEGACY_COL} = {effective}
		WHERE agent_seq_watermark <> {_LEGACY_COL}
		"""
	)


def stamp_watermark(message_name: str, watermark: int) -> None:
	"""Write the watermark to the new column AND, while it exists, the legacy one
	(dual-write: keeps a rollback's old-code readers correct). Does not touch
	``modified`` — matches the previous ``update_modified=False`` write."""
	cols = "agent_seq_watermark=%(w)s"
	if has_legacy_column():
		cols += f", {_LEGACY_COL}=%(w)s"
	frappe.db.sql(
		f"UPDATE `tab{MSG}` SET {cols} WHERE name=%(n)s",
		{"w": int(watermark), "n": message_name},
	)


def wm_expr(alias: str = "") -> str:
	"""SQL expression for the EFFECTIVE watermark during the transition: while the
	legacy column exists, an old-code writer may have stamped only it, so take
	``GREATEST`` of the pair; afterwards, just the new column. ``alias`` is the
	table alias prefix including the dot (e.g. ``"m."``)."""
	col = f"{alias}agent_seq_watermark"
	if has_legacy_column():
		return f"GREATEST({col}, {alias}{_LEGACY_COL})"
	return col
