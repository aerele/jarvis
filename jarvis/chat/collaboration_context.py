"""Bounded canonical shared context; never seed from a gateway's owner history."""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint

from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.share_projection import project_row

MAX_REQUEST_BYTES = 16 * 1024
MAX_HISTORY_BYTES = 64 * 1024
MAX_HISTORY_RECORDS = 100
OMITTED = "[Older content omitted]"


def validate_seed(binding, run_id, conversation, message):
	from jarvis.chat import collaboration as c

	require_live(binding)
	turn = frappe.db.get_value(
		"Jarvis Chat Turn",
		run_id,
		["actor", "collaboration_binding", "conversation", "seed_message"],
		as_dict=True,
	)
	seed = frappe.db.get_value(
		"Jarvis Chat Message",
		message,
		["actor", "collaboration_binding", "collaboration_run_id", "owner", "conversation"],
		as_dict=True,
	)
	if (
		binding.conversation != conversation
		or not turn
		or not seed
		or (turn.actor, turn.collaboration_binding, turn.conversation, turn.seed_message)
		!= (binding.actor, binding.name, conversation, message)
		or (seed.actor, seed.collaboration_binding, seed.collaboration_run_id, seed.owner, seed.conversation)
		!= (binding.actor, binding.name, run_id, binding.actor, conversation)
	):
		c._deny()


def validate_request(text: str, *, allow_empty=False) -> str:
	if not isinstance(text, str) or (not text.strip() and not allow_empty):
		frappe.throw(_("Write a message before sending."))
	if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
		frappe.throw(_("This message is too long for a shared chat. Shorten it and try again."))
	return text


def request_fingerprint(text: str, attachments: list) -> str:
	value = {
		"message": text,
		"files": [
			({"file_url": a["file_url"], "file_id": a["file_id"]} if a.get("file_id") else a["file_url"])
			for a in attachments
		],
	}
	return hashlib.sha256(
		json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
	).hexdigest()


def history_block(binding, run_id: str, *, max_records=MAX_HISTORY_RECORDS, max_bytes=MAX_HISTORY_BYTES):
	"""Completed prior turns only, even when their reply seq follows this seed.

	Queued prompts are not context. A logical turn may have completed children;
	those disclosed replies remain available to its own confirmation chain. The
	current run is always excluded and its complete request is appended separately.
	"""
	require_live(binding)
	current = frappe.db.get_value(
		"Jarvis Chat Turn", run_id, ["conversation", "collaboration_binding", "enqueued_at"], as_dict=True
	)
	if (
		not current
		or current.conversation != binding.conversation
		or current.collaboration_binding != binding.name
	):
		from jarvis.chat.collaboration import _deny

		_deny()
	limit = min(MAX_HISTORY_RECORDS, max(1, cint(max_records)))
	budget = min(MAX_HISTORY_BYTES, max(256, cint(max_bytes)))
	# m.* is trusted server data, never returned. project_row is the established
	# structural allowlist + secret scrubber + authorized shared-file rewriter.
	rows = frappe.db.sql(
		"""SELECT m.* FROM `tabJarvis Chat Message` m
		LEFT JOIN `tabJarvis Chat Turn` t ON t.name=m.collaboration_run_id AND t.conversation=m.conversation
		WHERE m.conversation=%(conversation)s AND COALESCE(m.hidden,0)=0
		AND COALESCE(m.streaming,0)=0 AND COALESCE(m.recovering,0)=0
		AND (COALESCE(m.collaboration_binding,'')='' OR
		 (t.state IN ('finalizing','done','errored','cancelled') AND t.name<>%(run)s
		  AND t.enqueued_at<=%(enqueued)s))
		ORDER BY COALESCE(t.enqueued_at,m.creation) DESC, m.seq DESC LIMIT %(limit)s""",
		{
			"conversation": binding.conversation,
			"run": run_id,
			"enqueued": current.enqueued_at,
			"limit": limit + 1,
		},
		as_dict=True,
	)
	parts, used = [], len(OMITTED.encode()) + 1
	from jarvis.chat.api import _compact_tool_results

	_compact_tool_results(rows)
	omitted = len(rows) > limit
	for row in rows[:limit]:
		for field in ("steps", "canvas"):
			if isinstance(row.get(field), str):
				try:
					row[field] = json.loads(row[field])
				except (ValueError, RecursionError):
					row[field] = None
		# The actor is an immutable server stamp. Historical identity is labelled
		# as transcript data and cannot replace the current authenticated actor.
		projected = project_row(row, binding.conversation)
		body = {
			"actor": row.actor or frappe.db.get_value("Jarvis Conversation", binding.conversation, "owner"),
			**projected,
		}
		encoded = json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":"))
		size = len(encoded.encode("utf-8")) + 1
		if size > budget - len(OMITTED.encode()) - 1:
			if used + len(OMITTED.encode()) + 1 > budget:
				omitted = True
				break
			parts.append(OMITTED)
			used += len(OMITTED.encode()) + 1
			omitted = True
			continue
		if used + size > budget:
			omitted = True
			break
		parts.append(encoded)
		used += size
	parts.reverse()
	if omitted:
		parts.insert(0, OMITTED)
	return "\n".join(parts)


def require_prompt_budget(text: str, *, attachments: list, context_tokens: int, runtime_reserve: int):
	"""Conservative byte bound for the COMPLETE app prompt and image payload.

	UTF-8 bytes upper-bound byte-tokenizer text tokens. The attested runtime
	reserve covers native instructions/tool schemas plus model output allowance;
	missing capacity or reserve metadata cannot silently select a larger model.
	"""
	capacity = cint(context_tokens)
	reserve = cint(runtime_reserve)
	if capacity <= 0 or reserve <= 0 or capacity <= reserve:
		frappe.throw(_("The model's shared-chat context budget is unavailable. Your draft has been kept."))
	size = len(text.encode("utf-8"))
	if attachments:
		size += len(json.dumps(attachments, ensure_ascii=False, default=str).encode("utf-8"))
	if size + reserve > capacity:
		frappe.throw(
			_(
				"This request and its attachments exceed the model's shared-chat context budget. Shorten the request or remove an attachment; your draft has been kept."
			)
		)


def fit_prompt_history(parts, *, attachments: list, context_tokens: int, runtime_reserve: int):
	"""Fit whole quoted records to this model; never trim the current request.

	Only server-assembled parts are accepted. Historical JSON is serialized on
	one line per record; removing its oldest lines cannot split a record or
	change the actor, current request, trusted clauses or attached content.
	"""
	from jarvis.chat.turn_handler import _fence_untrusted

	require_prompt_budget("", attachments=[], context_tokens=context_tokens, runtime_reserve=runtime_reserve)
	prefix, history, suffix = parts
	attachment_bytes = (
		len(json.dumps(attachments, ensure_ascii=False, default=str).encode()) if attachments else 0
	)
	budget = cint(context_tokens) - cint(runtime_reserve) - attachment_bytes
	remaining = history.splitlines()
	text = prefix + _fence_untrusted(history, "shared transcript") + suffix
	while len(text.encode()) > budget and remaining:
		remaining.pop(0)
		clipped = "\n".join([OMITTED, *remaining])
		text = prefix + _fence_untrusted(clipped, "shared transcript") + suffix
	# Even zero history may not fit a request plus attachments. Refuse rather
	# than shortening the request or silently choosing another model/identity.
	require_prompt_budget(
		text, attachments=attachments, context_tokens=context_tokens, runtime_reserve=runtime_reserve
	)
	return text
