"""Out-of-band push of tenant errors to the admin control plane.

Modeled 1:1 on ``jarvis.chat.usage_push``: self-gating (skip self-hosted /
un-onboarded), best-effort, and it NEVER raises into the scheduler. Runs on the
``*/5`` cron so the admin's Errors feed and Fleet Console badge stay fresh
without ever touching the chat hot path.

Two sources feed one batch:
  - unpushed ``Jarvis Client Error`` rows  (UI errors captured by the reporters)
  - new ``Error Log`` rows past a durable watermark  (code-level exceptions,
    filtered to the jarvis app by ``api_errors.collect_error_log``)

Only scrubbed text is ever sent; scrubbing already happened at capture time.
"""

from __future__ import annotations

import frappe

from jarvis import api_errors
from jarvis.chat.usage_push import _admin_configured
from jarvis.exceptions import AdminAuthError

DT = "Jarvis Client Error"

#: Durable cursor (global DefaultValue) on Error Log.creation, so a lost Redis
#: cache can't make us re-forward exceptions we already sent.
_WATERMARK_KEY = "jarvis_error_log_watermark"

#: Bounds one push. UI rows + the jarvis subset of one Error Log scan.
UI_CAP = 200

#: Delete pushed local rows older than this (daily prune).
PUSHED_RETENTION_DAYS = 7


def push_error_rollup() -> None:
	"""``*/5`` scheduler entry. Self-gating + best-effort; NEVER raises."""
	try:
		from jarvis import selfhost

		if selfhost.is_self_hosted():
			return
		if not _admin_configured():
			return

		ui_names, ui_rows = _collect_ui_errors()
		watermark = frappe.db.get_default(_WATERMARK_KEY) or None
		log_result = api_errors.collect_error_log(watermark)
		errors = ui_rows + log_result["rows"]
		if not errors:
			# Still advance the watermark if the scan moved it past framework noise.
			_advance_watermark(watermark, log_result["watermark"])
			return

		from jarvis import admin_client

		admin_client.push_error_rollup(errors)

		# Push succeeded: retire what we sent.
		if ui_names:
			_mark_pushed(ui_names)
		_advance_watermark(watermark, log_result["watermark"])
		frappe.db.commit()
	except AdminAuthError:
		# Not onboarded / no admin credentials. Nothing to push; not an error.
		return
	except Exception:
		frappe.log_error(
			title="jarvis errors: rollup push failed",
			message=frappe.get_traceback(),
		)


def _collect_ui_errors() -> tuple[list[str], list[dict]]:
	rows = frappe.get_all(
		DT,
		filters={"pushed": 0},
		fields=[
			"name",
			"surface",
			"route",
			"error_code",
			"error_class",
			"message",
			"stack",
			"user",
			"conversation",
			"run_id",
			"fingerprint",
			"count",
			"first_seen",
			"last_seen",
		],
		order_by="last_seen asc",
		limit=UI_CAP,
	)
	names = [r.name for r in rows]
	batch = [
		{
			"kind": "ui",
			"surface": r.surface or "unknown",
			"route": r.route or "",
			"error_code": r.error_code or "",
			"error_class": r.error_class or "",
			"message": r.message or "",
			"stack": r.stack or "",
			"user_ref": r.user or "",
			"conversation_ref": r.conversation or "",
			"run_id": r.run_id or "",
			"fingerprint": r.fingerprint,
			"count": int(r.count or 1),
			"first_seen": str(r.first_seen) if r.first_seen else None,
			"last_seen": str(r.last_seen) if r.last_seen else None,
			"severity": "error",
		}
		for r in rows
	]
	return names, batch


def _mark_pushed(names: list[str]) -> None:
	now = frappe.utils.now_datetime()
	for name in names:
		frappe.db.set_value(DT, name, {"pushed": 1, "pushed_at": now}, update_modified=False)


def _advance_watermark(old: str | None, new: str | None) -> None:
	if new and new != old:
		frappe.db.set_default(_WATERMARK_KEY, new)


def prune_pushed_client_errors() -> int:
	"""Daily: delete forwarded local rows past the retention window. The durable
	record lives in the admin's Jarvis Tenant Error, so the local buffer stays
	small."""
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -PUSHED_RETENTION_DAYS)
	names = frappe.get_all(
		DT,
		filters={"pushed": 1, "pushed_at": ["<", cutoff]},
		pluck="name",
		limit=1000,
	)
	deleted = 0
	for name in names:
		try:
			frappe.delete_doc(DT, name, ignore_permissions=True, force=True, delete_permanently=True)
			deleted += 1
		except Exception:
			frappe.logger("jarvis.client_errors").warning(f"could not prune {name}", exc_info=True)
	if deleted:
		frappe.db.commit()
	return deleted
