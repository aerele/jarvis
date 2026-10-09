"""Jarvis Custom Skill Allowed Role - child rows of ``Jarvis Custom Skill.allowed_roles``.

One Role per row (exact precedent: ``Jarvis Agent Allowed Role``). An EMPTY
table means the skill is visible to everyone (all pre-existing skills stay
backward compatible); a non-empty table means only the owner, shared-with
users and holders of at least one of these roles (or System Manager) may
see/invoke the skill. Enforcement is bench-side and instruction-level - a
soft boundary, not a confidentiality boundary (plan section 6.6).
"""

from frappe.model.document import Document

from jarvis.permissions import ChangedThroughParentOnly, NotRenamable


class JarvisCustomSkillAllowedRole(ChangedThroughParentOnly, NotRenamable, Document):
	pass
