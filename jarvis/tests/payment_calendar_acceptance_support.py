"""Helpers of the manual acceptance runs of the built-in reader (`payment_calendar_reader_acceptance`).

Test code only. Nothing here names a company, party or voucher in its output: identities are HMAC digests
under the site's `encryption_key`, and a difference is reported by field name, never by value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import resource
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import frappe
from frappe.model.meta import get_field_precision
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day
from pypika.functions import Cast

from jarvis.payment_calendar import builtin_reader
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, ReaderFallback
from jarvis.payment_calendar.shadow import canonical, canonical_ids, today_unguarded
from jarvis.tests.payment_calendar_fixtures import is_calendar_test_site

TEST_SITE = "test_jarvis"
PLAYGROUND_SITE = "jarvis_playground.localhost"
SIDES = ("Receivable", "Payable")
REPO = Path(__file__).resolve().parents[2]
ROW_IDENTITY = ("voucher_type", "voucher_no", "party", "account", "term", "due_date")
EXCLUDED_IDENTITY = ("voucher_type", "voucher_no", "party_type", "party")
# Timer names of `BuiltinReport` that do not nest in another one; the rest of a read is gates and assembly.
_INHERITED = []  # connections a forked child inherited; only the child ever appends
TOP_TIMERS = ("eligibility", "control", "discovery", "lines", "details", "accumulate", "build")


def require_site(expected: str) -> None:
	"""Refuse before any read or write when this is not the site the run is meant for.

	The playground functions name their site; the test-site functions (`expected` is `TEST_SITE`) accept
	only the allow-list of the calendar fixtures (`is_calendar_test_site`): `test_jarvis`, or `jarvis-ci.test`
	under CI. A site's own `allow_tests` flag is not enough: a real-data site may carry it.
	"""
	allowed = is_calendar_test_site() if expected == TEST_SITE else frappe.local.site == expected
	if not allowed:
		raise RuntimeError(f"this acceptance run is for {expected} only; the site is another one")


def prepare_out(out: str) -> Path:
	"""The output path: its folder exists and it is not in a git work tree or a `public` directory.

	The result holds HMAC digests and counts about a real company, so it must never land where a commit
	or a web server could pick it up. Any parent holding a `.git` entry (a directory, or the file of a
	linked worktree) is a work tree; any path part named `public` may be served.
	"""
	path = Path(out).resolve()
	if not path.parent.is_dir():
		raise RuntimeError("the output folder does not exist")
	if path == REPO or REPO in path.parents:
		raise RuntimeError("the output must be written outside the repository")
	if any((parent / ".git").exists() for parent in (path, *path.parents)):
		raise RuntimeError("the output must be written outside any git work tree")
	if "public" in path.parts:
		raise RuntimeError("the output must not be written inside a public directory")
	return path


def write_out(path: Path, data: dict) -> None:
	"""Write the result readable by the owner only (mode 0600, also when the file already existed)."""
	descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(descriptor, "w") as handle:
		os.fchmod(handle.fileno(), 0o600)
		handle.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


DIGEST_DOMAIN = b"pcal-acceptance"


def _digest_key() -> bytes:
	"""A key for this harness only: HMAC(encryption_key, domain), so the site key itself never keys a digest."""
	key = frappe.conf.get("encryption_key")
	if not key:
		raise RuntimeError("the site has no encryption_key; a digest needs one")
	return hmac.new(str(key).encode(), DIGEST_DOMAIN, hashlib.sha256).digest()


def digest(*parts) -> str:
	"""A stable 16-hex-digit identity for a name; not reversible without the site's key."""
	return hmac.new(_digest_key(), "\x1f".join(map(str, parts)).encode(), hashlib.sha256).hexdigest()[:16]


def window(anchor) -> tuple[str, str]:
	"""The window of the drift test: 12 months back to 11 months ahead plus 30 days."""
	return str(get_first_day(add_months(anchor, -12))), str(
		add_days(get_last_day(add_months(anchor, 11)), 30)
	)


def peak_rss_mb() -> float:
	"""Lifetime peak resident memory of THIS process (Linux reports kilobytes).

	It never goes down, so it is no measure of one read: use `run_isolated` for that.
	"""
	return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def current_rss_mb() -> float:
	"""Resident memory of this process right now."""
	with open("/proc/self/statm") as statm:
		pages = int(statm.read().split()[1])
	return round(pages * resource.getpagesize() / 2**20, 1)


def run_isolated(work, *, reopen: bool) -> tuple[dict, dict]:
	"""Run `work()` (returns a JSON-able dict) in a forked child; return it with the child's memory figures.

	The child's peak starts at the parent's resident size at the fork (Linux copies the page counts), so
	`read_growth_mb` = child peak - resident size at the fork is what the work itself added. The parent's
	lifetime peak (a fixture built in Python, an earlier read) is never in it. Wall time is measured by
	`work` itself, inside the child.

	`reopen=True` gives the child its own DB connection (committed data only; the inherited one is kept
	untouched and never closed). `reopen=False` is for a fixture the parent holds in an open transaction: the
	child then borrows the parent's connection while the parent waits, only SELECTs run on it, and
	`os._exit` ends the child without a rollback, a quit packet or any other write on the shared socket.
	The parent's transaction and savepoints are unchanged either way. Process-local caches are rebuilt by
	each child, as for a new request; Redis caches stay warm across reads.
	"""
	sys.stdout.flush()
	sys.stderr.flush()
	read_fd, write_fd = os.pipe()
	pid = os.fork()
	if pid == 0:
		code = 1
		try:
			os.close(read_fd)
			payload = _child_run(work, reopen)
			with os.fdopen(write_fd, "w") as pipe:
				pipe.write(json.dumps(payload, default=str))
			code = 0
		except BaseException:
			# Before `os._exit` (which skips every handler): without this the parent only sees a status.
			traceback.print_exc(file=sys.stderr)
			sys.stderr.flush()
		finally:
			os._exit(code)
	os.close(write_fd)
	try:
		with os.fdopen(read_fd) as pipe:
			data = pipe.read()
		_, status = os.waitpid(pid, 0)
	except BaseException:
		# An interrupt or timeout in the parent must not leave a child reading on the borrowed connection.
		_stop_child(pid)
		raise
	if status != 0 or not data:
		raise RuntimeError(f"the isolated read failed (wait status {status})")
	payload = json.loads(data)
	return payload["work"], payload["memory"]


def _stop_child(pid: int) -> None:
	"""Kill the child and reap it; a child that already ended is fine."""
	try:
		os.kill(pid, signal.SIGKILL)
	except ProcessLookupError:
		pass
	try:
		os.waitpid(pid, 0)
	except ChildProcessError:
		pass


def _child_run(work, reopen: bool) -> dict:
	baseline = current_rss_mb()
	# Kept alive until `os._exit`: freeing the inherited connection would send a quit on the parent's socket.
	_INHERITED.append(frappe.local.db)
	if reopen:
		frappe.connect(set_admin_as_user=False)
	try:
		done = work()
	finally:
		if reopen:
			frappe.local.db.close()
	peak = peak_rss_mb()
	return {
		"work": done,
		"memory": {
			"baseline_rss_mb": baseline,
			"peak_rss_mb": peak,
			"read_growth_mb": round(max(0.0, peak - baseline), 1),
		},
	}


@contextmanager
def read_only_session():
	"""From its next transaction on, the session refuses every write, also outside `consistent_read`.

	`consistent_read` starts its own READ ONLY views and refuses anything but SELECT; this mode also covers
	the rest of the run's statements, so the DB itself proves nothing is written.
	"""
	# End the open transaction (and the empty one Frappe's rollback begins): a session mode applies to the
	# NEXT transaction only.
	frappe.db.rollback()
	frappe.db.sql("ROLLBACK")
	frappe.db.sql("SET SESSION TRANSACTION READ ONLY")
	try:
		yield
	finally:
		# Session first: the rollback opens the next transaction with the session's mode.
		frappe.db.sql("SET SESSION TRANSACTION READ WRITE")
		frappe.db.rollback()


def attempt(function, *args, **kwargs):
	try:
		return function(*args, **kwargs), None
	except Exception as error:
		return None, error


def today_lifted(*args):
	"""Today's read with its size guards lifted: the oracle of the reader at any company size. The shadow's lift."""
	return today_unguarded(*args)


def error_label(error) -> str:
	if isinstance(error, ReaderFallback):
		return f"fallback:{error.reason}"
	if isinstance(error, CalendarSourceUnavailable):
		return f"refusal:{error.code}"
	return f"error:{type(error).__name__}"


def compare_reads(reader, today) -> dict:
	"""Classify one reader outcome against today's: compared, refusal or fallback, with a count of diffs.

	`reader` and `today` are `(result, error)` pairs. Only counts, field names and digests are returned.
	"""
	(result, error), (expected, expected_error) = reader, today
	if isinstance(error, ReaderFallback):
		return {"outcome": "fallback", "reason": error.reason, "diffs": 0}
	if error or expected_error:
		same = (
			error is not None
			and expected_error is not None
			and error_label(error) == error_label(expected_error)
			and (isinstance(error, CalendarSourceUnavailable) or str(error) == str(expected_error))
		)
		return {
			"outcome": "refusal",
			"diffs": 0 if same else 1,
			"reader": error_label(error) if error else "served",
			"today": error_label(expected_error) if expected_error else "served",
		}
	found = result_diff(result, expected)
	found["outcome"] = "compared"
	found["ids"] = "equal"
	if found["diffs"] == 0 and canonical_ids(result) != canonical_ids(expected):
		# An id is a position in the whole result: an id difference counts as a difference, like a row's.
		found["ids"] = "differ"
		found["diffs"] += 1
	return found


def result_diff(result: dict, expected: dict) -> dict:
	"""Differences of two results by row identity digest and field name; ids are compared apart."""
	top = sorted(
		key
		for key in set(result) | set(expected)
		if key not in ("rows", "excluded_rows") and result.get(key) != expected.get(key)
	)
	details = {"top_level_fields": top}
	diffs = len(top)
	for section, identity in (("rows", ROW_IDENTITY), ("excluded_rows", EXCLUDED_IDENTITY)):
		found = _section_diff(result.get(section, []), expected.get(section, []), identity)
		details[section] = found
		diffs += len(found["missing"]) + len(found["extra"]) + len(found["changed"])
	if diffs == 0 and canonical(result) != canonical(expected):
		details["unclassified"] = True
		diffs = 1
	return {"diffs": diffs, "details": details}


def _section_diff(rows, expected, identity) -> dict:
	mine, theirs = _by_identity(rows, identity), _by_identity(expected, identity)
	missing, extra, changed = [], [], []
	for key in sorted(set(mine) | set(theirs)):
		pairs = list(zip(mine.get(key, []), theirs.get(key, []), strict=False))
		extra += [key] * max(0, len(mine.get(key, [])) - len(pairs))
		missing += [key] * max(0, len(theirs.get(key, [])) - len(pairs))
		for row, other in pairs:
			fields = sorted(name for name in set(row) | set(other) if row.get(name) != other.get(name))
			if fields:
				changed.append({"key": key, "fields": fields})
	return {"missing": missing, "extra": extra, "changed": changed}


def _by_identity(rows, identity) -> dict:
	grouped = {}
	for row in rows:
		body = {name: value for name, value in row.items() if name != "id"}
		grouped.setdefault(digest(*(row.get(name) for name in identity)), []).append(body)
	return {
		key: sorted(bodies, key=lambda body: json.dumps(body, sort_keys=True, default=str))
		for key, bodies in grouped.items()
	}


def settled_rows(result: dict | None) -> int | None:
	"""Rows of the settled overlay (remaining 0); the overlay is the part a discovery miss would drop."""
	return None if result is None else sum(row["settlement"] == "Settled" for row in result["rows"])


def read_both(args) -> dict:
	"""The reader (timed, with stats) and today's read (guards lifted) on the same `args`.

	The caller owns the transaction view. Returns the two pairs, the reader's stats and seconds.
	"""
	stats = {}
	started = time.perf_counter()
	reader = attempt(builtin_reader.read_obligations, *args, stats=stats)
	seconds = time.perf_counter() - started
	return {
		"reader": reader,
		"today": attempt(today_lifted, *args),
		"stats": stats,
		"seconds": round(seconds, 3),
	}


def reader_record(read: dict, args) -> dict:
	"""What the gate needs from one side: outcome, diffs, control total, overlay counts, timings."""
	found = compare_reads(read["reader"], read["today"])
	stats = read["stats"]
	return {
		**found,
		"fallback": found.get("reason"),
		"control": stats.get("control") or {},
		"control_passed": (stats.get("control") or {}).get("passed"),
		"counts": stats.get("counts", {}),
		"timings_ms": stats.get("timings_ms", {}),
		"reader_seconds": read["seconds"],
		"settled_rows": {"reader": settled_rows(read["reader"][0]), "today": settled_rows(read["today"][0])},
		"rows": {
			"reader": len(read["reader"][0]["rows"]) if read["reader"][0] else None,
			"today": len(read["today"][0]["rows"]) if read["today"][0] else None,
		},
	}


def stale_headers(company: str, side: str) -> dict:
	"""Invoices whose stored outstanding differs from the ledger's (read only; the expected fallback rate).

	Same figure as ERPNext's `update_voucher_outstanding` (all lines of the invoice, any date, account
	currency), rounded to the field's precision. An invoice without ledger lines is not counted, as there
	ERPNext leaves the header alone.
	"""
	doctype, account_field = builtin_reader.SIDES[side]
	party_field = "customer" if side == "Receivable" else "supplier"
	ple = frappe.qb.DocType("Payment Ledger Entry")
	accounts = frappe.get_all("Account", filters={"account_type": side, "company": company}, pluck="name")
	rows = (
		frappe.qb.from_(ple)
		.select(
			ple.against_voucher_no, ple.account, ple.party, Cast(Sum(ple.amount_in_account_currency), "CHAR")
		)
		.where(
			(ple.company == company)
			& (ple.delinked == 0)
			& (ple.against_voucher_type == doctype)
			& ple.account.isin(accounts or [""])
		)
		.groupby(ple.against_voucher_no, ple.account, ple.party)
		.run()
	)
	ledger = {(name, account, party): amount for name, account, party, amount in rows}
	field = frappe.get_meta(doctype).get_field("outstanding_amount")
	currency = frappe.get_cached_value("Company", company, "default_currency")
	counted = stale = 0
	for header in frappe.get_all(
		doctype,
		filters={"company": company, "docstatus": 1},
		fields=["name", "outstanding_amount", "party_account_currency", party_field, account_field],
	):
		amount = ledger.get((header["name"], header[account_field], header[party_field]))
		if amount is None:
			continue
		counted += 1
		precision = get_field_precision(field, currency=header["party_account_currency"] or currency)
		stale += flt(header["outstanding_amount"], precision) != flt(amount, precision)
	return {"invoices_with_lines": counted, "stale": stale}
