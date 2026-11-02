"""The admin support view behind ``api.explain``: why a user got, or missed, a holiday touch.

It reuses the resolver's own reads (``resolve.occasion``, ``resolve.window_view``,
``resolve.safe_answer``) and the settings readers, so it cannot drift from what ``answer``
does. Read only: it writes nothing to the database. (``answer`` fills the occasion cache in
Redis, as a request by that user would.)

A date of birth is never part of the view: ``birthday_today`` is a yes/no.

``chain`` lists the steps ``answer`` applies, in its order. ``effect`` is ``blocks`` when that
step alone leaves the user with nothing, ``changes`` when it swaps the occasion (a fallback
survives, or a forced occasion replaces it), else ``none``.
The final word is always ``answer``: the chain reads a fresh ``resolve.occasion`` while
``answer`` may serve a cached one, so on a stale cache the two can differ (``answer`` wins).
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
			"answer_is_authoritative": True,  # the chain is a fresh read; answer may be cached
		}

	def chain(self, occ: dict) -> list[dict]:
		forced = self.forced()
		occ_used = forced or occ  # what answer() chooses from
		return [
			self.switch("site_switch", settings.on(self.site, "holiday_touches_enabled")),
			self.switch("personal_switch", not settings.personal_off(self.row)),
			self.switch("policy_switch", not settings.on(self.site, "holiday_policy_disabled")),
			self.force(forced),
			_step(
				"occasion", f"{occ['kind']} ({occ['reason']})", "blocks" if occ["kind"] == "none" else "none"
			),
			self.birthday_switch(occ_used),
			self.blocked_designs(occ_used),
			_step("white_label", "yes" if settings.white_label(self.site) else "no", "none"),
			self.cadence(forced),
		]

	def switch(self, step: str, enabled: bool) -> dict:
		return _step(step, "on" if enabled else "off", "none" if enabled else "blocks")

	def forced(self) -> dict | None:
		"""The developer-mode occasion ``answer`` would use instead of the real one."""
		forced = force.forced_occasion(self.now)
		return forced if forced is not None and has_jarvis_access(self.user) else None

	def force(self, forced: dict | None) -> dict:
		if forced is None:
			return _step("force", "none", "none")
		return _step("force", f"{forced['kind']} ({forced['occasion']})", "changes")

	def birthday_switch(self, occ: dict) -> dict:
		if settings.on(self.site, "birthday_greetings_enabled"):
			return _step("birthday_switch", "on", "none")
		return _step("birthday_switch", "off", _effect(occ, False, self.blocked, True, self.blocked))

	def blocked_designs(self, occ: dict) -> dict:
		on = settings.on(self.site, "birthday_greetings_enabled")
		effect = _effect(occ, on, self.blocked, on, frozenset())
		return _step("blocked_designs", ",".join(sorted(self.blocked)) or "none", effect)

	@property
	def blocked(self) -> frozenset:
		return settings.blocked_designs(self.site)

	def cadence(self, forced: dict | None) -> dict:
		if forced is not None:
			return _step("cadence", "force bypasses the cadence", "none")
		allowed = cadence.may_play(self.now, settings.shown_times(self.row), self.zone)
		return _step("cadence", "may play" if allowed else "already shown", "none" if allowed else "blocks")


def _effect(
	occ: dict, birthday_on: bool, blocked: frozenset, other_on: bool, other_blocked: frozenset
) -> str:
	"""What one switch does to the choice ``answer`` makes: compare the real choice with the
	choice that would be made had this switch been left out (``other_*``)."""
	actual = resolve._choose(occ, birthday_on, blocked)
	without = resolve._choose(occ, other_on, other_blocked)
	if actual == without:
		return "none"
	return "blocks" if actual is None else "changes"


def _step(step: str, value: str, effect: str) -> dict:
	return {"step": step, "value": value, "effect": effect}
