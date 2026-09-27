# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class JarvisUserMemory(Document):
	pass


def get_permission_query_conditions(user=None, doctype=None) -> str:
	"""Scope Jarvis User Memory list/report reads to the caller's own rows.

	The remember/recall tools key on ``frappe.session.user`` directly (and run
	under ``impersonate(user)`` via ``jarvis.api.call_tool``), so this hook is
	defense-in-depth for any OTHER read path (Desk list, REST ``get_list``): one
	user of a tenant can never enumerate a colleague's memory. Applied to every
	caller including System Manager - an admit-all audit read goes through the DB,
	not the ORM list."""
	user = user or frappe.session.user
	return f"`tabJarvis User Memory`.`user` = {frappe.db.escape(user)}"
