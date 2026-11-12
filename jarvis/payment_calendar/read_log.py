"""Logs of the snapshot read path: refusals, slow reads, missing parts and the Error Log rows.

No line carries a voucher, party, company or user name, and no request text: a `side` that is not
a known side is logged as `-`. Every function is best effort: a logging failure never changes a reply or a discard.
"""

from __future__ import annotations

from time import monotonic

import frappe

from jarvis.payment_calendar import store

SLOW_MS = 300
# The Error Log fires when this many different snapshots were discarded inside the store's window.
LOOP_SNAPSHOTS = 3
# The one refusal kind that is not a policy change: the site day moved on. It is logged but never counted.
UNCOUNTED_KINDS = ("anchor",)
# One Error Log row per (operation, exception class) in this many seconds; Frappe does not group the rows.
ERROR_LOG_WINDOW = 300


def refused(endpoint: str, kind: str, side: str | None = None) -> None:
	"""One refused read: the check that refused (scope, fingerprint, obligation, voucher, party, proof)."""
	side = side if side in store.SIDES else None
	_warn(f"read_refused endpoint={endpoint} kind={kind} side={side or '-'}")


def part_missing(endpoint: str, part: str) -> None:
	"""A live snapshot lacks one stored key: an eviction. `part` is a fixed name (payable, proofs-payable...)."""
	_warn(f"part_missing endpoint={endpoint} part={part}")


def slow(endpoint: str, started: float, rows: int, parties: int) -> None:
	"""A finished read that took longer than SLOW_MS; `started` is a `monotonic()` reading."""
	elapsed = int((monotonic() - started) * 1000)
	if elapsed > SLOW_MS:
		_warn(f"slow_read endpoint={endpoint} ms={elapsed} rows={rows} parties={parties}")


def count_discard(actor: str, snapshot_id: str, kind: str) -> None:
	"""Count one discarded snapshot; the third different one inside the window writes one Error Log."""
	if kind in UNCOUNTED_KINDS:
		return
	try:
		added, size = store.count_refusal(actor, snapshot_id)
		if added == 1 and size == LOOP_SNAPSHOTS:
			frappe.log_error(
				title="Payment Calendar refusal loop",
				message=f"{size} snapshots refused in {store.REFUSAL_WINDOW}s, last kind={kind}",
			)
	except Exception:
		_warn("refusal_counter_unavailable")


def error_log_once(title: str, message: str, operation: str, exception_class: str) -> None:
	"""`frappe.log_error` at most once per (operation, exception class) in `ERROR_LOG_WINDOW` seconds.

	A repeating failure would otherwise write a row per request. When the cache is down the row is written.
	"""
	if _first_in_window(operation, exception_class):
		frappe.log_error(title=title, message=message)


def _first_in_window(operation: str, exception_class: str) -> bool:
	try:
		key = frappe.cache.make_key(f"jarvis:pcal:errlog:{operation}:{exception_class}")
		return bool(frappe.cache.execute_command("SET", key, "1", "NX", "EX", ERROR_LOG_WINDOW))
	except Exception:
		return True


def _warn(line: str) -> None:
	try:
		frappe.logger("payment_calendar").warning(line)
	except Exception:
		pass
