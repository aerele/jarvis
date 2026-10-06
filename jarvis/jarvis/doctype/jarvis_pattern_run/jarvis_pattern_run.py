"""Jarvis Pattern Run DocType controller.

One row per nightly (or manual) behavioural-learning run: the read-audit
record of the Administrator-level analysis session (which doctypes were read,
detectors run/skipped with reasons, rows scanned, errors, coverage note).
Rows are written EXCLUSIVELY by the learning engine (jarvis/learning/) under
its fenced-write allowlist - System Manager gets read-only Desk access, no
create/write/delete. ``status=Partial`` marks a run that hit the window end
or the row budget (never masquerades as ``Completed``).
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPatternRun(NotRenamable, Document):
	pass
