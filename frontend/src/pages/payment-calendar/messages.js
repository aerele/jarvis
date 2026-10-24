export const riskLabel = (risk) =>
	({ red: "Below zero", orange: "Below minimum cash", green: "At or above minimum" }[risk] ||
	"Forecast unavailable");

const messages = {
	restricted_obligations:
		"Your party or company restrictions cover only part of the outstanding balances. Whole-company cash colours are unavailable.",
	restricted_document_details:
		"Some accounting document details are restricted. The calendar cannot forecast whole-company cash.",
	owner_only_obligations:
		"Only your own accounting records are visible. The calendar cannot forecast whole-company cash.",
	custom_accounting_scope:
		"Custom accounting permissions limit the available data. Whole-company cash forecasts are unavailable.",
	fiscal_year_unavailable:
		"No fiscal year covers the current site date for this company. Ask your accounts administrator to check fiscal-year coverage.",
	multibook_unsupported:
		"This company uses a finance-book scope that the calendar cannot combine. Use the ERPNext reports to review it.",
	not_found_or_expired:
		"This snapshot expired or was invalidated. Refresh to see current balances.",
	invalid_request:
		"The inputs could not be accepted. Check the selected values and company currency precision, then try again.",
	worker_unavailable: "A calendar worker is unavailable. Refresh to try again.",
	expired: "This snapshot expired. Refresh to see current balances.",
	forbidden:
		"You no longer have access to this financial view. Ask your accounts administrator for access.",
	scope_changed: "Access or company settings changed. Refresh to rebuild the calendar.",
	settings_changed:
		"The minimum was changed by another user. Review the current value before saving again.",
	configuration_conflict:
		"The minimum was changed by another user. Review the current value before saving again.",
	network_error: "The server could not be reached. Check your connection and try again.",
	timeout: "The calendar took too long to prepare. Refresh to try again.",
	job_lost: "The calendar job stopped before it finished. Refresh to try again.",
	worker_busy: "Your calendar jobs are still running. Please wait, then refresh.",
	floor_unconfigured: "A company minimum cash balance has not been configured.",
	bank_unavailable:
		"Selected bank balances are unavailable. Check account selection and finance access.",
	payables_unavailable: "Supplier dues are unavailable for this scope.",
	receivables_unavailable: "Customer collections are unavailable for this scope.",
	incomplete_obligations: "Your permitted records do not cover the whole company.",
	party_restriction:
		"Your party restrictions limit this view. Whole-company cash forecasts are unavailable.",
	invalid_floor: "Enter a non-negative minimum in the company currency.",
};
export const explain = (code) =>
	messages[code] ||
	"Complete financial data is unavailable for this scope. Check finance access and company configuration, then refresh.";
