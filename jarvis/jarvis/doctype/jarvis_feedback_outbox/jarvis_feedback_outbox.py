"""Jarvis Feedback Outbox - the bench's copy of one feedback answer until admin
has acknowledged it. Written, delivered, retried and pruned only by
``jarvis.chat.feedback_outbox``; see that module for the states and bounds.

Never versioned (``track_changes: 0``): high-churn delivery state, not a
business record.
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisFeedbackOutbox(NotRenamable, Document):
	def autoname(self) -> None:
		"""The docname IS the feedback identity, so the primary key closes the
		check-then-insert race in ``feedback_outbox.accept``: two taps on the
		same reply collide on insert instead of storing two rows."""
		from jarvis.chat.feedback_outbox import outbox_name

		self.name = outbox_name(self.kind, self.ref, self.user)
