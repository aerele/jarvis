"""Trusted collaboration adapter for the existing canonical admission transaction."""

import json
from dataclasses import dataclass, field

import frappe
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_access import authorize
from jarvis.chat.collaboration_bindings import create_binding, insert_message, require_live, stamp


@dataclass
class SharedAdmission:
	actor: str
	binding: c.ActorBinding | None = None
	seed_message: str | None = None
	request_hash: str | None = None
	file_ids_supplied: tuple[bool, ...] | None = None
	validate_inputs: object = None
	seed_fields: dict = field(default_factory=dict)

	def authorize(self, conversation: str, run_id: str):
		"""Inside shard/conversation locks, including BEFORE a duplicate response."""
		authorize(conversation, actor=self.actor)
		existing = frappe.db.get_value(
			"Jarvis Chat Turn",
			run_id,
			["conversation", "actor", "collaboration_binding", "seed_message"],
			as_dict=True,
			for_update=True,
		)
		if existing:
			if (
				existing.conversation != conversation
				or existing.actor != self.actor
				or not existing.collaboration_binding
			):
				c._deny()
			key = frappe.db.get_value(c.BINDING, existing.collaboration_binding, "session_key")
			binding = c.resolve_binding(key)
			require_live(binding)
			if self.binding is not None and self.binding != binding:
				c._deny()
			self.binding = binding
			self.seed_message = existing.seed_message
			if self.request_hash:
				from jarvis.chat.collaboration_context import request_fingerprint

				seed = frappe.db.get_value(
					"Jarvis Chat Message",
					existing.seed_message,
					["content", "canvas"],
					as_dict=True,
					for_update=True,
				)
				try:
					attachments = json.loads(seed.canvas or "[]") if seed else None
					if attachments is not None and self.file_ids_supplied is not None:
						if len(attachments) != len(self.file_ids_supplied):
							raise ValueError("Attachment scope changed")
						# The seed keeps resolved IDs. Match only IDs supplied in the
						# original request, including mixed old/new client references.
						attachments = [
							{key: value for key, value in item.items() if key != "file_id" or supplied}
							for item, supplied in zip(attachments, self.file_ids_supplied, strict=True)
						]
					stored_hash = request_fingerprint(seed.content or "", attachments) if seed else None
				except (ValueError, TypeError, KeyError, RecursionError):
					stored_hash = None
				if stored_hash != self.request_hash:
					frappe.throw(
						"This request ID was already used for different content. Keep your draft and try again.",
						frappe.ValidationError,
					)
		elif self.binding is not None:
			if self.binding.conversation != conversation or self.binding.actor != self.actor:
				c._deny()
			require_live(self.binding)

	def insert_seed(self, conversation: str, run_id: str, content: str, origin: str, hidden: bool):
		if self.validate_inputs:
			self.validate_inputs()
		if self.binding is None:
			self.binding = create_binding(conversation, run_id, actor=self.actor)
		message = insert_message(
			self.binding,
			{
				**self.seed_fields,
				"role": "user",
				"content": content or "",
				"streaming": 0,
				"hidden": cint(hidden),
				"origin": origin,
			},
			run_id=run_id,
		)
		self.seed_message = message.name
		return message.name

	def stamp_turn(self, turn):
		if not self.binding or turn.seed_message != self.seed_message:
			c._deny()
		stamp(turn, self.binding)
