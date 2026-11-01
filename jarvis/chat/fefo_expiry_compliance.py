"""Configuration boundary for the FEFO expiry-compliance auditor.

The bundle requires a named reviewer and a bounded near-expiry window.  Keep
that contract on the bench too, so a missing policy stops before a run,
conversation, or fleet request exists.  The optional Warehouse declaration is
validated separately by :mod:`jarvis.chat.agent_scope` and is intentionally not
sent to the bundle.
"""

import frappe
from frappe import _

from jarvis._session import impersonate
from jarvis.chat.agent_scope import resolve_scope
from jarvis.permissions import is_valid_unattended_owner

AGENT = "fefo-expiry-compliance-auditor"
_BUNDLE_KEYS = frozenset(("reviewer", "near_expiry_days", "test_declaration"))
_INSTALLATION_ONLY_KEYS = frozenset(("record_scope",))


def configuration_issues(raw) -> dict[str, str]:
	"""Return field-level FEFO setup guidance for saved installation settings.

	The evaluator receives only the ``fefo`` policy after the bench removes its
	optional Warehouse declaration.  Validate the same policy shape here, while
	allowing that declaration to continue through the separate, permission-bound
	scope resolver.
	"""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return {"config": _("Configuration must be a valid JSON object.")}
	if not isinstance(config, dict):
		return {"config": _("Configuration must be a JSON object.")}

	policy = config.get("fefo")
	if not isinstance(policy, dict):
		return {
			"fefo_reviewer": _("Name the expiry reviewer in the agent settings."),
			"fefo_near_expiry_days": _("Set near-expiry days to a whole number from 1 through 3650."),
		}

	issues: dict[str, str] = {}
	unsupported = set(policy) - _BUNDLE_KEYS - _INSTALLATION_ONLY_KEYS
	if unsupported:
		issues["config"] = _(
			"Use only the expiry reviewer, near-expiry days, test declaration, and optional Warehouse scope."
		)

	reviewer = policy.get("reviewer")
	if not isinstance(reviewer, str) or not 0 < len(reviewer.strip()) <= 140:
		issues["fefo_reviewer"] = _("Name the expiry reviewer in the agent settings (1 to 140 characters).")

	days = policy.get("near_expiry_days")
	if type(days) is not int or not 1 <= days <= 3650:
		issues["fefo_near_expiry_days"] = _("Set near-expiry days to a whole number from 1 through 3650.")

	declaration = policy.get("test_declaration", "")
	if not isinstance(declaration, str) or len(declaration) > 500:
		issues["fefo_test_declaration"] = _("Use a plain test declaration of 500 characters or less.")

	return issues


def assert_configuration(installation) -> None:
	"""Refuse a FEFO launch before it creates any persistent run state."""
	issues = configuration_issues(installation.get("config") or "{}")
	if issues:
		frappe.throw(list(issues.values()), title=_("Complete FEFO configuration"), as_list=True)


def assert_schedule_configuration(installation) -> None:
	"""Refuse an enabled FEFO schedule that cannot launch as its run-as user.

	This is shared by the SPA endpoint and the Installation controller.  The
	controller is the authoritative path: Desk, REST, import, and direct saves do
	not pass through ``agents_api.set_schedule``.  Run the policy and the optional
	Warehouse scope resolution as the identity that will execute the unattended
	run, before the schedule can be persisted.
	"""
	if str(installation.get("agent") or "").strip() != AGENT:
		return
	run_as = str(installation.get("run_as_user") or "").strip()
	if not is_valid_unattended_owner(run_as):
		frappe.throw(
			_(
				"This agent needs an enabled non-system run-as user before you can enable an "
				"automatic schedule."
			)
		)
	with impersonate(run_as):
		assert_configuration(installation)
		# A declared FEFO Warehouse root is binding.  Resolve it here so an
		# unreadable, disabled, foreign, or malformed scope cannot be saved into an
		# unattended schedule and fail only on its first tick.  This also validates
		# the ordinary company/period scope as the eventual run-as identity.
		from jarvis.exceptions import InvalidArgumentError

		try:
			resolve_scope(installation)
		except InvalidArgumentError as exc:
			frappe.throw(str(exc), title=_("Complete FEFO configuration"))
