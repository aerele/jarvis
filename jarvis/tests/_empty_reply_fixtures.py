"""Shared fixtures of the empty-reply re-send tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

from jarvis.chat import empty_reply_recovery as recovery

# A stored dispatch payload with the re-send marker alone (as the refused-session re-send
# writes it).
MARKER_ONLY = "marker_only"


class ResendOn:
	"""Mixin: the re-send switched on; every latency line kept in ``self.lines``."""

	def setUp(self):
		super().setUp()
		self.addCleanup(self._restore_switch, frappe.local.conf.get(recovery.SWITCH))
		frappe.local.conf[recovery.SWITCH] = 1
		self.lines: list = []
		self.logger = MagicMock()
		self.logger.info.side_effect = lambda *args, **kw: self.lines.append(args)
		self.logger.warning.side_effect = lambda *args, **kw: self.lines.append(args)
		log_patch = patch("jarvis.chat.latency.get_logger", return_value=self.logger)
		log_patch.start()
		self.addCleanup(log_patch.stop)

	@staticmethod
	def _restore_switch(prev):
		frappe.local.conf.pop(recovery.SWITCH, None)
		if prev is not None:
			frappe.local.conf[recovery.SWITCH] = prev

	def lines_of(self, fmt):
		return [a[1:] for a in self.lines if a and a[0] == fmt]
