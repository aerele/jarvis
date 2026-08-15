"""Copy openclaw_seq_watermark into the renamed agent_seq_watermark column.

The snapshot-recovery watermark field was renamed openclaw_seq_watermark ->
agent_seq_watermark (white-label). Frappe model-sync ADDS the new column
zero-filled and leaves the old column in place (it never drops a removed field's
column), so this post_model_sync patch copies the values across.

Clobber-safe + idempotent: it only fills a row whose new column is still 0 from a
real old value (> 0), so a re-run can never overwrite a live watermark with a
stale one. The old openclaw_seq_watermark column is RETAINED this release as the
rollback net; a later contract patch drops it once the rename is proven.
"""

import frappe

MSG = "Jarvis Chat Message"


def execute():
	# Fresh install: the JSON only ever shipped agent_seq_watermark, so the old
	# column never existed and there is nothing to copy.
	if "openclaw_seq_watermark" not in frappe.db.get_table_columns(MSG):
		return
	frappe.db.sql(
		"""
		UPDATE `tabJarvis Chat Message`
		SET agent_seq_watermark = openclaw_seq_watermark
		WHERE agent_seq_watermark = 0 AND openclaw_seq_watermark > 0
		"""
	)
