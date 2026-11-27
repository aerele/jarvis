"""The names of the reader and its rules that the cache uses, in one place.

The cache builds a generation by running the reader's own engine and reads some of its private names
(`sources._assemble`, `builtin_reader._builtin_class`, ...). A rename inside those modules, which change often,
would otherwise show as an `AttributeError` in a job or at night. `tests/test_payment_calendar_cache_contract.py`
fails first: it checks that every name below exists, and that the cache modules use no name that is not listed.
"""

# module name (a module of this package) -> the names the cache modules read or set on it
NAMES = {
	"builtin_reader": (
		"GENERIC_LINE_CAP",
		"RANK_PARTY_CAP",
		"READER_MAX_ROWS",
		"TOTAL_LINE_CAP",
		"_builtin_class",
		"_ledger_predicate",
		"ledger_control",
		"read_obligations",
	),
	"compatibility": ("bank_balance_arguments", "reader_pin_drift", "source_profile"),
	"eligibility": ("AUDITED_HOOKS", "GUARDED_DOCTYPES", "_AUDITED_SOURCE_NAMES", "reader_ineligibility"),
	"permissions": ("incomplete_scope_reasons", "require_company"),
	"reconciliation": ("InvoiceReconciler",),
	"snapshot": ("check_source_contract", "consistent_read", "require_defaults_cache_hit"),
	"sources": (
		"_assemble",
		"_engine_filters",
		"_key",
		"_ledger_rows_exceed",
		"_permission_row",
		"_report_class",
		"_require_inputs",
		"_run_engine",
		"money",
		"read_obligations",
	),
}
# The methods that the cache's engine subclass overrides on the reader's engine class.
ENGINE_METHODS = ("_discover",)
