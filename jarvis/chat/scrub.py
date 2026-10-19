"""Best-effort secret removal shared by transcript exports and chat sharing."""

from __future__ import annotations

import json
import re

from jarvis.audit import _SECRET_KEYS
from jarvis.chat import egress_rules
from jarvis.learning.source_guard import redact_secrets

REDACTED = "[redacted]"
_SECRET_SEGMENTS = frozenset(_SECRET_KEYS) | frozenset(
	{
		"token",
		"key",
		"secret",
		"password",
		"passwd",
		"pwd",
		"credential",
		"credentials",
		"authorization",
		"auth",
		"cookie",
		"sid",
		"otp",
		"apikey",
	}
)
_WHOLE_KEY = ("password", "secret", "credential", "authorization")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])")
_ASSIGNMENT = re.compile(
	r"""(?<![\w.-])(?P<keyquote>["']?)(?P<key>[A-Za-z_][\w.-]*)(?P=keyquote)(?P<sep>\s*[:=]\s*)"""
	r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;{}\[\]*`]+)"""
)
_VALUES = re.compile(
	r"\bBearer\s+[^\s\"'`,;<>]+"
	r"|\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
	r"|[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
	r"|\b(?:sk-[A-Za-z0-9_-]+|xox[a-z]-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9_]+|AKIA[A-Z0-9]+)\b"
)
_AUTH_BEARER = re.compile(r"""(?i)(\bauthorization["']?\s*[:=]\s*["']?)bearer\s+[^\s"'`,;<>]+""")


def _sensitive_key(key: str, *, prose: bool = False) -> bool:
	lower = key.lower()
	segmented = _CAMEL.sub(lambda m: (m[1] or m[3]) + "_" + (m[2] or m[4]), key)
	segments = re.split(r"[_\-.]+", segmented.lower())
	if prose and segments[-1] == "secretary":
		# A terminal business label is ordinary prose. Other credential fragments
		# still match, and structured/quoted keys retain the full original rule.
		lower = lower[: -len("secretary")]
	return any(part in lower for part in _WHOLE_KEY) or segments[-1] in _SECRET_SEGMENTS


def _assignment(match: re.Match) -> str:
	if not _sensitive_key(match["key"], prose=not match["keyquote"]):
		return match[0]
	value = match["value"]
	quote = value[0] if value.startswith(('"', "'")) else ""
	return match["keyquote"] + match["key"] + match["keyquote"] + match["sep"] + quote + REDACTED + quote


def _json_text(value) -> str:
	"""Encode parsed JSON without consuming a Python frame per nested value."""
	parts = []
	work = [(False, value)]
	while work:
		raw, item = work.pop()
		if raw:
			parts.append(item)
		elif isinstance(item, (dict, list)):
			is_dict = isinstance(item, dict)
			parts.append("{" if is_dict else "[")
			work.append((True, "}" if is_dict else "]"))
			items = list(item.items()) if is_dict else list(enumerate(item))
			for index in range(len(items) - 1, -1, -1):
				key, child = items[index]
				work.append((False, child))
				if is_dict:
					work.append((True, json.dumps(key, ensure_ascii=False) + ": "))
				if index:
					work.append((True, ", "))
		else:
			parts.append(json.dumps(item, ensure_ascii=False))
	return "".join(parts)


def _scrub_text(value: str) -> str:
	trimmed = value.strip()
	if trimmed.startswith(("{", "[")):
		try:
			parsed = json.loads(trimmed)
		except RecursionError:
			# Do not fall back to unparsed text whose nested/escaped keys could leak.
			return REDACTED
		except ValueError:
			pass
		else:
			if isinstance(parsed, (dict, list)):
				return _json_text(scrub_for_share(parsed))
	value, _count = redact_secrets(value)
	# HTTP authentication schemes are case-insensitive; ordinary prose is not
	# an Authorization header (for example, "the bearer cheque was deposited").
	value = _AUTH_BEARER.sub(lambda match: match[1] + REDACTED, value)
	return egress_rules.redact(_ASSIGNMENT.sub(_assignment, _VALUES.sub(REDACTED, value)))


def scrub_for_share(value):
	"""Return a scrubbed copy, including JSON strings nested in MCP results.

	Container traversal and JSON encoding do not recurse over container depth.
	Fenced message blocks stay text; their JSON is never decoded/re-serialized.
	"""
	if not isinstance(value, (dict, list)):
		return _scrub_text(value) if isinstance(value, str) else value
	result = {} if isinstance(value, dict) else [None] * len(value)
	work = [(value, result)]
	while work:
		source, target = work.pop()
		for key, item in source.items() if isinstance(source, dict) else enumerate(source):
			if isinstance(source, dict) and _sensitive_key(str(key)):
				target[key] = REDACTED
			elif isinstance(item, (dict, list)):
				target[key] = {} if isinstance(item, dict) else [None] * len(item)
				work.append((item, target[key]))
			else:
				target[key] = _scrub_text(item) if isinstance(item, str) else item
	return result
