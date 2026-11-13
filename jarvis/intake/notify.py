"""Document Intake: the one bell notifier for the uploader.

An uploader may have no Jarvis access (the spec's own persona), so the bell is the only
place they learn that a file was drafted, needs a reviewer, failed or was skipped.
"""

import frappe
from frappe.utils import escape_html


def notify(user: str, subject: str, doc=None) -> None:
	"""Bell notification to ``user``, in its own savepoint so a failure never undoes the
	draft or the status change that triggered it. The desk bell renders the subject as
	HTML, so it is escaped. ``doc`` (optional) is the record the bell opens."""
	savepoint = "jarvis_intake_notice"
	try:
		frappe.db.savepoint(savepoint)
		log = {
			"doctype": "Notification Log",
			"for_user": user,
			"type": "Alert",
			"subject": escape_html(subject[:140]),
		}
		if doc:
			log.update({"document_type": doc.doctype, "document_name": doc.name})
		frappe.get_doc(log).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="jarvis.intake.notify_failed", message=frappe.get_traceback())


def row_ended(row, summary: str) -> None:
	"""Tell the uploader that ``row`` ended Failed or Skipped, with its SAFE summary (never
	``detail``, which is for managers). The file name leads so a burst of failures is
	readable. Not for the duplicate Skipped row: re-uploading a file is not news."""
	if not row.get("event_user"):
		return
	file_name = frappe.db.get_value("File", row.source_file, "file_name") if row.get("source_file") else None
	notify(row.event_user, f"{file_name}: {summary}" if file_name else summary)
