"""Notices the agent runtime writes into a chat as if they were a reply. They name internal model ids
and are never the assistant's answer (live 2026-11-26: a fallback to an API-key model stored
``Model switched from anthropic/claude-opus-5 to deepseek-0/deepseek-flash (unknown).`` as the reply
while the real answer sat one entry earlier in the transcript)."""

import re

# "Model switched from <provider/model> to <provider/model> (<reason>)." (the whole text, one line)
_MODEL_SWITCH = re.compile(r"^\s*Model switched from \S+ to \S+ \([^()\n]*\)\.?\s*$")


def is_runtime_notice(text) -> bool:
	"""True when ``text`` is exactly a runtime notice, never when a real answer merely mentions one."""
	return isinstance(text, str) and bool(_MODEL_SWITCH.match(text))
