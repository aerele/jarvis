import frappe


def execute():
	"""Jarvis Approval -> Jarvis Approval Request.

	Order-proof: when this runs BEFORE model sync, a straight rename_doc
	works. When it runs AFTER sync (the new doctype already created from
	files), move the rows across and drop the orphaned old doctype - the
	two tables share an identical column set.
	"""
	old_dt, new_dt = "Jarvis Approval", "Jarvis Approval Request"
	if not frappe.db.table_exists(old_dt):
		return
	if not frappe.db.exists("DocType", new_dt):
		frappe.rename_doc("DocType", old_dt, new_dt, force=True)
		return
	# Sync won the race: copy rows (identical schema), then drop the old.
	cols = [c for c in frappe.db.get_table_columns(old_dt)]
	frappe.qb.into(new_dt).columns(*cols).from_(frappe.qb.DocType(old_dt)).select(*cols).ignore().run()
	if frappe.db.exists("DocType", old_dt):
		frappe.delete_doc("DocType", old_dt, force=True, ignore_permissions=True)
	elif frappe.db.table_exists(old_dt):
		frappe.db.sql_ddl(f"drop table `tab{old_dt}`")
