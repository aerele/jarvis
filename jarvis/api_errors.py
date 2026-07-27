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

import frappe

from jarvis.audit import _SECRET_KEYS

DT = "Jarvis Client Error"

#: Hard bounds so a hostile or runaway client can't flood one call.
MAX_ERRORS_PER_CALL = 50
MAX_MESSAGE = 500
MAX_STACK = 2000

#: How many Error Log rows one push cycle scans at most.
ERROR_LOG_SCAN_LIMIT = 300


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
# decimals, thousand-grouped, or 4+ digit runs = amounts / quantities / ids
_NUMBER_RE = re.compile(r"(?<![\w.])(?:\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{4,})(?![\w])")
# quoted literals that look like DATA (contain a digit, or are long)
_QUOTED_RE = re.compile(r"(['\"])((?:\\.|(?!\1).)*?)\1")


def _redact_quoted(m: re.Match) -> str:
	inner = m.group(2)
	if len(inner) > 24 or any(c.isdigit() for c in inner):
		return m.group(1) + "[VAL]" + m.group(1)
	return m.group(0)


def _scrub_error_text(text: str | None, *, keep_email: str | None = None) -> str:
	"""Redact PII / ERP values from a free-text error message or traceback.

	Best-effort and deliberately conservative on structure (keeps error codes,
	class names, field names) while removing values: emails (except the
	reporting user's own), secret-shaped tokens, long hashes, amounts /
	quantities / long ids, and data-shaped quoted literals."""
	if not text:
		return ""
	out = str(text)
	out = _KV_SECRET_RE.sub(lambda m: m.group(1) + "=[REDACTED]", out)
	out = _LONG_BLOB_RE.sub("[BLOB]", out)

	def _email_sub(m: re.Match) -> str:
		return m.group(0) if keep_email and m.group(0).lower() == keep_email.lower() else "[EMAIL]"

	out = _EMAIL_RE.sub(_email_sub, out)
	out = _QUOTED_RE.sub(_redact_quoted, out)
	out = _NUMBER_RE.sub("[NUM]", out)
	return out


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #
_NORM_DIGITS = re.compile(r"\d+")


def _fingerprint(error_code: str, error_class: str, message: str, surface: str) -> str:
	"""Stable dedupe key for a UI error - digits normalized out of the message so
	'... 42 units' and '... 7 units' collapse to one group."""
	norm = _NORM_DIGITS.sub("#", (message or "").lower())[:200]
	raw = f"{error_code}|{error_class}|{surface}|{norm}"
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
			_ingest_one(raw, user)
			accepted += 1
		except Exception:
			# One malformed row must not sink the batch.
			frappe.logger("jarvis.client_errors").debug("client error row dropped", exc_info=True)
	if accepted:
		frappe.db.commit()
	return {"ok": True, "accepted": accepted}


def _ingest_one(raw: dict, user: str) -> None:
	surface = (str(raw.get("surface") or "unknown"))[:40]
	error_code = (str(raw.get("error_code") or ""))[:64]
	error_class = (str(raw.get("error_class") or ""))[:140]
	route = _clean_route(raw.get("route"))
	conversation = (str(raw.get("conversation") or ""))[:140]
	run_id = (str(raw.get("run_id") or ""))[:140]
	message = _scrub_error_text(raw.get("message"), keep_email=user)[:MAX_MESSAGE]
	stack = _scrub_error_text(raw.get("stack"), keep_email=user)[:MAX_STACK]
	fp = _fingerprint(error_code, error_class, message, surface)

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
_JARVIS_APP_MARKER = "/apps/jarvis/"
_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\w+)')
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt)):?\s*(.*)$")


def is_jarvis_error(method: str | None, traceback: str | None) -> bool:
	"""True only for exceptions that originate in the jarvis app - so a
	customer's ERPNext / framework / other-app errors never leave their bench.

	Matches the jarvis app path in the traceback, or a ``jarvis.*`` dotted
	``method`` title (excluding the un-installed ``jarvis_admin`` checkout)."""
	tb = traceback or ""
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
		fields=["name", "method", "error", "creation"],
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
			}
		)
	return {"rows": rows, "watermark": watermark}
