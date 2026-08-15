"""Security review PART 2 TASK 10: migrate Jarvis Custom Skill onto the
{User, Role, Org} scope ladder (default User).

Legacy rows carried scope ∈ {Org, Personal} (default Org). The ladder renames
"Personal" -> "User" (a private, owner-only skill) and treats a NULL/empty
scope (pre-scope-field rows) as its historical meaning, Org. Org rows stay Org.
Idempotent.
"""

import frappe


def execute():
	if not frappe.db.has_column("Jarvis Custom Skill", "scope"):
		return
	# Personal is the pre-ladder spelling of User.
	frappe.db.sql("UPDATE `tabJarvis Custom Skill` SET `scope` = 'User' WHERE `scope` = 'Personal'")
	# Pre-scope rows (NULL/empty) meant Org.
	frappe.db.sql("UPDATE `tabJarvis Custom Skill` SET `scope` = 'Org' WHERE `scope` IS NULL OR `scope` = ''")
