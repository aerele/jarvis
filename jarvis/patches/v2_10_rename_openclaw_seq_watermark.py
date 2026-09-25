"""Preserve the rename patch's identity for installations upgrading directly.

The shared reconciliation also runs after every normal migration, covering
sites whose Patch Log already records this patch. It retains the larger fence
in both columns for mixed-version workers and rollback.
"""


def execute():
	from jarvis.chat.seq_watermark import reconcile_watermarks

	reconcile_watermarks()
