"""Send an email about a record - one, or a mail-merge batch - through the same
code path as the Desk "New Email" button, so a Communication audit row is
created alongside each queued send.

Wraps ``frappe.core.doctype.communication.email.make`` with ``send_email=True``
so the underlying helper both queues the send AND creates the audit-trail
Communication doc.

Permission: ``email.make`` checks email permission on the reference document
internally; we add an explicit per-record ``read`` floor + a boundary
existence check so the agent gets a clean error on typos and the per-record
check is visible here.

ALWAYS-CONFIRM territory: this fires an email to a real recipient. The
descriptor's agent-facing copy is explicit about that requirement.

Two shapes:

- **Single:** ``send_email(recipients, subject, content, doctype, name, ...)``.
- **Mail-merge batch:** ``send_email(messages=[{doctype, name, recipients,
  subject, content, print_format?, attachments?, cc?, bcc?}, ...])`` -> each message is a
  separate email about a separate doc (its own recipient + body + PDF). Queued
  in ONE atomic savepoint: if any message is invalid, NONE are queued or sent.
  "One message to many people" is already the single call's ``recipients`` list;
  this adds the per-document case.
"""

from __future__ import annotations

import frappe

from jarvis._text import plaintext_to_html
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch


def send_email(
	recipients: str | list[str] | None = None,
	subject: str | None = None,
	content: str | None = None,
	doctype: str | None = None,
	name: str | None = None,
	cc: str | list[str] | None = None,
	bcc: str | list[str] | None = None,
	send_me_a_copy: bool = False,
	print_format: str | None = None,
	messages: list | None = None,
	attachments: list[str] | None = None,
) -> dict:
	"""Send an email about ``doctype/name`` - or a mail-merge batch when
	``messages`` is given.

	``attachments`` accepts existing File IDs or local file URLs returned by
	export tools. Each file must be readable by the caller.

	Single: returns ``{communication_name, recipients, subject, doctype, name}``.
	Batch: returns ``{sent:[{name, recipients, communication_name}], count}``.
	"""
	if messages is not None:
		if attachments is not None:
			raise InvalidArgumentError("put attachments inside each message when using messages")
		return _send_batch(messages)

	cn = _send_one(
		recipients,
		subject,
		content,
		doctype,
		name,
		cc=cc,
		bcc=bcc,
		send_me_a_copy=send_me_a_copy,
		print_format=print_format,
		attachments=resolve_email_attachments(attachments, read_content=True),
	)
	return {
		"communication_name": cn,
		"recipients": recipients,
		"subject": subject,
		"doctype": doctype,
		"name": name,
	}


def _send_one(
	recipients,
	subject,
	content,
	doctype,
	name,
	*,
	cc=None,
	bcc=None,
	send_me_a_copy=False,
	print_format=None,
	attachments=None,
) -> str | None:
	"""Validate + per-record permission + queue ONE email. Returns the
	Communication doc name."""
	if not recipients:
		raise InvalidArgumentError("recipients is required")
	if not subject:
		raise InvalidArgumentError("subject is required")
	if content is None or content == "":
		raise InvalidArgumentError("content is required")
	require_doctype_and_name(doctype, name)
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")
	# Per-record floor; email.make also enforces email permission on the ref doc.
	frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.core.doctype.communication.email import make as _make

	# Communication.content is HTML; the agent composes plain text with \n
	# newlines, so convert or every paragraph collapses into one run-on block on
	# delivery. Shared with the other HTML surfaces - see jarvis._text.
	result = _make(
		doctype=doctype,
		name=name,
		subject=subject,
		content=plaintext_to_html(content),
		sent_or_received="Sent",
		recipients=recipients,
		cc=cc,
		bcc=bcc,
		send_email=True,
		send_me_a_copy=bool(send_me_a_copy),
		print_format=print_format,
		attachments=[f.name for f in attachments or []],
	)
	return (result or {}).get("name")


def _send_batch(messages: list) -> dict:
	if not isinstance(messages, list) or not messages:
		raise InvalidArgumentError(
			"messages must be a non-empty list of {doctype, name, recipients, subject, content}"
		)
	prepared = []
	for i, m in enumerate(messages):
		if not isinstance(m, dict):
			raise InvalidArgumentError(f"messages[{i}] must be a dict")
		# Resolve every message before any send is queued. Keep caller args intact.
		prepared.append(
			{**m, "attachments": resolve_email_attachments(m.get("attachments"), read_content=True)}
		)

	def _do(m: dict) -> dict:
		cn = _send_one(
			m.get("recipients"),
			m.get("subject"),
			m.get("content"),
			m.get("doctype"),
			m.get("name"),
			cc=m.get("cc"),
			bcc=m.get("bcc"),
			send_me_a_copy=bool(m.get("send_me_a_copy", False)),
			print_format=m.get("print_format"),
			attachments=m["attachments"],
		)
		return {"name": m.get("name"), "recipients": m.get("recipients"), "communication_name": cn}

	sent = run_atomic_batch(prepared, _do, label=lambda m: m.get("name") or str(m.get("recipients")))
	return {"sent": sent, "count": len(sent)}


def resolve_email_attachments(attachments, *, read_content=False):
	"""Resolve existing File IDs/local URLs and enforce the caller's File access.

	The confirmation card uses the same resolver without reading file bytes;
	execution rechecks access and content before queuing anything. Never accept
	raw bytes or fetch a remote URL supplied by the model.
	"""
	if attachments is None:
		return []
	if not isinstance(attachments, list) or len(attachments) > 20:
		raise InvalidArgumentError("attachments must be a list of at most 20 File IDs or local file URLs")
	resolved = []
	seen = set()
	for ref in attachments:
		if not isinstance(ref, str) or not ref.strip():
			raise InvalidArgumentError("each attachment must be a File ID or local file URL")
		ref = ref.strip()
		if ref.startswith(("/private/files/", "/files/")):
			candidates = frappe.get_all("File", filters={"file_url": ref}, pluck="name")
		elif "/" in ref or ":" in ref or "\\" in ref:
			raise InvalidArgumentError(
				"attachments must reference existing local Files, not external URLs or paths"
			)
		else:
			candidates = [ref] if frappe.db.exists("File", ref) else []
		file = None
		for candidate in candidates:
			doc = frappe.get_doc("File", candidate)
			if frappe.has_permission("File", "read", doc=doc):
				file = doc
				break
		if file is None:
			raise PermissionDeniedError("Attachment not found or you do not have permission to read it")
		if file.is_folder or not (file.file_url or "").startswith(("/private/files/", "/files/")):
			raise InvalidArgumentError("attachments must reference local files, not folders or remote URLs")
		if file.name in seen:
			continue
		if read_content:
			try:
				file.get_content()
			except OSError as exc:
				raise InvalidArgumentError(
					"Attachment content is unavailable; regenerate or upload the file again"
				) from exc
		seen.add(file.name)
		resolved.append(file)
	return resolved
