"""Positive pins for the retained wire-contract identifiers.

The white-label leak guard is one-directional: it fails on a LEFTOVER runtime name, but
stays green if a wire identifier is wrongly renamed — production readers and their
test fixtures rename in lockstep and every suite passes against a fictional wire
shape. That is not hypothetical: this branch shipped exactly that critical once
(the gateway transcript-metadata key renamed at 3 read sites, fixtures renamed with
them; post-deploy the recovery watermark would always read 0) and it was caught by
hand, not by a test.

These pins are the positive counterpart: they assert the frozen wire literals are
still present, verbatim, at the named production read/write sites. The literals are
ASSEMBLED FROM PARTS below — and this file never spells the runtime's name
contiguously — so a blanket rename rewrites the production sites but NOT these
assertions, and the break is loud instead of silent.
"""

import os

import frappe
from frappe.tests.utils import FrappeTestCase

# The gateway stamps this metadata key on every transcript message; the fence that
# stops recovery from stealing a later turn's answer reads it. Renaming it in our
# code does not rename what the gateway sends.
_KEY = "__open" + "claw"
# The live-frame marker the canvas/dashboard srcdoc pipeline embeds and strips.
_MARKER = _KEY + "__"

_KEY_SITES = (
	os.path.join("jarvis", "chat", "prepare.py"),
	os.path.join("jarvis", "chat", "turn_handler.py"),
	os.path.join("jarvis", "chat", "turn_recovery.py"),
)
_MARKER_SITES = (
	os.path.join("jarvis", "chat", "canvas.py"),
	os.path.join("frontend", "src", "lib", "dashboardSrcdoc.js"),
)


def _app_root() -> str:
	return os.path.dirname(frappe.get_app_path("jarvis"))


def _source(rel_path: str) -> str:
	with open(os.path.join(_app_root(), rel_path), encoding="utf-8") as fh:
		return fh.read()


class TestWireContractPins(FrappeTestCase):
	def test_transcript_metadata_key_read_verbatim(self):
		for rel in _KEY_SITES:
			src = _source(rel)
			self.assertIn(
				f'"{_KEY}"',
				src,
				f"{rel} no longer reads the gateway transcript-metadata key {_KEY!r} verbatim - "
				"the gateway still sends that key; renaming the reader silently zeroes the "
				"recovery watermark fence. If the read genuinely moved, update _KEY_SITES.",
			)

	def test_live_frame_marker_verbatim(self):
		for rel in _MARKER_SITES:
			src = _source(rel)
			self.assertIn(
				_MARKER,
				src,
				f"{rel} no longer carries the live-frame marker {_MARKER!r} verbatim - "
				"both the embedder and the stripper must keep the wire spelling. If the "
				"marker genuinely moved, update _MARKER_SITES.",
			)

	def test_this_pin_is_rename_proof(self):
		"""Self-check: this file must never contain the assembled literal as a
		contiguous token, or a blanket rename would rewrite pin and target together
		and the whole point is lost."""
		src = _source(os.path.join("jarvis", "tests", "test_wire_contract_pins.py"))
		self.assertNotIn(_KEY, src.replace('"__open" + "claw"', ""))
