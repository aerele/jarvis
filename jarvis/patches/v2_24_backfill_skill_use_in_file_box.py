"""File Box K-D1: "Use in File Box" is opt-out, so every existing Jarvis Custom
Skill starts ON.

The column is added NOT NULL DEFAULT 1, which already fills existing rows on
MariaDB; this makes the backfill explicit (the ``v1_12`` shape). Only NULLs are
touched, so a re-run never clobbers an owner's opt-out (0). Idempotent.
"""

import frappe


def execute():
	if not frappe.db.has_column("Jarvis Custom Skill", "use_in_file_box"):
		return
	frappe.db.sql("UPDATE `tabJarvis Custom Skill` SET `use_in_file_box` = 1 WHERE `use_in_file_box` IS NULL")
