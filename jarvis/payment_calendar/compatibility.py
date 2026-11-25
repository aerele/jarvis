"""Audited ERPNext/Frappe source boundaries, pinned by source hash. Three kinds of pin:

* classification pins (`_PROFILES`, the bank helper hashes): an unknown source refuses the read;
* speed pins (`_SPEED_PINS`): an unknown source makes the caller use the upstream method;
* reader pins (`_READER_PINS`): an unknown source makes the built-in reader fall back to today's read.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from inspect import getsource

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.payment_calendar.errors import CalendarSourceUnavailable


@dataclass(frozen=True)
class SourceProfile:
	major: str
	payment_schedule_hashes: tuple[str, ...]
	defaults_cache_hit: tuple[str, str]


# ERPNext 15.124.1 (4e0dce9e) and 16.50.0 (7474d9e). Schedule/withholding
# formulas were compared before permitting either classifier source. The v15
# schedule differs only in initial flt normalization and the single-value helper.
_PROFILES = {
	"15": SourceProfile(
		"15",
		(
			"fb85c419791fe15830f0d5794782897914a4946ef75c0c90972c65145ce07a77",
			# 15.125.0 (2026-11-12) makes the same change as v16 495380b5: the
			# outstanding/base_outstanding reset moves into set_term_outstanding().
			# payment_amount and base_payment_amount are computed exactly as before.
			"2bbc12063ff2646e3a2462e4f221e75e94ca53ea63938cfa58b7184fcaf3d713",
			# ERPNext 15.126.0 adds flt() around both grand totals; numeric values stay the same.
			"d69e1e0b63ad48ea04d3956561663c7f5ca56fd05b13bd7e22aca76b981abb3e",
		),
		("hget", "defaults"),
	),
	"16": SourceProfile(
		"16",
		(
			"da22739f601983c4070b44a73bdefb683286145aefc89c2374f684aed32bd0f3",
			# 495380b5 (2026-11-06) moves the outstanding/base_outstanding reset into
			# set_term_outstanding(), which keeps the settled part of a submitted
			# document's terms. payment_amount and base_payment_amount, the only term
			# values read here, are computed exactly as before.
			"682dbd8b14af63422e21cf8a40fbfb6668b1a38831f08a97e1ee1e8413b9dd7f",
		),
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
		# 495380b5 (2026-11-06) is the same body plus an opt-in
		# ``apply_gl_entry_permissions=False`` that only appends GL Entry match conditions;
		# we never pass it, so the default call is identical to 16.52.
		if actual in (
			"a365a6e531b6e95b0c37ec6768b5c3982122384f580aefa5e7712ce555228e2d",
			"827c90fc28a78da7f27de9257f5bc09b5538e7e1165d96dd339c0a5abad5364f",
		):
			return {"finance_book": None, "include_default_fb_balances": True}
	raise CalendarSourceUnavailable()


# Speed overrides copy the body of an ERPNext method, so each one is pinned by the hash of the
# upstream source it was audited against. Unlike the classification pins above, an unknown hash
# never refuses a read: the caller falls back to upstream. Bump procedure: docs/architecture/payment-calendar.md.
# get_payment_terms: ERPNext 16.55.0 (8e10a348), same source as version-16 head on 2026-11-17.
# Party details are pre-seeded from the ledger, not copied, so they need no pin.
_SPEED_PINS = {
	"16": {
		"get_payment_terms": ("4ccd3880aedf87b29d7e4ce3eea6b57e84e5e5cefe96741d877e686e9c10b763",),
	},
}


def upstream_matches(name: str) -> bool:
	"""True when the installed ERPNext method `name` is a source audited for its speed override."""
	import erpnext

	major = erpnext.__version__.split(".")[0]
	pins = _SPEED_PINS.get(major, {}).get(name, ())
	return bool(pins) and _upstream_hash(name, major) in pins


# (name, major) -> hash. `major` is only a key; the hash is read from whatever ERPNext is installed.
# Only a successful read is kept: an unreadable source is retried, not remembered as unknown.
_hashes: dict[tuple[str, str], str] = {}


def _upstream_hash(name: str, major: str) -> str | None:
	if (name, major) not in _hashes:
		found = installed_hash(name)
		if found is None:
			return None
		_hashes[(name, major)] = found
	return _hashes[(name, major)]


def installed_hash(name: str) -> str | None:
	"""Uncached sha256 of the installed upstream method source, or None when it cannot be read."""
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

	try:
		return sha256(getsource(getattr(ReceivablePayableReport, name, None)).encode()).hexdigest()
	except (OSError, TypeError):
		return None


# The built-in reader copies or narrows the logic of these upstream functions, so each one is pinned by the
# hash of the source it was audited against. Names are fully qualified (module path, then attribute path).
# An unknown hash never refuses a read: the reader raises ReaderFallback("pin_drift") and today's read runs.
# 16: ERPNext 16.55.0 (8e10a348) and version-16 head 16.60.0 (886ab6ae, 2026-11-17) have the same source for
# every name below but the last-added pair (build_qb_match_conditions, get_filtered_data), so one hash each;
# that pair is hashed from the installed ERPNext 16.55.0 and frappe 16.56.0 and not compared with the head.
# Only a "16" entry exists; v15 entries come with the v15 backport.
_RPR = "erpnext.accounts.report.accounts_receivable.accounts_receivable.ReceivablePayableReport."
_RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."
_READER_PINS: dict[str, dict[str, tuple[str, ...]]] = {
	"16": {
		# Sequence re-run: get_data and the methods it calls that the reader reuses or replaces.
		_RPR + "get_data": ("6afcb9e48b38f40c5574240d767dc85faf9a4ef09638edf46c0609776997f66b",),
		_RPR + "fetch_ple_in_buffered_cursor": (
			"fe84993c128d7c411eb243335c4ef546a3fc2f0a64f0e45a95c6205156b6be7c",
		),
		_RPR + "fetch_ple_in_unbuffered_cursor": (
			"96bc3244934f4f9cff91f7fe77b03125a377aa517e5adf072125922127584987",
		),
		_RPR + "build_delivery_note_map": (
			"3153088d774c46f36c57b34424a748cb077249e764128f511576737a8d58436a",
		),
		# Narrowed copy of the invoice details read.
		_RPR + "get_invoice_details": ("775e7c1262eeb713c91dcaa24279a48c1eced03768ed904a53a5362a3d187ef9",),
		# Subset closure: the sum of key residuals equals the sum of line amounts.
		_RPR + "init_voucher_balance": ("50fcf1862bf45c96f57b29dc6aa07d91a70430730a95865a6a62db439c7b3c40",),
		_RPR + "get_voucher_balance": ("85c6747e59c247caa53a0d2c8b6709d5e81fd4c4d73d7097f6e0b0740ce950f2",),
		_RPR + "update_voucher_balance": (
			"5bdcfc25d6b2a49949469b1c782c21d22d2cc11403e3948cacd82e702dc8d4d0",
		),
		# Control-total predicate copy; the narrow reads extend ple_query.
		_RPR + "prepare_conditions": ("ef00919561052e62486aa0bab3468b17852a2e7777fb6ab62aceb41edbeba640",),
		_RPR + "add_common_filters": ("f7b280f065d52e374db14a6183be68069d7e500b0ce14fdfcb7442a1c5fbfe92",),
		_RPR + "exclude_employee_transaction": (
			"5b498a34caa9b23773d103175623632ea031c9242262ce305882db5f8dfcac22",
		),
		_RPR + "prepare_ple_query": ("9e431342f355878cb768683aac464ebaf2d8e9485e652ebe2aa3371386f5e974",),
		# The must_consider rule behind FX discovery, and the return fold.
		_RPR + "build_data": ("e06946f70655cee1a4fee818b465e458e3c843c4c0d59758c1ea4177ecb702ab",),
		_RPR + "get_return_entries": ("1aad0cf0bdeaa4d8d32ca0b90cb4842b37181cb8fa6e6d99169d7e014001ba9b",),
		# Header semantics used for discovery: account currency, no posting-date filter.
		"erpnext.accounts.utils.update_voucher_outstanding": (
			"a1796c5b901bfa6ad5493c04dfe213fb21520473794dd77090909f6ab2ac8196",
		),
		# Eligibility: the match conditions behind the report's per-user narrowing, and the report's own
		# per-row filter that the calendar runs on the engine's rows. A change in either may narrow more.
		"erpnext.accounts.utils.build_qb_match_conditions": (
			"191620a81236486bbb1352187da7ba676d15d96b18577dc296d92e997d31d2c7",
		),
		"frappe.desk.query_report.get_filtered_data": (
			"da077a20feb39ce209213809cf6bc577867a9b17d0810430ebca9414edfbdfa0",
		),
		# Company restriction hooks (eligibility).
		_RESTRICTION + "get_permission_query_conditions": (
			"2b3c4cece676fea27a11739b1204f0657d04f0654c0c2b708413d6608244960f",
		),
		_RESTRICTION + "has_permission": (
			"914bbd15fcee2abea64077347613cac3324044ce9a0304896347772655ae33c4",
		),
		_RESTRICTION + "get_inherited_permission_query_conditions": (
			"e325af70ec524ddd4c8ee94108b9504b367e02b0ca524ff15ab13e6d3f936749",
		),
		_RESTRICTION + "has_inherited_permission": (
			"f944694dddd8567c796442b2f399a9da09b77a976c527b6933b11dc53e9d2ff7",
		),
	},
}


def qualified_source_hash(qualified: str) -> str | None:
	"""Uncached sha256 of the source of a fully qualified function or method, or None when unreadable.

	The longest importable module prefix is imported and the rest is resolved by `getattr`.
	"""
	parts = qualified.split(".")
	for end in range(len(parts) - 1, 0, -1):
		try:
			target = import_module(".".join(parts[:end]))
		except ImportError:
			continue
		try:
			for attribute in parts[end:]:
				target = getattr(target, attribute)
			return sha256(getsource(target).encode()).hexdigest()
		except (AttributeError, OSError, TypeError):
			return None
	return None


def reader_pin_drift(names: Iterable[str] | None = None) -> list[str]:
	"""Sorted reader pin names whose installed source is not an accepted hash.

	With no entry for the installed major, or a frappe and ERPNext major that differ, every name has drifted.
	"""
	import erpnext

	major = erpnext.__version__.split(".")[0]
	pins = _READER_PINS.get(major) if frappe.__version__.split(".")[0] == major else None
	if names is None:
		names = pins if pins is not None else {name for entry in _READER_PINS.values() for name in entry}
	if pins is None:
		return sorted(names)
	return sorted(name for name in names if qualified_source_hash(name) not in pins.get(name, ()))
