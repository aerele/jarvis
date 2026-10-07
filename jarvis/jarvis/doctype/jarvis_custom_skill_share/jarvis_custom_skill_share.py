"""Jarvis Custom Skill Share — child row: one user a skill is shared with."""

from frappe.model.document import Document

from jarvis.permissions import ChangedThroughParentOnly, NotRenamable


class JarvisCustomSkillShare(ChangedThroughParentOnly, NotRenamable, Document):
	pass
