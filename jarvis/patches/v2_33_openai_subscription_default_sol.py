"""Move a ChatGPT-subscription workspace from gpt-5.6-terra to gpt-5.6-sol.

Owner's call, 2026-11-24: Sol is the ChatGPT default (admin patch v1_59 flags it in
the catalog, which only decides what a NEW connection starts on). A workspace that
connected earlier saved Terra, and a reconnect keeps the saved model, so this patch
moves it. Only Terra moves: Terra was the default, so a workspace on Luna, Astra or
gpt-5.5 picked it and keeps it. Conversation pins are user picks and are kept too.

Two places hold the workspace model:

- ``Jarvis Settings.llm_model``. Each chat turn sends it, and the container already
  renders every ChatGPT model (fleet ``oauth_models``), so a direct ChatGPT login
  answers on Sol from its next message with no resync.
- an enabled ChatGPT subscription pool row. The container routes a pool itself, so
  the new row must be pushed: a resync job is queued for after the migrate. It does
  what the customer's Resync button does, and it skips itself if it ever runs inside
  the migrate (Frappe runs a job inline when Redis is down), so a customer's
  ``bench migrate`` never calls admin or restarts a container.

Sol needs ChatGPT Plus or higher. A Free or Go account fails on Sol and the fleet's
gpt-5.6-terra fallback leg answers instead (fleet compose.CODEX_FALLBACK_MODEL).

Never raises: a failure is rolled back to the savepoint and logged, so a tenant's
migrate always finishes. Idempotent: a second run finds no Terra to move.
"""

import frappe

from jarvis.patches.v2_19_clear_retired_openai_subscription_pins import (
	_direct_lane_is_openai_subscription,
	_openai_subscription_pool_rows,
)

OLD_DEFAULT = "gpt-5.6-terra"
NEW_DEFAULT = "gpt-5.6-sol"
SETTINGS = "Jarvis Settings"
POOL_ROW = "Jarvis LLM Pool Model"
_SAVEPOINT = "chatgpt_default_sol"
_RESYNC_JOB = "jarvis.patches.v2_33_openai_subscription_default_sol.resync_after_default_move"


def execute():
	frappe.db.savepoint(_SAVEPOINT)
	try:
		moved_rows = _move_to_sol()
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title="ChatGPT default to Sol: patch skipped", message=frappe.get_traceback())
		return
	if moved_rows:
		_queue_resync()


def _move_to_sol() -> list[str]:
	"""Repoint the workspace default and the pool rows. Returns the moved row names."""
	rows = _openai_subscription_pool_rows()
	moved_rows = _repoint_pool_rows(rows)
	moved_default = _repoint_workspace_default(rows, moved_rows)
	if moved_default or moved_rows:
		frappe.clear_cache(doctype=SETTINGS)
	frappe.logger().info(
		"ChatGPT default to Sol: workspace default moved=%s, pool rows moved=%s", moved_default, moved_rows
	)
	return moved_rows


def _repoint_pool_rows(rows: list) -> list[str]:
	"""Terra rows move to Sol, unless the pool already has a Sol row: two rows of one
	model would not save, so that pool already offers Sol and is left as it is."""
	if any((r.model or "").strip() == NEW_DEFAULT for r in rows):
		return []
	moved = []
	for row in rows:
		if (row.model or "").strip() != OLD_DEFAULT:
			continue
		frappe.db.set_value(POOL_ROW, row.name, "model", NEW_DEFAULT, update_modified=False)
		moved.append(row.name)
	return moved


def _repoint_workspace_default(rows: list, moved_rows: list[str]) -> bool:
	"""``llm_model`` moves when the direct lane is a ChatGPT login, or when it mirrors
	a pool whose first row just moved (Settings copies models[0] into llm_model)."""
	model = (frappe.db.get_single_value(SETTINGS, "llm_model") or "").strip()
	if model != OLD_DEFAULT:
		return False
	if not (_direct_lane_is_openai_subscription() or _first_row_moved(moved_rows)):
		return False
	frappe.db.set_single_value(SETTINGS, "llm_model", NEW_DEFAULT)
	return True


def _first_row_moved(moved_rows: list[str]) -> bool:
	if not moved_rows:
		return False
	settings = frappe.get_single(SETTINGS)
	enabled = [r for r in settings.get("models") or [] if r.enabled]
	return bool(enabled) and enabled[0].name in moved_rows


def _queue_resync() -> None:
	try:
		frappe.enqueue(
			_RESYNC_JOB,
			queue="long",
			job_id="jarvis::chatgpt-default-sol-resync",
			deduplicate=True,
			enqueue_after_commit=True,
		)
	except Exception:
		frappe.log_error(title="ChatGPT default to Sol: resync not queued", message=frappe.get_traceback())


def resync_after_default_move() -> None:
	"""Push the moved pool to the container, like the customer's Resync button."""
	if frappe.flags.in_migrate:
		# Frappe ran this inline because Redis is down: no admin call inside a migrate.
		# The pool reaches the container on the next settings save or Resync.
		frappe.logger().info("ChatGPT default to Sol: resync skipped inside migrate")
		return
	from jarvis.account import _has_llm_config
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import request_resync
	from jarvis.onboarding import _has_admin_credentials

	try:
		settings = frappe.get_single(SETTINGS)
		if _has_llm_config(settings) and _has_admin_credentials(settings):
			request_resync(settings)
	except Exception:
		frappe.log_error(title="ChatGPT default to Sol: resync failed", message=frappe.get_traceback())
