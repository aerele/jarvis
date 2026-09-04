"""Capture user-facing UI errors and code-level exceptions, buffer them locally,
and shape them for the out-of-band push to the admin control plane.

Two capture lanes (both feed ``jarvis.error_push``):

  - ``report_client_errors`` - the whitelisted endpoint the SPA / PWA / Desk
    reporters POST to. Session-authed. Scrubs message/stack of PII / ERP values,
    fingerprints for dedupe, and folds occurrences into the ``Jarvis Client
    Error`` buffer doctype.
  - ``collect_error_log`` - reads new Frappe ``Error Log`` rows (every unhandled
    server / RQ-worker / cron exception already lands there), **filtered to the
    jarvis app only**, and normalizes them. Error Log IS the storage, so this
    only reads + shapes; ``error_push`` owns the watermark.

The core privacy rule lives here: only a *scrubbed* copy of any error text ever
leaves the bench. The reporting user's own email is preserved (admin already
knows it via usage rollups); every other email, amount, quantity, quoted data
value, long hash and secret-shaped token is redacted.
"""

from __future__ import annotations

import hashlib
import json
import re
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

#: How many Error Log rows one push cycle scans at most.
ERROR_LOG_SCAN_LIMIT = 300

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
	if accepted:
		frappe.db.commit()
	return {"ok": True, "accepted": accepted}


def _over_report_rate_limit(user: str) -> bool:
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
	key = f"{frappe.local.site}:jarvis.client_error_report.{user}.{bucket}"
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 120)
	return count > REPORT_RATE_PER_MIN


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
	"""Fold one reported error into the local buffer. Returns True if stored,
	False if filtered out (non-Jarvis origin)."""
	surface = (str(raw.get("surface") or "unknown"))[:40]
	error_code = (str(raw.get("error_code") or ""))[:64]
	error_class = (str(raw.get("error_class") or ""))[:140]
	route = _clean_route(raw.get("route"))
	conversation = (str(raw.get("conversation") or ""))[:140]
	run_id = (str(raw.get("run_id") or ""))[:140]

	# Origin check runs on the RAW stack/route - scrubbing would blob the asset
	# path (`/assets/erpnext/...` -> `[BLOB]`) and hide the very marker we filter on.
	if _looks_non_jarvis(surface, str(raw.get("route") or ""), str(raw.get("stack") or "")):
		return False

	message = _scrub_error_text(raw.get("message"), keep_email=user)[:MAX_MESSAGE]
	stack = _scrub_error_text(raw.get("stack"), keep_email=user)[:MAX_STACK]
	# Fingerprint the SCRUBBED message: it doubles as the grouping normalizer, so
	# "Customer 'Tata Steel'" and "Customer 'Acme'" both scrub to "Customer '[VAL]'"
	# and collapse to one group - one bug across N customers is one row at count=N,
	# not N rows at count=1. The scrubber-coupling that creates (a scrubber change
	# would re-key the feed) is made explicit and deliberate via _SCRUB_VERSION,
	# baked into _fingerprint. Lane 2 keys on raw frames, so it is scrub-stable.
	fp = _fingerprint(error_code, error_class, message, surface)

	# Serialize concurrent reports of the SAME error (same user+fingerprint) so the
	# check-then-act below can't double-insert, and two occurrences can't both read
	# the same count and lose an increment. Uses the repo's portable Redis mutex
	# (jarvis._redis_lock) rather than a MySQL GET_LOCK: it is DB-agnostic and does
	# not pin the worker's DB connection. Best-effort with a short bounded wait -
	# different errors take different locks and never contend; on contention/timeout
	# we proceed unlocked (a rare duplicate row is harmless: admin folds by
	# (tenant, fingerprint)).
	with redis_lock(_row_lock(user, fp), timeout_s=10, blocking_timeout_s=2):
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
			return True

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
		return True


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
_JARVIS_APP_MARKER = "/apps/jarvis/"
_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\w+)')
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt)):?\s*(.*)$")

#: The error-reporting pipeline's OWN modules. A failure here (e.g. admin
#: unreachable) must never be forwarded - it would report on its own outage and,
#: once admin returns, flood the feed with a report per failed cycle. These logs
#: stay local (Error Log / journalctl) where the operator can still see them.
_REPORTER_SELF_MARKERS = (
	"/apps/jarvis/jarvis/error_push.py",
	"/apps/jarvis/jarvis/api_errors.py",
	# GAP 1 heartbeat: its own push-path errors are infra, not customer-relevant — do not
	# forward them into the tenant Errors feed (would be a fleet-wide false positive).
	"/apps/jarvis/jarvis/chat/heartbeat.py",
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
			rel = path.split(_JARVIS_APP_MARKER, 1)[1]
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


def collect_error_log(since: str | None, limit: int = ERROR_LOG_SCAN_LIMIT) -> dict:
	"""Read new ``Error Log`` rows after ``since``, keep only jarvis-origin ones,
	and normalize them into the push shape.

	Returns ``{"rows": [...], "watermark": <creation str or None>}``. The
	watermark advances past EVERY scanned row (jarvis or not) so skipped
	framework errors are never re-scanned."""
	filters = {"creation": (">", since)} if since else {}
	logs = frappe.get_all(
		"Error Log",
		filters=filters,
		fields=["name", "method", "error", "creation", "owner"],
		order_by="creation asc",
		limit=limit,
	)
	rows: list[dict] = []
	watermark = since
	for log in logs:
		watermark = str(log.creation)
		if not is_jarvis_error(log.method, log.error):
			continue
		exc_class, exc_message, frames = _parse_traceback(log.error or "")
		message = _scrub_error_text(exc_message or log.method or exc_class)[:MAX_MESSAGE]
		rows.append(
			{
				"kind": "exception",
				"surface": "server",
				"error_code": exc_class,
				"error_class": exc_class,
				"message": message,
				"stack": _scrub_error_text(log.error)[:MAX_STACK],
				"route": (log.method or "")[:200],
				"fingerprint": _traceback_fingerprint(exc_class, frames),
				"occurred_at": str(log.creation),
				"severity": "error",
				"user_ref": _error_log_user_ref(log.owner),
			}
		)
	return {"rows": rows, "watermark": watermark}
