"""The bench half of the onboarding / payment-recovery contract with admin.

Admin publishes a machine vocabulary (``jarvis_admin_v2/billing/codes.py``) and
a corpus of wire fixtures; this module is what the bench reads it WITH. Three
jobs, all of them consequences of one incident:

  1. **Codes, never prose.** Admin reworded its duplicate-signup message on
     2026-07-26. The bench's failed-payment resume string-matched the old
     sentence, so from that day every customer whose card was declined hit a
     dead end - and both repositories' suites stayed green, because each side
     kept its own copy of the sentence. Nothing here branches on a message.
  2. **Identity is proven by CREDENTIALS, never by an email comparison.** The
     resume gate also compared the user's typed address against the stored
     ``jarvis_admin_customer_email``, which holds admin's SYNTHETIC OAuth login
     (``cust-<hash>@jarvis.invalid``, signup.py ``_synthetic_login``) - never
     equal to a real address, so the gate was structurally dead even with the
     wording fixed. Possession of the credentials the first signup minted IS
     the ownership proof; admin resolves the customer from authentication.
  3. **Local display context.** A reloaded wizard used to render the SITE
     admin's email as the address a verification link was sent to. The bench
     keeps a small non-secret snapshot of whose signup this is, and server
     truth from admin overwrites it whenever a response carries any.

The vendored fixture corpus in ``jarvis/tests/fixtures/admin_contract/`` is the
executable half of this file's claims - see its PROVENANCE.md.
"""

import hashlib
import hmac
import json
import secrets
from urllib.parse import urlsplit

import frappe

# The contract vocabulary version this bench is written against
# (``codes.CONTRACT_VERSION`` on the admin side). Bumped only on a BREAKING
# shape change; a new CODE is additive and does not move it. Recorded in the
# local context so a support conversation can tell which vocabulary a stuck
# wizard was answered in.
CONTRACT_VERSION = 2

# --------------------------------------------------------------------------- #
# admin's codes (append-only vocabulary; a shipped code's meaning never changes)
# --------------------------------------------------------------------------- #
#: Guest signup refused: this (email, company) already has an account. Says
#: NOTHING about that account's status - deliberately, so the endpoint cannot be
#: used to enumerate. This is the code the failed-payment resume keys off.
ACCOUNT_ALREADY_EXISTS = "ACCOUNT_ALREADY_EXISTS"
#: The signup exists but the address is unproven; nothing happens until the
#: magic link is clicked.
SIGNUP_VERIFICATION_REQUIRED = "SIGNUP_VERIFICATION_REQUIRED"
#: An intent exists and the gateway has not said money moved. Poll; do not
#: re-initiate.
PAYMENT_CONFIRMATION_PENDING = "PAYMENT_CONFIRMATION_PENDING"
#: The gateway says this attempt will not complete. A NEW intent is the recovery.
PAYMENT_DECLINED = "PAYMENT_DECLINED"
#: Already paid. Terminal for the payment step - continue setup, never a second
#: intent.
PAYMENT_ALREADY_ACTIVE = "PAYMENT_ALREADY_ACTIVE"
#: Awaiting payment with no usable checkout handle. Only an explicit initiate
#: may create one.
NO_CURRENT_INTENT = "NO_CURRENT_INTENT"
#: Paid, live and already running: the recovery is the emailed reconnect code,
#: never a new signup and never another payment.
ACCOUNT_RECONNECT_REQUIRED = "ACCOUNT_RECONNECT_REQUIRED"
#: A mandate the gateway has already AUTHORIZED. Money moved; the recovery is
#: confirm, and a second intent would authorize a second mandate.
PAYMENT_AUTHORIZED_PENDING_CONFIRM = "PAYMENT_AUTHORIZED_PENDING_CONFIRM"
#: A handle exists but the gateway would not hand back the session token it has
#: to be opened with. Retryable, and NOT a decline.
INTENT_HANDLE_UNAVAILABLE = "INTENT_HANDLE_UNAVAILABLE"
#: Past the signup stage: renewal, reconnect or support, not a checkout.
SIGNUP_TERMINAL = "SIGNUP_TERMINAL"
#: A request field violated a documented bound (today: an over-long
#: idempotency_key).
INVALID_REQUEST = "INVALID_REQUEST"
#: Too many provider-truth checks for this account. Asserts NOTHING about the
#: payment - wait ``retry_after_seconds`` and ask again.
PAYMENT_CHECK_RATE_LIMITED = "PAYMENT_CHECK_RATE_LIMITED"

# --------------------------------------------------------------------------- #
# bench-local codes
# --------------------------------------------------------------------------- #
# Failures that never reached admin's contract at all: transport, authentication,
# an admin too old to send a code. Prefixed so they can never be confused with -
# or collide with a future addition to - admin's append-only vocabulary.
BENCH_ADMIN_UNREACHABLE = "BENCH_ADMIN_UNREACHABLE"
BENCH_ADMIN_AUTH_FAILED = "BENCH_ADMIN_AUTH_FAILED"
BENCH_ADMIN_REJECTED = "BENCH_ADMIN_REJECTED"
BENCH_RATE_LIMITED = "BENCH_RATE_LIMITED"
#: No signup is known on this site: no admin credentials at all (a bench on day
#: one, or one whose settings were reset), or nothing local to name a plan with
#: and the caller named none.
#:
#: Deliberately ONE code for both rather than a separate BENCH_NOT_STARTED. The
#: caller's decision is identical - show the signup form - and a wizard with two
#: codes for one decision grows a branch that can only ever disagree with itself.
BENCH_NO_SIGNUP_CONTEXT = "BENCH_NO_SIGNUP_CONTEXT"
#: A provider-truth check found money the control plane could not credit to this
#: exact attempt, and an operator is placing it. Refused HERE, before the network:
#: the customer has already paid, and the one thing that must not happen next is
#: a second intent. Bench-local because the refusal is this facade's - admin's
#: own durable guard is a separate, ledgered piece of work.
BENCH_AWAITING_RECONCILIATION = "BENCH_AWAITING_RECONCILIATION"
#: Admin refused the customer's OWN submitted details (a malformed GSTIN, an
#: unusable contact number) BEFORE it built any provider object. Its own code
#: because the generic BENCH_ADMIN_REJECTED it used to collapse into renders as
#: "the payment service refused this request", which is false in every particular:
#: no payment service was involved, nothing was created, nothing can be checked,
#: and retrying the identical data fails identically forever. The customer was left
#: at an unrecoverable dead end, three screens past the field that caused it, with
#: the exact server sentence ("billing.gstin is not a valid GSTIN") thrown away.
#: The recovery for this one is to go back and fix a field, and nothing else.
BENCH_SIGNUP_DETAILS_REJECTED = "BENCH_SIGNUP_DETAILS_REJECTED"

#: Admin exception classes that mean "what the customer typed is unusable", as
#: opposed to "the request was refused for a reason the customer cannot see". These
#: are matched on the wire ``exc_type`` string, so a class admin renames must be
#: added here too - which is the same append-only discipline every other code in
#: this module follows.
_DETAILS_REJECTION_EXC_TYPES = frozenset({"BillingMetadataRejected"})

#: Field prefixes admin uses when it names the offending input. A message starting
#: with one of these is, by admin's own convention, about a value the customer
#: entered, so it is safe to route to the details-rejection copy even from an admin
#: build whose exception class this bench has never heard of.
_DETAILS_REJECTION_PREFIXES = ("billing.",)


def is_details_rejection(message: str, exc_type: str = "") -> bool:
	"""Did admin refuse the customer's own submitted details?

	Checked on the exception CLASS first (structural, admin's own vocabulary) and
	only then on the message prefix, which is a documented convention rather than a
	guess: ``_normalize_billing`` names the offending key and never the value. This
	is not the prose-matching the contract exists to remove - it is a namespaced
	field prefix, and getting it wrong degrades to the previous generic copy rather
	than to a wrong verdict about money."""
	if exc_type and exc_type in _DETAILS_REJECTION_EXC_TYPES:
		return True
	msg = (message or "").strip().lower()
	return msg.startswith(_DETAILS_REJECTION_PREFIXES)


RECOVERY_RETRY = "retry"
RECOVERY_CONTINUE_SETUP = "continue_setup"
RECOVERY_CONTACT_SUPPORT = "contact_support"
RECOVERY_AUTHENTICATE_OR_RECONNECT = "authenticate_or_reconnect"
#: Bench-local hint, not one of admin's five: "ask the gateway again, and I will
#: know more". Advisory like every recovery hint, so an admin that later adopts
#: the name loses nothing and a consumer that does not know it ignores it.
RECOVERY_CHECK_STATUS = "check_status"

#: Admin's cap on an idempotency key (``billing/signup.py _MAX_IDEMPOTENCY_KEY_LEN``).
#: Mirrored rather than discovered: a key admin refuses must never be PERSISTED
#: here, because the stored key is what the next attempt reuses - see
#: next_idempotency_key.
MAX_IDEMPOTENCY_KEY_LEN = 128

# The codes whose recovery IS a new intent: whatever the stored idempotency key
# bought can no longer be paid. Reusing the key there would be actively wrong -
# admin returns the intent a key it has already seen created, so a customer
# retrying after a decline would be handed the dead order back and could never
# escape it. Everything else reuses the key, which is what makes a double-click
# (or a retried POST, or a refreshed pay screen) converge on ONE gateway object.
#
# INVALID_REQUEST is in the set for the opposite reason and it is load-bearing: a
# key admin REFUSED bought nothing at all, so a bench that kept reusing it would
# replay the same refusal forever with no way out but a settings reset. Validation
# below is what stops such a key being stored; this is what frees a bench that
# stored one anyway (an older build, or a bound admin tightens later).
_SPENT_INTENT_CODES = frozenset(
	{
		PAYMENT_DECLINED,
		NO_CURRENT_INTENT,
		INTENT_HANDLE_UNAVAILABLE,
		INVALID_REQUEST,
	}
)

# The codes a FAILED call may write into the local context. Almost all of them
# are admin's payment-state vocabulary - what this signup's money is actually
# doing - because the context drives both what the page renders and whether the
# next initiation reuses its key, and a transport failure or a rate-limit backoff
# says nothing about either. Absorbing "you are asking too often" as the
# payment's state is how a backoff would come to mint a fresh intent.
#
# INVALID_REQUEST is the one member that is NOT a payment state, and it is here
# on purpose: it is the verdict on the KEY. Without it the self-heal below is
# unreachable - a stored key admin refuses can never be recorded as refused, so
# _SPENT_INTENT_CODES never sees it and every later attempt replays the same
# refusal. Its presence is what makes that brick clear itself on the next click.
_PAYMENT_STATE_CODES = frozenset(
	{
		SIGNUP_VERIFICATION_REQUIRED,
		PAYMENT_CONFIRMATION_PENDING,
		PAYMENT_DECLINED,
		PAYMENT_ALREADY_ACTIVE,
		NO_CURRENT_INTENT,
		ACCOUNT_RECONNECT_REQUIRED,
		PAYMENT_AUTHORIZED_PENDING_CONFIRM,
		INTENT_HANDLE_UNAVAILABLE,
		SIGNUP_TERMINAL,
		INVALID_REQUEST,
	}
)


class SignupConflictError(frappe.ValidationError):
	"""A bench-side refusal that reaches the wire as 409, not 417.

	``handle_exception`` reads the status off the exception CLASS
	(``getattr(e, "http_status_code", 500)``), so hand-setting
	``frappe.local.response.http_status_code`` before a throw is overwritten by
	``report_error`` - the status is a property of the type and nothing else.
	Plain ``frappe.ValidationError`` is 417, which no HTTP client reads as "this
	conflicts with the state you are already in". Mirrors admin's own
	``codes.InvalidRequestError``, from the other end of the same contract."""

	http_status_code = 409


def is_duplicate_signup(err) -> bool:
	"""Is this admin rejection "that (email, company) already has an account"?

	Two machine signals, both stable, NEITHER of them prose:

	- ``error.code == ACCOUNT_ALREADY_EXISTS`` - the contract's own answer;
	- ``exc_type == "DuplicateEntryError"`` - what an admin older than the
	  contract sends, and still sends. Every version of admin's
	  ``_reject_duplicate_email`` back to the v2 fork throws
	  ``frappe.DuplicateEntryError``, whose ``http_status_code`` puts the 409 on
	  the wire, so this branch covers a mid-upgrade fleet with no prose fallback
	  needed anywhere.

	The substring match this replaces (``"already registered or pending" in
	str(err)``) is what made the failed-payment resume unreachable for every
	real customer the day admin improved its wording."""
	if getattr(err, "code", "") == ACCOUNT_ALREADY_EXISTS:
		return True
	return getattr(err, "exc_type", None) == "DuplicateEntryError"


# --------------------------------------------------------------------------- #
# local display context
# --------------------------------------------------------------------------- #
#: Jarvis Settings field holding the JSON snapshot. Non-secret by construction -
#: see _ADMIN_KEYS, an allowlist rather than a copy of admin's payload, so a
#: response carrying api_key / api_secret / customer_password can never leak
#: into it.
CONTEXT_FIELD = "signup_context"

# What the bench keeps out of an admin envelope. Display facts and machine codes
# only: identity as the customer typed it, what they are buying, where the
# attempt got to.
_ADMIN_KEYS = (
	# opaque per-attempt handle - the one identifier support and the bench may
	# both quote (contract rule 3 keeps document names on admin's side)
	"attempt_id",
	"generation",
	"contract_version",
	# server-side identity truth; overwrites whatever the wizard prefilled
	"email",
	"company",
	# what is being bought, and what is actually due today
	"payment_provider",
	"amount_inr",
	"signup_fee_inr",
	"due_today_inr",
	"trial_days",
	"effective_trial_days",
	"effective_first_charge_at",
	# where the attempt got to
	"code",
	"recovery",
	"subscription_status",
	"pending_verification",
	"verification_expires_at",
	"payment_last_checked_at",
	# Money the gateway holds that could not be credited to this attempt. Kept
	# because it OUTLIVES the response that reported it: an operator is placing
	# that payment, and until a later check says otherwise this bench must refuse
	# to open a second intent. See awaiting_reconciliation().
	"awaiting_manual_reconciliation",
)

# Never persisted, and asserted on: the signup response carries the account's
# credentials in the same dict as its display fields.
_NEVER_PERSIST = ("api_key", "api_secret", "customer", "customer_password", "agent_token")

# Held locally, never sent to the browser. The key is the bench's receipt for an
# initiation; handing it to the SPA would invite it to echo the same key back on
# a genuine second attempt, which is exactly the replay that returns the dead
# intent.
_LOCAL_ONLY_KEYS = ("idempotency_key",)

# Fields whose value changes on every single poll and means nothing on its own.
# They are excluded from change DETECTION only - they still ride along in a write
# that something real triggered. Without this a wizard polling every 2 seconds
# rewrites the Single forever, because the timestamp is different every time.
_VOLATILE_KEYS = ("payment_last_checked_at", "gateway_consulted", "updated_at")

# Credential-shaped keys that must never reach the browser, wherever in an admin
# envelope they appear. The bench persists them (write_connection) and then hands
# the SAME dict onward, so "we only return what admin sent" was, on the verified
# poll, a plaintext password in an HTTP response body the page had no use for.
# Stripping is by KEY, on the way out, so a future admin field named like a
# credential is caught without a bench release.
_WIRE_STRIP = ("api_key", "api_secret", "customer_password", "agent_token")


def strip_credentials(data: dict | None) -> dict:
	"""A copy of ``data`` with every credential-shaped key removed.

	The persist path reads the ORIGINAL before this runs; only the wire copy is
	stripped. ``customer`` is deliberately NOT here - it is the synthetic OAuth
	login, useless without the password, and the legacy verify flow's JS reads it
	off the signup response. It has no business being RENDERED, which is what the
	context block's real email is for."""
	if not isinstance(data, dict):
		return {}
	return {k: v for k, v in data.items() if k not in _WIRE_STRIP}


# --------------------------------------------------------------------------- #
# plan-09 WS7: the admin-hosted pay page. The bench builds the checkout URL from
# its OWN configured origin and never navigates to a URL admin supplied; admin's
# response instead carries a NON-NAVIGABLE origin digest (§R P0-3) the bench
# cross-checks against its own configured origin before the frontend navigates.
# --------------------------------------------------------------------------- #
#: The frozen envelope key admin uses for the pay-page token (brief §Frozen
#: contract). Its presence is what turns a response into a navigate-to-pay one.
PAY_PAGE_TOKEN_KEY = "pay_page_token"
#: Admin's non-navigable attestation of ITS canonical origin: the sha256 hex of
#: ``https://<host>``. The bench recomputes the digest of its OWN configured
#: origin and compares — agreement proves both sides mean the same origin without
#: admin handing the bench anything navigable.
PAY_ORIGIN_DIGEST_KEY = "pay_origin_digest"


def _normalize_pay_origin(raw: str | None) -> str:
	"""Normalize a configured pay origin, preserving scheme + port, or "" when unusable.

	Byte-identical to the admin-side origin._preserve_origin (its test-mode form) so the
	two sha256 digests agree; a live origin (https, no port) yields the same
	``https://<host>`` the admin's strict validator produces. Does NOT enforce the
	registered-host allowlist — that is admin's authority; the bench only needs a
	canonical string to digest and to build its own URL from."""
	raw = (raw or "").strip()
	if not raw:
		return ""
	parts = urlsplit(raw)
	scheme = (parts.scheme or "").lower()
	if scheme not in ("http", "https"):
		return ""
	if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
		return ""
	host = (parts.hostname or "").lower()
	if not host or "." not in host:
		return ""
	port = f":{parts.port}" if parts.port else ""
	return f"{scheme}://{host}{port}"


def _resolved_pay_origin() -> str:
	"""The bench's pay origin, normalized: the bench's admin URL (checkout is hosted on
	the control plane, so the admin URL the bench already knows IS the checkout origin).
	Read fresh; never raises. This anchors the digest cross-check against admin."""
	from jarvis.hooks import get_default_admin_url

	return _normalize_pay_origin(get_default_admin_url())


def pay_origin_digest(origin: str) -> str:
	"""The sha256 hex of a normalized origin — the shared, non-navigable digest."""
	return hashlib.sha256((origin or "").encode("utf-8")).hexdigest()


def augment_pay_page(data: dict) -> dict:
	"""Attach the bench's OWN attested pay origin to a token-bearing response.

	BEHAVIOUR-NEUTRAL unless admin sent a ``pay_page_token``: an old admin, or any
	non-token answer (a coded state, a paid connection payload), is returned
	untouched. On a token answer it injects two frontend-read fields:

	  * ``pay_origin`` — the bench's resolved, normalized pay origin (its admin URL),
	    or "" when invalid. The frontend builds ``{pay_origin}/jarvis-checkout#t=<token>``
	    from it; empty fails closed.
	  * ``pay_origin_attested`` — True iff ``pay_origin`` is set AND its sha256
	    digest equals admin's non-navigable ``pay_origin_digest`` (§R P0-3). The
	    frontend refuses to navigate unless this is True, so a config split-brain
	    (bench pointed at one origin, admin minted a token for another) fails
	    closed instead of sending the customer to the wrong host.

	The bench NEVER navigates to a URL from admin's body: it builds the URL from
	its own config, and this is only the cross-check that admin agrees."""
	if not isinstance(data, dict) or not data.get(PAY_PAGE_TOKEN_KEY):
		return data
	origin = _resolved_pay_origin()
	admin_digest = (data.get(PAY_ORIGIN_DIGEST_KEY) or "").strip().lower()
	attested = (
		bool(origin) and bool(admin_digest) and hmac.compare_digest(pay_origin_digest(origin), admin_digest)
	)
	out = dict(data)
	out["pay_origin"] = origin
	out["pay_origin_attested"] = attested
	return out


#: One Error Log per hour, not one per poll, for the deploy window below.
_MISSING_FIELD_LOG_KEY = "jarvis:signup_context:missing_field_logged"


def _field_installed() -> bool:
	"""Does the INSTALLED doctype have the context field?

	``frappe.get_meta().has_field`` and not ``frappe.db.has_column``: Jarvis
	Settings is a Single, so it has no table of its own and ``has_column`` raises
	``ProgrammingError('DocType', 'Jarvis Settings')`` when asked. Verified on
	this bench rather than assumed."""
	try:
		return bool(frappe.get_meta("Jarvis Settings").has_field(CONTEXT_FIELD))
	except Exception:
		return False


def _note_missing_field() -> None:
	"""Say ONCE an hour that the field is missing. A wizard polls every couple of
	seconds; an un-throttled log here would bury the Error Log it is trying to
	warn through."""
	cache = frappe.cache()
	if cache.get_value(_MISSING_FIELD_LOG_KEY):
		return
	cache.set_value(_MISSING_FIELD_LOG_KEY, 1, expires_in_sec=3600)
	frappe.log_error(
		title="signup context field missing - run bench migrate",
		message=(
			f"Jarvis Settings.{CONTEXT_FIELD} is not on the installed doctype, so the onboarding "
			"display context and the payment idempotency key are not being kept. Signup still "
			"works; a double-click may open two checkouts. Run `bench --site <site> migrate`."
		),
	)


def load() -> dict:
	"""The stored context, or ``{}``.

	Never raises. Three ways it can find nothing and all are survivable: nothing
	stored yet, a malformed value (hand-edited, half-written), and a bench between
	a code deploy and its ``bench migrate`` - where ``get_single_value`` THROWS on
	a field the installed doctype does not have. A wizard must not be bricked by
	its own bookkeeping."""
	if not _field_installed():
		_note_missing_field()
		return {}
	try:
		raw = frappe.db.get_single_value("Jarvis Settings", CONTEXT_FIELD) or ""
	except Exception:
		return {}
	if not raw:
		return {}
	try:
		out = json.loads(raw)
	except (ValueError, TypeError):
		return {}
	return out if isinstance(out, dict) else {}


def save(context: dict) -> None:
	"""Persist the context. ``db_set`` for the same reason write_connection uses
	it - Jarvis Settings' ``on_update`` pushes credentials to the container, and
	onboarding must not trigger that. ``update_modified=False`` keeps a 2-second
	wizard poll from churning the Single's timestamp.

	The secret sweep is the last line of defence, not the first: ``absorb``'s
	allowlist is what keeps credentials out of here, and this makes a future
	caller that hands the field a raw admin payload fail safe instead of parking
	an api_secret in a plain-text Settings column.

	SKIPPED entirely when the installed doctype has no such field - the state of a
	bench between a code deploy and its ``bench migrate``. CHECKING rather than
	catching, and the difference is the whole reason this guard exists: writing a
	Single validates NOTHING against meta. ``Document.db_set`` does not raise on a
	field the doctype has never heard of - verified on frappe 16.25.0 against a
	field with no prior row - it silently inserts an orphan ``tabSingles`` row
	that no migrate cleans up and no read can ever get back (``get_single_value``
	looks the field up in meta and THROWS, which is the asymmetry). A guard
	written as try/except would therefore have caught nothing and littered every
	bench deployed ahead of its migrate."""
	if not _field_installed():
		_note_missing_field()
		return
	clean = {k: v for k, v in (context or {}).items() if k not in _NEVER_PERSIST}
	settings = frappe.get_single("Jarvis Settings")
	try:
		settings.db_set(CONTEXT_FIELD, json.dumps(clean, sort_keys=True), update_modified=False)
	except Exception:
		frappe.log_error(
			title="signup context not persisted",
			message=frappe.get_traceback(),
		)


def _material(context: dict) -> dict:
	"""The part of a context that a write would be ABOUT."""
	return {k: v for k, v in context.items() if k not in _VOLATILE_KEYS}


def update(**fields) -> dict:
	"""Merge non-empty fields into the stored context and return the result.

	Empty values are dropped rather than written: a poll that answers with no
	``amount_inr`` (a verification-stage response, say) must not erase the amount
	a later screen still has to render. ``False`` and ``0`` are NOT empty and are
	written - ``awaiting_manual_reconciliation: False`` is how a cleared incident
	is recorded, and dropping it would leave the refusal it drives stuck on
	forever.

	A write happens only when something MATERIAL changed. The wizard polls every
	couple of seconds and every answer carries a fresh ``payment_last_checked_at``,
	so comparing whole dicts made a steady state write the Single on every tick -
	each write clearing the document cache for every other request on the site."""
	context = load()
	merged = dict(context)
	for key, value in fields.items():
		if value in (None, "", {}, []):
			continue
		merged[key] = value
	if _material(merged) != _material(context):
		merged["updated_at"] = frappe.utils.now()
		save(merged)
		return merged
	return context


def absorb(payload: dict | None) -> dict:
	"""Fold an admin envelope's DISPLAY facts into the local context.

	An allowlist, never a copy: the signup response carries the account's
	credentials beside its display fields, and a blanket merge would park them in
	a plain-text Settings field. ``plan`` arrives as ``{"name", "label"}`` from
	the state/resume surfaces and as a bare string from the bench's own signup
	call, so both are folded to ``plan`` + ``plan_label``."""
	if not isinstance(payload, dict):
		return load()
	fields = {key: payload.get(key) for key in _ADMIN_KEYS if key in payload}
	plan = payload.get("plan")
	if isinstance(plan, dict):
		fields["plan"] = plan.get("name") or ""
		fields["plan_label"] = plan.get("label") or ""
	elif isinstance(plan, str):
		fields["plan"] = plan
	return update(**fields)


def absorb_check(payload: dict | None) -> dict:
	"""``absorb`` for a PROVIDER-TRUTH check, which is additionally authoritative
	about the reconciliation flag.

	Admin sends ``awaiting_manual_reconciliation`` only when it is TRUE, so an
	ordinary absorb can raise the flag and nothing can ever lower it: the customer
	would be refused a retry forever, on an incident an operator closed weeks ago.
	A check is the one surface entitled to say the flag is FALSE, so it says so
	explicitly."""
	context = absorb(payload)
	if not isinstance(payload, dict):
		return context
	return update(awaiting_manual_reconciliation=bool(payload.get("awaiting_manual_reconciliation")))


def absorb_payment_outcome(err) -> dict:
	"""Fold a FAILED admin call into the context - but only when it actually
	reported the payment's state.

	The context drives two decisions (what the page renders, and whether the next
	initiation reuses its idempotency key), so what may enter it is admin's
	payment-state vocabulary and nothing else. A rate-limit backoff, a transport
	failure or a bench-local refusal describes THIS CALL, not the money: absorbing
	"you are asking too often" as the payment's state would let a backoff mint a
	fresh intent, and absorbing a network blip would overwrite a real decline.

	Narrow on purpose - the code, its recovery hint and the check stamp. A refusal
	is not a state read, and the fields it happens to carry are not a substitute
	for one."""
	code = getattr(err, "stable_code", "") or getattr(err, "code", "")
	if code not in _PAYMENT_STATE_CODES:
		return load()
	error = getattr(err, "error", None) or {}
	return update(
		code=code,
		recovery=error.get("recovery") or "",
		payment_last_checked_at=error.get("payment_last_checked_at") or "",
	)


def awaiting_reconciliation(context: dict | None = None) -> bool:
	"""Did the last provider-truth check find money an operator still has to
	place?

	While it did, this bench refuses to open another intent. The customer has
	already paid something the gateway is holding, and the code that comes with it
	is the ordinary pending one - deliberately, because a decline would invite a
	second payment - so nothing in the code alone stops a retry. The flag is what
	stops it."""
	context = load() if context is None else context
	return bool(context.get("awaiting_manual_reconciliation"))


def wire(context: dict | None = None) -> dict:
	"""The context block a facade response carries, minus anything local-only."""
	context = load() if context is None else context
	return {k: v for k, v in context.items() if k not in _LOCAL_ONLY_KEYS}


def clear() -> None:
	"""Drop the context (bench reset / disconnect)."""
	save({})


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #
def supplied_key_error(supplied: str | None) -> dict | None:
	"""Why a caller-supplied idempotency key is unusable, or None.

	Checked HERE, before the key is persisted, and that order is the whole point.
	The stored key is what the NEXT attempt reuses, so a key admin will refuse
	must never be written: the previous shape stored it, admin answered
	``INVALID_REQUEST``, and every subsequent attempt replayed the same refusal
	from the same stored key - a self-inflicted brick with no way out but a
	settings reset.

	The bound mirrors admin's ``_MAX_IDEMPOTENCY_KEY_LEN``. Duplicating a remote
	constant is a drift risk taken deliberately: a local refusal costs one HTTP
	round trip and leaves nothing behind, and the wrong direction of drift (ours
	stricter) is a caller told to shorten a key, while the other (ours looser) is
	the exact 400 admin already answers - which the INVALID_REQUEST entry in
	_SPENT_INTENT_CODES then clears rather than sticks on."""
	supplied = (supplied or "").strip()
	if not supplied:
		return None
	if len(supplied) > MAX_IDEMPOTENCY_KEY_LEN:
		return {
			"code": INVALID_REQUEST,
			"message": f"idempotency_key must be at most {MAX_IDEMPOTENCY_KEY_LEN} characters",
			"recovery": RECOVERY_RETRY,
		}
	return None


def next_idempotency_key(*, supplied: str | None = None, context: dict | None = None) -> str:
	"""The key for the payment initiation about to be made.

	``supplied`` (from the caller) wins verbatim. Never truncated: a key that is
	not the one the caller thinks it sent is worse than no key at all, so an
	unusable one is REFUSED by the endpoint (see supplied_key_error) instead of
	being quietly rewritten into a different request.

	Otherwise: reuse the stored key while the intent it bought is still payable
	(so a double-click, a retried POST and a refreshed pay screen all converge on
	one gateway object), and mint a fresh one once the last code says the
	recovery IS a new intent - because admin answers a key it has already seen
	with the intent that key created, and after a decline that is precisely the
	order the customer cannot pay.

	The key carries no identity: it is a receipt, admin stores only its SHA-256,
	and anything derived from the customer would put an identifier in a field
	operators read."""
	if supplied and supplied.strip():
		return supplied.strip()
	context = load() if context is None else context
	stored = (context.get("idempotency_key") or "").strip()
	if stored and context.get("code") not in _SPENT_INTENT_CODES:
		return stored
	return f"bench-{secrets.token_urlsafe(24)}"


# --------------------------------------------------------------------------- #
# wire shaping
# --------------------------------------------------------------------------- #
def error_object(err) -> tuple[dict, int]:
	"""One admin_client exception -> (wire error object, HTTP status).

	A contract error passes through as ITSELF: admin's code, its recovery hint
	and every extra it carried (``retry_after_seconds``, ``subscription_status``,
	``attempt_id``) reach the caller unaltered, because re-deriving them here is
	how one side's vocabulary becomes two. Everything else - transport,
	authentication, an admin too old to speak the contract - gets a BENCH_* code
	so its provenance is unambiguous."""
	from jarvis.exceptions import (
		AdminAuthError,
		AdminContractError,
		AdminRateLimitedError,
		AdminRejectedError,
		AdminUnreachableError,
		AdminValidationError,
	)

	if isinstance(err, AdminContractError):
		out = dict(err.error)
		out.setdefault("code", err.code)
		out.setdefault("message", str(err))
		return out, (err.http_status or 409)
	if isinstance(err, AdminRateLimitedError):
		out = dict(err.error) if err.error else {}
		out.setdefault("code", err.code or BENCH_RATE_LIMITED)
		out.setdefault("message", str(err))
		out.setdefault("recovery", err.recovery or RECOVERY_RETRY)
		if err.retry_after_seconds:
			out.setdefault("retry_after_seconds", err.retry_after_seconds)
		return out, 429
	if isinstance(err, AdminAuthError):
		return {
			"code": BENCH_ADMIN_AUTH_FAILED,
			"message": str(err),
			"recovery": RECOVERY_CONTACT_SUPPORT,
		}, (err.status_code or 401)
	if isinstance(err, AdminValidationError):
		# An admin older than the contract: a real rejection with a real
		# message, and no machine code anywhere in it. Fail closed onto the
		# generic branch rather than reading the sentence.
		exc_type = getattr(err, "exc_type", None) or ""
		message = str(err)
		# ...EXCEPT when the rejection is about a field the customer typed. That is a
		# different failure with a different recovery (fix the field, not retry the
		# payment), and answering it with the generic code told the customer their
		# PAYMENT SERVICE had refused - a service that was never contacted, since
		# admin refuses this before it builds any provider object.
		code = (
			BENCH_SIGNUP_DETAILS_REJECTED if is_details_rejection(message, exc_type) else BENCH_ADMIN_REJECTED
		)
		out = {"code": code, "message": message, "recovery": RECOVERY_RETRY}
		if exc_type:
			out["exc_type"] = exc_type
		return out, 409
	if isinstance(err, AdminRejectedError):
		# Admin was REACHED and permanently refused (its fleet layer answers a
		# config it can never accept with a 502 carrying its own error.code). It
		# is a subclass of AdminUnreachableError, so it must be answered before
		# that branch - collapsing it into "unreachable" is the exact mistake
		# jarvis #542 was about: a deterministic refusal recorded as "still
		# landing", which a caller then waits out forever.
		return {
			"code": err.code or BENCH_ADMIN_REJECTED,
			"message": err.detail or str(err),
			"recovery": RECOVERY_CONTACT_SUPPORT,
		}, 502
	if isinstance(err, AdminUnreachableError):
		return {
			"code": BENCH_ADMIN_UNREACHABLE,
			"message": str(err),
			"recovery": RECOVERY_RETRY,
		}, 502
	return {"code": BENCH_ADMIN_REJECTED, "message": str(err), "recovery": RECOVERY_RETRY}, 500


def stamp_error(error: dict) -> None:
	"""Park the machine-readable error on a response that is about to carry a
	RAISED exception, so a throw still delivers the contract.

	``frappe.utils.response.build_response`` serializes ``frappe.local.response``
	verbatim, which is what puts the object on the wire next to Frappe's own
	``exc_type`` - the same mechanism admin's ``codes.stamp_error`` uses, read
	from the other end. Call it immediately before the throw: it mutates the
	response, so a caller that CATCHES the throw would otherwise leave a stale
	error on an eventually-successful reply."""
	frappe.local.response["error"] = error


def success(data: dict, *, context: dict | None = None) -> dict:
	"""A facade success: admin's envelope plus the local context.

	Additive by default and subtractive only for credentials. Every capability
	flag, disclosure field and code admin added - including ones this bench build
	has never heard of - reaches the caller, which is what makes an additive admin
	release a no-op here; the ONE exception is the credential-shaped keys
	``strip_credentials`` removes, because "return exactly what admin sent" put
	the customer's OAuth password in an HTTP response body on the verified poll.
	The bench has already persisted it by the time this runs; the page never
	wanted it."""
	data = data or {}
	return {
		"ok": True,
		"contract_version": _int_or(data.get("contract_version"), CONTRACT_VERSION),
		"data": augment_pay_page(strip_credentials(data)),
		"context": wire(context),
	}


def _int_or(value, fallback: int) -> int:
	"""``int(value)``, or ``fallback`` when it is not a number. A malformed
	``contract_version`` from a proxy or a half-upgraded admin is a bad field, not
	a reason to 500 the payment page."""
	try:
		return int(value)
	except (TypeError, ValueError):
		return fallback


def failure(error: dict, status: int, *, context: dict | None = None) -> dict:
	"""A facade failure: the contract error, under a DELIBERATE 4xx/5xx.

	Never ``{"ok": false}`` on a 200 - the same rule that governs admin's half of
	this contract, for the same reason: a success-shaped body under a success
	status is a failure every reader has to be told about out of band."""
	frappe.local.response.http_status_code = status
	return {"ok": False, "error": error, "context": wire(context)}
