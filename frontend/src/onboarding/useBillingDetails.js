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

// The four Details-step inputs.
export const BILLING_FIELDS = ["contact", "address", "city", "gstin"];

// SPA field -> key in the shared billing-object contract
// (jarvis/tests/fixtures/billing_contract/billing_snapshot.json). The bench
// forwards this object to admin UNMODIFIED; admin owns the normalizer.
export const REQUEST_KEY = {
	contact: "contact_number",
	address: "address_line1",
	city: "city",
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
	"Saved in this browser for now — kept with your account once your signup is confirmed.";

// Transitional-storage key scheme (P0-02/C01-6): namespaced by site identity and
// user so one site's or user's billing PII can never prefill another's on a
// shared browser. The bare "jarvis-onboarding-billing" key this replaces was
// browser-global and never expired.
export const BILLING_LS_PREFIX = "jarvis-onboarding-billing";
export function billingStorageKey(site, user) {
	return `${BILLING_LS_PREFIX}::${site || "_"}::${user || "_"}`;
}

// ERP-defaults response (get_company_onboarding_defaults data) -> field values.
function fieldValuesFromDefaults(data) {
	const contact = (data && data.contact) || {};
	const addr = (data && data.billing_address) || {};
	return {
		contact: (contact.phone || "").trim(),
		address: (addr.address_line1 || "").trim(),
		city: (addr.city || "").trim(),
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
		gstin: (s.gstin || "").trim(),
	};
}

/**
 * @param {object} opts
 * @param {string} opts.site  canonical site identity (window.location.host)
 * @param {string} opts.user  logged-in user id (namespaces the transitional key)
 * @param {Storage} [opts.storage]  defaults to window.localStorage
 */
export function useBillingDetails(opts = {}) {
	const site = opts.site || "";
	const user = opts.user || "";
	const storage =
		opts.storage || (typeof window !== "undefined" ? window.localStorage : undefined);
	const key = billingStorageKey(site, user);

	const fields = reactive({
		contact: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		address: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		city: { value: "", source: "empty", source_company: "", last_auto_value: "" },
		gstin: { value: "", source: "empty", source_company: "", last_auto_value: "" },
	});

	// C01-5 version-skew acknowledgement: false until admin echoes billing_saved.
	const billingSaved = ref(false);
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

	function persist() {
		if (!storage) return;
		try {
			storage.setItem(
				key,
				JSON.stringify({
					contact: fields.contact.value,
					address: fields.address.value,
					city: fields.city.value,
					gstin: fields.gstin.value,
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
		let any = false;
		for (const name of BILLING_FIELDS) {
			const v = (d && d[name]) || "";
			if (v && fields[name].source === "empty") {
				fields[name].value = v;
				fields[name].source = "local_restore";
				any = true;
			}
		}
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
		if (saved) clearStorage();
		return saved;
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
		if (summary.source_company) sourceCompany.value = String(summary.source_company);
		if (summary.source_address) sourceAddress.value = String(summary.source_address);
		if (any) {
			markBillingSaved(true); // a stored snapshot is by definition persisted
			clearStorage();
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
			contact_number: "Contact",
			address_line1: "Billing address",
			city: "City",
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
