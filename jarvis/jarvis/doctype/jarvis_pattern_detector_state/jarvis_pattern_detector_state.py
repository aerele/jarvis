"""Jarvis Pattern Detector State DocType controller.

One row per detector registry id: enable flag, stream watermarks and the
data_starved / not_applicable readiness signals the preflight and the board
surface. Seeded best-effort by ``jarvis.learning.bootstrap.after_migrate``
and written only by engine code (SM read-only from Desk).
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPatternDetectorState(NotRenamable, Document):
	pass
