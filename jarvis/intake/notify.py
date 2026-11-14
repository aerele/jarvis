"""Document Intake: the one bell notifier for the uploader.

An uploader may have no Jarvis access (the spec's own persona), so the bell is the only
place they learn that a file was drafted, needs a reviewer, failed or was skipped.
"""

import frappe
from frappe.utils import escape_html

ACTIVITY = "Jarvis Trigger Activity"
FILE_BOX_LINK = "/jarvis/files?view=intake"
RETRY_HINT = " Open it and press Retry."
WAIT_HINT = " A reviewer will fill it in."


def notify(user: str, subject: str, doc=None, link: str | None = None) -> None:
	"""Bell notification to ``user``, in its own savepoint so a failure never undoes the
	draft or the status change that triggered it. The desk bell renders the subject as
	HTML, so it is escaped. ``doc`` (optional) is the desk record the bell opens; ``link``
	(optional) is a path that wins over it, for a user who works in the Jarvis app."""
	savepoint = "jarvis_intake_notice"
	try:
		frappe.db.savepoint(savepoint)
		log = {
			"doctype": "Notification Log",
			"for_user": user,
			"type": "Alert",
			"subject": escape_html(subject[:140]),
		}
		if link:
			log["link"] = link
		elif doc:
			log.update({"document_type": doc.doctype, "document_name": doc.name})
		frappe.get_doc(log).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="Jarvis intake: bell failed", message=frappe.get_traceback())


def _opens(user: str, row, board_path: str | None = None) -> dict:
	"""Where a bell takes ``user``. A Jarvis user lands in the app (``board_path`` or the
	File Box Intake list); anyone else (an uploader who only has the desk) gets the desk
	form of ``row``, the one place they can press Retry."""
	from jarvis.permissions import has_jarvis_access

	if has_jarvis_access(user):
		return {"link": board_path or FILE_BOX_LINK}
	return {"doc": frappe._dict(doctype=ACTIVITY, name=row.get("name"))} if row.get("name") else {}


def _file_name(row) -> str | None:
	return frappe.db.get_value("File", row.source_file, "file_name") if row.get("source_file") else None


def _with_hint(head: str, hint: str) -> str:
	"""``head`` cut to leave room for ``hint`` inside the bell's 140 characters."""
	return head[: 140 - len(hint)] + hint


def row_ended(row, summary: str) -> None:
	"""Tell the uploader that ``row`` ended Failed or Skipped, with its SAFE summary (never
	``detail``, which is for managers). The file name leads so a burst of failures is
	readable, the bell opens the row, and it says to press Retry unless retrying cannot
	help. Not for the duplicate Skipped row: re-uploading a file is not news."""
	if not row.get("event_user"):
		return
	from jarvis.intake.capture import PERMANENT_SUMMARIES

	file_name = _file_name(row)
	head = f"{file_name}: {summary}" if file_name else summary
	if summary not in PERMANENT_SUMMARIES:
		head = _with_hint(head, RETRY_HINT)
	notify(row.event_user, head, **_opens(row.event_user, row))


def review_waiting(row, count: int) -> None:
	"""Tell the uploader the file is with a reviewer, and that nothing is needed from them."""
	head = f"Waiting for a reviewer: {count} field(s) need a value."
	notify(row.event_user, _with_hint(head, WAIT_HINT), **_opens(row.event_user, row))


def rejected(row, reason: str) -> None:
	"""Tell the uploader a reviewer rejected the file, with the reviewer's reason."""
	file_name = _file_name(row)
	head = "Rejected by reviewer" + (f": {reason}" if reason else "")
	notify(row.event_user, f"{file_name}: {head}" if file_name else head, **_opens(row.event_user, row))


def review_needed(user: str, request, file_name: str) -> None:
	"""Tell a reviewer a request is waiting. Opens the board for a Jarvis user."""
	subject = f"Review needed: {file_name or request.title}"
	notify(
		user,
		subject,
		**_opens(user, {}, board_path=f"/jarvis/approvals/{request.name}")
		| {"doc": frappe._dict(doctype="Jarvis Approval Request", name=request.name)},
	)
