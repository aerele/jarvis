"""Best-effort secret removal shared by transcript exports and chat sharing."""

from __future__ import annotations

import json
import re

from jarvis.chat import egress_rules

REDACTED = "[redacted]"
_SECRET_SEGMENTS = frozenset(
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
	r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;{}\[\]]+)"""
)
_VALUES = re.compile(
	r"\bBearer\s+[^\s\"'`,;<>]+"
	r"|\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
	r"|[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
	r"|\b(?:sk-[A-Za-z0-9_-]+|xox[A-Za-z0-9_-]+|ghp_[A-Za-z0-9_]+|AKIA[A-Z0-9]+)\b",
	re.IGNORECASE,
)


def _sensitive_key(key: str) -> bool:
	lower = key.lower()
	if any(part in lower for part in _WHOLE_KEY):
		return True
	segmented = _CAMEL.sub(lambda m: (m[1] or m[3]) + "_" + (m[2] or m[4]), key)
	return re.split(r"[_\-.]+", segmented.lower())[-1] in _SECRET_SEGMENTS


def _assignment(match: re.Match) -> str:
	if not _sensitive_key(match["key"]):
		return match[0]
	value = match["value"]
	quote = value[0] if value.startswith(('"', "'")) else ""
	return match["keyquote"] + match["key"] + match["keyquote"] + match["sep"] + quote + REDACTED + quote


def scrub_for_share(value):
	"""Return a scrubbed copy, including JSON strings nested in MCP results.

	Fenced message blocks stay text: their JSON is never decoded/re-serialized.
	The textual value/key rules still hide recognizable secrets within that text.
	"""
	if isinstance(value, dict):
		return {
			key: REDACTED if _sensitive_key(str(key)) else scrub_for_share(item)
			for key, item in value.items()
		}
	if isinstance(value, list):
		return [scrub_for_share(item) for item in value]
	if not isinstance(value, str):
		return value
	trimmed = value.strip()
	if trimmed.startswith(("{", "[")):
		try:
			parsed = json.loads(trimmed)
		except (ValueError, RecursionError):
			pass
		else:
			if isinstance(parsed, (dict, list)):
				return json.dumps(scrub_for_share(parsed), ensure_ascii=False)
	return egress_rules.redact(_ASSIGNMENT.sub(_assignment, _VALUES.sub(REDACTED, value)))
