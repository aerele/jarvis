// Provenance-aware billing state for the onboarding Details step (Plan 01,
// FABLE-JUDGMENT C01-5/C01-6 + review round-2 P0-01/P0-02/P1-01/P1-03).
//
// The four billing inputs (contact phone, billing address, city, GSTIN) can't
// be plain strings: a Company change fetches ERP-derived defaults, and a late
// response for a Company the customer already switched away from must never
// clobber what they typed. Each field therefore carries provenance:
//
//     value            the current string
//     source           empty | erp_default | local_restore | user
//     source_company   the Company an erp_default came from (fence + switch)
//     last_auto_value   the last value an ERP default wrote (audit / diffing)
//
// Ownership rule, everywhere: `user` and `local_restore` are USER-OWNED and are
// never overwritten by a default. `empty` and `erp_default` are auto-owned and
// a matching default may fill/replace them. A stale-response fence (monotonic
// generation + Company match) is the second guard so out-of-order responses
// can't win.
//
// This module imports ONLY vue (no @/api, no frappe-ui) so the whole lineage is
// unit-testable without mounting the 12k-line OnboardingView — see
// useBillingDetails.spec.js.

import { reactive, ref, computed } from "vue";

// The Details-step billing inputs. State + Country drive India Compliance's place
// of supply (State is a Select of Indian states when Country is India); the books
// side maps them to a GST state code / Overseas.
export const BILLING_FIELDS = [
	"contact",
	"address",
	"address2",
	"city",
	"state",
	"pincode",
	"country",
	"gstin",
];

// The two IDENTITY inputs (work email, company). They are not billing fields and
// they carry no provenance, but they belong in the same snapshot for one blunt
// reason: paying TOP-LEVEL NAVIGATES the whole tab away to the admin-hosted
// checkout, so coming back is a fresh page load. Anything held only in the
// wizard's in-memory `state` is gone by then. Email and company were held only
// there, so a customer who came back from a failed payment and pressed Back found
// the Details step blank, or prefilled with the SITE ADMINISTRATOR's email rather
// than the one they had typed. Restoring them is the difference between resuming
// and starting over.
export const IDENTITY_FIELDS = ["email", "company"];

// SPA field -> key in the shared billing-object contract
// (jarvis/tests/fixtures/billing_contract/billing_snapshot.json). The bench
// forwards this object to admin UNMODIFIED; admin owns the normalizer.
export const REQUEST_KEY = {
	contact: "contact_number",
	address: "address_line1",
	address2: "address_line2",
	city: "city",
	state: "state",
	pincode: "pincode",
	country: "country",
	gstin: "gstin",
};

// User-owned sources are never overwritten by a default.
const USER_OWNED = new Set(["user", "local_restore"]);

// Storage-promise copy (P0-01): the "kept with your account" wording renders
// ONLY once admin acknowledges the durable write (billing_saved: true). Until
// then the honest local copy shows and the local snapshot is kept.
export const STORAGE_PROMISE_SAVED =
	"Billing details are kept with your account for upcoming invoicing.";
export const STORAGE_PROMISE_LOCAL =
	"Saved in this browser for now. Kept with your account once your signup is confirmed.";

// Transitional-storage key scheme (P0-02/C01-6): namespaced by site identity and
// user so one site's or user's billing PII can never prefill another's on a
// shared browser. The bare "jarvis-onboarding-billing" key this replaces was
// browser-global and never expired.
export const BILLING_LS_PREFIX = "jarvis-onboarding-billing";
export function billingStorageKey(site, user) {
	return `${BILLING_LS_PREFIX}::${site || "_"}::${user || "_"}`;
}

// How long a transitional snapshot may live before it is treated as absent and
// deleted on sight.
//
// This exists because the snapshot no longer dies at the durable-write ack (see
// markBillingSaved: dying there is what emptied the Details form after a checkout
// round trip). Moving the clear to the END of onboarding fixed resuming, but a
// customer who ABANDONS - closes the tab at the pay screen, never comes back -
// then never reaches the end, and their address, GSTIN and work email would sit
// in localStorage on a shared or kiosk browser forever. Neither "clear at the ack"
// nor "clear at the end" is sufficient on its own; retention has to be bounded by
// TIME, which holds however the customer leaves.
//
// 24 hours matches the verification link's own life, so the window never outlives
// a signup that could still legitimately be resumed.
export const BILLING_SNAPSHOT_TTL_MS = 24 * 60 * 60 * 1000;

// ERP-defaults response (get_company_onboarding_defaults data) -> field values.
function fieldValuesFromDefaults(data) {
	const contact = (data && data.contact) || {};
	const addr = (data && data.billing_address) || {};
	return {
		contact: (contact.phone || "").trim(),
		address: (addr.address_line1 || "").trim(),
		city: (addr.city || "").trim(),
		state: (addr.state || "").trim(),
		country: (addr.country || "").trim(),
		gstin: (addr.gstin || "").trim(),
	};
}

// Admin's normalized summary (get_signup_payment_state.data.billing /
// update_pending_billing.data.billing) -> field values.
function fieldValuesFromSummary(s) {
	s = s || {};
	return {
		contact: (s.contact_number || "").trim(),
		address: (s.address_line1 || "").trim(),
		city: (s.city || "").trim(),
		state: (s.state || "").trim(),
		country: (s.country || "").trim(),
		gstin: (s.gstin || "").trim(),
	};
}

/**
 * @param {object} opts
 * @param {string} opts.site  canonical site identity (window.location.host)
 * @param {string} opts.user  logged-in user id (namespaces the transitional key)
 * @param {Storage} [opts.storage]  defaults to window.localStorage
 * @param {Function} [opts.now]  ms clock, injected so the snapshot TTL is testable
 */
export function useBillingDetails(opts = {}) {
	const now = opts.now || (() => Date.now());
	const site = opts.site || "";
	const user = opts.user || "";
	const storage =
		opts.storage || (typeof window !== "undefined" ? window.localStorage : undefined);
	const key = billingStorageKey(site, user);

	const fields = reactive({
		contact: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		address: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		address2: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		city: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		state: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		pincode: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		// Country defaults to India (this is an India-first GST product) so the value
		// is actually captured, not just displayed — the field is required. Kept at
		// source "empty" so an ERP company default or a resumed snapshot still wins.
		country: { value: "India", source: "empty", source_company: "", last_auto_value: "" },
		gstin: { value: "", source: "empty", source_company: "", last_auto_value: "" },
	});

	// C01-5 version-skew acknowledgement: false until admin echoes billing_saved.
	const billingSaved = ref(false);
	// The identity half of the snapshot (see IDENTITY_FIELDS). Held here rather
	// than in the wizard's `state` so it survives the checkout round trip, and kept
	// deliberately dumb: last write wins, no provenance, because unlike the billing
	// fields nothing auto-fills these behind the customer's back.
	const identity = reactive({ email: "", company: "" });

	// Invoicing Details: the party the GST invoice is raised to. Optional overrides — blank means
	// the invoice falls back to the customer's own company (identity.company) + account email. Simple
	// values (no ERP-default provenance), persisted + restored like identity so a checkout round
	// trip keeps them.
	const invoicing = reactive({ company_name: "", email: "" });
	// The invoicing company name + email DEFAULT to the chosen company + work email and follow them,
	// until the customer edits that invoicing field — then it is user-owned and stops auto-tracking.
	let invoicingCompanyUserSet = false;
	let invoicingEmailUserSet = false;
	function setInvoicing(company_name, email) {
		if (company_name !== undefined) {
			invoicing.company_name = company_name == null ? "" : String(company_name);
			invoicingCompanyUserSet = true;
		}
		if (email !== undefined) {
			invoicing.email = email == null ? "" : String(email);
			invoicingEmailUserSet = true;
		}
		persist();
	}
	// Mirror the company + work email into the invoicing party, unless the customer overrode them.
	// The view calls this whenever the company / work email changes — INCLUDING the prefilled
	// defaults on mount, which is why the default must live here and not in setIdentity.
	function syncInvoicingDefaults(company, email) {
		if (!invoicingCompanyUserSet) invoicing.company_name = (company || "").trim();
		if (!invoicingEmailUserSet) invoicing.email = (email || "").trim();
	}
	// No contact-consent state here any more: the Details-step "okay to contact
	// me" checkbox was folded into the required T&C acceptance on Review & Pay
	// (owner decision 2026-08-14), so consent rides start_signup as a literal
	// true alongside terms_accepted and nothing about it needs to survive a
	// reload.
	// Provenance of the last-applied ERP defaults, forwarded to admin so it can
	// record where the snapshot originated. Never rendered on Review & Pay.
	const sourceCompany = ref("");
	const sourceAddress = ref("");

	// Stale-response fence state. `generation` is monotonic; `activeCompany` is
	// the Company a fetch is currently in flight for. A response applies only
	// when BOTH still match (P1-01).
	let generation = 0;
	let activeCompany = "";

	function _resetToEmpty(name) {
		const f = fields[name];
		f.value = "";
		f.source = "empty";
		f.source_company = "";
		f.last_auto_value = "";
	}

	function isUserOwned(name) {
		return USER_OWNED.has(fields[name].source);
	}

	// The Details inputs call this on every edit: the value becomes user-owned
	// and a default can no longer touch it.
	function setUserValue(name, value) {
		const f = fields[name];
		f.value = value == null ? "" : String(value);
		f.source = "user";
		f.source_company = "";
		persist();
	}

	// Begin an ERP-defaults fetch for `company`: bump the generation, record the
	// active Company, and clear any erp_default value that belonged to a DIFFERENT
	// Company so stale ERP data doesn't linger while the new fetch is in flight
	// (this is also the "custom Company clears only ERP-owned values" path — a
	// custom name resolves nothing, so the cleared auto fields simply stay empty).
	// User-owned values survive untouched. Returns the generation to echo back on
	// applyDefaults.
	function beginCompanyFetch(company) {
		company = (company || "").trim();
		activeCompany = company;
		generation += 1;
		for (const name of BILLING_FIELDS) {
			const f = fields[name];
			if (f.source === "erp_default" && f.source_company !== company) {
				_resetToEmpty(name);
			}
		}
		// Provenance is only meaningful once the new company's defaults resolve.
		sourceCompany.value = "";
		sourceAddress.value = "";
		return generation;
	}

	// Apply an ERP-defaults response. Fenced: ignored unless it is the newest
	// in-flight request (generation match) AND still for the selected Company
	// (company match). Fills only empty / erp_default-owned fields; user-owned
	// values are never overwritten. Returns "applied" or a rejection reason so
	// the fence is observable in tests.
	function applyDefaults(resp, gen, company) {
		company = (company || "").trim();
		if (gen !== generation) return "stale-generation";
		if (company !== activeCompany) return "stale-company";
		const data = (resp && resp.data) || resp || {};
		const incoming = fieldValuesFromDefaults(data);
		for (const name of BILLING_FIELDS) {
			if (isUserOwned(name)) continue; // never overwrite a user edit
			const v = incoming[name];
			if (!v) continue; // a blank default doesn't wipe an existing erp_default
			const f = fields[name];
			f.value = v;
			f.source = "erp_default";
			f.source_company = company;
			f.last_auto_value = v;
		}
		sourceCompany.value = (data.company || company || "").trim();
		sourceAddress.value = ((data.billing_address && data.billing_address.name) || "").trim();
		persist();
		return "applied";
	}

	// --- transitional localStorage (namespaced, ack-gated) --------------------

	// Record the identity half of the snapshot. Called on every edit of the work
	// email / company inputs, so a mid-flow reload or a checkout round trip finds
	// them again.
	function setIdentity(email, company) {
		if (email !== undefined) identity.email = email == null ? "" : String(email);
		if (company !== undefined) identity.company = company == null ? "" : String(company);
		persist();
	}

	function persist() {
		if (!storage) return;
		try {
			storage.setItem(
				key,
				JSON.stringify({
					contact: fields.contact.value,
					address: fields.address.value,
					address2: fields.address2.value,
					city: fields.city.value,
					state: fields.state.value,
					pincode: fields.pincode.value,
					country: fields.country.value,
					gstin: fields.gstin.value,
					email: identity.email,
					company: identity.company,
					invoice_company_name: invoicing.company_name,
					invoice_email: invoicing.email,
					// Stamped on every write so restore() can bound how long this PII
					// lives, whatever happens to the session that wrote it.
					saved_at: now(),
				})
			);
		} catch (e) {
			/* storage full/blocked — purely best-effort */
		}
	}

	// Restore the namespaced snapshot into any still-empty field. Restored values
	// are USER-OWNED (local_restore) so a later ERP default cannot overwrite what
	// the customer had already entered. Only this site+user's key is read, so a
	// different site/user namespace is invisible. Returns true if anything was
	// restored.
	function restore() {
		if (!storage) return false;
		let d;
		try {
			d = JSON.parse(storage.getItem(key) || "{}");
		} catch (e) {
			return false; // corrupt entry — ignore
		}
		// Past its window: treat as absent AND delete it. This is the retention bound
		// that "clear when onboarding finishes" cannot provide on its own, because a
		// customer who abandons at the pay screen never reaches the finish. Deleting
		// on READ is deliberate: it needs no timer, no lifecycle hook and no
		// cooperation from the page that abandoned, and the next visit to any
		// onboarding screen is what collects it.
		//
		// A snapshot with NO stamp is one written by a build older than this change.
		// It is dropped too, on the same reasoning: its age is unknowable, so it
		// cannot be shown to be within the window.
		const savedAt = Number(d && d.saved_at);
		if (!Number.isFinite(savedAt) || now() - savedAt > BILLING_SNAPSHOT_TTL_MS) {
			clearStorage();
			return false;
		}
		let any = false;
		for (const name of BILLING_FIELDS) {
			const v = (d && d[name]) || "";
			if (v && fields[name].source === "empty") {
				fields[name].value = v;
				fields[name].source = "local_restore";
				any = true;
			}
		}
		// Identity restores unconditionally into whatever is still blank. There is no
		// provenance to respect here, and the caller reads `identity` to decide
		// whether to overwrite its own prefill (a restored value the CUSTOMER typed
		// must beat getAccountDefaults, which can be the site admin's email).
		for (const name of IDENTITY_FIELDS) {
			const v = (d && d[name]) || "";
			if (v && !identity[name]) {
				identity[name] = v;
				any = true;
			}
		}
		// Invoicing overrides restore into whatever is still blank (like identity, no provenance).
		if (d && d.invoice_company_name && !invoicing.company_name) {
			invoicing.company_name = d.invoice_company_name;
			invoicingCompanyUserSet = true; // a resumed value must not be clobbered by a company change
			any = true;
		}
		if (d && d.invoice_email && !invoicing.email) {
			invoicing.email = d.invoice_email;
			invoicingEmailUserSet = true;
			any = true;
		}
		// A `contact_consent` key from a snapshot written by an older build is
		// simply ignored: the consent checkbox no longer exists (it lives inside
		// the T&C acceptance now), so there is nothing to restore it into.
		return any;
	}

	function clearStorage() {
		if (!storage) return;
		try {
			storage.removeItem(key);
		} catch (e) {
			/* ignore */
		}
	}

	// Record admin's version-skew acknowledgement. Only a literal `true` flips the
	// promise to "kept with your account" (an older admin drops the kwarg and never
	// echoes it → stays honest). A durable ack retires the local snapshot.
	function markBillingSaved(ack) {
		const saved = ack === true;
		billingSaved.value = saved;
		// Deliberately does NOT clear the local snapshot any more, and this is the
		// single most load-bearing change in this file.
		//
		// It used to, on the reasoning that once admin holds the data durably there
		// is no reason to leave PII sitting in localStorage. The reasoning is sound;
		// the TIMING was wrong. The ack arrives from start_signup, which is the call
		// made immediately BEFORE the customer is navigated away to the checkout - so
		// the snapshot was destroyed at the exact moment the only copy that could
		// survive the round trip was needed. A customer whose payment then failed
		// pressed Back and found the form empty, with nothing left to restore from
		// except admin's own state read, which carries no snapshot for a signup that
		// never completed.
		//
		// The privacy intent is preserved by clearing at the END of onboarding
		// instead (see `finish`), which is where "we no longer need this locally"
		// actually becomes true.
		return saved;
	}

	// Onboarding is over: drop the transitional local copy. This is where the
	// storage promise is finally kept, rather than at the durable-write ack (see
	// markBillingSaved for why that was too early).
	function finish() {
		clearStorage();
	}

	// Cross-device recovery (P1-03 / brief §6): admin's authenticated state
	// response carries the normalized snapshot. Server truth WINS over local —
	// hydrated fields become user-owned, and the local remnant is cleared. Only a
	// present, non-empty summary hydrates.
	function hydrateServerSnapshot(summary) {
		if (!summary || typeof summary !== "object") return false;
		const incoming = fieldValuesFromSummary(summary);
		let any = false;
		for (const name of BILLING_FIELDS) {
			const v = incoming[name];
			if (v) {
				fields[name].value = v;
				fields[name].source = "user"; // authoritative, non-overwritable
				fields[name].source_company = "";
				any = true;
			}
		}
		if (summary.company_name) {
			invoicing.company_name = String(summary.company_name);
			invoicingCompanyUserSet = true; // server truth is authoritative; do not auto-track after
			any = true;
		}
		if (summary.email) {
			invoicing.email = String(summary.email);
			invoicingEmailUserSet = true;
			any = true;
		}
		if (summary.source_company) sourceCompany.value = String(summary.source_company);
		if (summary.source_address) sourceAddress.value = String(summary.source_address);
		if (any) {
			markBillingSaved(true); // a stored snapshot is by definition persisted
			// The local copy is kept until onboarding finishes (see markBillingSaved):
			// server truth having won here says nothing about whether the customer is
			// about to be navigated away to checkout and back.
			persist();
		}
		return any;
	}

	// The single normalized billing object sent to admin AND rendered on Review &
	// Pay (P1-03: the card reads THIS object, never re-joined form strings). Only
	// non-empty keys are included; blank optional fields are simply omitted. GSTIN
	// is upper-cased to match admin's normalizer so the card shows what is stored.
	function buildBilling() {
		const out = {};
		for (const name of BILLING_FIELDS) {
			let v = (fields[name].value || "").trim();
			if (name === "gstin") v = v.toUpperCase();
			if (v) out[REQUEST_KEY[name]] = v;
		}
		// Invoicing Details party (admin's _normalize_billing maps company_name -> billing_company_name,
		// email -> billing_email). Blank => omitted, so admin falls back to the customer's own company.
		const invCompany = (invoicing.company_name || "").trim();
		if (invCompany) out.company_name = invCompany;
		const invEmail = (invoicing.email || "").trim();
		if (invEmail) out.email = invEmail;
		if (Object.keys(out).length) {
			if (sourceCompany.value) out.source_company = sourceCompany.value;
			if (sourceAddress.value) out.source_address = sourceAddress.value;
		}
		return out;
	}

	// Display rows for the Review & Pay card, derived from buildBilling() (so the
	// card can never drift from the payload). Provenance keys are dropped.
	const reviewRows = computed(() => {
		const b = buildBilling();
		const labels = {
			company_name: "Invoicing company",
			email: "Invoicing email",
			contact_number: "Contact",
			address_line1: "Billing address",
			address_line2: "Address line 2",
			city: "City",
			state: "State",
			pincode: "Pincode",
			country: "Country",
			gstin: "GSTIN",
		};
		return Object.keys(labels)
			.filter((k) => b[k])
			.map((k) => ({ key: k, label: labels[k], value: b[k] }));
	});

	const promiseCopy = computed(() =>
		billingSaved.value ? STORAGE_PROMISE_SAVED : STORAGE_PROMISE_LOCAL
	);

	return {
		fields,
		identity,
		setIdentity,
		invoicing,
		setInvoicing,
		syncInvoicingDefaults,
		finish,
		billingSaved,
		sourceCompany,
		sourceAddress,
		promiseCopy,
		reviewRows,
		storageKey: key,
		// generation is internal; expose readers for tests / telemetry.
		currentGeneration: () => generation,
		activeCompany: () => activeCompany,
		isUserOwned,
		setUserValue,
		beginCompanyFetch,
		applyDefaults,
		persist,
		restore,
		clearStorage,
		markBillingSaved,
		hydrateServerSnapshot,
		buildBilling,
	};
}

// Pure decision for the post-Details "Continue" action when the customer is
// EDITING billing after returning from Review & Pay (brief §5 / P1-03): once a
// payment intent exists, edits save through the authenticated update_billing
// facade — NEVER guest signup, which would create/replace an intent. Before an
// intent exists there is nothing to persist yet (the snapshot rides the first
// signup call), so the action is a no-op return to review.
export function billingEditAction(intentExists) {
	return intentExists ? "update_billing" : "none";
}
