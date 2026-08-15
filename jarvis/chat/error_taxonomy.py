"""jarvis#823 — the ONE chat-turn failure classifier.

Before this module, a turn failure was keyword-guessed twice (here and again in
``frontend/src/lib/errors.js``) and the SPA offered Retry on every result, so a
revoked key, a bad model id and an exhausted quota all invited a retry that could
never work. Two shipped bugs (#757, #760) came out of the three hand-synced
copies of that ladder.

Every failure now gets a stable machine ``code`` AND an explicit ``retryable``
boolean, produced in three tiers, most trustworthy first:

  ``code``    a machine-readable code we already hold. The agent gateway rejects
              an RPC with ``{code, message, details}`` which arrives as
              ``AgentUnreachableError(code=..., details=...)``; the bench mints
              its own ``turn-timeout``; the worker backstop stamps ``internal``.
  ``parsed``  deterministic STRUCTURE inside provider prose: the HTTP status the
              vendor put in the sentence ("... API error (429): You exceeded your
              current quota"), the vendor's error-type slug, and a reset clock.
              This is parsing, not vibes: a status number means one thing.
  ``guess``   the legacy keyword ladder, last resort only. Its verdict is marked
              low confidence and FORCED retryable, because the one thing we must
              not do on a guess is tell a customer their problem is permanent.

Exhausted vs throttled borrows the host plane's distinction (fleet-agent's
``proxy_probe.is_exhausted_code``): a usage limit, a cooldown or an insufficient
balance is EXHAUSTED — a definitive refusal with its own reset clock, where
retrying is a lie — while a rate limit or an overloaded model is THROTTLED,
transient, and worth retrying. The reset field keeps that plane's key name,
``resets_in_seconds``, unchanged.

The MARKERS are deliberately stricter than that plane's, and the difference is
the point: it matches its substrings against a machine ``detail_code``, where
"quota" can only mean one thing, whereas everything reaching this module is free
prose. So a bare word never qualifies here (see ``_EXHAUSTED_MARKERS``). The two
planes agree on the concept; they cannot share a matcher.

The taxonomy is DISPLAY classification. It never steers control flow. In
particular the context-overflow park branch in ``turn_handler`` (the agent
auto-compacts and retries, so the turn parks for snapshot recovery rather than
erroring) runs BEFORE any classification and keeps its position; a
``context-overflow`` code is only ever stamped on a failure that already got
past it.

``TURN_ERROR_CODES`` below is one of three files that must agree. The other two
are ``jarvis/chat/turn_error_codes.json`` (the contract) and the ``TURN_ERROR_CODES``
table in ``frontend/src/lib/errors.js``. Both test suites assert against the
contract, so adding a code without updating all three goes red.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------- #
# The taxonomy
# --------------------------------------------------------------------------- #

# code -> retryable. Mirrored by jarvis/chat/turn_error_codes.json and by
# frontend/src/lib/errors.js; test_turn_handler + errors.test.js pin all three
# together.
TURN_ERROR_CODES: dict[str, bool] = {
	# Terminal. A Retry button on any of these is a promise we cannot keep.
	"agent-unpaired": False,
	"auth-invalid": False,
	"cancelled": False,
	"context-overflow": False,
	"model-not-found": False,
	"quota-exhausted": False,
	# Retryable.
	"gateway": True,
	"internal": True,
	"provider": True,
	"recovery-expired": True,
	"throttled": True,
	"timeout": True,
	"unreachable": True,
}

# Pre-#823 code for "busy / quota / billing", now split into `throttled` and
# `quota-exhausted`. The classifier never produces it; it stays in the table (and
# in the SPA's copy map) so an in-flight event or an externally supplied code
# still renders a sentence instead of falling through to "Something went wrong".
LEGACY_CODES = frozenset({"provider"})

DEFAULT_CODE = "gateway"

# --------------------------------------------------------------------------- #
# Tier 1 — machine codes already in hand
# --------------------------------------------------------------------------- #

# The gateway maps every explicit-deviceToken auth failure to one coarse reason
# and the bench self-heals by re-pairing and reconnecting ONCE
# (agent_client._attempt_connect). So a pairing fault that got far enough to be
# classified here has already survived that repair: it is terminal for a Retry
# click, and the honest next step is support, not another attempt.
_UNPAIRED_CODES = frozenset({"device-not-paired", "token-mismatch", "token-revoked", "device-id-mismatch"})
_UNPAIRED_AUTH_REASONS = frozenset({"device_token_mismatch"})
_UNPAIRED_DETAIL_CODES = frozenset({"AUTH_DEVICE_TOKEN_MISMATCH"})

# Exception ``.code`` values the bench itself mints, mapped straight through.
_EXC_CODE_MAP = {
	"turn-timeout": "timeout",
	"ack-timeout": "timeout",
}

# --------------------------------------------------------------------------- #
# Tier 2 — structure parsed out of provider prose
# --------------------------------------------------------------------------- #

# A vendor status inside the sentence. Providers write it two ways and both are
# unambiguous: "Google Generative AI API error (429): ..." and "... status code:
# 401". A bare three-digit number anywhere in the text is NOT matched — that
# would read a token count or a model name as a status.
_STATUS_PAREN_RE = re.compile(r"\((\d{3})\)")
_STATUS_WORD_RE = re.compile(r"\b(?:http[ _-]?)?(?:status|error)[ _-]?code[:= ]\s*(\d{3})\b", re.IGNORECASE)

# A reset clock the provider named. Both key spellings are in live use across
# providers (`resets_in_seconds` from one, `reset_seconds` from another) and the
# host plane deliberately keeps them un-normalised, so match both here.
_RESET_SECONDS_RE = re.compile(
	r"(?:resets?[_ ]in[_ ]seconds|reset[_ ]seconds|retry[-_ ]?after)[\"'\s:=]+(\d+)", re.IGNORECASE
)
_RESET_PHRASE_RE = re.compile(r"\bresets? in (\d+) (second|minute|hour)s?\b", re.IGNORECASE)
_RESET_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}

# EXHAUSTED: a definitive refusal with its own reset clock. Retrying does not
# help until that clock runs out, so these are terminal and the customer is sent
# to their plan instead of to a button.
#
# Every entry here is an unambiguous SLUG or a vendor's verbatim exhaustion
# sentence, never a bare English word. That is the whole discipline of this list.
# The host plane matches "quota" as a substring, but it matches it against a
# machine ``detail_code`` where the word can only mean one thing; here the same
# substring would be tested against free prose, and Gemini writes "You exceeded
# your current quota" for an ordinary per-minute throttle that clears in seconds
# as readily as for a spent balance. A bare "quota" would therefore strand a
# customer on a failure a retry would have fixed, so an ambiguous 429 is left to
# fall through to the status check below and read as `throttled`. Terminal is the
# expensive verdict; it has to be earned by an unambiguous marker.
_EXHAUSTED_MARKERS = (
	"usage_limit_reached",
	"model_cooldown",
	"usage_limit",
	"insufficient_quota",
	"insufficient_balance",
	"insufficient balance",
	"insufficient credit",
	"insufficient funds",
	"credit balance is too low",
	"billing_hard_limit",
	"out of credits",
)

# THROTTLED: transient back-pressure that clears on its own in seconds. The host
# plane excludes rate limits from "exhausted" for exactly this reason.
_THROTTLE_MARKERS = (
	"rate_limit_exceeded",
	"rate limit",
	"rate-limit",
	"ratelimit",
	"too many requests",
	"overloaded",
	"capacity",
	"cooldown",
)

_AUTH_MARKERS = (
	"invalid_api_key",
	"invalid api key",
	"incorrect api key",
	"authentication_error",
	"authentication failed",
	"unauthorized",
	"api key not valid",
	"no api key found",
	"permission_denied",
	"subscription_rejected",
	"credential",
)

_MODEL_MARKERS = (
	"model_not_found",
	"model not found",
	"unknown model",
	"does not exist or you do not have access",
	"is not a valid model",
	"unsupported model",
	"no such model",
)

# Deliberately EXCLUDES the literal "context overflow": that string routes a turn
# into the auto-compact park branch in turn_handler, which never reaches this
# classifier. These are the terminal variants other vendors use, where no
# compaction retry is coming.
_OVERFLOW_MARKERS = (
	"maximum context length",
	"context_length_exceeded",
	"context length exceeded",
	"prompt is too long",
	"prompt too large",
	"too many tokens",
	"reduce the length of the messages",
)

# --------------------------------------------------------------------------- #
# Tier 3 — the legacy keyword ladder
# --------------------------------------------------------------------------- #

_CANCELLED_PREFIXES = ("you cancelled this message", "waited too long in the queue")
_UNREACHABLE_MARKERS = ("ws open failed", "unreachable", "connection timed out")
_TIMEOUT_MARKERS = ("timed out", "timeout", "deadline")

# The vendor status -> code map, as data rather than a branch ladder, so the
# parity test can assert it against the contract file.
HTTP_STATUS_CODES = {
	401: "auth-invalid",
	402: "quota-exhausted",
	403: "auth-invalid",
	404: "model-not-found",
	429: "throttled",
}

# Every marker list the text ladder runs, in one place the parity test can read.
# The SPA keeps the same lists (frontend/src/lib/errors.js) for the rows written
# before #823, which have an error string and no code; if the two ladders
# disagree, one failure reads differently before and after a page refresh.
MARKERS = {
	"overflow": _OVERFLOW_MARKERS,
	"exhausted": _EXHAUSTED_MARKERS,
	"model": _MODEL_MARKERS,
	"auth": _AUTH_MARKERS,
	"throttle": _THROTTLE_MARKERS,
	"unreachable": _UNREACHABLE_MARKERS,
	"timeout": _TIMEOUT_MARKERS,
}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def classify(err_text: str | None, exc: Exception | None = None) -> dict[str, Any]:
	"""Classify one turn failure into ``{code, retryable, confidence, data}``.

	TOTAL: any input shape returns a usable envelope and never raises — this runs
	on the error path, where a second exception would strand the turn's spinner.
	``data`` is None unless the provider named a reset clock, in which case it is
	``{"resets_in_seconds": int}``.
	"""
	try:
		return _classify(err_text, exc)
	except Exception:
		return envelope("internal", confidence="guess")


def envelope(code: str, *, confidence: str = "code", data: dict | None = None) -> dict[str, Any]:
	"""Build the envelope for ``code``, taking retryable from the ONE table.

	A tier-3 guess is forced retryable whatever the table says: the failure mode
	that matters is telling a customer "this can never work" on a keyword match,
	so an unrecognised code guessed from prose always keeps the Retry button.
	"""
	retryable = TURN_ERROR_CODES.get(code, True)
	if confidence == "guess":
		retryable = True
	return {"code": code, "retryable": bool(retryable), "confidence": confidence, "data": data or None}


def _classify(err_text: str | None, exc: Exception | None) -> dict[str, Any]:
	low = str(err_text or "").lower()

	tier1 = _from_exception(exc)
	if tier1 is not None:
		return tier1

	# The admission layer's durable cancel markers. Checked before anything else:
	# a cancelled turn is a muted note, never a red card, and its prose ("waited
	# too long in the queue") would otherwise read as a timeout.
	# Matched at "parsed" confidence, not "guess": these are OUR OWN durable
	# marker strings, minted by the admission layer and the worker backstop, not
	# provider prose. A guess would be forced retryable and put a Retry button on
	# a cancelled turn.
	if low.startswith(_CANCELLED_PREFIXES):
		return envelope("cancelled", confidence="parsed")

	# The worker's own backstop stamps code="internal" explicitly and never calls
	# this function; a page refresh has only the persisted string, so match its
	# wording here or the two paths disagree.
	if low.startswith("unexpected worker error"):
		return envelope("internal", confidence="parsed")

	tier2 = _from_structure(low)
	if tier2 is not None:
		return tier2

	return _from_keywords(low, exc)


def _from_exception(exc: Exception | None) -> dict[str, Any] | None:
	"""Tier 1: the machine code the gateway or the bench already handed us."""
	if exc is None:
		return None
	code = getattr(exc, "code", None)
	details = getattr(exc, "details", None)
	details = details if isinstance(details, dict) else {}

	if code in _UNPAIRED_CODES:
		return envelope("agent-unpaired")
	if details.get("authReason") in _UNPAIRED_AUTH_REASONS:
		return envelope("agent-unpaired")
	if details.get("code") in _UNPAIRED_DETAIL_CODES:
		return envelope("agent-unpaired")
	if code in _EXC_CODE_MAP:
		return envelope(_EXC_CODE_MAP[code])

	# Any other AgentUnreachableError is a transport fault or a rejection whose
	# code we do not recognise. Transport is retryable and that is the honest
	# reading of an unknown gateway rejection too: we cannot claim it is
	# permanent. Deferred (return None) when there is no code at all, so a
	# WS-level failure still gets the text tiers below rather than a blanket
	# "unreachable" over prose that says something more specific.
	from jarvis.exceptions import AgentUnreachableError

	if isinstance(exc, AgentUnreachableError) and code:
		return envelope("unreachable")
	return None


def _from_structure(low: str) -> dict[str, Any] | None:
	"""Tier 2: the HTTP status and vendor slugs the provider put in the sentence."""
	data = _reset_data(low)
	status = _http_status(low)

	# Marker checks run before the status so a 429 that names a usage limit is
	# read as exhausted (terminal) rather than as ordinary back-pressure — the
	# distinction the customer's next action turns on.
	if any(m in low for m in _OVERFLOW_MARKERS):
		return envelope("context-overflow", confidence="parsed", data=data)
	if any(m in low for m in _EXHAUSTED_MARKERS):
		return envelope("quota-exhausted", confidence="parsed", data=data)
	if any(m in low for m in _MODEL_MARKERS):
		return envelope("model-not-found", confidence="parsed", data=data)
	if any(m in low for m in _AUTH_MARKERS):
		return envelope("auth-invalid", confidence="parsed", data=data)
	if any(m in low for m in _THROTTLE_MARKERS):
		return envelope("throttled", confidence="parsed", data=data)

	# A bare 429 with nothing naming a usage limit is back-pressure, hence
	# `throttled`: the host plane keeps rate limits out of "exhausted" for the same
	# reason, that waiting and retrying genuinely works.
	if status in HTTP_STATUS_CODES:
		return envelope(HTTP_STATUS_CODES[status], confidence="parsed", data=data)
	return None


def _from_keywords(low: str, exc: Exception | None) -> dict[str, Any]:
	"""Tier 3: the pre-#823 ladder. Everything it returns is low confidence."""
	from jarvis.exceptions import AgentUnreachableError

	# A reset clock is structure even when nothing else in the text is, and it is
	# the difference between "try again in a moment" and an honest "try again in
	# about 40 minutes". Carried here too so an unrecognised failure that DID name
	# its wait still tells the customer what it is.
	data = _reset_data(low)
	# "connection timed out" is a transport failure (we could not reach the
	# gateway), not the model taking too long. Kept in this branch, and mirrored
	# in the SPA, so one string cannot read as unreachable live and timeout on a
	# reload.
	if isinstance(exc, AgentUnreachableError) or any(m in low for m in _UNREACHABLE_MARKERS):
		return envelope("unreachable", confidence="guess", data=data)
	if "recovery window" in low:
		return envelope("recovery-expired", confidence="guess", data=data)
	if any(m in low for m in _TIMEOUT_MARKERS):
		return envelope("timeout", confidence="guess", data=data)
	# A run that got this far was accepted and started, so it is a mid-run fault
	# the agent reported for itself. Its own wording is not reliable ("LLM request
	# failed: network connection error." was the verbatim text for a turn that
	# failed because a paired-device file was mid-rewrite), which is exactly why
	# this tier is capped at low confidence.
	return envelope(DEFAULT_CODE, confidence="guess", data=data)


def _http_status(low: str) -> int | None:
	m = _STATUS_PAREN_RE.search(low) or _STATUS_WORD_RE.search(low)
	if not m:
		return None
	try:
		return int(m.group(1))
	except (TypeError, ValueError):
		return None


def _reset_data(low: str) -> dict | None:
	m = _RESET_SECONDS_RE.search(low)
	if m:
		return _reset_dict(int(m.group(1)))
	m = _RESET_PHRASE_RE.search(low)
	if m:
		return _reset_dict(int(m.group(1)) * _RESET_UNIT_SECONDS[m.group(2).lower()])
	return None


def _reset_dict(seconds: int) -> dict | None:
	# A non-positive or absurd clock is noise, not a promise to show a customer.
	if seconds <= 0 or seconds > 30 * 24 * 3600:
		return None
	return {"resets_in_seconds": seconds}


# --------------------------------------------------------------------------- #
# Persistence — the ONE definition of "how a turn failure is written to a row"
# --------------------------------------------------------------------------- #

# Seven places write a failed turn's Message row (the worker, its backstop, the
# relay lifecycle handler, pump settlement, the pre-ack rejection, the recovery
# budget exhaust, the stale sweep). Before #823 each spelled the write out by
# hand, and the envelope has to land on ALL of them or a reload reclassifies from
# the string and contradicts what the customer just saw. So the column set is
# defined once, here, in the two shapes those callers need.

ERROR_TEXT_MAX_CHARS = 1000

MSG_ERROR_ASSIGNMENTS = (
	"streaming=0, error=%(err)s, error_code=%(err_code)s, "
	"error_retryable=%(err_retryable)s, error_data=%(err_data)s"
)


def error_row_values(err: str | None, env: dict[str, Any]) -> dict[str, Any]:
	"""The Message columns one turn failure writes, for ``frappe.db.set_value``."""
	return {
		"streaming": 0,
		"error": (err or "")[:ERROR_TEXT_MAX_CHARS],
		"error_code": env.get("code") or DEFAULT_CODE,
		"error_retryable": 1 if env.get("retryable") else 0,
		"error_data": json.dumps(env["data"]) if env.get("data") else None,
	}


def error_row_params(err: str | None, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
	"""The same write as ``error_row_values``, bound for ``MSG_ERROR_ASSIGNMENTS``.

	Used by the raw-SQL CAS sites, which cannot go through ``set_value`` because
	the write has to ride inside the fenced settlement transaction.
	"""
	v = error_row_values(err, env)
	return {
		"err": v["error"],
		"err_code": v["error_code"],
		"err_retryable": v["error_retryable"],
		"err_data": v["error_data"],
		**extra,
	}


def publish_extra(env: dict[str, Any]) -> dict[str, Any]:
	"""The ``run:error`` payload keys carrying this envelope to the SPA.

	``retryable`` is what the Retry button is gated on, so it always travels;
	``resets_in_seconds`` only when the provider actually named a clock.
	"""
	out: dict[str, Any] = {
		"code": env.get("code") or DEFAULT_CODE,
		"retryable": bool(env.get("retryable")),
	}
	data = env.get("data") or {}
	if data.get("resets_in_seconds"):
		out["resets_in_seconds"] = int(data["resets_in_seconds"])
	return out


def stored_envelope(row: dict[str, Any]) -> dict[str, Any] | None:
	"""Rebuild the envelope a Message row (or a Turn row) persisted, or None.

	None means the row predates #823 (an ``error`` with no ``error_code``): the
	caller falls back to classifying the text, which is what the SPA does for the
	same rows. Absence of the CODE is the test, never falsiness of ``retryable`` —
	``error_retryable = 0`` is a meaningful terminal verdict.
	"""
	code = (row or {}).get("error_code")
	if not code:
		return None
	data = row.get("error_data")
	if isinstance(data, str):
		try:
			data = json.loads(data)
		except (TypeError, ValueError):
			data = None
	return {
		"code": code,
		"retryable": bool(row.get("error_retryable")),
		"confidence": "stored",
		"data": data if isinstance(data, dict) else None,
	}
