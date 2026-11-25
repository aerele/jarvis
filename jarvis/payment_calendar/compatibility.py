"""Audited ERPNext/Frappe source boundaries, pinned by source hash. Four kinds of pin:

* classification pins (`_PROFILES`, the bank helper hashes): an unknown source refuses the read;
* speed pins (`_SPEED_PINS`): an unknown source makes the caller use the upstream method;
* reader pins (`_READER_PINS`): an unknown source makes the built-in reader fall back to today's read;
* permission pins (`_PERMISSION_PINS`): an unknown source or an unexpected binding sends the batch
  permission evaluator to the per-document path. They are separate from the reader pins on purpose.
"""

import ast
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from importlib import import_module
from importlib.util import find_spec
from inspect import getattr_static, getsource
from types import FunctionType, ModuleType

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable


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


# --- Batch permission evaluator: pins and identity checks ---
#
# Pin names are "<module>.<qualname>" of the DEFINING module; a trailing ".func" marks a cached_property
# body and is dropped before the lookup. The hash covers the whole definition, decorators included.
# 16: Frappe 16.56.0 and ERPNext 16.55.0 (read 2026-11-22). Only a "16" entry exists.
# fmt: off
_PERMISSION_PINS: dict[str, dict[str, tuple[str, ...]]] = {
	"16": {
		"frappe.has_permission": ("7e66e753754009093d399907ce40fee5195545f8b44af6cdaa082114f23b79c0",),
		"frappe.permissions.has_permission": ("2116abae42c94524bddde4a8a9cdba15f7f88d536ddeb970e9dcadf707e40ce1",),
		"frappe.permissions.print_has_permission_check_logs": ("43c094e9d07ee6953e36110d5f2e6ce5dbcf318f53660fa0c893ce8876aad869",),
		"frappe.permissions.get_doc_permissions": ("c825ae94f6825068b4928b3801cf5638bffc6ec2bacbac9626127ce64608180c",),
		"frappe.permissions.get_role_permissions": ("350819631e5417085a79986a7a4eeb55c172052e054f3fc8017071724119d50c",),
		"frappe.permissions.get_user_permissions": ("430d7895110c1904b374deac2d92172ba13854d86a6f54a16c2a1060d8b4eb1e",),
		"frappe.permissions.has_user_permission": ("d1ed2d8bcc8ba9eeb892f589f8bd030227c016f0ca9098423f8f59643a3f8067",),
		"frappe.permissions.has_controller_permissions": ("80f0568623b280c17cb66870bf7370df74039e1b912a27416d5f523bfd6714b0",),
		"frappe.permissions.get_allowed_docs_for_doctype": ("618662c27228adec96028daa8204a099b18900630d14d4a8c8f5cede545e351d",),
		"frappe.permissions.filter_allowed_docs_for_doctype": ("d3f6668f9962c634ea116d9f500e6d91eda3ab0fe6e6684301468eaeb56b7f05",),
		"frappe.permissions.push_perm_check_log": ("ef7473d61d7a9b995df671b30c3901887c7ea8e378644309ca40c2505680d1b7",),
		"frappe.permissions.get_doc_name": ("fd2ff25fd9271def333f242461206c9686bb6eae2c00942f78249097b133fa1d",),
		"frappe.core.doctype.permission_type.permission_type.get_doctype_ptype_map": ("85d58592d449d645bed669c4f4c411f7f7e456edea1c331cb6329a8b384d4be4",),
		"frappe.only_has_select_perm": ("5f930a4e0dded4ada3cc1286e9c8955e09fac2e1401a5491d534040a725778f9",),
		"frappe.is_table": ("84df7e7a229ba6a8f6ac68119e8cb73dd381eb79d757e19a132725eaae367c6e",),
		"frappe.call": ("1757684999d360521b7cf7ee3e099b7b9cc5c0c7cc99f1610a54b80cc880accd",),
		"frappe.share.get_shared": ("d4d06addc4fceb777a64e393db388f72953f60b80809b31b1f8cf706ed3926b9",),
		"frappe.get_all": ("64cc2db29d4af82c782b75376568e71e4ba790b6a5f7417444adb6e01bade173",),
		"frappe.utils.get_table_name": ("f203a79a14ffc4d0df43ea0ac9539fb0838f90e0fd2a654a297a5933f949c39e",),
		"frappe.utils.caching.site_cache": ("6bef8d8576570a3dc33f9a4209de5fc7db3bd8168257a6afdfe84163f0ce4323",),
		"frappe.model.document.get_lazy_doc": ("a388eaa5bb5d62afea0f282d82c41c77b09fe588b174c37a42a276800a2ca4c8",),
		"frappe.model.document.get_doc_permission_check": ("8457f821962c80963725448f93fbc911f00812db845a93a66850bc8458b04c11",),
		"frappe.model.document.get_lazy_controller": ("4da38adcf7f2893314302157ae1ff84789d64181d17c78c019cbad6b8ad65050",),
		"frappe.model.document.get_controller": ("3aa72e4751a4e0663912679fdea8e9af19154de5bc795b8dbb477a1042f613cb",),
		"frappe.model.document._DocumentBase.__init__": ("6f568c7ac4f3bb50a76e30053222d3ce5b7a608c993f9193748dcf1f21230cf0",),
		"frappe.model.document.Document.__init__": ("e77a5c5c8de9fc1ae171b45d1c20551d20cc5568ada197f1e42db957522140e6",),
		"frappe.model.document.Document.load_from_db": ("501d8836fcf1c12fdedbc8cb2a18db29845933b409644107712feb2b1ae9a0b1",),
		"frappe.model.document.Document.init_from_dict": ("7371e29f5490187204f9da146c250aa4d16ff191649fc21edad9a5bdc908a8f5",),
		"frappe.model.document.Document.update": ("8134d7867d7d3f17cb1474b8fc03a1beb867d2f3517b89c58e4f0ed187d27848",),
		"frappe.model.document.Document.get": ("c97c40804a88c9abec92812b60f4c1489eb6ca40782ebdd921f2b1c0680c0bac",),
		"frappe.model.document.Document.set": ("437e89dbaeeca4bf92c8b6fc46134628f6d4296b39aaadc58533234b0070027d",),
		"frappe.model.document.Document.load_children_from_db": ("67d378aadebca91ac02047721b3c20b027c185bce63e1fd0f481c73a925974d4",),
		"frappe.model.document.Document._load_child_table_from_db": ("c7a67e48a73ff1d912be9df1bb9e53a2f3f7c1a41af1809d3b9ba144c01d802c",),
		"frappe.model.document.Document.get_all_children": ("bca479549b766a86c50b2852597ef22502eeb64c26eb8d36fd23ba1afd464e76",),
		"frappe.model.document.Document.append": ("470e1b0e181a31a6a1d368338a67f0e2e5dc21867dc613805320f4df12e7d81b",),
		"frappe.model.document.Document.extend": ("afaae536347a8e59f240c2597b70472b9e7434a05615d50e1e8bdd8eaaeb3404",),
		"frappe.model.document.Document._init_child": ("c263e111047ee1e849df488e360cd8a0a6ef012210212d78ef9293a512d9368f",),
		"frappe.model.document.Document.init_child_tables": ("ca5148cf66643145cef29577be0d505b64e34c930df96355acc692c3fb27c0e1",),
		"frappe.model.document.Document.init_valid_columns": ("2907199a76b145a45fd45f52f6a914b882f28ae3c536f2a4b828d3f07d595f81",),
		"frappe.model.document.Document.get_valid_columns": ("ff423a4f7f9566e933af33259cc2ca23dccc9dd6fbd96dc95caef780b5c4b902",),
		"frappe.model.document.Document._table_fieldnames.func": ("2c608946b8d01dc673a610719360a86c39e3e81b9da972d8bc67cc559ed387e0",),
		"frappe.model.document.Document._non_computed_table_fieldnames.func": ("8d8e81f6ef5f120f5fa95bdbefc49fb94ea551c365f7e0a3cc017f62451bb081",),
		"frappe.model.document.Document.meta": ("a0746e4022801cbd3e4d7976438e9f34788b40aaae603c46667760ae27812ef1",),
		"frappe.model.document.LazyDocument.get": ("45b428e22a84b0ab7765a8f43dcb95e2c9d006b9aa03ae8541ead364a39a64c8",),
		"frappe.model.document.LazyDocument.append": ("b5b7a2847eb873afd34af9906c526644e9139cd042b3a488c776f8433f360516",),
		"frappe.model.document.LazyDocument.load_children_from_db": ("c3b56454c945124925f9498a625e30cfe4327ad9dcc5ce96cb00a20734ceb649",),
		"frappe.model.document.LazyDocument.init_child_tables": ("945f9845fee87d860b2f11a74e9111522808ef5e9e87d7d97d76ef5182c6c6d1",),
		"frappe.model.document.LazyChildTable.__get__": ("9ddd6a831f8c52f2daa39fe98e557aba82d8da345b6c34a345707b10194227f0",),
		"frappe.model.meta.Meta.get_link_fields": ("a278b6d56910b045b1aaa2ae975545bd956c5ca5479571c72e9b6720bfcc0e6f",),
		"frappe.model.meta.Meta.get_dynamic_link_fields": ("449a6f064cf2f692af10830bca36c9b8cf9de3ea1126d7ca6d9d6a149b02e224",),
		"frappe.model.meta.Meta.get_table_fields": ("6a728027096f68f5ba041bf5be7f8775508dbeb5f2bdcd6eb86aba2d7adf75c7",),
		"frappe.model.meta.Meta._table_fields.func": ("ee77a07428a86d643a1747ec996b9f1cd652a4f303ca079146e81134b7aaf35c",),
		"frappe.model.meta.Meta._table_doctypes.func": ("56ccf6347bc410ddc0a653b803a22e6df98e062a5996129ff1282904d8076799",),
		"frappe.model.meta.Meta._non_computed_table_fields.func": ("55e981ced2433c5ebcd5ebcb5db6ba160b7fd0f751cf40924a3266bbe07ec4b0",),
		"frappe.model.meta.Meta._non_computed_table_doctypes.func": ("9a5d63ee06af24408e6d1a6a4fc1aafda66b9e1de7f4d5c32abd02d9aed7b4fe",),
		"frappe.core.doctype.user_permission.user_permission.get_user_permissions": ("1706c91982f882c9dfca20bc22ac74cc319aabf625c533966998f38d8b3c3542",),
		"frappe.core.doctype.system_settings.system_settings.get_system_settings": ("016b578465cdbd75364aaedf72946d4385a18447ae9cebe39d2d0a6eedc5274e",),
		"erpnext.stock.doctype.company_restriction.company_restriction.has_permission": ("914bbd15fcee2abea64077347613cac3324044ce9a0304896347772655ae33c4",),
		"erpnext.stock.doctype.company_restriction.company_restriction.has_inherited_permission": ("f944694dddd8567c796442b2f399a9da09b77a976c527b6933b11dc53e9d2ff7",),
		"erpnext.stock.doctype.company_restriction.company_restriction.get_allowed_companies": ("286f23d1f2ddea9f73983d4313ed774558f42b3df24ce42fc371c53cd4d4a9ce",),
		"erpnext.stock.doctype.company_restriction.company_restriction.get_inherited_master_links": ("e34ea22beade82f760fa8f9fad1fae8772dc6de9072e908b0983d68ccfbaf6ac",),
		"erpnext.stock.doctype.company_restriction.company_restriction.get_master_links": ("ddb89dab9f8ed5556bf92abfac295d460258f91f5309d6a5fc84a0f9e4334503",),
		"erpnext.stock.doctype.company_restriction.company_restriction.collect_master_references": ("594a364b5e64c10f5cdc9ec25f67f5e3587bfb92cd76615e89b0ef3a4c61acb9",),
		"erpnext.stock.doctype.company_restriction.company_restriction.get_blocked_masters": ("b818a74a252878c5ba6f020dbb3016cc90c259b27acfe7da5225a06648a1605e",),
		"erpnext.accounts.doctype.sales_invoice.sales_invoice.SalesInvoice.__init__": ("5daf67e94b0ed67957ce81bb28cbab4e1e23a6be5761da68377bf1ae0d4a2e45",),
		"erpnext.accounts.doctype.purchase_invoice.purchase_invoice.PurchaseInvoice.__init__": ("8f0ae34332c975fb841bd2dfaebbcab87238d14d9eaf45297f7118e1d55ee8af",),
		"erpnext.accounts.doctype.payment_entry.payment_entry.PaymentEntry.__init__": ("0b694648ab6b098a259c67cfca46f376f6a6f355ea4fd7674a21a591b313c492",),
		"erpnext.accounts.doctype.payment_entry.payment_entry.PaymentEntry.setup_party_account_field": ("38f5593e5db3e574f784f33bcb832e333c9e2d0d928d7a82874e235b9a724611",),
		"erpnext.accounts.doctype.journal_entry.journal_entry.JournalEntry.__init__": ("d94dec95fa246b680b211adea649552417c7e630aa075d24e7ce6e79323b3a61",),
		"erpnext.controllers.selling_controller.SellingController.__setup__": ("4bc718edae7d36fefcf3fbe8d7f6c88836cc329e3d53e41aac6c39f07feae62a",),
		"erpnext.controllers.buying_controller.BuyingController.__setup__": ("f0dda6d2e42a1529a2faa8439c4c431f0c133aee4d9cf87f67b62995afdb5e38",),
		"erpnext.controllers.subcontracting_controller.SubcontractingController.__init__": ("32dc9d1b5cefee5f6e69b591e045bda99f904cd2f19d0e229ee5a6e2ef038072",),
		"jarvis_playground.visitors.access.has_permission": ("4bf03016fcba1fc2eda89964021ee9b808be2cdc4ff2ed2f0d6175b2bf1b147c",),
	},
}
# fmt: on

# Pinned only when the app is importable (the other apps are required: a missing one is drift).
_OPTIONAL_APPS = frozenset({"jarvis_playground"})

# Value pins: the batch copies these constants' meaning, so a changed value is drift. Major-independent.
_VALUE_CHECKS: dict[str, Callable[[object], bool]] = {
	"erpnext.stock.doctype.company_restriction.company_restriction.RESTRICTABLE_MASTER_DOCTYPES": (
		lambda value: value == ("Item", "Customer", "Supplier")
	),
	"jarvis_playground.visitors.access.WRITE_PTYPES": lambda value: {"read", "select"}.isdisjoint(value),
}

# Controller methods that the batch accepts in the MRO of a document, because each only sets non-field state
# and is audited: (class __qualname__, method)
# -> (defining file, qualname). Each one is pinned above and identity-checked below.
_ACCOUNTS = "erpnext/accounts/doctype/"
_CONTROLLER_DEFS: dict[tuple[str, str], tuple[str, str]] = {
	("SalesInvoice", "__init__"): (_ACCOUNTS + "sales_invoice/sales_invoice.py", "SalesInvoice.__init__"),
	("PurchaseInvoice", "__init__"): (
		_ACCOUNTS + "purchase_invoice/purchase_invoice.py",
		"PurchaseInvoice.__init__",
	),
	("PaymentEntry", "__init__"): (_ACCOUNTS + "payment_entry/payment_entry.py", "PaymentEntry.__init__"),
	("PaymentEntry", "setup_party_account_field"): (
		_ACCOUNTS + "payment_entry/payment_entry.py",
		"PaymentEntry.setup_party_account_field",
	),
	("JournalEntry", "__init__"): (_ACCOUNTS + "journal_entry/journal_entry.py", "JournalEntry.__init__"),
	("SellingController", "__setup__"): (
		"erpnext/controllers/selling_controller.py",
		"SellingController.__setup__",
	),
	("BuyingController", "__setup__"): (
		"erpnext/controllers/buying_controller.py",
		"BuyingController.__setup__",
	),
	("SubcontractingController", "__init__"): (
		"erpnext/controllers/subcontracting_controller.py",
		"SubcontractingController.__init__",
	),
}
CONTROLLER_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(_CONTROLLER_DEFS)


@dataclass(frozen=True)
class LookupRow:
	"""One name that the per-document permission path resolves (the lookup table of the batch design).

	`binding` is a module path (module attribute), a class path (fixed class attribute), `<parent>` (the lazy
	controller of the doctype), `<child>` (each child table controller) or `<descriptors>` (the table
	descriptors in the lazy class dict). `file` is relative to the app's parent directory, so it starts with
	the app name. `wrapper` is the (file, qualname) of a decorator wrapper that the live object must be, with
	the definition one `__wrapped__` level below it. Kind: B per-document only, S shared (checked anyway),
	X shared and not checked.
	"""

	row: str
	binding: str
	attribute: str
	file: str | None
	qualname: str | None
	kind: str
	wrapper: tuple[str, str] | None = None
	controllers: bool = False  # an allowlisted controller method may stand in for the definition
	absent_ok: bool = False  # the attribute may be missing


_FRAPPE = "frappe/__init__.py"
_PERMISSIONS = "frappe/permissions.py"
_DOCUMENT = "frappe/model/document.py"
_META = "frappe/model/meta.py"
_RESTRICTION_FILE = "erpnext/stock/doctype/company_restriction/company_restriction.py"
_USER_PERMISSION_FILE = "frappe/core/doctype/user_permission/user_permission.py"
_PERMISSION_TYPE_FILE = "frappe/core/doctype/permission_type/permission_type.py"
_SYSTEM_SETTINGS_FILE = "frappe/core/doctype/system_settings/system_settings.py"
_SITE_CACHE_WRAPPER = "site_cache.<locals>.time_cache_wrapper.<locals>.site_cache_wrapper"
_HAS_PERMISSION_WRAPPER = "print_has_permission_check_logs.<locals>.inner"
_DOC_CLASS = "frappe.model.document.Document"
_LAZY_CLASS = "frappe.model.document.LazyDocument"
_META_CLASS = "frappe.model.meta.Meta"
_RESTRICTION_MODULE = "erpnext.stock.doctype.company_restriction.company_restriction"
_PARENT, _CHILD = "<parent>", "<child>"


_row = LookupRow
# fmt: off
PERMISSION_LOOKUP: tuple[LookupRow, ...] = (
	_row("1", "frappe", "has_permission", _FRAPPE, "has_permission", "B"),
	_row("2", "frappe.permissions", "has_permission", _PERMISSIONS, "has_permission", "B", wrapper=(_PERMISSIONS, _HAS_PERMISSION_WRAPPER)),
	_row("3", "frappe.permissions", "has_permission", _PERMISSIONS, "has_permission", "B", wrapper=(_PERMISSIONS, _HAS_PERMISSION_WRAPPER)),
	_row("4", "frappe", "is_table", _FRAPPE, "is_table", "B"),
	_row("5", "frappe", "get_lazy_doc", _DOCUMENT, "get_lazy_doc", "B"),
	_row("6", "frappe.permissions", "get_doc_permissions", _PERMISSIONS, "get_doc_permissions", "B"),
	_row("7", "frappe.permissions", "push_perm_check_log", _PERMISSIONS, "push_perm_check_log", "B"),
	# The live object is the site_cache wrapper, not the function in permission_type.py.
	_row("8", "frappe.permissions", "get_doctype_ptype_map", _PERMISSION_TYPE_FILE, "get_doctype_ptype_map", "B", wrapper=("frappe/utils/caching.py", _SITE_CACHE_WRAPPER)),
	_row("9", "frappe.permissions", "get_doc_name", _PERMISSIONS, "get_doc_name", "B"),
	_row("10", "frappe.share", "get_shared", "frappe/share.py", "get_shared", "B"),
	_row("11", "frappe.permissions", "has_controller_permissions", _PERMISSIONS, "has_controller_permissions", "B"),
	_row("12", "frappe.permissions", "get_role_permissions", _PERMISSIONS, "get_role_permissions", "S"),
	_row("13", "frappe.permissions", "has_user_permission", _PERMISSIONS, "has_user_permission", "S"),
	_row("14", "frappe", "call", _FRAPPE, "call", "S"),
	_row("15", "frappe", "get_attr", None, None, "X"),
	_row("16", "frappe.core.doctype.user_permission.user_permission", "get_user_permissions", _USER_PERMISSION_FILE, "get_user_permissions", "S"),
	_row("17", "frappe", "get_system_settings", _SYSTEM_SETTINGS_FILE, "get_system_settings", "S"),
	_row("18", "frappe.permissions", "get_allowed_docs_for_doctype", _PERMISSIONS, "get_allowed_docs_for_doctype", "S"),
	_row("19", "frappe.permissions", "filter_allowed_docs_for_doctype", _PERMISSIONS, "filter_allowed_docs_for_doctype", "S"),
	_row("20", "frappe", "only_has_select_perm", _FRAPPE, "only_has_select_perm", "S"),
	_row("21a", "frappe.permissions", "get_user_permissions", _PERMISSIONS, "get_user_permissions", "S"),
	_row("21b", "frappe.permissions", "get_allowed_docs_for_doctype", _PERMISSIONS, "get_allowed_docs_for_doctype", "S"),
	_row("22a", "frappe", "get_all", _FRAPPE, "get_all", "B"),
	_row("22b", "frappe.model.document", "get_table_name", "frappe/utils/__init__.py", "get_table_name", "B"),
	_row("22-get_inherited_master_links", _RESTRICTION_MODULE, "get_inherited_master_links", _RESTRICTION_FILE, "get_inherited_master_links", "S"),
	_row("22-get_master_links", _RESTRICTION_MODULE, "get_master_links", _RESTRICTION_FILE, "get_master_links", "S"),
	_row("22-collect_master_references", _RESTRICTION_MODULE, "collect_master_references", _RESTRICTION_FILE, "collect_master_references", "S"),
	_row("22-get_allowed_companies", _RESTRICTION_MODULE, "get_allowed_companies", _RESTRICTION_FILE, "get_allowed_companies", "S"),
	_row("22-get_blocked_masters", _RESTRICTION_MODULE, "get_blocked_masters", _RESTRICTION_FILE, "get_blocked_masters", "S"),
	_row("22-Meta.get_link_fields", _META_CLASS, "get_link_fields", _META, "Meta.get_link_fields", "S"),
	_row("22-Meta.get_dynamic_link_fields", _META_CLASS, "get_dynamic_link_fields", _META, "Meta.get_dynamic_link_fields", "S"),
	_row("22-Meta.get_table_fields", _META_CLASS, "get_table_fields", _META, "Meta.get_table_fields", "S"),
	_row("22-Meta._table_fields", _META_CLASS, "_table_fields", _META, "Meta._table_fields", "S"),
	_row("22-Meta._table_doctypes", _META_CLASS, "_table_doctypes", _META, "Meta._table_doctypes", "S"),
	_row("22-Meta._non_computed_table_fields", _META_CLASS, "_non_computed_table_fields", _META, "Meta._non_computed_table_fields", "S"),
	_row("22-Meta._non_computed_table_doctypes", _META_CLASS, "_non_computed_table_doctypes", _META, "Meta._non_computed_table_doctypes", "S"),
	_row("23", _RESTRICTION_MODULE, "has_inherited_permission", _RESTRICTION_FILE, "has_inherited_permission", "S"),
	_row("24", _RESTRICTION_MODULE, "has_permission", _RESTRICTION_FILE, "has_permission", "S"),
	_row("25", "jarvis_playground.visitors.access", "has_permission", "jarvis_playground/visitors/access.py", "has_permission", "S"),
	_row("26", "frappe.model.document", "get_lazy_controller", _DOCUMENT, "get_lazy_controller", "B"),
	_row("27", "frappe.model.document", "get_controller", _DOCUMENT, "get_controller", "B"),
	_row("28", "frappe.model.document", "get_doc_permission_check", _DOCUMENT, "get_doc_permission_check", "B"),
	_row("29", _PARENT, "__init__", _DOCUMENT, "Document.__init__", "B", controllers=True),
	_row("29a", "frappe.model.document._DocumentBase", "__init__", _DOCUMENT, "_DocumentBase.__init__", "B"),
	# An allowlisted controller __init__ ends in Document.__init__, so that one is checked at the class too.
	_row("29b", _DOC_CLASS, "__init__", _DOCUMENT, "Document.__init__", "B"),
	_row("30", _PARENT, "load_from_db", _DOCUMENT, "Document.load_from_db", "B"),
	_row("31", _PARENT, "init_from_dict", _DOCUMENT, "Document.init_from_dict", "B"),
	_row("31c", _CHILD, "init_from_dict", _DOCUMENT, "Document.init_from_dict", "B"),
	_row("32", _PARENT, "update", _DOCUMENT, "Document.update", "B"),
	_row("32a", _PARENT, "set", _DOCUMENT, "Document.set", "B"),
	_row("32c", _CHILD, "update", _DOCUMENT, "Document.update", "B"),
	_row("33", _PARENT, "__setup__", None, None, "B", controllers=True, absent_ok=True),
	_row("33c", _CHILD, "__setup__", None, None, "B", controllers=True, absent_ok=True),
	_row("34", _PARENT, "load_children_from_db", _DOCUMENT, "LazyDocument.load_children_from_db", "B"),
	_row("35", _PARENT, "get_all_children", _DOCUMENT, "Document.get_all_children", "B"),
	_row("35a", _PARENT, "_table_fieldnames", _DOCUMENT, "Document._table_fieldnames", "B"),
	_row("35b", _PARENT, "_non_computed_table_fieldnames", _DOCUMENT, "Document._non_computed_table_fieldnames", "B"),
	_row("36", _PARENT, "get", _DOCUMENT, "LazyDocument.get", "B"),
	_row("36a", _DOC_CLASS, "get", _DOCUMENT, "Document.get", "B"),
	_row("37", "<descriptors>", "__dict__", None, None, "B"),
	_row("37a", "frappe.model.document.LazyChildTable", "__get__", _DOCUMENT, "LazyChildTable.__get__", "B"),
	_row("38", _PARENT, "_load_child_table_from_db", _DOCUMENT, "Document._load_child_table_from_db", "B"),
	_row("39", _PARENT, "extend", _DOCUMENT, "Document.extend", "B"),
	_row("40", _PARENT, "append", _DOCUMENT, "LazyDocument.append", "B"),
	_row("40a", _DOC_CLASS, "append", _DOCUMENT, "Document.append", "B"),
	_row("41", _PARENT, "_init_child", _DOCUMENT, "Document._init_child", "B"),
	_row("42", _CHILD, "__new__", None, None, "B"),
	_row("42a", _CHILD, "__init__", _DOCUMENT, "Document.__init__", "B"),
	_row("43", _CHILD, "init_child_tables", _DOCUMENT, "Document.init_child_tables", "B"),
	_row("43a", _CHILD, "get_valid_columns", _DOCUMENT, "Document.get_valid_columns", "B"),
	_row("43b", _CHILD, "init_valid_columns", _DOCUMENT, "Document.init_valid_columns", "B"),
	_row("44", _PARENT, "init_child_tables", _DOCUMENT, "LazyDocument.init_child_tables", "B"),
	_row("45", _PARENT, "meta", _DOCUMENT, "Document.meta", "B"),
	_row("46", "frappe", "get_meta", None, None, "X"),
	_row("47", "frappe", "get_hooks", None, None, "X"),
)
# fmt: on
# Rows that fail when the generated `Lazy<Name>` class holds more than its table descriptors (the `get` lookup
# on the lazy class and the descriptor dict): `batch_permission` reports them as `controller_override`.
LAZY_CLASS_ROWS: frozenset[str] = frozenset(
	row.row
	for row in PERMISSION_LOOKUP
	if row.binding == "<descriptors>" or (row.binding == _PARENT and row.attribute == "get")
)


def permission_pin_drift() -> list[str]:
	"""Sorted permission pin names whose installed source is not an accepted hash, or whose value changed.

	With no entry for the installed major, or a frappe and ERPNext major that differ, every applicable name
	has drifted. A pinned file that cannot be read has drifted. An optional app that is not installed is skipped.
	"""
	pins = _installed_permission_pins()
	if pins is None:
		names = {name for entry in _PERMISSION_PINS.values() for name in entry}
		return sorted(name for name in names | set(_VALUE_CHECKS) if _applies(name))
	drifted = [name for name, accepted in pins.items() if _applies(name) and not _pin_holds(name, accepted)]
	drifted += [
		name for name, check in _VALUE_CHECKS.items() if _applies(name) and not _value_holds(name, check)
	]
	return sorted(drifted)


def permission_identity_failures(lazy_cls: type, child_classes: Iterable[type]) -> list[str]:
	"""Sorted ids of the lookup rows whose live object is not the pinned definition.

	`lazy_cls` is the class per-document instantiates for the parent, `child_classes` the controllers of
	every table field. A child row id carries the class name (`42:Name`). `allowlist:Class.method` is an allowlisted
	controller method that is not the pinned one. `inspect.unwrap` is never used: a wrapper from another app
	has its own file and fails.
	"""
	children = list(child_classes)
	failures: set[str] = set()
	for row in PERMISSION_LOOKUP:
		try:
			if row.kind != "X" and _row_applies(row):
				failures.update(_row_failures(row, lazy_cls, children))
		except TIMEOUTS:
			raise
		except Exception:
			failures.add(row.row)  # a check that cannot run never passes
	try:
		failures.update(_controller_failures([lazy_cls, *children]))
	except TIMEOUTS:
		raise
	except Exception:
		failures.add("allowlist")
	return sorted(failures)


def defined_source_hash(path: str, qualname: str) -> str | None:
	"""sha256 of the source of the definition `qualname` in the file `path`, or None when it cannot be read.

	The qualname is dotted, with `<locals>` for a nested function (`outer.<locals>.inner`, `Class.method`).
	The text is the whole lines from the first decorator to the last line, which is what `inspect.getsource`
	returns. A file is parsed once per process; a change of its modification time or size parses it again.
	"""
	index = _source_index(path)
	return index.hash_of(qualname) if index else None


def defined_line(path: str, qualname: str) -> int | None:
	"""First line of a definition (its first decorator when decorated), the value of `co_firstlineno`."""
	index = _source_index(path)
	return index.line_of(qualname) if index else None


class _SourceIndex:
	"""The definitions of one source file: qualname -> (first line, sha256 of the definition text)."""

	def __init__(self, definitions: dict[str, tuple[int, str]]):
		self.definitions = definitions

	def hash_of(self, qualname: str) -> str | None:
		found = self.definitions.get(qualname)
		return found[1] if found else None

	def line_of(self, qualname: str) -> int | None:
		found = self.definitions.get(qualname)
		return found[0] if found else None

	@classmethod
	def parse(cls, path: str) -> "_SourceIndex":
		with open(path, encoding="utf-8") as handle:
			text = handle.read()
		lines = text.split("\n")
		spans: dict[str, tuple[int, int]] = {}
		cls._collect(ast.parse(text, path), "", spans)
		return cls(
			{
				qualname: (first, sha256(("\n".join(lines[first - 1 : last]) + "\n").encode()).hexdigest())
				for qualname, (first, last) in spans.items()
			}
		)

	@classmethod
	def _collect(cls, node: ast.AST, prefix: str, spans: dict[str, tuple[int, int]]) -> None:
		# Walk statements only; a later definition of the same name replaces an earlier one, as at run time.
		for child in ast.iter_child_nodes(node):
			if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
				qualname = prefix + child.name
				first = min([child.lineno] + [decorator.lineno for decorator in child.decorator_list])
				spans[qualname] = (first, child.end_lineno)
				cls._collect(
					child, qualname + ("." if isinstance(child, ast.ClassDef) else ".<locals>."), spans
				)
			elif isinstance(child, ast.stmt | ast.ExceptHandler | ast.match_case):
				cls._collect(child, prefix, spans)


# real path -> ((st_mtime_ns, st_size), index). Only a successful read is kept: an unreadable file is retried.
_source_indexes: dict[str, tuple[tuple[int, int], _SourceIndex]] = {}

# The definitions of a file are also kept in Redis, so a forked job or a new process does not parse a file
# that nobody changed. The key holds the file's stat, so an edited file never finds the old record. A Redis
# error is a miss: the file is parsed, never assumed unchanged. The value is a cache of a hash of a file on
# disk; Redis already holds the permission caches that the per-document path trusts.
_PERSISTED_TTL_S = 24 * 60 * 60


def _source_index(path: str) -> _SourceIndex | None:
	real = _real_path(path)
	try:
		stat = os.stat(real)
	except OSError:
		return None
	key = (stat.st_mtime_ns, stat.st_size)
	cached = _source_indexes.get(real)
	if cached and cached[0] == key:
		return cached[1]
	index = _load_persisted(real, key)
	if index is None:
		try:
			index = _SourceIndex.parse(real)
		except TIMEOUTS:
			raise
		except Exception:
			return None
		_store_persisted(real, key, index)
	_source_indexes[real] = (key, index)
	return index


def _persisted_key(real: str, key: tuple[int, int]) -> str:
	return frappe.cache.make_key(f"jarvis:pcal:srcdefs:{real}:{key[0]}:{key[1]}")


def _load_persisted(real: str, key: tuple[int, int]) -> _SourceIndex | None:
	"""The stored definitions of a file, or None when absent or not exactly the shape that was stored."""
	try:
		raw = frappe.cache.execute_command("GET", _persisted_key(real, key))
		if not raw:
			return None
		definitions = {}
		for qualname, (first, digest) in json.loads(raw).items():
			if not (isinstance(first, int) and isinstance(digest, str) and len(digest) == 64):
				return None
			definitions[str(qualname)] = (first, digest)
		return _SourceIndex(definitions)
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _store_persisted(real: str, key: tuple[int, int], index: _SourceIndex) -> None:
	try:
		frappe.cache.execute_command(
			"SET", _persisted_key(real, key), json.dumps(index.definitions), "EX", _PERSISTED_TTL_S
		)
	except TIMEOUTS:
		raise
	except Exception:
		pass  # no Redis: the next process parses again


# path string -> os.path.realpath. A symlink that is retargeted later is not followed; the file key still is.
_real_paths: dict[str, str] = {}


def _real_path(path: str) -> str:
	real = _real_paths.get(path)
	if real is None:
		real = _real_paths[path] = os.path.realpath(path)
	return real


def _installed_permission_pins() -> dict[str, tuple[str, ...]] | None:
	try:
		import erpnext
	except ImportError:
		return None
	major = erpnext.__version__.split(".")[0]
	return _PERMISSION_PINS.get(major) if frappe.__version__.split(".")[0] == major else None


def _applies(name: str) -> bool:
	package = name.split(".")[0]
	return package not in _OPTIONAL_APPS or _importable(package)


def _importable(package: str) -> bool:
	try:
		return find_spec(package) is not None
	except (ImportError, ValueError):
		return False


def _pin_holds(name: str, accepted: tuple[str, ...]) -> bool:
	try:
		resolved = _resolve_pin(name)
		return bool(resolved) and defined_source_hash(*resolved) in accepted
	except TIMEOUTS:
		raise
	except Exception:
		return False


# pin name -> (real path of the defining file, qualname). Only a success is kept.
_pin_sources: dict[str, tuple[str, str]] = {}


def _resolve_pin(name: str) -> tuple[str, str] | None:
	if name in _pin_sources:
		return _pin_sources[name]
	parts = name.removesuffix(".func").split(".")
	for end in range(len(parts) - 1, 0, -1):
		try:
			module = import_module(".".join(parts[:end]))
		except ImportError:
			continue
		except TIMEOUTS:
			raise
		except Exception:
			return None  # a module that fails to import is not a source we can pin
		path = getattr(module, "__file__", None)
		if path is None:
			return None
		_pin_sources[name] = (_real_path(path), ".".join(parts[end:]))
		return _pin_sources[name]
	return None


def _value_holds(name: str, check: Callable[[object], bool]) -> bool:
	module_path, attribute = name.rsplit(".", 1)
	try:
		return bool(check(getattr(import_module(module_path), attribute)))
	except TIMEOUTS:
		raise
	except Exception:
		return False


def _is_module(binding: str) -> bool:
	return not binding.startswith("<") and isinstance(_binding(binding), ModuleType)


def _row_applies(row: LookupRow) -> bool:
	return (
		row.file is None
		or row.file.split("/")[0] not in _OPTIONAL_APPS
		or _importable(row.file.split("/")[0])
	)


def _row_definitions(row: LookupRow) -> list[tuple[str, str]]:
	"""(real path, qualname) of the definition and of the wrapper of a row, as pinned."""
	definitions = [(row.file, row.qualname), *([row.wrapper] if row.wrapper else [])]
	return [(path, qualname) for file, qualname in definitions if file and (path := _source_path(file))]


# relative file -> real path. Only a success is kept.
_source_paths: dict[str, str] = {}


def _source_path(relative: str) -> str | None:
	"""Real path of a file given relative to the parent directory of its app package."""
	if relative in _source_paths:
		return _source_paths[relative]
	app, _, rest = relative.partition("/")
	try:
		package_file = import_module(app).__file__
	except ImportError:
		return None
	except TIMEOUTS:
		raise
	except Exception:
		return None
	_source_paths[relative] = _real_path(
		os.path.join(os.path.dirname(os.path.dirname(package_file)), app, rest)
	)
	return _source_paths[relative]


def _is_definition(live: object, file: str | None, qualname: str | None) -> bool:
	"""True when `live` is a function whose code is the definition `qualname` in `file`, at its AST line."""
	path = _source_path(file) if file else None
	if path is None or not isinstance(live, FunctionType):
		return False
	code = live.__code__
	return (
		(code.co_filename == path or _real_path(code.co_filename) == path)
		and code.co_qualname == qualname
		and code.co_firstlineno == defined_line(path, qualname)
	)


def _row_failures(row: LookupRow, lazy_cls: type, children: list[type]) -> list[str]:
	if row.binding == _PARENT:
		return [row.row] if not _class_row_ok(row, lazy_cls) else []
	if row.binding == _CHILD:
		return [f"{row.row}:{cls.__name__}" for cls in children if not _class_row_ok(row, cls)]
	if row.binding == "<descriptors>":
		return [f"{row.row}:{key}" if key else row.row for key in _descriptor_failures(lazy_cls)]
	target = _binding(row.binding)
	if target is None:
		return [row.row]
	if isinstance(target, type):
		return [] if _class_row_ok(row, target) else [row.row]
	return [] if _module_row_ok(row, target) else [row.row]


def _module_row_ok(row: LookupRow, module: object) -> bool:
	live = getattr(module, row.attribute, None)
	if row.wrapper is None:
		return _is_definition(live, row.file, row.qualname)
	# The wrapper is exactly one level thick: its code is the pinned decorator's, and what it wraps is the
	# pinned definition. `functools.wraps` from another app has its own code and fails the first test.
	return _is_definition(live, *row.wrapper) and _is_definition(
		getattr(live, "__wrapped__", None), row.file, row.qualname
	)


def _class_row_ok(row: LookupRow, cls: type) -> bool:
	found = _static(cls, row.attribute)
	if row.attribute == "__new__":
		return found is object.__dict__["__new__"]
	if found is _ABSENT:
		return row.absent_ok
	accepted = [(row.file, row.qualname)] if row.file else []
	if row.controllers:
		accepted += [
			definition for (_, method), definition in _CONTROLLER_DEFS.items() if method == row.attribute
		]
	return any(_is_definition(found, *definition) for definition in accepted)


_ABSENT = object()


def _static(cls: type, name: str) -> object:
	"""The object `cls` resolves for `name` through its MRO without running descriptors, unwrapped one level."""
	try:
		found = getattr_static(cls, name)
	except AttributeError:
		return _ABSENT
	if isinstance(found, cached_property):
		return found.func
	if isinstance(found, property):
		return found.fget
	return found


# binding path -> (module, attributes below it). The module is memoised, never the attribute: the owner is
# read live on every call, so a rebinding after the memo is built is still seen. Only a success is kept.
_binding_modules: dict[str, tuple[ModuleType, tuple[str, ...]]] = {}


def _binding(path: str) -> object | None:
	"""The module, or the class, that a binding path names."""
	if path not in _binding_modules:
		parts = path.split(".")
		for end in range(len(parts), 0, -1):
			try:
				module = import_module(".".join(parts[:end]))
			except ImportError:
				continue
			_binding_modules[path] = (module, tuple(parts[end:]))
			break
		else:
			return None
	target, attributes = _binding_modules[path]
	for attribute in attributes:
		target = getattr(target, attribute, None)
	return target


_CLASS_ATTRIBUTES = frozenset(
	{
		"__module__",
		"__qualname__",
		"__doc__",
		"__dict__",
		"__weakref__",
		"__firstlineno__",
		"__static_attributes__",
	}
)


def _descriptor_failures(lazy_cls: type) -> list[str]:
	"""Keys of the generated lazy class that break the generated-class layout; "" when the layout cannot be known.

	The class dict holds exactly one pinned `LazyChildTable` per table field of its doctype (same fieldname and
	doctype) next to the standard type attributes, and nothing else. A missing, swapped or extra entry fails.
	"""
	from frappe.model.document import LazyChildTable

	tables = _table_fields_of(lazy_cls)
	if tables is None:
		return [""]
	own = {key: value for key, value in lazy_cls.__dict__.items() if key not in _CLASS_ATTRIBUTES}
	failures = [
		key
		for key, value in own.items()
		if not (type(value) is LazyChildTable and value.fieldname == key and tables.get(key) == value.doctype)
	]
	return failures + [fieldname for fieldname in tables if fieldname not in own]


def _table_fields_of(lazy_cls: type) -> dict[str, str] | None:
	"""{fieldname: child doctype} of the doctype that `get_lazy_controller` built `lazy_cls` for, or None."""
	registered = getattr(frappe, "lazy_controllers", {}).get(frappe.local.site, {})
	for doctype, cls in registered.items():
		if cls is lazy_cls:
			return {df.fieldname: df.options for df in frappe.get_meta(doctype).get_table_fields()}
	return None


def _controller_failures(classes: list[type]) -> list[str]:
	failures = []
	for cls in {base for controller in classes for base in controller.__mro__}:
		for class_name, method in CONTROLLER_ALLOWLIST:
			if cls.__qualname__ == class_name and method in cls.__dict__:
				if not _is_definition(cls.__dict__[method], *_CONTROLLER_DEFS[(class_name, method)]):
					failures.append(f"allowlist:{class_name}.{method}")
	return failures
