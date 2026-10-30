"""The admin support view behind ``api.explain``: why a user got, or missed, a holiday touch.

It reuses the resolver's own reads (``resolve.occasion``, ``resolve.window_view``,
``resolve.safe_answer``) and the settings readers, so it cannot drift from what ``answer``
does. Read only: it writes nothing to the database. (``answer`` fills the occasion cache in
Redis, as a request by that user would.)

A date of birth is never part of the view: ``birthday_today`` is a yes/no.

``chain`` lists the steps ``answer`` applies, in its order. ``effect`` is ``blocks`` when that
step alone drops this user's occasion, ``changes`` when it swaps the occasion, else ``none``.
The final word is always ``answer``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe

from jarvis.holiday_touch import cadence, force, pick, resolve, settings
from jarvis.holiday_touch.report import report
from jarvis.permissions import has_jarvis_access


def build(user: str, now: datetime) -> dict:
	"""The support view for ``user`` at ``now`` (UTC). Never raises."""
	name = frappe.db.exists("User", user) if isinstance(user, str) and user.strip() else None
	if not name:
		return {"user": user, "known": False, "summary": "unknown user"}
	try:
		return _View(name, now).build()
	except Exception:
		report("explain", "Holiday touch: explain failed")
		return {"user": name, "known": True, "summary": "explain failed"}


class _View:
	def __init__(self, user: str, now: datetime):
		self.user = user
		self.now = now
		self.zone = resolve.zone_for(user)
		self.today = now.astimezone(ZoneInfo(self.zone)).date()
		self.site = settings.site()
		self.row = settings.user_row(user)

	def build(self) -> dict:
		occ = resolve.occasion(self.user, self.now, self.zone)
		window = resolve.window_view(self.user, self.now, self.zone)
		span = timedelta(days=pick.SPAN)
		return {
			"user": self.user,
			"known": True,
			"zone": self.zone,
			"today": self.today.isoformat(),
			"window": {"from": (self.today - span).isoformat(), "to": (self.today + span).isoformat()},
			"birthday_today": window["birthday_today"],
			"rows_status": window["rows_status"],
			"rows": window["rows"],
			"occasion": occ,
			"chain": self.chain(occ),
			"answer": resolve.safe_answer(self.user, self.now),
		}

	def chain(self, occ: dict) -> list[dict]:
		return [
			self.switch("site_switch", settings.on(self.site, "holiday_touches_enabled")),
			self.switch("personal_switch", not settings.personal_off(self.row)),
			self.switch("policy_switch", not settings.on(self.site, "holiday_policy_disabled")),
			self.force(),
			_step(
				"occasion", f"{occ['kind']} ({occ['reason']})", "blocks" if occ["kind"] == "none" else "none"
			),
			self.birthday_switch(occ),
			self.blocked_designs(occ),
			_step("white_label", "yes" if settings.white_label(self.site) else "no", "none"),
			self.cadence(),
		]

	def switch(self, step: str, enabled: bool) -> dict:
		return _step(step, "on" if enabled else "off", "none" if enabled else "blocks")

	def force(self) -> dict:
		forced = force.forced_occasion(self.now)
		if forced is None or not has_jarvis_access(self.user):
			return _step("force", "none", "none")
		return _step("force", f"{forced['kind']} ({forced['occasion']})", "changes")

	def birthday_switch(self, occ: dict) -> dict:
		enabled = settings.on(self.site, "birthday_greetings_enabled")
		blocked = not enabled and "birthday" in _kinds(occ)
		return _step("birthday_switch", "on" if enabled else "off", "blocks" if blocked else "none")

	def blocked_designs(self, occ: dict) -> dict:
		blocked = settings.blocked_designs(self.site)
		hit = bool(blocked & _designs(occ))
		return _step("blocked_designs", ",".join(sorted(blocked)) or "none", "blocks" if hit else "none")

	def cadence(self) -> dict:
		allowed = cadence.may_play(self.now, settings.shown_times(self.row), self.zone)
		return _step("cadence", "may play" if allowed else "already shown", "none" if allowed else "blocks")


def _step(step: str, value: str, effect: str) -> dict:
	return {"step": step, "value": value, "effect": effect}


def _candidates(occ: dict) -> list[dict]:
	return [c for c in (occ, occ.get("fallback")) if c]


def _kinds(occ: dict) -> set:
	return {c["kind"] for c in _candidates(occ)}


def _designs(occ: dict) -> set:
	return {c["design"] for c in _candidates(occ) if c["design"]}
