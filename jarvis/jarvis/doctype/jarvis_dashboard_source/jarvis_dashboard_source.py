"""Jarvis Dashboard Source — child row of Jarvis Dashboard (one declared data
source: name + tool + JSON spec). Validation lives in the parent controller so
every row is checked in dashboard context (caps, name uniqueness, per-tool spec
shape)."""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisDashboardSource(NotRenamable, Document):
	pass
