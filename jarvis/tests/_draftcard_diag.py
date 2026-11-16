"""TEMPORARY CI diagnostic (remove with the fix): print the state the pump watchdog reads.

Every line starts with ``DRAFTCARD-DIAG`` so it can be grepped from the CI log. Read-only,
never fails a test."""

from __future__ import annotations

import sys


def _emit(line: str) -> None:
	print(f"DRAFTCARD-DIAG {line}", file=sys.__stderr__, flush=True)


def snapshot(label: str) -> None:
	try:
		_snapshot(label)
	except Exception as exc:  # a probe must never fail a run
		_emit(f"{label} probe-error {exc!r}")


def _snapshot(label: str) -> None:
	import frappe

	from jarvis.chat import pump

	if not getattr(frappe.local, "site", None):
		_emit(f"{label} no-site")
		return
	_emit(f"{label} begin")
	conf = {k: v for k, v in frappe.conf.items() if "jarvis" in str(k).lower() or "pump" in str(k).lower()}
	_emit(f"{label} conf {conf}")
	pumps = frappe.db.sql(
		"SELECT relay_target_id, transport_mode, mode_epoch, pump_epoch, lease_holder, lease_expires_at, "
		"hop_counter FROM `tabJarvis Relay Pump`",
		as_dict=True,
	)
	for p in pumps:
		_emit(f"{label} pump {dict(p)}")
	turns = frappe.db.sql(
		"SELECT t.run_id, t.relay_target_id, t.state, t.conversation, t.creation, t.turn_class, "
		"c.owner AS conv_owner FROM `tabJarvis Chat Turn` t "
		"LEFT JOIN `tabJarvis Conversation` c ON c.name = t.conversation "
		"WHERE t.state NOT IN ('done', 'errored', 'cancelled')",
		as_dict=True,
	)
	_emit(f"{label} nonterminal-turns {len(turns)}")
	for t in turns:
		_emit(f"{label} turn {dict(t)}")
	# What the watchdog would decide for each such shard with the kill switch conf the two
	# failing tests set (jarvis_pump_enabled=0) and with the ambient conf.
	from unittest.mock import patch

	for target in sorted({t["relay_target_id"] for t in turns}):
		ambient = pump._row_transport_mode(target)
		with patch.dict(frappe.local.conf, {"jarvis_pump_enabled": 0}):
			killed = pump._row_transport_mode(target)
		_emit(
			f"{label} shard {target!r} row_mode_ambient={ambient} row_mode_conf0={killed} "
			f"mirror_live={pump._lease_mirror_live(target)}"
		)
	_emit(f"{label} open-effect-shards {list(_safe(frappe_shards_open_effects))}")
	_emit(f"{label} end")


def frappe_shards_open_effects():
	from jarvis.chat import turn_state

	return turn_state.shards_with_open_effects()


def _safe(fn):
	try:
		return fn()
	except Exception as exc:
		return [f"error {exc!r}"]
