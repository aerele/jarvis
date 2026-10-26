"""Capture user-facing UI errors and code-level exceptions, buffer them locally,
and shape them for the out-of-band push to the admin control plane.

Every way a jarvis error can reach a user is captured (all feed
``jarvis.error_push``):

  - ``report_client_errors`` - the whitelisted endpoint the SPA / PWA / Desk
    reporters POST to. Session-authed. Scrubs message/stack of PII / ERP values,
    fingerprints for dedupe, and folds occurrences into the ``Jarvis Client
    Error`` buffer doctype.
  - ``collect_error_log`` - reads new Frappe ``Error Log`` rows (every unhandled
    server / RQ-worker / cron exception already lands there), **filtered to the
    jarvis app only**, and normalizes them. Error Log IS the storage, so this
    only reads + shapes; ``error_push`` owns the watermark.
    ``tag_error_log_origin`` stamps the jarvis caller onto a row written by
    ``frappe.log_error(title, message)``, which stores no traceback.
  - ``capture_failed_request`` - a jarvis API call that answered 4xx: a
    ``frappe.throw``, permission or validation error the user saw. Frappe
    writes no Error Log row for these (only 5xx), so nothing else sees them.
  - ``record_tool_failure`` - a tool that failed and returned ``{ok: false}``
    to the agent (a 200, so no other lane sees it).
  - ``collect_failed_turns`` - every turn that ended ``errored``, read from the
    durable turn row whether or not a browser was open to see it fail.

The server-side lanes write the same buffer as the browser, under a
``SERVER_SURFACES`` surface that sets their kind and severity.

The core privacy rule lives here: only a *scrubbed* copy of any error text ever
leaves the bench. The reporting user's own email is preserved (admin already
knows it via usage rollups); every other email, amount, quantity, quoted data
value, long hash and secret-shaped token is redacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

import frappe

from jarvis._redis_lock import redis_lock
from jarvis.audit import _SECRET_KEYS

DT = "Jarvis Client Error"

#: Hard bounds so a hostile or runaway client can't flood one call.
MAX_ERRORS_PER_CALL = 50
MAX_MESSAGE = 500
MAX_STACK = 2000

#: Server-side ceiling on the endpoint: the client caps are advisory (curl
#: ignores them), so cap accepted batches per user per minute. Past this the call
#: returns early WITHOUT touching the DB - each accepted batch is up to
#: MAX_ERRORS_PER_CALL get_value+save cycles, so this is the real DoS guard.
REPORT_RATE_PER_MIN = 60

#: How many Error Log rows one page of the scan reads.
ERROR_LOG_SCAN_LIMIT = 300

#: Surfaces the server writes into the buffer (never a browser), with the
#: severity each is sent at. A tool or API failure the user can act on is a
#: warning; a turn that failed outright is an error.
SERVER_SURFACES = {"turn": "error", "tool": "warning", "api": "warning"}

#: Ceiling on server-side buffer writes per user per minute. Past it the
#: occurrence is dropped: one runaway loop must not flood the buffer.
SERVER_RATE_PER_MIN = 120

#: Positive "this browser error came from another app, not Jarvis" evidence -
#: an asset path only a non-Jarvis Desk bundle loads. Used as a server-side
#: backstop to the client-side scoping (a stray ERPNext/HRMS Desk error must not
#: cross to the control plane). See _looks_non_jarvis.
_NONJARVIS_ASSET_MARKERS = (
	"/assets/frappe/",
	"/assets/erpnext/",
	"/assets/hrms/",
	"/assets/payments/",
	"/assets/lms/",
)


# --------------------------------------------------------------------------- #
# Scrubbing (balanced)
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# secret-shaped key=value / key: value (key contains password|pwd|secret|token|key)
_KV_SECRET_RE = re.compile(
	r"(?i)\b(\w*(?:" + "|".join(_SECRET_KEYS) + r")\w*)\b\s*[=:]\s*[^\s,;)]+",
)
# long hex / base64-ish blobs (hashes, ids, tokens)
_LONG_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{24,}\b")
# fixed-width UPPERCASE alphanumeric IDs under the blob threshold that mix
# letters AND digits: GSTIN (27AAPFU0939F1ZV), PAN, batch/serial codes. These
# are ERP identifiers, not taxonomy. Restricted to [A-Z0-9] on purpose: a
# CamelCase class name that carries a digit ("OAuth2Error", "Http404Error",
# "S3UploadError") always has a lowercase letter, so it stays intact; a pure
# uppercase ID does not. A pure-letter token (an exception class) has no digit;
# a pure-digit run is handled by _NUMBER_RE.
_ID_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{10,23}\b")
# decimals, thousand-grouped, or 4+ digit runs = amounts / quantities / ids
_NUMBER_RE = re.compile(r"(?<![\w.])(?:\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{4,})(?![\w])")
# any quoted literal - redacted unless it sits in JSON object-key position (see
# _redact_quoted). ERP messages quote entity names ('Tata Steel Ltd') that carry
# no digit and sit under 24 chars, so the old length/digit heuristic leaked them.
# A traceback's `File "..."` path is dropped too; the frame's function name +
# line survive unquoted, which is what triage needs.
_QUOTED_RE = re.compile(r"(['\"])((?:\\.|(?!\1).)*?)\1")
# a run of 2+ Capitalised words - the shape of an *unquoted* ERP entity name
# ("Employee Priya Sharma", "Supplier Reliance Industries"). Most
# frappe.throw(_("...{0}...").format(name)) messages interpolate the name bare,
# so it is never quoted and no value regex above would catch it. CamelCase
# exception classes ("ValueError", "TypeError") are single tokens and are not a
# run, so the taxonomy survives.
_NAME_RUN_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z0-9]*){1,}\b")


def _redact_quoted(m: re.Match) -> str:
	# Preserve a quoted literal ONLY in true JSON object-key position: preceded
	# by '{' or ',' (start of an object/member) and followed by ':' (the key/value
	# separator), whitespace either side allowed. A one-sided "followed by ':'"
	# check is not enough - ordinary prose like "Item 'widget': not found" would
	# leak the value, since colons show up outside JSON too. Every other quoted
	# literal, including a VALUE that happens to look like a key (e.g. a string
	# value "provider"), is a candidate data value and is redacted unconditionally.
	# Keeping "path-shaped" quotes was tempting but unsafe - a base64 secret
	# contains '/' too, and _LONG_BLOB_RE already drops long paths regardless.
	head = m.string[: m.start()].rstrip()
	tail = m.string[m.end() :].lstrip()
	if head.endswith(("{", ",")) and tail.startswith(":"):
		return m.group(0)
	return m.group(1) + "[VAL]" + m.group(1)


def _scrub_error_text(text: str | None, *, keep_email: str | None = None) -> str:
	"""Redact PII / ERP values from a free-text error message or traceback.

	Deliberately conservative on structure (keeps error codes, class names, field
	names, frame functions, and JSON object keys) while removing values: emails
	(except the reporting user's own), secret-shaped tokens, long hashes, amounts /
	quantities / long ids, and both *quoted* and *bare capitalised* entity names.
	A quoted literal in JSON key position (see _redact_quoted) survives; every
	quoted value does not. The name redaction is blunt on purpose - the failure
	mode of a blocklist here (a leaked customer / supplier / employee name crossing
	benches to the control plane) is worse than over-redacting a doctype label."""
	if not text:
		return ""
	out = str(text)
	out = _KV_SECRET_RE.sub(lambda m: m.group(1) + "=[REDACTED]", out)
	out = _LONG_BLOB_RE.sub("[BLOB]", out)

	def _email_sub(m: re.Match) -> str:
		return m.group(0) if keep_email and m.group(0).lower() == keep_email.lower() else "[EMAIL]"

	out = _EMAIL_RE.sub(_email_sub, out)
	out = _ID_RE.sub("[ID]", out)
	out = _QUOTED_RE.sub(_redact_quoted, out)
	out = _NAME_RUN_RE.sub("[NAME]", out)
	out = _NUMBER_RE.sub("[NUM]", out)
	return out


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #
_NORM_DIGITS = re.compile(r"\d+")

#: Grouping-key version. The UI fingerprint is computed from the SCRUBBED message
#: (see _ingest_one), which doubles as the normalizer that collapses different
#: entity names into one group - so one bug across N customers stays one row at
#: count=N, not N rows at count=1. That couples grouping to redaction policy: any
#: change to _scrub_error_text would silently re-key the whole admin feed. Bump
#: this constant in the SAME commit as any scrubber change, so the re-key becomes
#: an intentional, dated event and the old rows age out via the 90-day prune.
_SCRUB_VERSION = "2"


def _fingerprint(error_code: str, error_class: str, message: str, surface: str) -> str:
	"""Stable dedupe key for a UI error - digits normalized out of the message so
	'... 42 units' and '... 7 units' collapse to one group. Fed the SCRUBBED
	message by _ingest_one, so entity-name variants ('Tata Steel' vs 'Acme') that
	both scrub to '[VAL]'/'[NAME]' collapse to one group too. Versioned by
	_SCRUB_VERSION so a scrubber change re-keys deliberately, not silently."""
	norm = _NORM_DIGITS.sub("#", (message or "").lower())[:200]
	raw = f"{_SCRUB_VERSION}|{error_code}|{error_class}|{surface}|{norm}"
	return hashlib.sha1(raw.encode()).hexdigest()


def _traceback_fingerprint(exc_class: str, frames: list[str]) -> str:
	raw = exc_class + "\n" + "\n".join(frames[:8])
	return hashlib.sha1(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Lane 1: the browser reporter endpoint
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
def report_client_errors(errors: str | list) -> dict:
	"""Accept a batch of scrubbed-on-arrival UI errors from a Jarvis surface.

	Session-authed (any logged-in user of this bench); the client never picks the
	user - we stamp ``frappe.session.user``. Returns how many rows were accepted.
	Bad rows are skipped, never fatal.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.AuthenticationError

	if _over_report_rate_limit(user):
		# Soft-throttle: bail before any DB work. The client treats a non-ok
		# result as "drop" (best-effort telemetry), so no retry storm.
		return {"ok": False, "accepted": 0, "throttled": True}

	if isinstance(errors, str):
		try:
			errors = json.loads(errors)
		except Exception:
			return {"ok": False, "accepted": 0}
	if not isinstance(errors, list):
		return {"ok": False, "accepted": 0}

	accepted = 0
	for raw in errors[:MAX_ERRORS_PER_CALL]:
		if not isinstance(raw, dict):
			continue
		try:
			if _ingest_one(raw, user):
				accepted += 1
		except Exception:
			# One malformed row must not sink the batch.
			frappe.logger("jarvis.client_errors").debug("client error row dropped", exc_info=True)
	return {"ok": True, "accepted": accepted}


def _over_report_rate_limit(
	user: str, scope: str = "client_error_report", limit: int = REPORT_RATE_PER_MIN
) -> bool:
	"""Per-user calendar-minute bucket. Atomic ``incrby`` (not get-then-set): it
	is race-free under concurrent requests, and - unlike get_value/set_value with
	a TTL - it is not fooled by frappe's request-local cache when the same request
	checks more than once. The key self-expires, so old buckets never accumulate."""
	if frappe.flags.in_test:
		return False
	bucket = int(time.time()) // 60
	# incrby is a raw redis-py call that bypasses RedisWrapper.make_key's site
	# prefix, so prefix the site ourselves - otherwise a multi-site bench sharing
	# one Redis would let two sites' same-named users share a bucket.
	key = f"{frappe.local.site}:jarvis.{scope}.{user}.{bucket}"
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 120)
	return count > limit


def _looks_non_jarvis(surface: str, route: str, stack: str) -> bool:
	"""Server-side backstop mirroring lane 2's jarvis-only filter for lane 1.

	The SPA/PWA reporters only load on Jarvis surfaces, so they are Jarvis-scoped
	by construction; the Desk bundle loads on every Desk page, so a stray
	ERPNext/HRMS error could reach here. Drop a row only on POSITIVE non-Jarvis
	evidence (another app's asset in the stack) and no Jarvis marker anywhere -
	so an empty-stack or genuinely-Jarvis error is always kept."""
	if "jarvis" in f"{surface}\n{route}\n{stack}".lower():
		return False
	return any(m in stack for m in _NONJARVIS_ASSET_MARKERS)


def _row_lock(user: str, fp: str) -> str:
	return "jce:" + hashlib.sha1(f"{user}|{fp}".encode()).hexdigest()[:40]


def _ingest_one(raw: dict, user: str) -> bool:
	"""Fold one browser-reported error into the local buffer. Returns True if
	stored, False if filtered out (non-Jarvis origin, or a failed turn)."""
	surface = (str(raw.get("surface") or "unknown"))[:40]
	if surface in SERVER_SURFACES:
		surface = "ui"  # a browser can't speak for a server lane (it sets severity)
	error_class = (str(raw.get("error_class") or ""))[:140]
	if error_class == "RunError":
		# A failed turn is read from its durable row (collect_failed_turns), which
		# sees it even with no browser open. Counting the browser's copy as well
		# would double every failed turn on admin.
		return False

	# Origin check runs on the RAW stack/route - scrubbing would blob the asset
	# path (`/assets/erpnext/...` -> `[BLOB]`) and hide the very marker we filter on.
	if _looks_non_jarvis(surface, str(raw.get("route") or ""), str(raw.get("stack") or "")):
		return False

	_fold(
		surface=surface,
		error_code=raw.get("error_code"),
		error_class=error_class,
		message=raw.get("message"),
		stack=raw.get("stack"),
		route=raw.get("route"),
		user=user,
		conversation=raw.get("conversation"),
		run_id=raw.get("run_id"),
		lock_wait_s=2,
	)
	return True


def _fold(
	*,
	surface: str,
	error_code,
	error_class,
	message,
	stack,
	route,
	user: str,
	conversation=None,
	run_id=None,
	lock_wait_s: float,
) -> None:
	"""Scrub one occurrence and fold it into the buffer: a new row, or one more
	on the unpushed row with the same fingerprint for this user."""
	surface = str(surface or "unknown")[:40]
	error_code = str(error_code or "")[:64]
	error_class = str(error_class or "")[:140]
	route = _clean_route(route)
	conversation = str(conversation or "")[:140]
	run_id = str(run_id or "")[:140]
	message = _scrub_error_text(message, keep_email=user)[:MAX_MESSAGE]
	stack = _scrub_error_text(stack, keep_email=user)[:MAX_STACK]
	# Fingerprint the SCRUBBED message: it doubles as the grouping normalizer, so
	# "Customer 'Tata Steel'" and "Customer 'Acme'" both scrub to "Customer '[VAL]'"
	# and collapse to one group - one bug across N customers is one row at count=N,
	# not N rows at count=1. The scrubber-coupling that creates (a scrubber change
	# would re-key the feed) is made explicit and deliberate via _SCRUB_VERSION,
	# baked into _fingerprint. Lane 2 keys on raw frames, so it is scrub-stable.
	# A server row's route is the endpoint or tool that failed, so the same
	# message from two endpoints stays two rows. A browser route is a page path
	# (it carries conversation ids), so browser rows keep grouping without it.
	group_by = f"{error_class}|{route}" if surface in SERVER_SURFACES else error_class
	fp = _fingerprint(error_code, group_by, message, surface)

	# Serialize concurrent reports of the SAME error (same user+fingerprint) so the
	# check-then-act below can't double-insert, and two occurrences can't both read
	# the same count and lose an increment. Uses the repo's portable Redis mutex
	# (jarvis._redis_lock) rather than a MySQL GET_LOCK: it is DB-agnostic and does
	# not pin the worker's DB connection. Best-effort with a short bounded wait -
	# different errors take different locks and never contend; on contention/timeout
	# we proceed unlocked (a rare duplicate row is harmless: admin folds by
	# (tenant, fingerprint)).
	with redis_lock(_row_lock(user, fp), timeout_s=10, blocking_timeout_s=lock_wait_s):
		now = frappe.utils.now_datetime()
		existing = frappe.db.get_value(DT, {"fingerprint": fp, "user": user, "pushed": 0}, "name")
		if existing:
			doc = frappe.get_doc(DT, existing)
			doc.count = (doc.count or 1) + 1
			doc.last_seen = now
			doc.message = message or doc.message
			doc.stack = stack or doc.stack
			doc.route = route or doc.route
			doc.save(ignore_permissions=True)
			return

		frappe.get_doc(
			{
				"doctype": DT,
				"surface": surface,
				"route": route,
				"error_code": error_code,
				"error_class": error_class,
				"message": message,
				"stack": stack,
				"user": user,
				"conversation": conversation,
				"run_id": run_id,
				"fingerprint": fp,
				"count": 1,
				"first_seen": now,
				"last_seen": now,
			}
		).insert(ignore_permissions=True)


def _clean_route(route) -> str:
	"""Path only - strip query string and fragment so ids in the URL don't ride
	along."""
	if not route:
		return ""
	r = str(route)
	for sep in ("?", "#"):
		r = r.split(sep, 1)[0]
	return r[:200]


# --------------------------------------------------------------------------- #
# Lane 2: the Error Log reader (code-level exceptions, jarvis-only)
# --------------------------------------------------------------------------- #
#: The jarvis package inside the bench, as a traceback names it. Frappe strips
#: the bench path from every traceback it stores (``frappe.get_traceback``
#: replaces ``<bench>/`` with ""), so a real Error Log frame reads
#: ``File "apps/jarvis/jarvis/chat/api.py"`` with no leading slash. Matching
#: ``/apps/jarvis/`` instead dropped almost every jarvis error from the feed.
#: This form matches both the stored (relative) and a raw (absolute) path, and
#: never ``apps/jarvis_admin/`` or ``apps/jarvis_partner/``.
_JARVIS_APP_MARKER = "apps/jarvis/jarvis/"
_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\w+)')
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt)):?\s*(.*)$")

#: The error-reporting pipeline's OWN modules. A failure here (e.g. admin
#: unreachable) must never be forwarded - it would report on its own outage and,
#: once admin returns, flood the feed with a report per failed cycle. These logs
#: stay local (Error Log / journalctl) where the operator can still see them.
_REPORTER_SELF_MARKERS = (
	_JARVIS_APP_MARKER + "error_push.py",
	_JARVIS_APP_MARKER + "api_errors.py",
	# GAP 1 heartbeat: its own push-path errors are infra, not customer-relevant — do not
	# forward them into the tenant Errors feed (would be a fleet-wide false positive).
	_JARVIS_APP_MARKER + "chat/heartbeat.py",
)


def is_jarvis_error(method: str | None, traceback: str | None) -> bool:
	"""True only for exceptions that originate in the jarvis app - so a
	customer's ERPNext / framework / other-app errors never leave their bench.

	Matches the jarvis app path in the traceback, or a ``jarvis.*`` dotted
	``method`` title (excluding the un-installed ``jarvis_admin`` checkout).
	Excludes the reporter's own modules so error reporting can't report itself."""
	tb = traceback or ""
	if any(marker in tb for marker in _REPORTER_SELF_MARKERS):
		return False
	if _JARVIS_APP_MARKER in tb:
		return True
	m = (method or "").strip()
	return m.split(".", 1)[0] == "jarvis"


def _parse_traceback(traceback: str) -> tuple[str, str, list[str]]:
	"""Return (exc_class, exc_message, jarvis_frame_signatures) from a Python
	traceback string."""
	lines = [ln for ln in (traceback or "").splitlines() if ln.strip()]
	exc_class, exc_message = "Exception", ""
	for ln in reversed(lines):
		m = _EXC_LINE_RE.match(ln.strip())
		if m:
			exc_class, exc_message = m.group(1), m.group(2)
			break
	frames: list[str] = []
	for fm in _FRAME_RE.finditer(traceback or ""):
		path, func = fm.group(1), fm.group(2)
		if _JARVIS_APP_MARKER in path:
			rel = "jarvis/" + path.split(_JARVIS_APP_MARKER, 1)[1]
			frames.append(f"{rel}:{func}")
	return exc_class, exc_message, frames


#: Error Log.owner values that are not a real reporting user - mirrors the
#: convention used elsewhere in this app (e.g. jarvis.chat.agent_runs).
_NON_USER_OWNERS = ("Administrator", "Guest")


def _error_log_user_ref(owner: str | None) -> str:
	"""``owner`` when it names a real user, else "" - the same per-user
	attribution ``_collect_ui_errors`` already sends for UI errors
	(``jarvis.error_push``), extended to code-level exceptions so admin's
	per-user error rollup covers both lanes."""
	if not owner or owner in _NON_USER_OWNERS:
		return ""
	return owner


#: How many Error Log rows one push cycle reads at most, in pages of
#: ERROR_LOG_SCAN_LIMIT. A bench that writes more than this in 5 minutes falls
#: behind for a while, but the cursor never skips a row.
ERROR_LOG_MAX_SCAN = 3000

#: Most distinct errors one cycle sends per scanned lane. Admin refuses a batch
#: over 600 errors, so error_push keeps the sum of every lane's cap under it.
MAX_GROUPS = 200


def _split_cursor(cursor: str | None) -> tuple[str | None, str]:
	"""A scan cursor is ``"<timestamp>|<name>"`` (rows sharing a timestamp are
	ordered by name, so none is skipped). A bare timestamp, as older versions
	stored, means "after this instant"."""
	if not cursor:
		return None, ""
	ts, _sep, name = str(cursor).partition("|")
	return ts, name


def _after_cursor(field, name_field, cursor: str | None):
	ts, name = _split_cursor(cursor)
	if ts is None:
		return None
	return (field > ts) | ((field == ts) & (name_field > name))


class _Groups:
	"""Folds scanned occurrences into one row per (fingerprint, user), so a
	burst of the same failure is one row with a count, not hundreds of rows."""

	def __init__(self, max_groups: int):
		self.max_groups = max_groups
		self.rows: dict[tuple[str, str], dict] = {}

	def full_for(self, key: tuple[str, str]) -> bool:
		return key not in self.rows and len(self.rows) >= self.max_groups

	def add(self, key: tuple[str, str], row: dict, at: str) -> None:
		have = self.rows.get(key)
		if have is None:
			self.rows[key] = {**row, "count": 1, "first_seen": at, "last_seen": at}
			return
		# Keep the latest sample; scans run oldest-first.
		have.update({**row, "count": have["count"] + 1, "first_seen": have["first_seen"], "last_seen": at})

	def values(self) -> list[dict]:
		return list(self.rows.values())


def collect_error_log(
	since: str | None,
	limit: int = ERROR_LOG_MAX_SCAN,
	*,
	upto: str | None = None,
	max_groups: int = MAX_GROUPS,
) -> dict:
	"""Read new ``Error Log`` rows after the ``since`` cursor, keep only
	jarvis-origin ones, and fold them into the push shape.

	``upto`` bounds the scan to rows created at or before it, so a row whose
	transaction commits a little late isn't passed by the cursor. Returns
	``{"rows": [...], "watermark": <cursor or None>}``. The cursor advances past
	EVERY scanned row (jarvis or not) so skipped framework errors are never
	re-scanned, and stops before a row that would open a group past
	``max_groups``, which the next cycle then reads."""
	el = frappe.qb.DocType("Error Log")
	groups = _Groups(max_groups)
	watermark = since
	scanned = 0
	while scanned < limit:
		q = (
			frappe.qb.from_(el)
			.select(el.name, el.method, el.error, el.creation, el.owner)
			.orderby(el.creation)
			.orderby(el.name)
			.limit(min(ERROR_LOG_SCAN_LIMIT, limit - scanned))
		)
		after = _after_cursor(el.creation, el.name, watermark)
		if after is not None:
			q = q.where(after)
		if upto:
			q = q.where(el.creation <= upto)
		logs = q.run(as_dict=True)
		if not logs:
			break
		for log in logs:
			try:
				row = _error_log_row(log) if is_jarvis_error(log.method, log.error) else None
			except Exception:
				row = None  # one unreadable row must not stall the lane behind it
			if row:
				key = (row["fingerprint"], row["user_ref"])
				if groups.full_for(key):
					return {"rows": groups.values(), "watermark": watermark}
				groups.add(key, row, str(log.creation))
			watermark = f"{log.creation}|{log.name}"
		scanned += len(logs)
		if len(logs) < ERROR_LOG_SCAN_LIMIT:
			break
	return {"rows": groups.values(), "watermark": watermark}


def _error_log_row(log) -> dict:
	exc_class, exc_message, frames = _parse_traceback(log.error or "")
	return {
		"kind": "exception",
		"surface": "server",
		"error_code": exc_class,
		"error_class": exc_class,
		"message": _scrub_error_text(exc_message or log.method or exc_class)[:MAX_MESSAGE],
		"stack": _scrub_error_text(log.error)[:MAX_STACK],
		"route": (log.method or "")[:200],
		"fingerprint": _traceback_fingerprint(exc_class, frames),
		"severity": "error",
		"user_ref": _error_log_user_ref(log.owner),
	}


# --------------------------------------------------------------------------- #
# Error Log origin: rows written without a traceback
# --------------------------------------------------------------------------- #
def tag_error_log_origin(doc, method=None) -> None:
	"""``before_insert`` on Error Log. ``frappe.log_error(title, message)``
	stores ``message`` as the error text, with no traceback, so a row jarvis
	wrote that way names no jarvis frame and the Error Log lane can't tell it
	apart from an ERPNext error (142 jarvis call sites log that way). When the
	text names no jarvis frame but jarvis code is on the call stack, append the
	nearest jarvis caller in traceback form, which the lane already reads.

	A row Frappe defers (a request's 500, read-only mode) skips this hook; those
	carry a full traceback, which names the jarvis frames itself. Never raises."""
	try:
		if _JARVIS_APP_MARKER in (doc.error or ""):
			return
		caller = _jarvis_caller(sys._getframe(1))
		if caller:
			doc.error = f"{(doc.error or '').rstrip()}\n\nLogged from:\n{caller}\n"
	except Exception:
		pass


def _jarvis_caller(frame) -> str | None:
	"""The innermost jarvis frame on the stack, as a traceback line."""
	package = os.path.dirname(os.path.abspath(__file__)) + os.sep
	while frame is not None:
		path = os.path.abspath(frame.f_code.co_filename)
		if path.startswith(package):
			rel = path[len(package) :].replace(os.sep, "/")
			return f'  File "{_JARVIS_APP_MARKER}{rel}", line {frame.f_lineno}, in {frame.f_code.co_name}'
		frame = frame.f_back
	return None


# --------------------------------------------------------------------------- #
# Server-side buffer writes: failed requests and failed tools
# --------------------------------------------------------------------------- #
def record_server_error(
	surface: str,
	*,
	error_code: str,
	error_class: str,
	message: str,
	route: str = "",
	stack: str = "",
	user: str | None = None,
	conversation: str = "",
	run_id: str = "",
) -> None:
	"""Buffer one server-side occurrence (``surface`` in SERVER_SURFACES) for the
	next push. Cheap and silent: never waits on a lock (a rare duplicate row
	folds on admin), never raises, and drops past a per-user rate."""
	try:
		user = user or frappe.session.user or "Guest"
		if _over_report_rate_limit(user, "server_error", SERVER_RATE_PER_MIN):
			return
		_fold(
			surface=surface,
			error_code=error_code,
			error_class=error_class,
			message=message,
			stack=stack,
			route=route,
			user=user,
			conversation=conversation,
			run_id=run_id,
			lock_wait_s=0,
		)
	except Exception:
		frappe.logger("jarvis.client_errors").debug("server error not buffered", exc_info=True)


_METHOD_PATH_RE = re.compile(r"^/api/(?:v\d+/)?method/(jarvis\.[\w.]+)")

#: Endpoints whose failures are not captured: the reporter's own (a loop).
_UNCAPTURED_METHODS = ("jarvis.api_errors.",)


def capture_failed_request(response=None, request=None) -> None:
	"""``after_request``: buffer a jarvis API call that answered 4xx.

	A ``frappe.throw``, permission or validation error is shown to the user but
	never written to Error Log (Frappe logs only 5xx, which the Error Log lane
	reads), so without this it goes unnoticed. 401 (a signed-out session) is not
	an error. Runs after Frappe has finished the request transaction, so it
	rolls back whatever the failed request left and commits only its own row.
	Never raises: an after_request hook that throws is only logged by Frappe."""
	try:
		status = int(getattr(response, "status_code", 0) or 0)
		if not 400 <= status < 500 or status == 401 or request is None:
			return
		m = _METHOD_PATH_RE.match(getattr(request, "path", "") or "")
		if not m or m.group(1).startswith(_UNCAPTURED_METHODS):
			return
		user = frappe.session.user if getattr(frappe.local, "session", None) else None
		if not user or user == "Guest" or not frappe.db:
			return
		error_class, message = _request_failure(response)
		frappe.db.rollback()
		record_server_error(
			"api",
			error_code=str(status),
			error_class=error_class or f"HTTP {status}",
			message=message,
			route=m.group(1),
			user=user,
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- after_request runs past the request commit
	except Exception:
		pass


def _request_failure(response) -> tuple[str, str]:
	"""(error class, message) of a failed request: Frappe's ``exc_type`` and
	message log, or a jarvis ``{ok: false, error}`` envelope in the body."""
	local = getattr(frappe.local, "response", None) or {}
	error_class = str(local.get("exc_type") or "")
	messages = []
	for entry in getattr(frappe.local, "message_log", None) or []:
		text = entry.get("message") if isinstance(entry, dict) else entry
		if text:
			messages.append(frappe.utils.strip_html(str(text)))
	if not messages:
		try:
			body = response.get_json(silent=True) or {}
			error = (body.get("message") or {}).get("error") if isinstance(body, dict) else None
			if isinstance(error, dict):
				error_class = error_class or str(error.get("code") or "")
				messages.append(str(error.get("message") or ""))
		except Exception:
			pass
	return error_class, " ".join(messages)[:MAX_MESSAGE]


def record_tool_failure(tool: str, error: dict | None, *, conversation: str = "", run_id: str = "") -> None:
	"""A tool that returned ``{ok: false}`` to the agent. A refusal by the user's
	write guard is policy working, not a failure, and is not recorded."""
	try:
		error = error or {}
		code = str(error.get("code") or "")
		from jarvis.tools import _write_risk

		if code in _write_risk.REFUSAL_CODES:
			return
		record_server_error(
			"tool",
			error_code=code,
			error_class=str(tool or ""),
			message=str(error.get("message") or ""),
			route=str(tool or ""),
			conversation=conversation,
			run_id=run_id,
		)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# Failed turns, from the durable turn row
# --------------------------------------------------------------------------- #
TURN_DT = "Jarvis Chat Turn"
TURN_MAX_SCAN = 2000
TURN_MAX_GROUPS = 150


def collect_failed_turns(
	since: str | None,
	limit: int = TURN_MAX_SCAN,
	*,
	upto: str | None = None,
	max_groups: int = TURN_MAX_GROUPS,
) -> dict:
	"""Turns that ended ``errored`` after the ``since`` cursor (on ``done_at``,
	which every errored transition stamps and nothing changes afterwards).

	The turn row is the one record every failed turn leaves, whichever path
	failed it and whether or not a browser saw the ``run:error``. Folded by
	(error class + scrubbed message, user). Same return shape and cursor rules
	as ``collect_error_log``."""
	turn = frappe.qb.DocType(TURN_DT)
	q = (
		frappe.qb.from_(turn)
		.select(turn.name, turn.conversation, turn.error, turn.done_at, turn.owner)
		.where(turn.state == "errored")
		.where(turn.done_at.isnotnull())
		.orderby(turn.done_at)
		.orderby(turn.name)
		.limit(limit)
	)
	after = _after_cursor(turn.done_at, turn.name, since)
	if after is not None:
		q = q.where(after)
	if upto:
		q = q.where(turn.done_at <= upto)
	groups = _Groups(max_groups)
	watermark = since
	for t in q.run(as_dict=True):
		try:
			row = _failed_turn_row(t)
		except Exception:
			row = None  # one unreadable row must not stall the lane behind it
		if row:
			key = (row["fingerprint"], row["user_ref"])
			if groups.full_for(key):
				break
			groups.add(key, row, str(t.done_at))
		watermark = f"{t.done_at}|{t.name}"
	return {"rows": groups.values(), "watermark": watermark}


def _failed_turn_row(t) -> dict:
	err = str(t.error or "")
	try:
		from jarvis.chat.error_taxonomy import classify_error_text

		code = classify_error_text(err) or "internal"
	except Exception:
		code = "internal"
	message = _scrub_error_text(err or "The turn failed.")[:MAX_MESSAGE]
	return {
		"kind": "exception",
		"surface": "turn",
		"error_code": code,
		"error_class": "TurnError",
		"message": message,
		"stack": "",
		"route": "",
		"fingerprint": _fingerprint(code, "TurnError", message, "turn"),
		"severity": SERVER_SURFACES["turn"],
		"user_ref": _error_log_user_ref(t.owner),
		"conversation_ref": str(t.conversation or "")[:140],
		"run_id": str(t.name or "")[:140],
	}
