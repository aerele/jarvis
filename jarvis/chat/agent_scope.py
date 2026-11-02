"""Generic, zero-IP scope / period resolution for agent runs (amendment A6).

Resolves an explicit
``{company, fiscal_year, from_date, to_date, prior_fy_start, prior_fy_end}``
from an installation's ``config`` JSON plus the site date, so the container
bundle NEVER infers "the current period" itself (a UTC container clock vs an IST
site picks the wrong fiscal year for ~5.5h every day — catastrophic at the
Mar-31 / Apr-1 boundary). ``_launch_audit`` stamps the result on the Run
(``scope_json``) and injects it verbatim into the run message; prior-FY
selection stays versioned bench code, never LLM prose.

It is DELIBERATELY generic: it carries no rule shape, only ordinary accounting
dimensions (company / fiscal year / period boundaries), so the delegate bundles
resolve their run scope from bench-injected, versioned values — never LLM prose.

The helper runs under the caller's identity (the run-as user in
``_launch_audit``), so ``frappe.defaults`` / erpnext's fiscal-year lookup are
resolved for that user.
"""

from calendar import monthrange
from datetime import date

import frappe

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
_PAYROLL_DEPOSIT_AGENT = "statutory-payroll-deposit-status"


def resolve_scope(installation, *, config: dict | None = None) -> dict:
	"""Resolve the explicit run scope for an installation.

	``installation`` may be a ``Jarvis Agent Installation`` doc or its name.
	Reads ``company`` / ``fiscal_year`` / ``from_date`` / ``to_date`` from the
	install's ``config`` JSON; falls back to the caller's default Company (or the
	site's single Company) and erpnext's current fiscal year for the missing
	pieces. Raises :class:`InvalidArgumentError` when no company / period can be
	determined — the caller (``_launch_audit``) treats that as a soft failure and
	degrades to an unscoped run rather than aborting the launch.
	"""
	if isinstance(installation, str):
		installation = frappe.get_doc(INSTALLATION, installation)

	cfg: dict = {}
	if config is not None:
		# Deposit-status passes a server-stamped launch snapshot here. It must
		# never re-read mutable installation configuration after launch.
		if not isinstance(config, dict):
			raise InvalidArgumentError("agent scope configuration is invalid")
		cfg = dict(config)
	elif installation.get("config"):
		try:
			parsed = frappe.parse_json(installation.config)
			if isinstance(parsed, dict):
				cfg = parsed
		except Exception:
			cfg = {}

	def _s(key):
		val = cfg.get(key)
		return str(val).strip() if val is not None and str(val).strip() else None

	is_payroll_deposit = installation.get("agent") == _PAYROLL_DEPOSIT_AGENT
	if is_payroll_deposit and _s("fiscal_year"):
		raise InvalidArgumentError(
			"statutory payroll deposit status requires explicit from_date and to_date, not fiscal_year"
		)
	if is_payroll_deposit and not (_s("from_date") and _s("to_date")):
		raise InvalidArgumentError("statutory payroll deposit status requires explicit from_date and to_date")
	scope = _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))
	prior_from, prior_to = _s("prior_period_from"), _s("prior_period_to")
	if prior_from or prior_to:
		try:
			if not prior_from or not prior_to:
				raise ValueError
			start, end = date.fromisoformat(prior_from), date.fromisoformat(prior_to)
			if start > end or end >= frappe.utils.getdate(scope["from_date"]):
				raise ValueError
		except (ValueError, TypeError):
			raise InvalidArgumentError("prior_period_from and prior_period_to must define an earlier period")
		scope.update(prior_period_from=start.isoformat(), prior_period_to=end.isoformat())
	if not is_payroll_deposit and cfg.get("record_scope"):
		record = cfg["record_scope"]
		if not isinstance(record, dict) or not all(record.get(k) for k in ("doctype", "name")):
			raise InvalidArgumentError("record_scope requires a doctype and name")
		doc = frappe.get_doc(record["doctype"], record["name"])
		doc.check_permission("read")
		if doc.get("company") and doc.company != scope["company"]:
			raise InvalidArgumentError("record_scope must belong to the selected company")
		scope["record_scope"] = {k: str(record[k]) for k in ("doctype", "name")}
	if is_payroll_deposit:
		_validate_payroll_deposit_period(scope, cfg)
		scope["record_scope"] = _payroll_deposit_record_scope(cfg, scope["company"])
	return scope


def _validate_payroll_deposit_period(scope: dict, cfg: dict) -> None:
	"""Reject an invalid liability month before any delegate run starts.

	The bundle repeats these checks as its final fail-closed boundary. Keeping the
	launcher check here prevents an API caller from bypassing the typed Configure
	panel and creating a run that can only end as not-evaluable.
	"""
	deposit = cfg.get("deposit")
	if not isinstance(deposit, dict):
		raise InvalidArgumentError("deposit configuration is required")
	try:
		start = date.fromisoformat(str(scope["from_date"]))
		end = date.fromisoformat(str(scope["to_date"]))
		report_date = date.fromisoformat(str(scope["report_date"]))
		cutoff = date.fromisoformat(str(deposit["settlement_as_of"]))
	except (KeyError, TypeError, ValueError):
		raise InvalidArgumentError(
			"statutory payroll deposit status requires valid period and booking evidence cutoff dates"
		)
	month_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
	if start.day != 1 or end != month_end:
		raise InvalidArgumentError("statutory payroll deposit status requires one full calendar month")
	if cutoff < end:
		raise InvalidArgumentError("booking evidence cutoff must be on or after the period end")
	if report_date < end:
		raise InvalidArgumentError("the selected payroll liability month has not ended")


def _payroll_deposit_record_scope(cfg: dict, company: str) -> dict:
	"""Return the one Account-group scope the deposit agent declares.

	The account group is an explicit part of the agent's nested ``deposit``
	configuration. It has no default: selecting all accounts would turn a
	declared-voucher review into an unsafe company-wide scan. Resolve it under
	the run-as user and retain only the bounded doctype/name pair for the
	delegate.
	"""
	deposit = cfg.get("deposit")
	if not isinstance(deposit, dict):
		raise InvalidArgumentError("deposit account group is required")
	account_name = deposit.get("account_group")
	if not isinstance(account_name, str) or not account_name.strip():
		raise InvalidArgumentError("deposit account group is required")
	account = frappe.get_doc("Account", account_name.strip())
	account.check_permission("read")
	if account.company != company:
		raise InvalidArgumentError("deposit account group must belong to the selected company")
	if not frappe.utils.cint(account.is_group) or frappe.utils.cint(account.disabled):
		raise InvalidArgumentError("deposit account group must be an enabled Account group")
	mapping = deposit.get("liability_accounts")
	if not isinstance(mapping, dict) or not mapping:
		raise InvalidArgumentError("a PF or ESI liability account mapping is required")
	for account_name, scheme in mapping.items():
		if not isinstance(account_name, str) or not account_name.strip() or scheme not in {"pf", "esi"}:
			raise InvalidArgumentError("each liability account mapping must name PF or ESI")
		liability_account = frappe.get_doc("Account", account_name.strip())
		liability_account.check_permission("read")
		if (
			liability_account.company != company
			or liability_account.parent_account != account.name
			or frappe.utils.cint(liability_account.is_group)
			or frappe.utils.cint(liability_account.disabled)
		):
			raise InvalidArgumentError(
				"each liability account must be an enabled direct child of the selected Account group"
			)
	return {"doctype": "Account", "name": account.name}


def _resolve(company, fiscal_year, from_date, to_date) -> dict:
	if not company:
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_default("company")
	if not company:
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if len(companies) == 1:
			company = companies[0]
	if not company:
		raise InvalidArgumentError("company is required (could not infer a single default Company)")
	if not frappe.db.exists("Company", company):
		raise InvalidArgumentError(f"unknown Company: {company}")

	if fiscal_year:
		fy = frappe.db.get_value(
			"Fiscal Year", fiscal_year, ["year_start_date", "year_end_date"], as_dict=True
		)
		if not fy:
			raise InvalidArgumentError(f"unknown Fiscal Year: {fiscal_year}")
		from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)

	if not (from_date and to_date):
		# Default to the company's CURRENT fiscal year via erpnext (site date,
		# never a container clock). erpnext is a hard dependency of every agent
		# (min_apps=["erpnext"]); if it is somehow absent the caller degrades.
		from erpnext.accounts.utils import get_fiscal_year as _gfy

		try:
			fy = _gfy(frappe.utils.today(), company=company, as_dict=True)
			from_date, to_date = str(fy.year_start_date), str(fy.year_end_date)
			fiscal_year = fy.name
		except Exception:
			raise InvalidArgumentError("from_date and to_date (or fiscal_year) are required")

	prior = _prior_fiscal_year(from_date)
	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": from_date,
		"to_date": to_date,
		"prior_fy_start": prior["py_start"] if prior else None,
		"prior_fy_end": prior["py_end"] if prior else None,
		# The run's as-of date (site date, never a container clock). to_date bounds
		# the analysis window and MAY be in the future (a user-set or fiscal-year
		# end), but a period-end finding must not assess data that has not happened
		# yet: an evaluator caps its "due" cutoff at report_date so future-dated
		# schedule rows (e.g. the rest of the year's depreciation) are never flagged.
		"report_date": frappe.utils.today(),
	}


def _prior_fiscal_year(from_date) -> dict | None:
	"""The enabled Fiscal Year with the greatest ``year_end_date`` strictly
	before ``from_date`` (the period immediately preceding the scope). Returns
	``{name, py_start, py_end}`` or ``None`` when no such FY exists. Generic
	prior-period accounting logic."""
	rows = frappe.db.sql(
		"""select name, year_start_date, year_end_date from `tabFiscal Year`
           where year_end_date < %(from_date)s and disabled = 0
           order by year_end_date desc limit 1""",
		{"from_date": from_date},
		as_dict=True,
	)
	if not rows:
		return None
	fy = rows[0]
	return {"name": fy.name, "py_start": str(fy.year_start_date), "py_end": str(fy.year_end_date)}
