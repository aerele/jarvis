"""Jarvis Agent Run DocType controller.

One row per auditor/operator execution (manual or scheduled), modeled on
``Jarvis Macro Run``. Rows are server-generated (the scheduler / the
persistence bridge insert them with ``ignore_permissions`` and reassign the
owner to the installation owner), so the ``All`` role gets ``if_owner`` READ
only — the customer sees their own run history but cannot forge rows.
``status=partial`` marks a scan that hit the turn envelope (never masquerades
as ``completed``).
"""

from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import NotRenamable

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
	"assessment_config_json",
	"input_snapshot_json",
	"assessment_output_json",
	"permission_profile",
	"visibility_receipt_json",
)

DEPOSIT_AGENT = "statutory-payroll-deposit-status"
_NO_TRUSTED_DEPOSIT_REMOVAL = object()
_trusted_deposit_removal_installation = ContextVar(
	"jarvis_deposit_run_removal_installation", default=_NO_TRUSTED_DEPOSIT_REMOVAL
)


@contextmanager
def trusted_deposit_run_removal(installation: str | None):
	"""Allow one server-owned uninstall/retention deletion of a Deposit Run.

	This is a process-local capability, not a document flag supplied by a Desk or
	REST caller.  The caller must first remove the linked immutable dashboard via
	the matching server helper.
	"""
	token = _trusted_deposit_removal_installation.set(installation)
	try:
		yield
	finally:
		_trusted_deposit_removal_installation.reset(token)


class JarvisAgentRun(NotRenamable, Document):
	def validate(self):
		self._guard_immutable_launch_fields()
		self._guard_immutable_deposit_report_link()

	def on_trash(self):
		self._guard_deposit_run_removal()

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

	def _guard_immutable_deposit_report_link(self):
		"""Keep a Deposit report link and agent identity immutable on every save.

		The stored identity is checked as well as the incoming one.  Without both,
		a privileged caller could first relabel a Deposit Run as a generic agent,
		then clear or repoint its Dashboard link and make the report editable.
		"""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		stored_agent = before.get("agent")
		stored_dashboard = before.get("dashboard")
		is_deposit = stored_agent == DEPOSIT_AGENT or self.get("agent") == DEPOSIT_AGENT
		if not is_deposit:
			return
		if self.get("agent") != stored_agent:
			frappe.throw(_("A payroll deposit run cannot change its agent identity."), frappe.PermissionError)
		if self.get("dashboard") != stored_dashboard:
			frappe.throw(_("A payroll deposit run cannot change its dashboard link."), frappe.PermissionError)

	def _guard_deposit_run_removal(self):
		"""Reject Desk/REST/administrator deletion of a Deposit Run.

		Only a process-local server capability can remove the Run.  It must match
		the current installation (or ``None`` for the detached retention purge),
		and the Dashboard link must already be gone through the server cleanup path.
		"""
		if self.get("agent") != DEPOSIT_AGENT:
			return
		expected_installation = _trusted_deposit_removal_installation.get()
		if (
			expected_installation is _NO_TRUSTED_DEPOSIT_REMOVAL
			or expected_installation != self.get("installation")
			or self.get("dashboard")
		):
			frappe.throw(
				_("This payroll deposit run is server-retained and cannot be deleted directly."),
				frappe.PermissionError,
			)


def on_doctype_update():
	"""Composite (owner, started_at, creation) index.

	Neither ``owner`` nor ``started_at`` carries a default Frappe index (only
	``name``, ``creation`` and ``modified`` do), so ``list_runs_page``
	(agents_api.py:1349) -- the Runs tab's ONLY paging query, called on every
	page load -- forces a full-table scan sorted in Python: it filters
	``{"owner": me}`` (+ optional agent/status) and always sorts
	``started_at desc, creation desc``. This composite lets MariaDB satisfy
	both the filter and the sort from one index (a backward scan gives the
	DESC/DESC order); the owner-only case still benefits since started_at and
	creation are trailing columns of the same index.

	``frappe.db.add_index`` no-ops when the index already exists, so repeated
	migrates are harmless.
	"""
	frappe.db.add_index(
		"Jarvis Agent Run",
		["owner", "started_at", "creation"],
		index_name="owner_started_creation_index",
	)
	# finished_at index: DocType JSON search_index=1 (updatedb-managed, survives migrate).
