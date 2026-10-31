"""The holiday touch endpoint.

``get_today`` answers for the session user only: it takes no user argument, so it cannot be
asked about someone else. The same payload rides the desktop page boot
(``context.boot["holiday_touch"]`` in ``www/jarvis.py``); this endpoint is for a client that
must refresh it without a reload. A 403 here means "none" to the client.

``record_shown`` is the client's report that it is about to play the scene. It answers
``{"play": bool}`` and, when the answer is true, has already recorded the showing, so the
client plays exactly when told to. Every fault answers ``{"play": False}``; the only error a
client can see is the 403 for a user without Jarvis access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frappe

from jarvis.chat import txn, usage
from jarvis.holiday_touch import cadence, force, resolve
from jarvis.holiday_touch.report import report
from jarvis.holiday_touch.settings import shown_times
from jarvis.permissions import require_jarvis_access

_USER_SETTINGS = "Jarvis User Settings"
_SHOWN = "holiday_touch_shown"


@frappe.whitelist(methods=["GET"])
def get_today() -> dict:
	"""What the holiday touch shows the session user today. A Guest or a user without Jarvis
	access gets ``frappe.PermissionError`` (403); any other fault becomes a ``none`` payload."""
	require_jarvis_access()  # a Guest has no access: it is refused here too
	return resolve.safe_answer(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def record_shown() -> dict:
	"""May the scene play now? ``{"play": True}`` means the showing is recorded and the client
	plays it; ``{"play": False}`` means do not (no scene today, the cadence says no, another
	request just took the slot, or anything went wrong).

	Acts only on the session user. A Guest or a user without Jarvis access gets
	``frappe.PermissionError`` (403)."""
	require_jarvis_access()
	user = frappe.session.user
	now = _now()
	answer = resolve.safe_answer(user, now)
	if not answer.get("scene"):
		return {"play": False}
	if force.forced_occasion(now) is not None:
		# The developer-mode override bypasses the cadence, so there is nothing to record and
		# the scene can be replayed as often as the tester likes.
		return {"play": True}
	return {"play": _claim_showing(user, now, answer["tz"])}


def _now() -> datetime:
	return datetime.now(timezone.utc)


def _claim_showing(user: str, now: datetime, tz: str) -> bool:
	"""Take today's slot for ``user`` if the cadence allows it. Never raises.

	The settings row is created first when it is missing (that write also makes a replay
	unsafe, which the lost-race handling below turns into ``False``). The check and the write
	are one ``replay_on_conflict`` unit on the row lock: of two requests at once, the second
	one's lock fails with the snapshot-isolation conflict, the unit replays on a fresh
	snapshot, sees the first one's showing and says no."""
	try:
		if not frappe.get_meta(_USER_SETTINGS).has_field(_SHOWN):
			return False  # a release that has not migrated the field yet
		name = usage.user_settings_name(user)
		return txn.replay_on_conflict(lambda: _take_slot(name, now, tz), label="holiday_touch.record_shown")
	except Exception as e:
		if txn.is_write_conflict(e):
			txn.report_lost_race(e, title="Holiday touch: record_shown lost a write race")
		else:
			report("record_shown", "Holiday touch: record_shown failed")
		return False


def _take_slot(name: str, now: datetime, tz: str) -> bool:
	"""The unit: lock the row, read the showings, and append this one when the cadence allows."""
	raw = frappe.db.get_value(_USER_SETTINGS, name, _SHOWN, for_update=True)
	if not cadence.may_play(now, shown_times({_SHOWN: raw}), tz):
		return False
	state = cadence.add_showing(_stored_state(raw), now, tz)
	frappe.db.set_value(_USER_SETTINGS, name, _SHOWN, json.dumps(state), update_modified=False)
	return True


def _stored_state(raw) -> dict | None:
	try:
		state = json.loads(raw or "null")
	except Exception:  # ValueError, and RecursionError on a pathologically nested value
		return None
	return state if isinstance(state, dict) else None
