"""Jarvis Pattern Snapshot DocType controller (plan sections 4.4, 6.1).

Append-only monthly aggregates for Access-Log (row-stream) detectors: one row
per (detector_id, period, company), engine-written only (SM read-only from
Desk; the doctype is in the engine's ALLOWED_WRITE_DOCTYPES). Uniqueness of
the triple is enforced by the ``snapshot_key`` unique index; the key is
computed here so every write path (upsert, direct insert) gets the same
constraint - the Jarvis Learned Pattern ``pattern_key`` idiom.
"""

import re

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class JarvisPatternSnapshot(NotRenamable, Document):
	def validate(self):
		if not _PERIOD_RE.match(self.period or ""):
			frappe.throw(f"Jarvis Pattern Snapshot period must be YYYY-MM, got {self.period!r}")
		from jarvis.learning.snapshots import make_snapshot_key

		self.snapshot_key = make_snapshot_key(self.detector_id, self.period, self.company)
