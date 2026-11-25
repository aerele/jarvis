"""Move a direct ChatGPT-login workspace from gpt-5.6-terra to gpt-5.6-sol.

Owner's call, 2026-11-24: Sol is the ChatGPT default (admin patch v1_59 flags it in
the catalog, which only decides what a NEW connection starts on). A workspace that
connected earlier saved Terra, and a reconnect keeps the saved model, so this patch
moves it. Only Terra moves: Terra was the default, so a workspace on Luna, Astra or
gpt-5.5 picked it and keeps it. Conversation pins are user picks and are kept too.

DIRECT workspaces only (``compute_pool_mode`` False). Each chat turn sends
``llm_model`` and the container already renders every ChatGPT model (fleet
``oauth_models``), so the workspace answers on Sol from its next message with no
sync and no container restart. Its lone pool row, if it has one, moves too: Settings
copies that row into ``llm_model`` on the next save, so leaving it on Terra would
undo the move.

POOL workspaces are left alone. A pool's model is the container's own config
primary, and fleet restarts the container to change it (a roster change cannot hot
reload: fleet ``_pool_apply_can_hot_reload``). Measured on the dev tenant 2026-11-25:
a deploy-time resync restarted the container and left the apply pending. Pool
customers pick Sol in the model picker or on their next save.

Data only: no document hooks, no sync, no admin call, so ``bench migrate`` never
touches a container. Sol needs ChatGPT Plus or higher; a Free or Go account falls
back to the fleet's gpt-5.6-terra leg (fleet compose.CODEX_FALLBACK_MODEL).

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


def execute():
	frappe.db.savepoint(_SAVEPOINT)
	try:
		_move_direct_workspace_to_sol()
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title="ChatGPT default to Sol: patch skipped", message=frappe.get_traceback())


def _move_direct_workspace_to_sol() -> None:
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	if not _direct_lane_is_openai_subscription():
		return
	if compute_pool_mode(frappe.get_single(SETTINGS)):
		frappe.logger().info("ChatGPT default to Sol: pool workspace left on its saved model")
		return
	model = (frappe.db.get_single_value(SETTINGS, "llm_model") or "").strip()
	if model != OLD_DEFAULT:
		return
	frappe.db.set_single_value(SETTINGS, "llm_model", NEW_DEFAULT)
	moved_rows = [r.name for r in _openai_subscription_pool_rows() if (r.model or "").strip() == OLD_DEFAULT]
	for name in moved_rows:
		frappe.db.set_value(POOL_ROW, name, "model", NEW_DEFAULT, update_modified=False)
	frappe.clear_cache(doctype=SETTINGS)
	frappe.logger().info("ChatGPT default to Sol: direct workspace moved, rows=%s", moved_rows)
