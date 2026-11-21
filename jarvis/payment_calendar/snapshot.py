"""Worker-only coherent primary-database reads, with scoped cache bypasses."""

from contextlib import ExitStack, contextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from time import monotonic

import frappe

from jarvis.payment_calendar import read_log, routing
from jarvis.payment_calendar import sources as obligation_sources
from jarvis.payment_calendar.access import authorize_scope
from jarvis.payment_calendar.banks import read_bank_balances
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable
from jarvis.payment_calendar.projection import horizon_summary, project_snapshot
from jarvis.payment_calendar.sources import read_obligations
from jarvis.payment_calendar.store import RESULT_SCHEMA

# `store.RESULT_SCHEMA` is stamped on every result; `store.split_result` refuses a result without it at
# publish, and no reader accepts another shape. `horizon` is always stored, null only without `with_horizon`.
HORIZON_DAYS = 30


@contextmanager
def _replace(target, name, value):
	owned = name in vars(target)
	original = getattr(target, name)
	setattr(target, name, value)
	try:
		yield
	finally:
		if owned:
			setattr(target, name, original)
		else:
			delattr(target, name)


@contextmanager
def _source_caches():
	"""Scoped misses reuse upstream cache-miss paths, without stale shared fills.

	Called only from the sequential RQ source reader. The client-cache invalidator
	thread does not call these primitives. No web request uses this context.
	"""
	profile = source_profile()
	cache = frappe.cache
	hits, policies = set(), {}

	def document(key):
		return isinstance(key, str) and (
			key.startswith("document_cache::Company::")
			or key
			in (
				"document_cache::Accounts Settings::Accounts Settings",
				"document_cache::System Settings::System Settings",
			)
			or key == "defaults::__default"
		)

	def wrap(target, name, match):
		original = getattr(target, name)

		def wrapped(key, *args, **kwargs):
			if match(key):
				hits.add((name, key))
				return None
			return original(key, *args, **kwargs)

		return wrapped

	original_hget, original_hset = cache.hget, cache.hset

	def hget(name, key, generator=None, shared=False):
		if profile.major == "15" and name == "defaults" and key == "__default":
			hits.add(profile.defaults_cache_hit)
			return None
		if name == "fiscal_years":
			hits.add(("hget", name))
			return None
		if name in ("roles", "user_permissions"):
			identity = (name, key)
			if identity not in policies and generator:
				policies[identity] = generator()
			return policies.get(identity)
		return original_hget(name, key, generator=generator, shared=shared)

	def hset(name, key, value, *args, **kwargs):
		if profile.major == "15" and name == "defaults" and key == "__default":
			return None
		if name == "fiscal_years":
			return None
		if name in ("roles", "user_permissions"):
			policies[(name, key)] = value
			return None
		return original_hset(name, key, value, *args, **kwargs)

	replacements = [
		(cache, "get_value", wrap(cache, "get_value", document)),
		(cache, "set_value", wrap(cache, "set_value", document)),
		(cache, "hget", hget),
		(cache, "hset", hset),
	]
	if profile.major == "16":
		client = frappe.client_cache
		for name in ("get_value", "set_value"):
			replacements.append((client, name, wrap(client, name, lambda key: key == "defaults::__default")))
	with ExitStack() as stack:
		for target, name, replacement in replacements:
			if name in vars(target):
				raise CalendarSourceUnavailable("source_contract_unavailable")
			stack.enter_context(_replace(target, name, replacement))
		yield hits


def fiscal_years_read(hits) -> bool:
	"""True when the source view read fiscal years itself instead of taking them from the shared cache.

	Balance reads in the view must pass this check: a stale cached year would answer for the wrong books.
	"""
	return ("hget", "fiscal_years") in hits


@contextmanager
def consistent_read():
	"""Pin a real repeatable read, refusing helper writes or transaction controls."""
	db = frappe.db
	db.rollback()
	# Frappe rollback() immediately begins another transaction. End that empty
	# one before setting characteristics for this source view.
	db.sql("ROLLBACK")
	db.sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	db.sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
	connection = db._conn
	original_sql = db.sql
	broken = False

	def refuse(*args, **kwargs):
		nonlocal broken
		broken = True
		raise CalendarSourceUnavailable("source_changed")

	def read_sql(query, *args, **kwargs):
		if db._conn is not connection or kwargs.get("auto_commit"):
			refuse()
		if "for update" in str(query).lower() or "lock in share mode" in str(query).lower():
			refuse()
		# All canonical source SQL is SELECT (including nested/CTE queries).
		if str(query).lstrip().split(None, 1)[0].lower() not in ("select", "with", "show", "describe"):
			refuse()
		return original_sql(query, *args, **kwargs)

	try:
		with ExitStack() as stack:
			for name, replacement in (
				("sql", read_sql),
				("commit", refuse),
				("rollback", refuse),
				("begin", refuse),
			):
				stack.enter_context(_replace(db, name, replacement))
			hits = stack.enter_context(_source_caches())
			yield hits
			if broken or db._conn is not connection or not original_sql("SELECT @@in_transaction")[0][0]:
				refuse()
	finally:
		db.rollback()


def build_result(
	scope,
	sources,
	bank,
	bank_code,
	unavailable,
	*,
	currency,
	floor,
	precision,
	generated_at,
	with_horizon=True,
):
	"""The stored result from fixed sources; the worker and the golden dataset both call this.

	`with_horizon=False` stores a null horizon (Task 0 fallback: compute it for the current month only).
	"""
	projection = project_snapshot(
		sources["Payable"],
		sources["Receivable"],
		bank["total"] if bank else None,
		floor["safety_floor"],
		scope["anchor"],
		scope["window_start"],
		scope["window_end"],
		precision=precision,
		month=scope.get("month"),
	)
	return {
		"schema": RESULT_SCHEMA,
		"sources": sources,
		"projection": projection,
		"horizon": _horizon(scope, sources, bank, floor, precision) if with_horizon else None,
		"currency": currency,
		"floor": floor,
		"bank": bank,
		"bank_code": bank_code,
		"unavailable": unavailable,
		"generated_at": generated_at,
	}


def _horizon(scope, sources, bank, floor, precision):
	"""A second projection over the next 30 days, whatever month the snapshot shows."""
	anchor = scope["anchor"]
	end = (Date.fromisoformat(anchor) + timedelta(days=HORIZON_DAYS - 1)).isoformat()
	projected = project_snapshot(
		sources["Payable"],
		sources["Receivable"],
		bank["total"] if bank else None,
		floor["safety_floor"],
		anchor,
		anchor,
		end,
		precision=precision,
	)
	return horizon_summary(projected)


def collect(scope: dict, fingerprint: str) -> dict:
	"""All financial sources share the fixed view; fresh policy also fences publish.

	The routed sides leave records (`routing.read_side`); they are logged in a `finally` after the view
	has ended, so a job that then fails still reports, and no log row is written inside the view.
	"""
	started, outcomes = monotonic(), []
	try:
		return _collect(scope, fingerprint, started, outcomes)
	finally:
		_write_outcomes(outcomes, started)


def _write_outcomes(outcomes: list[dict], started: float) -> None:
	"""Stamp the records with the view length and the reader build, then log them. Never raises."""
	try:
		view_ms = elapsed_ms(started)
		build = _reader_build() if any(record.get("routed") for record in outcomes) else None
		for record in outcomes:
			record.update(view_ms=view_ms, reader_build=build)
		read_log.reader_outcomes(outcomes, routing._today())
	except TIMEOUTS:
		raise
	except Exception:
		frappe.logger("payment_calendar").warning("reader_outcome_unavailable")


def _reader_build() -> str | None:
	"""The build id of the reader code, or None: a failed lookup must not drop the records."""
	try:
		return routing.reader_build()[:12]
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _collect(scope: dict, fingerprint: str, started: float, outcomes: list[dict]) -> dict:
	profile = source_profile()
	with consistent_read() as hits:
		cutoff = datetime.now(timezone.utc).isoformat()
		proof = authorize_scope(scope, expected_fingerprint=fingerprint)
		bank, bank_code = None, None
		if scope["accounts"]:
			try:
				bank = read_bank_balances(scope["company"], scope["accounts"], scope["anchor"])
			except CalendarSourceUnavailable as exc:
				bank_code = exc.code
		sources, unavailable = {}, {}
		for side in ("Payable", "Receivable"):
			sources[side] = None
			if not proof["reports"][side]:
				unavailable[side] = "forbidden"
				continue
			try:
				# Today's read decides; the router tries the reader only after a `too_large` refusal.
				sources[side] = routing.read_side(
					read_obligations,
					scope["company"],
					side,
					scope["anchor"],
					scope["window_start"],
					scope["window_end"],
					started=started,
					outcomes=outcomes,
				)
				if proof["scope_reasons"]:
					sources[side]["complete_for_company"] = False
					sources[side]["scope_reasons"] = sorted(
						set(sources[side]["scope_reasons"] + proof["scope_reasons"])
					)
			except CalendarSourceUnavailable as exc:
				unavailable[side] = exc.code
		# Before collect's own precision read: that read would itself add the cache hit.
		require_defaults_cache_hit(hits, sources, profile)
		from erpnext.accounts.utils import get_currency_precision

		precision = get_currency_precision() or 2
		check_source_contract(hits, sources, bank, proof["currency"], precision, profile)
		projecting = monotonic()
		result = build_result(
			scope,
			sources,
			bank,
			bank_code,
			unavailable,
			currency=proof["currency"],
			floor=proof["floor"],
			precision=precision,
			generated_at=cutoff,
		)
		projections_ms = elapsed_ms(projecting)
	# Ends the source view before looking for revocations/settings changes.
	authorize_scope(scope, expected_fingerprint=fingerprint, result=result)
	_log_collected(started, projections_ms, sources)
	return result


def require_defaults_cache_hit(hits, sources, profile):
	"""A source read that never met the defaults cache did not run inside the fixed view."""
	if any(sources.values()) and profile.defaults_cache_hit not in hits:
		raise CalendarSourceUnavailable("source_contract_unavailable")


def check_source_contract(hits, sources, bank, currency, precision, profile):
	"""Every source was read in the fixed view and agrees on currency and precision."""
	if any(
		source and (source["currency"] != currency or source["precision"] != precision)
		for source in sources.values()
	):
		raise CalendarSourceUnavailable("currency_reconciliation_unavailable")
	if bank and (not fiscal_years_read(hits) or bank["precision"] != precision):
		raise CalendarSourceUnavailable("source_contract_unavailable")
	if any(sources.values()) and (
		profile.defaults_cache_hit not in hits
		or not any(
			key.startswith("document_cache::Company::") for method, key in hits if method == "get_value"
		)
	):
		raise CalendarSourceUnavailable("source_contract_unavailable")


def elapsed_ms(started):
	return int((monotonic() - started) * 1000)


def _log_collected(started, projections_ms, sources):
	"""Timing, row counts and the speed-path flag only: no company, party or voucher text (B11)."""
	counts = {side: len(source["rows"]) if source else 0 for side, source in sources.items()}
	frappe.logger("payment_calendar").info(
		f"collect complete duration_ms={elapsed_ms(started)} projections_ms={projections_ms} "
		f"payable_rows={counts['Payable']} receivable_rows={counts['Receivable']} "
		f"fast_path={obligation_sources.FAST_PATH}"
	)
