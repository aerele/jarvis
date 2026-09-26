"""Wire consumers follow the validated runtime profile, including old runtimes.

Names used to live inline at each reader. They now belong to the negotiated
profile, so source-literal assertions would reject a correctly configured reader.
Exercise actual wire behavior as well as the three metadata-reader boundaries.
"""

import inspect
from dataclasses import replace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jarvis.chat import canvas, prepare, turn_handler, turn_recovery
from jarvis.tests._gateway_fixtures import TEST_PROFILE

_KEY = "__open" + "claw"


class TestWireContractPins(FrappeTestCase):
	def test_transcript_consumers_use_profile_metadata_key(self):
		for module in (prepare, turn_handler, turn_recovery):
			self.assertIn("get_profile().message_metadata_key", inspect.getsource(module))
		for key in (_KEY, "__alternate_gateway"):
			with (
				self.subTest(key=key),
				patch.object(
					turn_recovery, "get_profile", return_value=replace(TEST_PROFILE, message_metadata_key=key)
				),
			):
				messages = [
					{"role": "assistant", "content": "previous", key: {"seq": 10}},
					{"role": "assistant", "content": "this turn", key: {"seq": 11}},
					{"role": "assistant", "content": "later turn", key: {"seq": 12}},
				]
				self.assertEqual(
					turn_recovery._latest_assistant_raw(messages, min_seq=10, max_seq=11), "this turn"
				)

	def test_live_frame_marker_uses_profile_and_preserves_other_scripts(self):
		for route in (f"/{_KEY}__/ws", "/alternate_gateway/ws"):
			with (
				self.subTest(route=route),
				patch.object(
					canvas, "get_profile", return_value=replace(TEST_PROFILE, live_reload_route=route)
				),
			):
				chart = "<script>renderChart()</script>"
				live = f'<script>new WebSocket("{route}")</script>'
				self.assertEqual(canvas._strip_host_client(chart + live), chart)
