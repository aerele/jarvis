"""Drop the three dev-only operator-tab fields that the unified-local-dev work removed.

Safe: those fields were only ever written by the retired bootstrap module (now deleted)
and only read by the retired push module (also deleted). In production they were never
populated.
"""

import frappe

_FIELDS = [
	"agent_llm_key_path",
	"agent_config_path",
	"agent_compose_dir",
]


def execute():
	for field in _FIELDS:
		try:
			frappe.db.sql(f"ALTER TABLE `tabJarvis Settings` DROP COLUMN `{field}`")
		except Exception:
			pass  # column may already be gone
	frappe.db.commit()
