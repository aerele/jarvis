"""Audited ERPNext/Frappe source boundaries; unknown framework pairs fail closed."""

from dataclasses import dataclass
from hashlib import sha256
from inspect import getsource

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.payment_calendar.errors import CalendarSourceUnavailable


@dataclass(frozen=True)
class SourceProfile:
	major: str
	payment_schedule_hash: str
	defaults_cache_hit: tuple[str, str]


# ERPNext 15.124.1 (4e0dce9e) and 16.50.0 (7474d9e). Schedule/withholding
# formulas were compared before permitting either classifier source. The v15
# schedule differs only in initial flt normalization and the single-value helper.
_PROFILES = {
	"15": SourceProfile(
		"15",
		"fb85c419791fe15830f0d5794782897914a4946ef75c0c90972c65145ce07a77",
		("hget", "defaults"),
	),
	"16": SourceProfile(
		"16",
		"da22739f601983c4070b44a73bdefb683286145aefc89c2374f684aed32bd0f3",
		("get_value", "defaults::__default"),
	),
}


def source_profile() -> SourceProfile:
	import erpnext

	major = erpnext.__version__.split(".")[0]
	if frappe.__version__.split(".")[0] != major or major not in _PROFILES:
		raise CalendarSourceUnavailable()
	return _PROFILES[major]


def has_native_field_masks() -> bool:
	"""Frappe v15 has no native DocField/DocPerm mask columns."""
	source_profile()
	return frappe.get_meta("DocField").has_field("mask")


def require_obligation_scope(account_type: str) -> None:
	"""v15 AR lacks the canonical Sales Person user-permission filter in v16."""
	if (
		source_profile().major == "15"
		and account_type == "Receivable"
		and get_user_permissions().get("Sales Person")
	):
		raise CalendarSourceUnavailable("restricted_obligations")


def bank_balance_arguments(helper) -> dict:
	"""Preserve NULL/blank/default-book union across audited helper revisions."""
	profile = source_profile()
	try:
		actual = sha256(getsource(helper).encode()).hexdigest()
	except (OSError, TypeError) as exc:
		raise CalendarSourceUnavailable() from exc
	if profile.major == "15" and actual == "a1c0ba915be2a8555108b840f791f99bcae62636b5df47eb87eacb7e31f3aae2":
		return {}
	if profile.major == "16":
		# 16.50 has an unfiltered default call; include=True excludes blank books.
		if actual == "7af005912349b570c56e0fdd4306bab43721cb620a0e418ae25d38c359ebe3c6":
			return {"finance_book": None, "include_default_fb_balances": False}
		# 16.52 (3cd2183d) always filters: include the company default explicitly.
		if actual == "a365a6e531b6e95b0c37ec6768b5c3982122384f580aefa5e7712ce555228e2d":
			return {"finance_book": None, "include_default_fb_balances": True}
	raise CalendarSourceUnavailable()
