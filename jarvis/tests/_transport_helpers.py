"""Shared test helper — provision the shard for the LEGACY dispatch path (CDX-10).

Not a test module (leading underscore; no ``test_`` prefix), so it is imported, never
discovered/run as tests.

Under the default-ON pump inversion, the fenced dispatch decision reads the
DB-authoritative ``Jarvis Relay Pump.transport_mode`` ROW, and patterntest's row is
``pump`` by the migration default — so a send/continuation there routes to the pump (no
legacy worker enqueue, ``_dispatch_turn`` not called via the accept callback). Tests that
assert the LEGACY worker enqueue / the shared ``_dispatch_turn`` continuation dispatch
cover a REAL shipped path (explicit-0 kill-switch sites, self-host), so they must run on an
honestly-legacy site: this flips the ROW to ``legacy`` (via the sanctioned
``pump.set_transport_mode`` row writer) AND sets the explicit conf mirror
``jarvis_pump_enabled=0`` for the request context, so both the entry ``turn_machine_enabled()``
(conf) and the enqueue-boundary gate (ROW) choose legacy. Restores the ROW to its prior mode
and removes the conf key after the test (registered via ``addCleanup``, LIFO), leaving the
site at its pump-default. Assertions are UNCHANGED — only the site state is made honest for
what they assert.
"""

from unittest.mock import patch

import frappe

from jarvis.chat import admission, pump

PUMP = "Jarvis Relay Pump"


def provision_legacy_site(test_case) -> None:
	target = admission.DEFAULT_RELAY_TARGET
	prior = frappe.db.get_value(PUMP, target, "transport_mode")
	pump.set_transport_mode(target, pump._MODE_LEGACY)  # sanctioned row writer (mode_epoch+1)
	frappe.db.commit()
	conf_patch = patch.dict(frappe.local.conf, {"jarvis_pump_enabled": 0})
	conf_patch.start()

	def _restore():
		conf_patch.stop()
		frappe.db.set_value(PUMP, target, "transport_mode", prior, update_modified=False)
		frappe.db.commit()

	test_case.addCleanup(_restore)
