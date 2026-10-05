"""Jarvis Learned Pattern Role - child rows of ``Jarvis Learned Pattern.roles``.

One Role per row: the roles the detector computed for a pattern (via
permission enumeration, never hardcoded). They label the review card's role
chips and seed the compiled skill's allowed_roles on Apply. Labels for
organization and audit, not a delivery guarantee.
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisLearnedPatternRole(NotRenamable, Document):
	pass
