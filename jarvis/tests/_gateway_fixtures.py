"""Synthetic gateway inputs for app behavior tests.

These values deliberately differ from production. Admin's runtime-profile tests
pin the external wire contract independently; these tests prove that app
consumers follow their injected profile, rather than hardcoded runtime values.
"""

from contextlib import ExitStack, contextmanager
from unittest import addModuleCleanup
from unittest.mock import patch

from jarvis.chat.runtime_profile import RuntimeProfile

TEST_PROFILE = RuntimeProfile(
	message_metadata_key="__test_gateway",
	canvas_route="/__test_gateway__/canvas/",
	media_route="/__test_gateway__/assistant-media",
	live_reload_route="/__test_gateway__/ws",
	media_root="/srv/test-gateway/media/",
)


@contextmanager
def synthetic_runtime_profile():
	"""Inject the consumer boundary; production profile validation stays intact."""
	with ExitStack() as stack:
		for module in ("canvas", "generated_media", "turn_recovery", "prepare", "turn_handler"):
			stack.enter_context(patch(f"jarvis.chat.{module}.get_profile", return_value=TEST_PROFILE))
		stack.enter_context(
			patch("jarvis.chat.runtime_profile.get_saved_content_profile", return_value=TEST_PROFILE)
		)
		yield TEST_PROFILE


def transcript_message(role: str, content: str, *, seq: int) -> dict:
	"""Build one sequenced synthetic message, with fresh metadata per call."""
	return {"role": role, "content": content, "__test_gateway": {"seq": seq}}


def install_synthetic_runtime_profile():
	"""Install until unittest finishes this module, including on test failures."""
	context = synthetic_runtime_profile()
	context.__enter__()
	addModuleCleanup(context.__exit__, None, None, None)
