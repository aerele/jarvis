"""Jarvis Agent Run DocType controller.

One row per auditor/operator execution (manual or scheduled), modeled on
``Jarvis Macro Run``. Rows are server-generated (the scheduler / the
persistence bridge insert them with ``ignore_permissions`` and reassign the
owner to the installation owner), so the ``All`` role gets ``if_owner`` READ
only — the customer sees their own run history but cannot forge rows.
``status=partial`` marks a scan that hit the turn envelope (never masquerades
as ``completed``).
"""

import frappe
from frappe import _
from frappe.model.document import Document

# PP-5: launch-time facts stamped once when the run starts. Unlike the listing /
# installation versions (mutable), these are the run's immutable provenance — the
# bundle it actually executed, whether it ran in shadow, and the human who
# triggered it. The engine stamps them at launch and updates run lifecycle via
# raw ``db.set_value`` (which bypasses this controller); any ORM save that tries
# to change a stamped launch fact is refused.
#
# JF-017 extends the same protection to the run's CAPABILITY CONTRACT (which tools
# it may call, its nature, its declared writes). That contract is the run's
# authorization, so it has to be at least as immutable as its provenance: if it
# could be edited after launch, snapshotting it would buy nothing over reading the
# mutable listing.
_IMMUTABLE_LAUNCH_FIELDS = (
	"bundle_version",
	"preparation_mode",
	"initiating_human",
	"capability_contract",
	"tools_allow_json",
	"capability_nature",
	"capability_writes_json",
)


class JarvisAgentRun(Document):
	def validate(self):
		self._guard_immutable_launch_fields()

	def _guard_immutable_launch_fields(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		for field in _IMMUTABLE_LAUNCH_FIELDS:
			stored = before.get(field)
			if stored and self.get(field) != stored:
				frappe.throw(
					_("{0} is stamped immutably at launch (PP-5) and cannot be changed.").format(field),
					frappe.PermissionError,
				)
