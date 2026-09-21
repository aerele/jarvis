"""Jarvis Conversation DocType controller.

One row per chat thread, owned by the Frappe user who created it. The
agent session_key is populated on the first agent turn and reused for
subsequent turns so agent-side context is preserved within a thread.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import has_jarvis_admin_access


class JarvisConversation(Document):
	def before_insert(self):
		if not self.last_active_at:
			self.last_active_at = frappe.utils.now()
		if not self.status:
			self.status = "Active"

	def validate(self):
		self._guard_file_box_enable()
		self._guard_skip_confirmation_enable()
		self._guard_skill_autorun_enable()
		self._guard_request_autorun_enable()

	def _guard_file_box_enable(self):
		"""``file_box`` grants a create/update confirm-card bypass (destructive ops
		still park) for an unattended File Box run - the ONE remaining direct-apply
		path now that admin Auto-Apply is removed - so it must be unforgeable: a
		non-admin owner must not flip it 0 -> 1 through a generic ``doc.save()`` /
		``update_doc`` to self-grant the bypass. The legitimate enabler is the
		server-side File Box drop path, which writes via ``frappe.db.set_value`` and
		bypasses this controller.
		"""
		if not self.file_box:
			return
		previous = self.get_doc_before_save()
		if previous and bool(previous.file_box):
			return
		# PART 4 REVISED, TASK 45: the admin tier (Jarvis Admin / System Manager).
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Enabling File Box mode requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)

	def _guard_skip_confirmation_enable(self):
		"""``skip_confirmation`` is the flag the write-confirmation gate reads to run
		a macro's writes uncarded (the BROAD covered set - run_method/send_email/etc.,
		not just the create/update pair ``file_box`` covers). The one legitimate enabler
		is an armed macro run (``jarvis.chat.macros.run_macro`` -> raw
		``frappe.db.set_value``, which bypasses this controller).

		This guards every OTHER path: the field is owner-writable with no
		permlevel, so a non-admin owner must not flip it 0 -> 1 through a generic
		``doc.save()`` / ``update_doc`` / ``frappe.client.set_value`` and turn their
		own chat into an uncarded-write conversation. This is LOAD-BEARING (the gate
		reads THIS field), not mere defense-in-depth. Only 0/unset -> 1 is gated;
		disabling and no-op saves stay free for the owner.
		"""
		if not self.skip_confirmation:
			return
		previous = self.get_doc_before_save()
		if previous and bool(previous.skip_confirmation):
			return
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Enabling skip-confirmation requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)

	def _guard_skill_autorun_enable(self):
		"""``skill_autorun`` is the flag the write-confirmation gate reads to run an
		approved skill's writes uncarded (the explicit ``_SKILL_AUTORUN_COVERED``
		allowlist - see the "Approve & run" design doc D-COVERED/D-CONTROL). The one
		legitimate enabler is ``approve_and_run`` (a Jarvis Custom Skill armed via its
		own ``allow_approve_run`` guard), and only AFTER the first covered write of the
		declared plan succeeds -> raw ``frappe.db.set_value``, which bypasses this
		controller.

		This guards every OTHER path: the field is owner-writable with no permlevel,
		so a non-admin owner must not flip it 0 -> 1 through a generic ``doc.save()`` /
		``update_doc`` / ``frappe.client.set_value`` and turn their own chat into an
		uncarded-write conversation. This is LOAD-BEARING (the gate reads THIS field),
		not mere defense-in-depth. Only 0/unset -> 1 is gated; disabling and no-op
		saves stay free for the owner."""
		if not self.skill_autorun:
			return
		previous = self.get_doc_before_save()
		if previous and bool(previous.skill_autorun):
			return
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Enabling skill auto-run requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)

	def _guard_request_autorun_enable(self):
		"""``request_autorun`` is the flag the write-confirmation gate reads to run the
		CURRENT request's covered writes uncarded after the user typed 'confirm all' /
		'do everything' (design Layer B). The one legitimate enabler is
		``jarvis.chat.api`` (``_typed_confirmation`` after a sweep, or the upfront
		detector on a compound send) -> raw ``frappe.db.set_value``, which bypasses this
		controller.

		This guards every OTHER path: the field is owner-writable with no permlevel, so a
		non-admin owner must not flip it 0 -> 1 through a generic ``doc.save()`` /
		``update_doc`` / ``frappe.client.set_value`` and turn their own chat into an
		uncarded-write conversation. LOAD-BEARING (the gate reads THIS field). Only
		0/unset -> 1 is gated; disabling and no-op saves stay free for the owner."""
		if not self.request_autorun:
			return
		previous = self.get_doc_before_save()
		if previous and bool(previous.request_autorun):
			return
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Enabling request auto-run requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)
