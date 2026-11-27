"""Logs of the snapshot read path: refusals, slow reads, missing parts and the Error Log rows.

No line carries a voucher, party, company or user name, and no request text: a `side` that is not
a known side is logged as `-`. Every function is best effort: a logging failure never changes a reply or a discard.

Lines are WARNING level, except `speed_fallback`: it is ERROR level because production logs at ERROR and the
line asks for an action (audit a new ERPNext source and bump the pin). It carries a method name, a source
hash and a version only.
"""

from __future__ import annotations

import re
from hashlib import sha256
from hmac import new as hmac_new
from time import monotonic

import frappe

from jarvis.payment_calendar import store
from jarvis.payment_calendar.errors import TIMEOUTS

SLOW_MS = 300
# The Error Log fires when this many different snapshots were discarded inside the store's window.
LOOP_SNAPSHOTS = 3
# The one refusal kind that is not a policy change: the site day moved on. It is logged but never counted.
UNCOUNTED_KINDS = ("anchor",)
# One Error Log row per (operation, exception class) in this many seconds; Frappe does not group the rows.
ERROR_LOG_WINDOW = 300
# HMAC domain of every operational digest (company, row key): a log, a key or an Error Log row names a
# company only by this digest, which cannot be reversed without the site's encryption key.
OPS_DOMAIN = b"pcal-ops"
DIGEST_HEX = 16
# Failure classes of the reader outcome counters; the other classes (ineligible, busy...) are counted only.
FAILURE_PREFIXES = ("fallback:", "reader_refused:", "reader_exception:")
# Keys of an outcome record: `routing.read_side` writes them, `reader_outcomes` reads them.
ROUTED = "routed"
STEP = "step"
FALLBACK = "fallback"
DETAIL = "fallback_detail"
REFUSED = "reader_refused"
EXCEPTION = "reader_exception"
PROBE_ERROR = "probe_error"
CAUSE = "cause"
READER_TITLE = "Payment Calendar reader failure"
# The day counters live 35 days, the same as the shadow's night records.
COUNTER_TTL_S = 35 * 24 * 3600
# One Error Log row per company and class and day.
OUTCOME_ROW_WINDOW = 86_400
# A snapshot view longer than this is a WARNING line (the job budget is 90 s, the view cap 120 s).
VIEW_WARN_MS = 60_000
# Reasons the reader is not at fault for: counted, never an Error Log row (the user is ineligible).
COUNTED_ONLY_FALLBACKS = frozenset({"ineligible", "sales_person_scope"})
# Steps that keep the switch out of play: not counted at all.
UNCOUNTED_STEPS = frozenset({"settings"})
# The counter of the sides a daily row reports that the reader served.
SERVED = "served"
_SAFE_TEXT = re.compile(r"[A-Za-z0-9_.:]{1,80}")
_DIGEST = re.compile(r"[0-9a-f]{%d}" % DIGEST_HEX)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


def speed_fallback(method: str, source_hash: str | None, erpnext_version: str) -> None:
	"""A speed override ran upstream because its source hash is not pinned; ERROR level so it is seen in production."""
	try:
		frappe.logger("payment_calendar").error(
			f"speed_fallback method={method} hash={source_hash or 'none'} erpnext={erpnext_version}"
		)
	except Exception:
		pass


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


def error_log_once(
	title: str,
	message: str,
	operation: str,
	exception_class: str,
	*,
	window: int = ERROR_LOG_WINDOW,
	when_cache_down: bool = True,
) -> None:
	"""`frappe.log_error` at most once per (operation, exception class) in `window` seconds.

	A repeating failure would otherwise write a row per request. `when_cache_down` says whether the row is
	written when the cache cannot answer: a request-path failure writes it, a repeating background job does not.
	When the insert itself fails the window is freed again, so the next failure can still write the row.
	"""
	key = _first_in_window(operation, exception_class, window)
	if key is None:
		if not when_cache_down:
			return
	elif key is False:
		return
	try:
		frappe.log_error(title=title, message=message)
	except Exception:
		if key:
			_free_window(key)
		raise


def _window_key(operation: str, exception_class: str):
	return frappe.cache.make_key(f"jarvis:pcal:errlog:{operation}:{exception_class}")


def window_open(operation: str, exception_class: str) -> bool | None:
	"""True when no row is recorded for (operation, class) in its window, False when one is, None when the cache is down.

	It claims nothing. A caller that writes the row itself calls `close_window` only after the row is committed,
	so a failed or rolled-back write never leaves a key that hides the alert for the rest of the window.
	"""
	try:
		return not frappe.cache.execute_command("EXISTS", _window_key(operation, exception_class))
	except TIMEOUTS:
		raise
	except Exception:
		return None


def close_window(operation: str, exception_class: str, window: int) -> None:
	"""Record that the row of (operation, class) was written: no other row for `window` seconds. Best effort."""
	try:
		frappe.cache.execute_command("SET", _window_key(operation, exception_class), "1", "EX", window)
	except TIMEOUTS:
		raise
	except Exception:
		pass


def _first_in_window(operation: str, exception_class: str, window: int):
	"""The window key when this is the first failure in the window, False when it is not, None when the cache is down."""
	try:
		key = _window_key(operation, exception_class)
		return key if frappe.cache.execute_command("SET", key, "1", "NX", "EX", window) else False
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _free_window(key) -> None:
	try:
		frappe.cache.execute_command("DEL", key)
	except TIMEOUTS:
		raise
	except Exception:
		pass


def ops_digest(value: str) -> str:
	"""HMAC of `value` under a subkey of the site's encryption key, `DIGEST_HEX` hex characters.

	The subkey is HMAC(encryption_key, domain), so the site key itself never keys a digest (the acceptance
	harness does the same). The key is read from the site config and never created (`get_encryption_key`
	writes site_config). A caller checks that the key exists first; without one this raises `RuntimeError`.
	"""
	key = frappe.conf.get("encryption_key")
	if not key:
		raise RuntimeError("no encryption key")
	subkey = hmac_new(str(key).encode(), OPS_DOMAIN, sha256).digest()
	return hmac_new(subkey, value.encode(), sha256).hexdigest()[:DIGEST_HEX]


def company_digest(company: str) -> str:
	return ops_digest(company)


# The digest that stands for "the site" where no company applies, and where a site has no encryption key.
SITE_DIGEST = "site"


def company_digest_or(company: str, default: str | None = None) -> str | None:
	"""`company_digest`, or `default` on a site with no encryption key (nothing can be keyed by a digest then)."""
	try:
		return company_digest(company)
	except RuntimeError:
		return default


def day_counts(digest: str, date: str) -> dict[str, int]:
	"""The day's outcome counters of one company: class -> count; {} when there are none or the cache is down.

	The counters are a Redis hash `jarvis:pcal:reader:day:<digest>:<date>` written with HINCRBY.
	"""
	try:
		key = frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest}:{date}")
		raw = frappe.cache.execute_command("HGETALL", key) or {}
		return {_text(field): int(value) for field, value in raw.items()}
	except TIMEOUTS:
		raise
	except Exception:
		return {}


def site_count(klass: str, date: str) -> int:
	"""Count one site-level event of the day and return the new count.

	One Redis string `jarvis:pcal:site:day:<date>:<klass>` (INCR, `COUNTER_TTL_S`): no company digest, because
	the permission batch has no company. `klass` is a fixed name, which may end in a doctype name. A cache error
	propagates: the caller decides what an unknown count means.
	The count and the expiry are two commands: a failure between them leaves a day counter without an expiry
	(a counter of at most one site day, deleted by `reset_breaker`), which is accepted.
	"""
	key = frappe.cache.make_key(f"jarvis:pcal:site:day:{date}:{klass}")
	count = frappe.cache.execute_command("INCR", key)
	frappe.cache.execute_command("EXPIRE", key, COUNTER_TTL_S)
	return int(count)


def reader_outcomes(records: list[dict], date: str) -> None:
	"""Log and count the records of `routing.read_side`; never raises (a job deadline excepted).

	Called after the snapshot view has ended, in a `finally`, so a failed job still reports. Per record:

	* an INFO line for a counted routed side, with the company as its digest (none when the reader never
	  saw the side: mode off, today served it);
	* for a routed side, one Redis day counter per class `jarvis:pcal:reader:day:<digest>:<date>`
	  (HINCRBY, 35 days): `served`, a step (`busy`, `memo`, `budget`, `cache`, `probe`, `v15`, `no_key`),
	  `ineligible` and `ineligible:<reason>`, `sales_person_scope`, `probe_error:<class>`, and the failure
	  classes `fallback:<reason>`, `reader_refused:<code>`, `reader_exception:<class>`;
	* a WARNING when the view lasted more than `VIEW_WARN_MS`;
	* an Error Log row for a failure class, once per company, class and day (`error_log_once`); no row when
	  the cache cannot answer. The other classes are counted only: `daily_report` carries them all.

	A line or a row holds fixed words and numbers: never `str(exc)`, a traceback or a name. Each text that
	came from a record is checked against `_SAFE_TEXT` and replaced by `unknown` otherwise.
	"""
	for record in records:
		try:
			_outcome(record, date)
		except TIMEOUTS:
			raise
		except Exception:
			_warn("reader_outcome_unavailable")


def daily_report(digest: str, date: str, extra: dict[str, int] | None = None) -> str | None:
	"""The message of one company's daily row, or None when nothing but `served` was counted that day.

	It lists every counter of the day, `served` included, so the operator sees the magnitude of each
	failure and of each refusal that has no row of its own (`probe`, `busy`, `ineligible`...). `extra`
	adds counters the caller measured itself (`listed_but_missing`). Names are checked against `_SAFE_TEXT`.
	"""
	counts = {
		name: value for name, value in {**day_counts(digest, date), **(extra or {})}.items() if value > 0
	}
	if not any(name != SERVED for name in counts):
		return None
	failures = sum(
		value for name, value in counts.items() if name.startswith(FAILURE_PREFIXES + (PROBE_ERROR + ":",))
	)
	listed = " ".join(
		f"{name}={value}" for name, value in sorted(counts.items()) if _SAFE_TEXT.fullmatch(name)
	)
	return f"company={digest} date={date} failures={failures} {listed}"


def _outcome(record: dict, date: str) -> None:
	classes = _outcome_classes(record)
	digest = record.get("company_digest")
	digest = digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None
	recovered = False
	if classes and digest and _DATE.fullmatch(date):
		primary = classes[0][0]
		recovered = primary == SERVED and _failures_today(digest, date)
		for name, _ in classes:
			_count(digest, date, name)
	if classes:
		# Only a side the reader was tried for (or refused by a step): mode off logs nothing.
		_info(record, classes[0][0], digest, recovered)
	if classes and _number(record.get("view_ms")) > VIEW_WARN_MS:
		_warn(f"reader_view_slow ms={_number(record.get('view_ms'))} company={digest or '-'}")
	if digest:
		for name, row in classes:
			if row:
				error_log_once(
					READER_TITLE,
					_row_message(record, name, digest),
					f"reader:{digest}:{name}",
					"reader",
					window=OUTCOME_ROW_WINDOW,
					when_cache_down=False,
				)


def _outcome_classes(record: dict) -> list[tuple[str, bool]]:
	"""[(counter name, writes an Error Log row)] of a routed side; [] when the side is not counted."""
	if not record.get(ROUTED):
		return []
	if record.get("path") == "reader" and record.get("outcome") == "served":
		return [(SERVED, False)]
	classes: list[tuple[str, bool]] = []
	if record.get(FALLBACK):
		reason = _safe(record[FALLBACK])
		if reason in COUNTED_ONLY_FALLBACKS:
			classes.append((reason, False))
			if reason == "ineligible":
				details = {_safe(item) for item in record.get(DETAIL) or [] if isinstance(item, str)}
				classes += [(f"ineligible:{item}", False) for item in sorted(details)]
		else:
			classes.append((f"fallback:{reason}", True))
	if record.get(REFUSED):
		classes.append((f"reader_refused:{_safe(record[REFUSED])}", True))
	if record.get(EXCEPTION):
		# One row per failure is enough: a statement error already has its `fallback:reader_error` row.
		classes.append((f"reader_exception:{_safe(record[EXCEPTION])}", not classes))
	if classes:
		return classes
	step = record.get(STEP)
	if record.get(PROBE_ERROR):
		return [(f"probe_error:{_safe(record[PROBE_ERROR])}", False)]
	if step and step not in UNCOUNTED_STEPS:
		return [(_safe(step), False)]
	return []


def _safe(text) -> str:
	return text if isinstance(text, str) and _SAFE_TEXT.fullmatch(text) else "unknown"


def _number(value) -> int:
	return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _count(digest: str, date: str, klass: str) -> None:
	try:
		key = frappe.cache.make_key(f"jarvis:pcal:reader:day:{digest}:{date}")
		frappe.cache.execute_command("HINCRBY", key, klass, 1)
		frappe.cache.execute_command("EXPIRE", key, COUNTER_TTL_S)
	except TIMEOUTS:
		raise
	except Exception:
		_warn("reader_counter_unavailable")


def _failures_today(digest: str, date: str) -> bool:
	return any(
		name.startswith(FAILURE_PREFIXES) and value > 0 for name, value in day_counts(digest, date).items()
	)


def _info(record: dict, klass: str, digest: str | None, recovered: bool) -> None:
	side = record.get("side")
	fields = {
		"side": side if side in store.SIDES else "-",
		"path": _safe(record.get("path")),
		"class": klass,
		"company": digest or "-",
		"today_ms": _number(record.get("today_ms")),
		"reader_ms": _number(record.get("reader_ms")),
		"view_ms": _number(record.get("view_ms")),
		**_permission_fields(record.get("counts")),
	}
	line = "reader_outcome " + " ".join(f"{name}={value}" for name, value in fields.items())
	frappe.logger("payment_calendar").info(line + (" recovered=1" if recovered else ""))


def _permission_fields(counts) -> dict[str, str]:
	"""`permission_mode` and `permission_reason` of a side whose reader counts carry the batch permission counts.

	Empty when they do not (the batch switch is off): the line is then byte-identical to the one without them.
	`permission_mode` is `batch`, `per_doc` or `mixed`; `permission_reason` the per-document reasons joined by a
	comma, or `-`. Fixed words only.
	"""
	if not isinstance(counts, dict) or not _number(counts.get("permission_calls")):
		return {}
	batch, per_doc = (
		_number(counts.get("permission_mode_batch")),
		_number(counts.get("permission_mode_per_doc")),
	)
	reasons = sorted(
		_safe(name.removeprefix("permission_reason_"))
		for name, value in counts.items()
		if isinstance(name, str) and name.startswith("permission_reason_") and _number(value)
	)
	return {
		"permission_mode": "mixed" if batch and per_doc else "per_doc" if per_doc else "batch",
		"permission_reason": ",".join(reasons) or "-",
	}


def _row_message(record: dict, klass: str, digest: str) -> str:
	detail = [
		item for item in record.get(DETAIL) or [] if isinstance(item, str) and _SAFE_TEXT.fullmatch(item)
	]
	counts = record.get("counts") or {}
	side = record.get("side") if record.get("side") in store.SIDES else "-"
	numbers = ",".join(
		f"{name}={_number(value)}"
		for name, value in sorted(counts.items())
		if _SAFE_TEXT.fullmatch(str(name))
	)
	return (
		f"company={digest} side={side} class={klass} cause={_safe(record.get(CAUSE)) if record.get(CAUSE) else '-'} "
		f"build={_safe(record.get('reader_build'))} today_ms={_number(record.get('today_ms'))} "
		f"reader_ms={_number(record.get('reader_ms'))} view_ms={_number(record.get('view_ms'))} "
		f"counts={numbers} detail={','.join(detail)}"
	)


def _text(value) -> str:
	return value.decode() if isinstance(value, bytes) else str(value)


def _warn(line: str) -> None:
	try:
		frappe.logger("payment_calendar").warning(line)
	except Exception:
		pass
