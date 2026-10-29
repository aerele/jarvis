"""Composite index on Deleted Document (deleted_doctype, deleted_name) for the Chat
question audit guards (jarvis.chat.approval_reviewers).

The Version / Deleted Document permission conditions look up an approval's, a
comment's or a file's archives by (deleted_doctype, deleted_name). Unindexed, a
System Manager's generic count of either doctype scans the archive table once per
candidate row. add_index is idempotent; fresh sites get it from after_install.
"""


def execute():
	from jarvis.chat.approval_reviewers import ensure_audit_lookup_index

	ensure_audit_lookup_index()
