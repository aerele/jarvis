"""Pre-send self-repair of a conversation's ARCHIVED gateway session (jarvis-admin-v2#753).

A Jarvis Conversation owns one gateway session (``Jarvis Conversation.session_key``)
and the bench reuses that key on every send. The gateway refuses new work on a
session in two states, and before this module the chat was stuck for good:

* archived (``Session "<key>" is archived. Restore it before starting new work.``):
  the stale-dashboard archive, a manual archive, or ``active-session-cap``;
* tombstoned (``... ended during restart recovery. Use /new or /reset ...``): restart
  recovery gave up on the session. The tombstone is usually NOT archived.

Scope: ARCHIVED ONLY. Healing a tombstone needs ``sessions.reset``, which the gateway
gates on ``operator.admin``, and the bench connects with read + write only
(``agent_client._REQUESTED_SCOPES``). Restore (``sessions.patch``) needs write and
describe needs read, so the archived path works with the scopes the bench holds. A
tombstoned chat is reported (``UNREPAIRABLE``) and the user is told to start a new
chat (the ``session-ended`` rule); a signed plugin route for reset is a follow-up.

``ensure_session_usable`` runs once per turn, BEFORE the send and before the mint
branch of both the pump (``prepare.run_prepare``) and the legacy turn handler, so a
key minted in this same turn is never probed. The ladder:

1. Skip (``SKIPPED``) when the kill switch is off, the key is empty, or it is a
   shared-chat key (``:dashboard:collab:``). Zero gateway frames.
2. Probe with ``sessions.describe``. Any gateway error means ``FAILSOFT`` (send as
   today); so does any non-gateway exception, which is also logged. A DEFINITIVE
   refusal of the probe is additionally written to the Error Log.
3. A row that is not a dict means the gateway has no such session yet (a key whose
   first send never arrived, an eagerly-minted macro key): ``ABSENT``, send as today.
4. Classify. Tombstoned (``restartRecoveryStatus == "tombstoned"``), archived or
   not: ``UNREPAIRABLE``, with no mutating call, no ``on_waking`` and no Error Log
   row (INFO line only, so a stuck chat cannot write one row per turn); the tombstone
   is checked first because the gateway rejects a tombstoned send before it looks at
   the archive state. Archived (``archived is True``, or ``archivedAt`` truthy and not
   a bool, so ``0``, ``""`` and ``False`` are not archived): go on. Neither:
   ``HEALTHY``.
5. Restore, announcing ``on_waking()`` once just before the first restore
   (``sessions.patch archived=False`` guarded by ``expectedSessionId``). A
   ``session-changed`` refusal gets ONE re-describe and re-classification (and one
   retry of the restore if the session is still archived); a second one is ``REFUSED``.
6. Log one INFO line per call (see below).

Outcomes (``Repair``): SKIPPED, HEALTHY, ABSENT, RESTORED, UNREPAIRABLE, REFUSED,
FAILSOFT. The INFO line may also carry ``outcome=unreachable``, a log-only value for
a TEMPORARY restore error that was re-raised (it is not a ``Repair`` member).

INFO line, through ``latency.get_logger()``:
``session_repair outcome=<o> step=<s> probe_ms=<n> repair_ms=<n> path=<pump|legacy>
run_id=<id> [archive_reason=<t>] [recovery=<t>]``. ``step`` is the step that decided
the outcome (``probe``, ``classify``, ``restore`` or ``re-describe``; ``-`` for
``SKIPPED``); ``repair_ms`` is the whole call, ``probe_ms`` the describe alone.

Error policy. Only an explicit gateway refusal is final (``is_definitive_refusal``).
Error Log titles, dotted because the hotfix lines' ``is_jarvis_error`` only accepts a
``jarvis.`` first segment:

* ``jarvis.chat.session_repair.refused``: the restore (or the re-describe) was
  refused, or the row has no usable session id. Returns ``REFUSED``, and the send
  then fails into the "needs to reconnect" copy.
* ``jarvis.chat.session_repair.probe_refused``: ``sessions.describe`` itself was
  definitively refused (for example a scope or pairing problem that will repeat every
  turn). Returns ``FAILSOFT``. A TEMPORARY probe error stays silent.
* ``jarvis.chat.session_repair.probe_failed``: an unexpected non-gateway exception.
  Returns ``FAILSOFT``.

A TEMPORARY error on the restore (UNAVAILABLE, AGENT_TIMEOUT, ack-timeout,
send-not-started, no code) is re-raised so the turn ends as today's retryable
"unreachable" error and the next turn retries the repair. rq's job-timeout exception
is re-raised from every broad ``except`` here (``_ensure``, ``on_waking``,
``_log_error``), so a timed-out job still dies as the worker intended. No message
carries the session key or ``str(exc)``.

Kill switch: ``bench --site <site> set-config jarvis_session_repair_enabled 0``.
On by default; off for the same explicit tokens as the pump flag
(``""``, ``0``, ``"0"``, ``false``, ``"false"``, ``"off"``, ``"no"``). It is read per
turn, so a change takes effect on the next job. Off means no ``sessions.describe``
frames at all.

Concurrency. The legacy path holds no per-conversation lock, so two tabs sending at
once can both probe and both restore; the second restore is harmless (the first one
changed nothing the second depends on, and ``expectedSessionId`` guards the
incarnation). The pump path is serialised by pump admission.

LRU note: a restore bumps the session's ``updatedAt``, so the gateway's
``active-session-cap`` LRU never re-archives the session it just restored. It may
archive another dormant session instead, which heals on its own next message.

There is no key change, no Jarvis Conversation write and no Chat Session write.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from enum import Enum

import frappe

from jarvis.chat import latency
from jarvis.chat.agent_client import is_definitive_refusal, is_session_changed
from jarvis.exceptions import AgentUnreachableError

try:
	from rq.timeouts import JobTimeoutException as _JobTimeout
except Exception:  # rq missing or reorganised: fall back to the class name below
	_JobTimeout = None

REFUSED_TITLE = "jarvis.chat.session_repair.refused"
PROBE_REFUSED_TITLE = "jarvis.chat.session_repair.probe_refused"
PROBE_FAILED_TITLE = "jarvis.chat.session_repair.probe_failed"

_COLLAB_MARKER = ":dashboard:collab:"
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,40}")
_TIMEOUT_NAMES = frozenset({"JobTimeoutException", "BaseTimeoutException"})


class Repair(str, Enum):
	SKIPPED = "skipped"
	HEALTHY = "healthy"
	ABSENT = "absent"
	RESTORED = "restored"
	UNREPAIRABLE = "unrepairable"
	REFUSED = "refused"
	FAILSOFT = "failsoft"


def session_repair_enabled() -> bool:
	"""On unless ``jarvis_session_repair_enabled`` is explicitly off. Reuses the pump
	flag's token logic verbatim (absent is on, ``""`` is off)."""
	# Lazy: pump pulls in the relay stack, which the legacy path does not need to load
	# just to read a flag.
	from jarvis.chat.pump import _pump_flag_explicit_off

	return not _pump_flag_explicit_off(frappe.conf.get("jarvis_session_repair_enabled"))


def _is_job_timeout(exc: BaseException) -> bool:
	"""rq's job-timeout (an ``Exception`` subclass, so a broad ``except Exception``
	would swallow it). Class check when rq imports, class-name check otherwise."""
	if _JobTimeout is not None and isinstance(exc, _JobTimeout):
		return True
	return any(cls.__name__ in _TIMEOUT_NAMES for cls in type(exc).__mro__)


def _token(value, default: str = "-") -> str:
	"""A gateway-supplied value as a short [A-Za-z0-9_-] token, else ``default``.
	Anything free-form (a key with its colons, a newline, a sentence) is dropped
	whole rather than stripped, so no fragment of it can reach a log line."""
	if not isinstance(value, str | int) or isinstance(value, bool):
		return default
	text = str(value)
	if not _SAFE_TOKEN.fullmatch(text):
		return default
	return text


def _log_error(title: str, message: str) -> None:
	try:
		frappe.log_error(title=title, message=message)
	except Exception as exc:
		if _is_job_timeout(exc):
			raise


def _log_refused(step: str, exc: AgentUnreachableError, title: str = REFUSED_TITLE) -> None:
	details = getattr(exc, "details", None)
	reason = details.get("reason") if isinstance(details, dict) else None
	_log_error(
		title,
		f"step={_token(step)} code={_token(getattr(exc, 'code', None))} reason={_token(reason)}",
	)


def _log_failed(step: str, exc: BaseException) -> None:
	# The exception CLASS is the category; its message may carry the session key.
	_log_error(PROBE_FAILED_TITLE, f"step={_token(step)} exc={_token(type(exc).__name__)}")


def _is_archived(row: dict) -> bool:
	"""``archived`` must be exactly True; ``archivedAt`` counts only when truthy and not
	a bool, so ``0``, ``""``, ``None`` and ``False`` leave a session usable."""
	if row.get("archived") is True:
		return True
	at = row.get("archivedAt")
	return bool(at) and not isinstance(at, bool)


def _classify(row: dict) -> tuple[bool, bool, object]:
	tombstoned = row.get("restartRecoveryStatus") == "tombstoned"
	return _is_archived(row), tombstoned, row.get("sessionId")


def _note(extra: dict, row: dict, archived: bool) -> None:
	"""Record what the INFO line reports about ``row`` (the latest one described)."""
	extra["archive_reason"] = row.get("archiveReason") if archived else None
	extra["recovery"] = row.get("restartRecoveryStatus")


def _usable_sid(sid) -> bool:
	return isinstance(sid, str) and bool(sid)


def _announce(on_waking: Callable[[], None]) -> None:
	"""Tell the user the session is waking. Never blocks the repair."""
	try:
		on_waking()
	except Exception as exc:
		if _is_job_timeout(exc):
			raise


def _log_line(outcome, timing: dict, path: str, run_id, extra: dict) -> None:
	try:
		value = outcome.value if isinstance(outcome, Repair) else str(outcome)
		line = (
			f"session_repair outcome={value} step={_token(extra.get('step'))} "
			f"probe_ms={timing['probe_ms']} repair_ms={timing['repair_ms']} "
			f"path={_token(path)} run_id={_token(run_id)}"
		)
		if extra.get("archive_reason"):
			line += f" archive_reason={_token(extra['archive_reason'])}"
		if extra.get("recovery"):
			line += f" recovery={_token(extra['recovery'])}"
		latency.get_logger().info(line)
	except Exception as exc:
		if _is_job_timeout(exc):
			raise


def ensure_session_usable(
	sess,
	conv,
	*,
	on_waking: Callable[[], None],
	path: str,
	run_id: str | None = None,
) -> Repair:
	"""Make ``conv``'s archived gateway session accept a send. See the module docstring.

	Raises only the AgentUnreachableError of a TEMPORARY failure on the restore (and
	rq's job-timeout); every other outcome is a ``Repair`` value."""
	extra: dict = {"step": "probe"}
	timing = {"probe_ms": 0, "repair_ms": 0}
	started = time.monotonic()
	try:
		outcome = _ensure(sess, conv, on_waking, extra, timing)
	except AgentUnreachableError:
		timing["repair_ms"] = int((time.monotonic() - started) * 1000)
		_log_line("unreachable", timing, path, run_id, extra)
		raise
	timing["repair_ms"] = int((time.monotonic() - started) * 1000)
	_log_line(outcome, timing, path, run_id, extra)
	return outcome


def _ensure(sess, conv, on_waking, extra: dict, timing: dict) -> Repair:
	try:
		key = getattr(conv, "session_key", None)
		if not session_repair_enabled() or not isinstance(key, str) or not key or _COLLAB_MARKER in key:
			extra["step"] = "-"
			return Repair.SKIPPED

		extra["step"] = "probe"
		started = time.monotonic()
		try:
			row = sess.describe_session(key)
		except AgentUnreachableError as exc:
			if is_definitive_refusal(exc):
				# Not a blip: a scope or pairing refusal repeats every turn. Say so once per
				# turn. Temporary probe errors stay silent.
				_log_refused("probe", exc, PROBE_REFUSED_TITLE)
			return Repair.FAILSOFT
		finally:
			timing["probe_ms"] = int((time.monotonic() - started) * 1000)

		if not isinstance(row, dict):
			return Repair.ABSENT

		extra["step"] = "classify"
		archived, tombstoned, sid = _classify(row)
		_note(extra, row, archived)
		if tombstoned:
			# Needs sessions.reset (operator.admin), which the bench does not hold. No
			# mutating call, no Error Log row: the INFO line is the only trace.
			return Repair.UNREPAIRABLE
		if not archived:
			return Repair.HEALTHY
		if not _usable_sid(sid):
			_log_error(REFUSED_TITLE, "step=classify code=- reason=no-session-id")
			return Repair.REFUSED

		extra["step"] = "restore"
		_announce(on_waking)
		try:
			try:
				sess.restore_session(key, sid)
			except AgentUnreachableError as exc:
				if not is_session_changed(exc):
					raise
				# Replaced since we looked: look once more and act on what is there.
				extra["step"] = "re-describe"
				row = sess.describe_session(key)
				if not isinstance(row, dict):
					return Repair.ABSENT
				archived, tombstoned, sid = _classify(row)
				_note(extra, row, archived)  # the INFO line describes the fresh row
				if tombstoned:
					return Repair.UNREPAIRABLE
				if not archived:
					return Repair.HEALTHY
				if not _usable_sid(sid):
					_log_error(REFUSED_TITLE, "step=re-describe code=- reason=no-session-id")
					return Repair.REFUSED
				extra["step"] = "restore"
				# A second session-changed propagates and is refused below.
				sess.restore_session(key, sid)
			return Repair.RESTORED
		except AgentUnreachableError as exc:
			if not is_definitive_refusal(exc):
				raise
			_log_refused(extra["step"], exc)
			return Repair.REFUSED
	except AgentUnreachableError:
		raise
	except Exception as exc:
		if _is_job_timeout(exc):
			raise
		_log_failed(extra["step"], exc)
		return Repair.FAILSOFT
