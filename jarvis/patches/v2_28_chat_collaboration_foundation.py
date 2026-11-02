"""Additive post-model-sync backfill. Never activate collaboration or upgrade grants."""

import frappe


def execute():
	frappe.db.sql("""UPDATE `tabJarvis Conversation Share`
		SET access_mode='View' WHERE access_mode IS NULL OR access_mode=''""")
	frappe.db.sql("""UPDATE `tabJarvis Conversation Share`
		SET access_revision=1 WHERE access_revision IS NULL OR access_revision < 1""")
