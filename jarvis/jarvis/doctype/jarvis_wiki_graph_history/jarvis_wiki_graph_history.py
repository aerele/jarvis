# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisWikiGraphHistory(NotRenamable, Document):
	pass
