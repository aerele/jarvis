"""The recipient transcript boundary: allowlist, scrub, then rewrite files.

Owner responses never pass through this module. Unknown keys are discarded at
each structural level, including pending rows and nested canvas/step objects.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

import frappe

from jarvis.chat.error_taxonomy import headline_for
from jarvis.chat.scrub import REDACTED, _json_text, scrub_for_share

ROW_KEYS = (
	"name",
	"role",
	"content",
	"creation",
	"modified",
	"seq",
	"streaming",
	"stopped",
	"recovering",
	"tool_name",
	"tool_status",
	"tool_args",
	"tool_result",
	"tool_result_compact",
	"action_outcome",
	"live_step_times",
	"run_id",
	"pump_epoch",
	"last_event_seq",
	"turn_state",
	"ref_doctype",
	"ref_name",
	"tool_call_id",
	"tool_call_ids",
)
PENDING_KEYS = ("name", "seq", "role", "creation", "tool_name")
PAGING_KEYS = ("start", "before", "has_more", "older_counts")
_PRIVATE_URL = re.compile(r"/private/files/[^\s\\\"'<>`)\]}]+")


def file_url(conversation: str, url: str) -> str:
	return "/api/method/jarvis.chat.sharing.get_shared_file?" + urlencode(
		{"conversation": conversation, "file_url": url}
	)


def safe_value(value, conversation: str):
	"""Scrub once, preserving JSON-string/container types during URL rewriting.

	An explicit work stack also handles deeply nested containers without a
	Python frame per value. Nested MCP JSON strings are decoded/re-encoded as
	JSON, so their escaped quotes cannot become part of an attachment URL.
	Fenced message blocks stay text.
	"""

	def rewrite_text(text):
		return _PRIVATE_URL.sub(lambda match: file_url(conversation, match[0]), text)

	result = [None]
	work = [(False, scrub_for_share(value), result, 0)]
	while work:
		encode, item, target, key = work.pop()
		if encode:
			target[key] = _json_text(item[0])
			continue
		if isinstance(item, str):
			if item.strip().startswith(("{", "[")):
				try:
					parsed = json.loads(item)
				except RecursionError:
					target[key] = REDACTED
					continue
				except ValueError:
					pass
				else:
					if isinstance(parsed, (dict, list)):
						decoded = [None]
						work.append((True, decoded, target, key))
						work.append((False, parsed, decoded, 0))
						continue
			target[key] = rewrite_text(item)
		elif isinstance(item, (dict, list)):
			out = {} if isinstance(item, dict) else [None] * len(item)
			target[key] = out
			for child_key, child in item.items() if isinstance(item, dict) else enumerate(item):
				if isinstance(child_key, str):
					child_key = rewrite_text(child_key)
				work.append((False, child, out, child_key))
		else:
			target[key] = item
	return result[0]


def _steps(value, conversation):
	if not isinstance(value, list):
		return []
	return [
		{
			"text": safe_value(item.get("text", ""), conversation),
			"at": safe_value(item.get("at"), conversation)
			if isinstance(item.get("at"), (str, int, float))
			else None,
		}
		if isinstance(item, dict)
		else {"text": safe_value(item, conversation), "at": None}
		for item in value
		if isinstance(item, (dict, str))
	]


def project_row(row: dict, conversation: str) -> dict:
	if row.get("tool_status") == "pending":
		return {
			**{key: safe_value(row.get(key), conversation) for key in PENDING_KEYS},
			"awaiting_owner": True,
		}
	result = {key: safe_value(row.get(key), conversation) for key in ROW_KEYS}
	result["error_headline"] = headline_for(row.get("error")) if row.get("error") else None
	result["steps"] = _steps(row.get("steps"), conversation)
	result["live_steps"] = _steps(row.get("live_steps"), conversation)
	result["canvas"] = []
	canvas = row.get("canvas")
	for item in canvas if isinstance(canvas, list) else []:
		if not isinstance(item, dict):
			continue
		url = item.get("file_url")
		result["canvas"].append(
			{
				**{key: safe_value(item.get(key), conversation) for key in ("name", "type", "title")},
				"file_url": file_url(conversation, url)
				if isinstance(url, str) and url.startswith(("/private/files/", "/files/"))
				else None,
			}
		)
	return result


def project_conversation(body: dict, doc) -> dict:
	return {
		"conversation": {
			"name": doc.name,
			"title": safe_value(doc.title, doc.name),
			"owner": doc.owner,
			"owner_name": safe_value(frappe.utils.get_fullname(doc.owner), doc.name),
			"last_active_at": doc.last_active_at,
			"status": doc.status,
		},
		**{
			key: [project_row(row, doc.name) for row in body.get(key, [])]
			for key in ("messages", "older_pending", "refreshed")
		},
		"paging": {key: body.get("paging", {}).get(key) for key in PAGING_KEYS},
	}


def project_index_row(row: dict) -> dict:
	return {
		"name": row["name"],
		"title": safe_value(row.get("title"), row["name"]),
		"owner_name": safe_value(row.get("owner_name"), row["name"]),
		"last_active_at": row.get("last_active_at"),
		"unseen": bool(
			row.get("latest") and (not row.get("seen_marker") or row["latest"] > row["seen_marker"])
		),
	}
