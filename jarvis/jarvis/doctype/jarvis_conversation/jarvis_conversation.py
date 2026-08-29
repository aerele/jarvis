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
		self._guard_auto_apply_enable()
		self._guard_file_box_enable()
		self._guard_skip_confirmation_enable()

	def _guard_auto_apply_enable(self):
		"""Defense-in-depth backstop for the admin-gated ``auto_apply`` flag
		(issue #186, Task 4).

		``jarvis.chat.api.set_auto_apply`` already enforces the System
		Manager requirement for ENABLING, but it writes via
		``frappe.db.set_value``, which bypasses the controller entirely -
		that path stays correct on its own and is unaffected by this method.

		This guards the OTHER path: the doctype grants "All" write with
		if_owner and no permlevel/validate on the field, so a non-admin
		owner could flip ``auto_apply`` 0 -> 1 through any generic
		``doc.save()`` route (``update_doc``, ``frappe.client.set_value``,
		desk) without ever touching ``set_auto_apply`` - defeating the
		"a non-admin can never turn it on" guarantee. Block that transition
		here, at the data layer, regardless of which code path drove it.

		Only the 0/unset -> 1 transition is gated. Disabling (1 -> 0) and
		no-op saves (value unchanged, e.g. editing the title) are always
		allowed for the owner.
		"""
		if not self.auto_apply:
			return  # not being enabled

		previous = self.get_doc_before_save()
		was_on = bool(previous.auto_apply) if previous else False
		if was_on:
			return  # already on - no transition, nothing to gate

		# PART 4 REVISED, TASK 45: the admin tier (Jarvis Admin / System Manager),
		# matching the widened set_auto_apply enable gate.
		if not has_jarvis_admin_access(frappe.session.user):
			frappe.throw(
				_("Enabling auto-apply requires a Jarvis Admin or System Manager role."),
				frappe.PermissionError,
			)

	def _guard_file_box_enable(self):
		"""``file_box`` grants the same create/update confirm-card bypass as
		``auto_apply`` (destructive ops still park), so it must be just as
		unforgeable: a non-admin owner must not flip it 0 -> 1 through a
		generic ``doc.save()`` / ``update_doc`` to self-grant auto-apply.
		The legitimate enabler is the server-side File Box drop path, which
		writes via ``frappe.db.set_value`` and bypasses this controller.
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
		not just the create/update ``auto_apply`` covers). The one legitimate enabler
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
