"""Drop the three dev-only operator-tab fields that the unified-local-dev work removed.

Safe: those fields were only ever written by the retired bootstrap module (now deleted)
and only read by the retired push module (also deleted). In production they were never
populated.
"""

import frappe

_DROP_COLUMNS = (
	"ALTER TABLE `tabJarvis Settings` DROP COLUMN `agent_llm_key_path`",
	"ALTER TABLE `tabJarvis Settings` DROP COLUMN `agent_config_path`",
	"ALTER TABLE `tabJarvis Settings` DROP COLUMN `agent_compose_dir`",
)


def execute():
	for statement in _DROP_COLUMNS:
		try:
			frappe.db.sql(statement)
		except Exception:
			pass  # column may already be gone
	frappe.db.commit()
