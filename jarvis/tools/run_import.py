"""Run a Data Import: load a file straight into a DocType, as-is.

GATED + DESTRUCTIVE: an import creates (or overwrites) many records and is NOT reversible,
so this never auto-applies - it always parks for a human confirmation card built from the
same preview ``preview_import`` shows. The park refuses up front if the file has blocking
problems or the caller can't run imports, so a confirmed card always corresponds to an
import that can actually proceed.

On confirm it stages a ``Data Import`` (as the calling user - Frappe's own gate requires
Data Import create permission, usually System Manager) and starts it; the import runs in
the background. Only for loading a file AS-IS - for data that needs any transformation
before it becomes records, use ``create_doc(docs=[...])`` instead.
"""

import json

import frappe
from frappe.utils.scheduler import is_scheduler_inactive

from jarvis import compat
from jarvis.exceptions import FeatureDisabledError, InvalidArgumentError
from jarvis.tools._import_preview import build_import_preview


def run_import(
	doctype: str,
	file_url: str | None = None,
	filename: str | None = None,
	import_type: str = "Insert New Records",
	mapping: dict | None = None,
	submit_after_import: bool = False,
) -> dict:
	"""Stage and start a Data Import. Runs only after the human confirms the card.

	Identify the file by ``file_url`` (exact, preferred) or ``filename``. ``import_type``
	is one of "Insert New Records" / "Update Existing Records" / "Insert or Update
	Records". ``mapping`` optionally forces columns onto fields. ``submit_after_import``
	submits each created record (submittable doctypes only; default: leave as Draft).
	"""
	if frappe.conf.get("jarvis_import_disabled"):
		raise FeatureDisabledError(
			"imports are switched off on this site right now; ask an administrator to re-enable them."
		)

	# Belt: recompute the preview AS THE CALLER and re-check blocking. The park already
	# refused a blocking import, but the file (or a referenced record) could have changed
	# since park - never start an import that is doomed to import nothing.
	prev = build_import_preview(
		doctype=doctype,
		file_url=file_url,
		filename=filename,
		import_type=import_type,
		mapping=mapping,
	)
	if not prev.get("importable"):
		raise InvalidArgumentError(prev.get("reason") or "this doctype does not allow import")
	blocking = (prev.get("warnings") or {}).get("blocking") or []
	if blocking:
		reasons = "; ".join(b.get("message", "") for b in blocking if b.get("message"))
		raise InvalidArgumentError(f"the import can't run - {reasons}")

	# Refuse cleanly BEFORE creating anything if the scheduler is paused, so a staged
	# import never sits Pending with nothing to run it. Test / developer_mode run inline.
	run_now = compat.in_test() or frappe.conf.developer_mode
	if not run_now and is_scheduler_inactive():
		# FeatureDisabledError (not InvalidArgumentError): a paused scheduler is an ops
		# condition, so the model must NOT be nudged to "check the fields and retry" - the
		# fix is an administrator enabling the scheduler, which its hint conveys.
		raise FeatureDisabledError(
			"the import can't start because the site scheduler is paused; ask an "
			"administrator to enable it, then try again."
		)

	# Stage the Data Import AS THE CALLER (no ignore_permissions): validate() enforces the
	# import gate AND Data Import create = System Manager, which is the elevation guard.
	di = frappe.new_doc("Data Import")
	di.reference_doctype = doctype
	di.import_type = import_type
	di.import_file = prev["resolved_file_url"]
	# submit_after_import only means something on a submittable target (else it silently
	# no-ops); gate it so the DI flag matches what the card told the user.
	di.submit_after_import = 1 if (submit_after_import and prev.get("submittable")) else 0
	di.mute_emails = 1
	if prev.get("template_options"):
		di.template_options = prev["template_options"]
	di.insert()

	# Commit BEFORE starting the import: start_import enqueues a background job that reloads
	# the Data Import by name; an uncommitted row would race the worker to a "does not
	# exist". _dispatch_and_wrap guards the (now-gone) savepoint, so committing here is safe.
	frappe.db.commit()

	started = di.start_import()  # enqueues (prod) or runs inline (test / developer_mode)

	records = prev.get("total_records")
	return {
		"data_import": di.name,
		"status": "queued" if started else "already_running",
		"total_records": records,
		"total_rows": prev.get("total_rows"),
		"note": (
			f"Import of {records} {doctype} record(s) from {prev['file']['name']} has "
			"started in the background. Ask me to check on it."
		),
	}
