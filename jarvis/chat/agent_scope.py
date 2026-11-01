"""Generic, zero-IP scope / period resolution for agent runs (amendment A6).

Resolves an explicit
``{company, fiscal_year, from_date, to_date, prior_fy_start, prior_fy_end}``
from an installation's ``config`` JSON plus the site date, so the container
bundle NEVER infers "the current period" itself (a UTC container clock vs an IST
site picks the wrong fiscal year for ~5.5h every day — catastrophic at the
Mar-31 / Apr-1 boundary). ``_launch_audit`` stamps the result on the Run
(``scope_json``) and injects it verbatim into the run message; prior-FY
selection stays versioned bench code, never LLM prose.

It is DELIBERATELY generic for normal delegates: it carries only ordinary
accounting dimensions (company / fiscal year / period boundaries). A narrow
delegate can add an explicit record root only through a server-side validation
hook, so its bundle receives a bench-injected boundary — never owner JSON or
LLM prose.

The helper runs under the caller's identity (the run-as user in
``_launch_audit``), so ``frappe.defaults`` / erpnext's fiscal-year lookup are
resolved for that user.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
ATTENDANCE_AGENT = "attendance-leave-integrity-auditor"
_ATTENDANCE_OUTPUT_SCOPE_KEYS = ("company", "from_date", "to_date", "report_date")


def _agent_slug(installation) -> str:
	return str(installation.get("agent") or installation.get("agent_slug") or "").strip()


def requires_bound_scope(installation) -> bool:
	"""True when a missing or bad root must refuse a launch.

	Most delegates can safely use the ordinary company/date scope. Attendance is
	different: its native evaluator requires one exact Department, and omitting a
	configured root would otherwise turn a narrow review into an unbounded one.
	"""
	return _agent_slug(installation) == ATTENDANCE_AGENT


def attendance_record_scope(value) -> dict | None:
	"""Return the canonical Attendance Department root, or ``None``.

	This is intentionally a shape check only. Launch validation also confirms the
	Department's company, leaf state, enabled state, and run-as-user read access.
	The shared shape check lets writeback code reject a model-created or widened
	root without copying any raw owner configuration into a Run snapshot.
	"""
	if (
		not isinstance(value, dict)
		or set(value) != {"doctype", "name"}
		or value.get("doctype") != "Department"
		or not isinstance(value.get("name"), str)
		or not value["name"].strip()
		or value["name"] != value["name"].strip()
	):
		return None
	return {"doctype": "Department", "name": value["name"]}


def attendance_output_scope(value) -> dict | None:
	"""Return the five safe Attendance output keys, or ``None``.

	The evaluator may return this small projection at writeback. It cannot return
	raw installation configuration or widen the Department root through this
	helper. The server compares this projection with the launch-stamped scope.
	"""
	if not isinstance(value, dict):
		return None
	boundary = attendance_record_scope(value.get("record_scope"))
	if boundary is None:
		return None
	output = {}
	for key in _ATTENDANCE_OUTPUT_SCOPE_KEYS:
		item = value.get(key)
		if not isinstance(item, str) or not item.strip() or item != item.strip():
			return None
		output[key] = item
	output["record_scope"] = boundary
	return output


def _attendance_department_scope(config: dict, company: str) -> dict:
	"""Validate the Attendance Department root as the active run-as user.

	Only this two-field, server-checked object may enter the run scope. The
	caller runs ``resolve_scope`` after it has impersonated ``run_as_user``, so
	``has_permission`` applies that identity's DocPerm and User Permission rules.
	"""
	declared = attendance_record_scope(config.get("record_scope"))
	if declared is None:
		raise InvalidArgumentError("Attendance record_scope must name one Department leaf")

	name = declared["name"]
	try:
		department = frappe.db.get_value(
			"Department", name, ["company", "is_group", "disabled"], as_dict=True
		)
		department_readable = frappe.has_permission("Department", "read", doc=name)
	except Exception as exc:
		raise InvalidArgumentError("Attendance Department scope could not be verified") from exc
	if not department or not department_readable:
		raise InvalidArgumentError(
			"Attendance record_scope must be a readable leaf Department in the company"
		)
	try:
		company_readable = frappe.has_permission("Company", "read", doc=company)
	except Exception as exc:
		raise InvalidArgumentError("Attendance Company scope could not be verified") from exc
	if (
		not company_readable
		or department.get("company") != company
		or department.get("is_group") is None
		or frappe.utils.cint(department.get("is_group"))
		or department.get("disabled") is None
		or frappe.utils.cint(department.get("disabled"))
	):
		raise InvalidArgumentError(
			"Attendance record_scope must be a readable leaf Department in the company"
		)
	return {"doctype": "Department", "name": name}


def resolve_scope(installation) -> dict:
	"""Resolve the explicit run scope for an installation.

	``installation`` may be a ``Jarvis Agent Installation`` doc or its name.
	Reads ``company`` / ``fiscal_year`` / ``from_date`` / ``to_date`` from the
	install's ``config`` JSON; falls back to the caller's default Company (or the
	site's single Company) and erpnext's current fiscal year for the missing
	pieces. Attendance also requires a server-validated Department root. A
	missing, malformed, foreign, group, or unreadable root raises instead of
	falling back to a broader review.
	"""
	if isinstance(installation, str):
		installation = frappe.get_doc(INSTALLATION, installation)

	cfg: dict = {}
	if installation.get("config"):
		try:
			parsed = frappe.parse_json(installation.config)
			if isinstance(parsed, dict):
				cfg = parsed
		except Exception:
			cfg = {}

	def _s(key):
		val = cfg.get(key)
		return str(val).strip() if val is not None and str(val).strip() else None

	scope = _resolve(_s("company"), _s("fiscal_year"), _s("from_date"), _s("to_date"))
	if requires_bound_scope(installation):
		scope["record_scope"] = _attendance_department_scope(cfg, scope["company"])
	return scope


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
