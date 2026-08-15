"""Jarvis Voice Note DocType controller ("Note", Skills-area rework part 2).

One row per captured note, owned by the user who recorded it (``if_owner``
permission; System Manager gets org-wide read for the Business processing
status). Originally voice-transcript-only; extended with a ``kind`` field
(Text/Voice/Attachment/Link) so the Personalise composer can capture text,
voice, file attachments and links through the same doctype/pipeline
(DESIGN.md section 2.3). Business-context notes are mined by the daily
``jarvis.learning.voice_facts`` sweep into learned-pattern candidates and wiki
updates; Conversation-context notes feed the wiki ingest directly; the sweep
and the immediate-ingest path both read ``extracted_text or transcript`` so
Attachment/Link content is treated the same as a Text/Voice transcript once
extraction has run (Wave B1).

Validation lives here so it runs on every insert/save regardless of whether
the write came from an SPA endpoint (``jarvis.chat.voice_notes_api``,
``jarvis.chat.personalise_api``), the Desk, or a test.
"""

import frappe
from frappe import _
from frappe.model.document import Document

MAX_TRANSCRIPT_LEN = 20000

ALLOWED_KINDS = ("Text", "Voice", "Attachment", "Link")
# Kinds where transcript IS the content (the user's own words); Attachment/
# Link content lives in extracted_text, so transcript is an optional caption.
TRANSCRIPT_REQUIRED_KINDS = ("Text", "Voice")
_URL_SCHEMES = ("http://", "https://")


class JarvisVoiceNote(Document):
	def validate(self):
		self._validate_kind()
		self._validate_transcript()
		self._validate_context()

	def _validate_kind(self):
		kind = self.kind or "Text"
		if kind not in ALLOWED_KINDS:
			frappe.throw(_("Unknown note kind: {0}").format(kind))
		self.kind = kind
		if kind == "Attachment" and not self.attachment:
			frappe.throw(_("An Attachment note must have a file attached."))
		if kind == "Link":
			self.url = (self.url or "").strip()
			if not self.url:
				frappe.throw(_("A Link note must have a URL."))
			if not self.url.lower().startswith(_URL_SCHEMES):
				frappe.throw(_("Link URL must start with http:// or https://."))

	def _validate_transcript(self):
		self.transcript = (self.transcript or "").strip()
		if self.kind in TRANSCRIPT_REQUIRED_KINDS and not self.transcript:
			frappe.throw(_("Transcript is required for Text and Voice notes."))
		if len(self.transcript) > MAX_TRANSCRIPT_LEN:
			frappe.throw(_("Transcript must be at most {0} characters.").format(MAX_TRANSCRIPT_LEN))
		if (self.duration_s or 0) < 0:
			self.duration_s = 0

	def _validate_context(self):
		context_type = self.context_type or "Business"
		if context_type == "Conversation" and not self.conversation:
			frappe.throw(_("A Conversation-context voice note must link a conversation."))
		# Cross-link ownership guard (security review TASK 5): a note may only
		# reference a conversation the caller OWNS. if_owner blocks cross-user
		# READS, but without this a user could still link attacker content into
		# another user's conversation namespace (and its wiki-ingest pipeline).
		# Server writers (ignore_permissions — the API endpoints already verify
		# ownership before insert) and Administrator are exempt.
		if self.conversation and not (
			self.flags.ignore_permissions or frappe.session.user == "Administrator"
		):
			owner = frappe.db.get_value("Jarvis Conversation", self.conversation, "owner")
			if owner and owner != frappe.session.user:
				frappe.throw(
					_("You can only attach notes to your own conversations."),
					frappe.PermissionError,
				)
