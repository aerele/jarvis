"""Configuration boundary for the FEFO expiry-compliance auditor.

The policy belongs to the private evaluator. The optional Warehouse boundary
belongs only to the public top-level ``record_scope`` contract and is resolved
by :mod:`jarvis.chat.agent_scope` as the run-as user.
"""

import frappe
from frappe import _

from jarvis._session import impersonate
from jarvis.permissions import is_valid_unattended_owner

AGENT = "fefo-expiry-compliance-auditor"
_BUNDLE_KEYS = frozenset(("reviewer", "near_expiry_days", "test_declaration"))
_TOP_LEVEL_KEYS = frozenset(("fefo", "record_scope"))


def validate_configuration_edit(raw) -> None:
	"""Reject malformed supplied FEFO values but allow an unfinished setup.

	The form deliberately lets an owner save reviewer/window fields in more than
	one step.  A supplied value, however, cannot be malformed or use the retired
	nested scope shape.  Launch and schedule preflight enforce completeness.
	"""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError) as exc:
		raise ValueError("Configuration must be a valid JSON object.") from exc
	if not isinstance(config, dict):
		raise ValueError("Configuration must be a JSON object.")
	if set(config) - _TOP_LEVEL_KEYS:
		raise ValueError("Use only FEFO settings and the optional top-level record_scope.")

	policy = config.get("fefo")
	if policy is not None:
		if not isinstance(policy, dict):
			raise ValueError("FEFO settings must be a JSON object.")
		if set(policy) - _BUNDLE_KEYS:
			raise ValueError(
				"Use only the expiry reviewer, near-expiry days, and test declaration in the FEFO settings."
			)
		if "reviewer" in policy:
			reviewer = policy["reviewer"]
			if not isinstance(reviewer, str) or not 0 < len(reviewer.strip()) <= 140:
				raise ValueError("FEFO reviewer must contain 1 to 140 characters.")
		if "near_expiry_days" in policy:
			days = policy["near_expiry_days"]
			if type(days) is not int or not 1 <= days <= 3650:
				raise ValueError("FEFO near_expiry_days must be an integer from 1 to 3650.")
		if "test_declaration" in policy:
			declaration = policy["test_declaration"]
			if not isinstance(declaration, str) or len(declaration) > 500:
				raise ValueError("FEFO test_declaration must contain at most 500 characters.")

	if "record_scope" in config:
		from jarvis.chat.agent_scope import fefo_record_scope

		if fefo_record_scope(config["record_scope"]) is None:
			raise ValueError("FEFO record_scope must name one Warehouse leaf.")


def configuration_issues(raw) -> dict[str, str]:
	"""Return safe field-level setup guidance for a FEFO policy."""
	try:
		config = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		return {"config": _("Configuration must be a valid JSON object.")}
	if not isinstance(config, dict):
		return {"config": _("Configuration must be a JSON object.")}
	if set(config) - _TOP_LEVEL_KEYS:
		return {"config": _("Use only FEFO settings and the optional top-level record_scope.")}
	if "record_scope" in config:
		from jarvis.chat.agent_scope import fefo_record_scope

		if fefo_record_scope(config["record_scope"]) is None:
			return {"config": _("FEFO record_scope must name one Warehouse leaf.")}

	policy = config.get("fefo")
	if not isinstance(policy, dict):
		return {
			"fefo_reviewer": _("Name the expiry reviewer in the agent settings."),
			"fefo_near_expiry_days": _("Set near-expiry days to a whole number from 1 through 3650."),
		}

	issues: dict[str, str] = {}
	if set(policy) - _BUNDLE_KEYS:
		issues["config"] = _(
			"Use only the expiry reviewer, near-expiry days, and test declaration in the FEFO settings."
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
	"""Validate an enabled FEFO schedule as its eventual run-as identity."""
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
		from jarvis.chat.agent_scope import resolve_scope
		from jarvis.exceptions import InvalidArgumentError

		try:
			resolve_scope(installation, allow_record_scope=True)
		except InvalidArgumentError as exc:
			frappe.throw(str(exc), title=_("Complete FEFO configuration"))
