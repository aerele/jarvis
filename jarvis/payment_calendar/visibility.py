"""Whether to show the Payment Calendar entry points to the current user.

The result is a UX flag only. The calendar API still applies its own permission gate, so a
stale or wrong flag can never grant data. Every failure answers False: boot must not fail
because of this check, and a hidden link is the safe side. Only ``frappe`` is imported at
module load, so a broken calendar package can never break the page that reads the flag.
"""

import frappe

REPORT_KIND = "payment_calendar_visibility"

# (frappe version, erpnext version) -> did the ERPNext source contract pass. Only a pass or
# a deterministic CalendarSourceUnavailable is kept: the first is final for the process, and
# the second is reported once, when it is stored. Any other failure may be transient, so it
# is reported (the reporter limits that to one row an hour) and tried again on the next load.
_contract_memo: dict[tuple[str, str], bool] = {}


def reset_contract_cache() -> None:
	"""Forget the remembered contract result. For tests only."""
	_contract_memo.clear()


def _report_unexpected(call_site: str, title: str) -> None:
	"""Log the exception in flight under ``payment_calendar_visibility:<call_site>``. Never raises."""
	try:
		from jarvis.holiday_touch import report

		report.report(kind=f"{REPORT_KIND}:{call_site}", title=title)
	except Exception:
		pass


def _probe_contract() -> bool:
	"""True when the contract passes, False on CalendarSourceUnavailable. Other errors propagate."""
	from jarvis.payment_calendar import sources
	from jarvis.payment_calendar.errors import CalendarSourceUnavailable

	try:
		sources.check_contract()
	except CalendarSourceUnavailable:
		return False
	return True


def _source_contract_ok() -> bool:
	"""True when the installed Frappe/ERPNext pair passes the calendar source contract."""
	try:
		import erpnext
	except ImportError:
		# Only reached when ERPNext is in installed_apps, so this is a broken install.
		_report_unexpected("contract", "Payment Calendar hidden: ERPNext is installed but cannot be imported")
		return False
	key = (frappe.__version__, erpnext.__version__)
	if key in _contract_memo:
		return _contract_memo[key]
	try:
		ok = _probe_contract()
	except Exception:
		_report_unexpected("contract", "Payment Calendar hidden: source contract check failed unexpectedly")
		return False
	_contract_memo[key] = ok
	if not ok:
		_report_unexpected(
			"contract",
			f"Payment Calendar hidden: ERPNext source contract failed (frappe {key[0]} / erpnext {key[1]})",
		)
	return ok


def _can_run_ap_report() -> bool:
	"""True when the user may run the standard Accounts Payable report."""
	from jarvis.payment_calendar.permissions import is_standard_accounts_report
	from jarvis.tools._ledger_access import _can_run_report

	return _can_run_report("Accounts Payable") and is_standard_accounts_report(
		frappe.get_cached_doc("Report", "Accounts Payable")
	)


def can_view_payment_calendar() -> bool:
	"""True on an ERPNext site where the user can run Accounts Payable and see a Company."""
	saved = list(frappe.local.message_log)
	try:
		from jarvis.chat.agent_installability import installed_apps

		if "erpnext" not in installed_apps():
			return False
		if not _source_contract_ok():
			return False
		if not _can_run_ap_report():
			return False
		try:
			return bool(frappe.get_list("Company", pluck="name", limit=1))
		except frappe.PermissionError:
			return False
	except Exception:
		_report_unexpected("check", "Payment Calendar visibility check failed")
		return False
	finally:
		frappe.local.message_log = saved
