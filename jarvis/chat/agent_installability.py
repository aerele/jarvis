"""min_apps / required-DocType installability gate (PP-3, R5-P0-05 / R5-J8).

A marketplace capability is INSTALLABLE only when every app in the listing's
``min_apps`` is installed on the site AND every DocType the listing declares as
required actually exists. Absence is an install-time NOT-INSTALLABLE STATE
(typed reason ``app_absent_or_ineligible``) — never a run coverage result: a
non-installable capability produces no run and no finding.

Two enforcement surfaces, one predicate (``evaluate_installability``):

  * INSTALL TIME — ``install_agent`` and the ``Jarvis Agent Installation``
    controller ``validate()`` refuse a fresh install when the predicate fails,
    stamping ``installable`` / ``not_installable_reason`` on the row.
  * AFTER AN APP/CATALOG CHANGE — ``reconcile_installations`` (wired into
    ``agent_catalog.after_migrate``) re-evaluates EVERY existing installation and
    marks it ``installable=0`` + reason when a dependency has since disappeared.
    It NEVER deletes a row (an install may still reference a retired app once the
    app is reinstalled the next reconcile clears the flag).

Downstream, the enable / schedule / run-now gates and the container push payload
refuse / exclude a non-installable row so a dependency that vanished after
install can never silently keep running.

Test seam — installability reads installed apps through the module-local
``installed_apps`` accessor, NOT ``frappe.get_installed_apps`` directly, so a
test can simulate app absence/removal by patching THIS function. Patching the
framework helper globally breaks Frappe's own hook resolution (the same reason
``jarvis.site_profile.apps`` keeps its ``_installed_apps`` seam), so the seam is
the supported simulation point.
"""

from __future__ import annotations

import frappe
from frappe import _

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"

# The single typed reason this gate ever stamps / throws (a closed-enum member of
# jarvis_agent_installation.not_installable_reason and coverage_reasons).
APP_ABSENT = "app_absent_or_ineligible"


def installed_apps() -> set[str]:
	"""The site's installed apps as a set (test seam — patch HERE to simulate
	absence/removal, never ``frappe.get_installed_apps`` globally). Fails toward
	the empty set so a lookup hiccup marks a min_apps agent not-installable rather
	than silently installable."""
	try:
		return set(frappe.get_installed_apps())
	except Exception:
		frappe.log_error(title="jarvis agent installability: get_installed_apps failed")
		return set()


def _json_list(raw) -> list[str]:
	try:
		vals = frappe.parse_json(raw) if raw else []
	except Exception:
		vals = []
	if not isinstance(vals, list):
		return []
	return [v.strip() for v in vals if isinstance(v, str) and v.strip()]


def min_apps_for(agent: str) -> list[str]:
	"""The listing's declared ``min_apps`` (deduped, trimmed)."""
	return _json_list(frappe.db.get_value(LISTING, agent, "min_apps"))


def required_doctypes_for(agent: str) -> list[str]:
	"""The listing's declared ``doctypes_required`` (UNFILTERED — absence is a
	gate signal, not something to silently drop; the controller preflight decides
	what to do with a missing one)."""
	return _json_list(frappe.db.get_value(LISTING, agent, "doctypes_required"))


def missing_dependencies(agent: str) -> tuple[list[str], list[str]]:
	"""``(missing_apps, missing_doctypes)`` for ``agent`` against the current
	site. A min_app is missing when it is not in ``installed_apps()``; a required
	DocType is missing when no ``DocType`` row exists for it."""
	installed = installed_apps()
	missing_apps = sorted(a for a in min_apps_for(agent) if a not in installed)
	missing_dts = sorted(d for d in required_doctypes_for(agent) if not frappe.db.exists("DocType", d))
	return missing_apps, missing_dts


def evaluate_installability(agent: str) -> tuple[bool, str | None, str | None]:
	"""``(installable, reason, detail)``. ``reason``/``detail`` are ``None`` when
	installable. The predicate: ``min_apps ⊆ installed_apps`` AND every required
	DocType exists."""
	missing_apps, missing_dts = missing_dependencies(agent)
	if not missing_apps and not missing_dts:
		return True, None, None
	bits: list[str] = []
	if missing_apps:
		bits.append("app(s) {0}".format(", ".join(missing_apps)))
	if missing_dts:
		bits.append("DocType(s) {0}".format(", ".join(missing_dts)))
	detail = "{0} not present on this site".format("; ".join(bits))
	return False, APP_ABSENT, detail


def assert_installable(agent: str) -> None:
	"""Refuse (``frappe.throw``) when ``agent`` is not installable on this site,
	surfacing the missing dependency. The typed reason is ``app_absent_or_
	ineligible`` — the caller stamps the row; this only guards the throw."""
	ok, _reason, detail = evaluate_installability(agent)
	if not ok:
		frappe.throw(
			_("This agent requires {0}; it cannot be installed or run here.").format(detail),
			title=_("Agent not installable"),
		)


def reconcile_installations() -> dict:
	"""Re-evaluate EVERY installation against the current site and persist
	``installable`` / ``not_installable_reason`` where it changed. Never deletes a
	row. Idempotent; wired into ``agent_catalog.after_migrate`` so an app install/
	uninstall (which runs a migrate) or a catalog change re-marks affected rows.

	Uses ``frappe.db.set_value`` (not ``doc.save``) so it bypasses the controller
	activation/run-as validators — a reconcile must never be blocked by unrelated
	row state — and never bumps ``modified``."""
	rows = frappe.get_all(
		INSTALLATION,
		fields=["name", "agent", "installable", "not_installable_reason"],
	)
	changed = 0
	for r in rows:
		ok, reason, _detail = evaluate_installability(r.agent)
		new_installable = 1 if ok else 0
		new_reason = "" if ok else reason
		cur_installable = frappe.utils.cint(r.installable)
		cur_reason = r.not_installable_reason or ""
		if cur_installable == new_installable and cur_reason == new_reason:
			continue
		frappe.db.set_value(
			INSTALLATION,
			r.name,
			{"installable": new_installable, "not_installable_reason": new_reason},
			update_modified=False,
		)
		changed += 1
	if changed:
		frappe.db.commit()
	return {"reconciled": len(rows), "changed": changed}
