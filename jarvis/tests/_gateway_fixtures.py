"""Gateway transcript fixtures shared by chat lifecycle tests.

Keep the wire shape independent of production constants: these fixtures model
what the supported gateway sends, not what the current parser expects.
Boundary tests retain explicit literal messages for compatibility coverage.
"""


def transcript_message(role: str, content: str, *, seq: int) -> dict:
	"""Build one sequenced transcript message, with fresh metadata per call."""
	return {"role": role, "content": content, "__openclaw": {"seq": seq}}
