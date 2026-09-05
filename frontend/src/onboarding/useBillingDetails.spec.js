import { describe, it, expect, beforeEach } from "vitest";
import {
	useBillingDetails,
	billingEditAction,
	billingStorageKey,
	STORAGE_PROMISE_SAVED,
	BILLING_SNAPSHOT_TTL_MS,
} from "./useBillingDetails.js";

// A default ERP-defaults response for "Aerele" (company A) and one for "Beta".
const DEF_A = {
	ok: true,
	data: {
		company: "Aerele",
		contact: { name: "C-A", display_name: "A Contact", phone: "+91 90000 00001" },
		billing_address: {
			name: "Aerele-Billing",
			address_line1: "12 MG Road",
			city: "Chennai",
			gstin: "33ABCDE1234F1Z5",
		},
	},
};
const DEF_B = {
	ok: true,
	data: {
		company: "Beta",
		contact: { name: "C-B", phone: "+91 90000 00002" },
		billing_address: {
			name: "Beta-Billing",
			address_line1: "9 Ring Road",
			city: "Delhi",
			gstin: "07ABCDE1234F1Z5",
		},
	},
};

// Fetch + apply defaults for `company` in one shot, at the CURRENT generation.
function fetchAndApply(b, company, resp) {
	const gen = b.beginCompanyFetch(company);
	return b.applyDefaults(resp, gen, company);
}

beforeEach(() => {
	window.localStorage.clear();
});

describe("blank fields receive defaults", () => {
	it("fills every empty field from the ERP defaults, marked erp_default", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(fetchAndApply(b, "Aerele", DEF_A)).toBe("applied");
		expect(b.fields.contact.value).toBe("+91 90000 00001");
		expect(b.fields.address.value).toBe("12 MG Road");
		expect(b.fields.city.value).toBe("Chennai");
		expect(b.fields.gstin.value).toBe("33ABCDE1234F1Z5");
		for (const k of ["contact", "address", "city", "gstin"]) {
			expect(b.fields[k].source).toBe("erp_default");
			expect(b.fields[k].source_company).toBe("Aerele");
		}
	});
});

describe("user edits are never overwritten by a default", () => {
	it("a value typed BEFORE the response survives it", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const gen = b.beginCompanyFetch("Aerele");
		b.setUserValue("city", "Mumbai"); // user types while the fetch is in flight
		expect(b.applyDefaults(DEF_A, gen, "Aerele")).toBe("applied");
		expect(b.fields.city.value).toBe("Mumbai"); // user wins
		expect(b.fields.city.source).toBe("user");
		expect(b.fields.contact.value).toBe("+91 90000 00001"); // empty one still filled
	});
});

describe("stale-response fence (P1-01)", () => {
	it("A→B with responses arriving B→A keeps B", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const genA = b.beginCompanyFetch("Aerele");
		const genB = b.beginCompanyFetch("Beta");
		// B resolves first (it is the current selection)…
		expect(b.applyDefaults(DEF_B, genB, "Beta")).toBe("applied");
		// …then A's late response arrives and MUST be dropped.
		expect(b.applyDefaults(DEF_A, genA, "Aerele")).toBe("stale-generation");
		expect(b.fields.city.value).toBe("Delhi");
		expect(b.fields.contact.value).toBe("+91 90000 00002");
	});

	// Mutation guard for the fence: if the generation check were removed the
	// stale A response above would apply. Assert both fence dimensions reject.
	it("rejects a wrong-generation response without mutating state", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const gen = b.beginCompanyFetch("Aerele");
		b.applyDefaults(DEF_A, gen, "Aerele");
		const before = b.fields.city.value;
		expect(b.applyDefaults(DEF_B, gen - 1, "Aerele")).toBe("stale-generation");
		expect(b.fields.city.value).toBe(before);
	});

	it("rejects a right-generation but wrong-company response", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const gen = b.beginCompanyFetch("Aerele");
		expect(b.applyDefaults(DEF_B, gen, "Beta")).toBe("stale-company");
		expect(b.fields.city.value).toBe("");
	});
});

describe("company switch ownership", () => {
	it("previous ERP-owned values change to the new company's defaults", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		fetchAndApply(b, "Aerele", DEF_A);
		expect(b.fields.city.value).toBe("Chennai");
		fetchAndApply(b, "Beta", DEF_B);
		expect(b.fields.city.value).toBe("Delhi");
		expect(b.fields.city.source_company).toBe("Beta");
	});

	it("user-owned values survive a company change", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		fetchAndApply(b, "Aerele", DEF_A);
		b.setUserValue("contact", "+91 99999 12345");
		fetchAndApply(b, "Beta", DEF_B);
		expect(b.fields.contact.value).toBe("+91 99999 12345"); // kept
		expect(b.fields.contact.source).toBe("user");
		expect(b.fields.city.value).toBe("Delhi"); // erp_default still swapped
	});

	it("custom Company clears only ERP-owned values, keeps user edits", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		fetchAndApply(b, "Aerele", DEF_A);
		b.setUserValue("gstin", "29ABCDE1234F1Z5"); // user override
		// A custom company resolves nothing: beginCompanyFetch clears prior
		// erp_default values; no applyDefaults follows (server returns NOT_FOUND).
		b.beginCompanyFetch("Totally New Pvt Ltd");
		expect(b.fields.city.value).toBe(""); // erp_default cleared
		expect(b.fields.city.source).toBe("empty");
		expect(b.fields.gstin.value).toBe("29ABCDE1234F1Z5"); // user kept
		expect(b.fields.gstin.source).toBe("user");
	});
});

describe("local restore is user-owned", () => {
	it("restores into empty fields and marks them local_restore (non-overwritable)", () => {
		const seed = useBillingDetails({ site: "s1", user: "u1" });
		seed.setUserValue("city", "Pune");
		seed.setUserValue("gstin", "27ABCDE1234F1Z5");
		// New instance, same site+user, simulates a reload.
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.restore()).toBe(true);
		expect(b.fields.city.value).toBe("Pune");
		expect(b.fields.city.source).toBe("local_restore");
		// A subsequent ERP default must NOT overwrite the restored value.
		fetchAndApply(b, "Aerele", DEF_A);
		expect(b.fields.city.value).toBe("Pune");
	});
});

describe("transitional-storage namespace isolation (P0-02/C01-6)", () => {
	it("a different site or user cannot read the snapshot", () => {
		const a = useBillingDetails({ site: "siteA", user: "userA" });
		a.setUserValue("gstin", "33SECRET1234F1Z5");
		// Different user, same site.
		const b1 = useBillingDetails({ site: "siteA", user: "userB" });
		expect(b1.restore()).toBe(false);
		expect(b1.fields.gstin.value).toBe("");
		// Different site, same user.
		const b2 = useBillingDetails({ site: "siteB", user: "userA" });
		expect(b2.restore()).toBe(false);
		expect(b2.fields.gstin.value).toBe("");
		// Same site+user CAN.
		const b3 = useBillingDetails({ site: "siteA", user: "userA" });
		expect(b3.restore()).toBe(true);
		expect(b3.fields.gstin.value).toBe("33SECRET1234F1Z5");
	});

	it("the storage key encodes site and user", () => {
		expect(billingStorageKey("siteA", "userA")).not.toBe(billingStorageKey("siteA", "userB"));
		expect(billingStorageKey("siteA", "userA")).not.toBe(billingStorageKey("siteB", "userA"));
	});
});

describe("re-fetch discipline (back/continue does not lose edits)", () => {
	it("re-selecting the SAME company keeps its erp_default values", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		fetchAndApply(b, "Aerele", DEF_A);
		// Navigating back to details and re-entering re-runs beginCompanyFetch on
		// the same company: erp_default from the SAME company must not be cleared.
		b.beginCompanyFetch("Aerele");
		expect(b.fields.city.value).toBe("Chennai");
		expect(b.fields.city.source).toBe("erp_default");
	});

	it("a fresh generation is minted per fetch (fence stays monotonic)", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const g1 = b.beginCompanyFetch("Aerele");
		const g2 = b.beginCompanyFetch("Beta");
		expect(g2).toBeGreaterThan(g1);
		expect(b.currentGeneration()).toBe(g2);
	});
});

// The "ack-gated storage promise (P0-01)" suite that lived here is gone: the
// rendered promise copy (billing.promiseCopy) is no longer shown anywhere in
// the UI (see OnboardingView.vue B4). promiseCopy/STORAGE_PROMISE_* remain
// exported from useBillingDetails.js only because usePaymentFlow.spec.js
// still imports and asserts on them directly; that file was left untouched.
//
// Two behaviors this suite covered are NOT re-tested for promiseCopy itself,
// but the underlying mechanics they exercised (unrelated to the promise text)
// are still covered below: markBillingSaved's "only a literal true flips the
// ack" guard, and the local snapshot surviving a durable ack but being
// retired by finish().
describe("markBillingSaved / local snapshot lifecycle", () => {
	// Mutation guard for the ack gate: only a literal boolean true may flip it.
	// A truthy-but-not-true ack (older admin echoing 1, or a "true" string) must
	// NOT claim persistence.
	it("only a literal true ack returns true", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.markBillingSaved(1)).toBe(false);
		expect(b.markBillingSaved("true")).toBe(false);
		expect(b.markBillingSaved(undefined)).toBe(false);
		expect(b.markBillingSaved(true)).toBe(true);
	});

	// The durable ack used to ALSO clear the local snapshot. It no longer does, and
	// that is the fix rather than a regression: the ack arrives from start_signup,
	// which is the call made immediately before the customer is top-level-navigated
	// away to the checkout. Clearing there destroyed the only copy that could
	// survive the round trip, so a customer who came back from a failed payment and
	// pressed Back found an empty form. The snapshot is now retired at the END of
	// onboarding instead, via finish().
	it("keeps the local snapshot through a durable ack, so a checkout round trip can resume", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("city", "Chennai");
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy();
		b.markBillingSaved(false);
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy(); // kept
		b.markBillingSaved(true);
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy(); // still kept
	});

	it("retires the local snapshot when onboarding finishes", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("city", "Chennai");
		b.markBillingSaved(true);
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy();
		b.finish();
		expect(window.localStorage.getItem(b.storageKey)).toBeNull(); // retired
	});
});

describe("identity survives the checkout round trip", () => {
	it("persists and restores the work email and company", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setIdentity("payer@acme.test", "Acme Inc.");
		// A fresh instance is what a return from the pay page actually produces: the
		// tab navigated away, so nothing in memory survived.
		const after = useBillingDetails({ site: "s1", user: "u1" });
		expect(after.identity.email).toBe("");
		after.restore();
		expect(after.identity.email).toBe("payer@acme.test");
		expect(after.identity.company).toBe("Acme Inc.");
	});

	it("stays namespaced: another site or user cannot see it", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setIdentity("payer@acme.test", "Acme Inc.");
		const otherUser = useBillingDetails({ site: "s1", user: "u2" });
		otherUser.restore();
		expect(otherUser.identity.email).toBe("");
		const otherSite = useBillingDetails({ site: "s2", user: "u1" });
		otherSite.restore();
		expect(otherSite.identity.company).toBe("");
	});

	// restore() fills BLANKS only. The wizard calls it once on mount, before the
	// customer can type and before prefillAccount runs, which is what stops the site
	// administrator's email (a different person) from replacing the payer's.
	it("restore fills only what is still blank", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setIdentity("stored@acme.test", "Stored Co.");
		const fresh = useBillingDetails({ site: "s1", user: "u1" });
		fresh.restore();
		expect(fresh.identity.email).toBe("stored@acme.test");
		// A value present at restore time is never clobbered by a second restore.
		fresh.identity.email = "typed@acme.test";
		fresh.restore();
		expect(fresh.identity.email).toBe("typed@acme.test");
		expect(fresh.identity.company).toBe("Stored Co.");
	});
});

describe("Review & Pay card equals the normalized payload (P1-03)", () => {
	it("card rows are derived from buildBilling(), never from separate strings", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		fetchAndApply(b, "Aerele", DEF_A);
		b.setUserValue("gstin", "27abcde1234f1z5"); // lower-case user entry
		const payload = b.buildBilling();
		// GSTIN normalized (upper) in the SAME object the card reads.
		expect(payload.gstin).toBe("27ABCDE1234F1Z5");
		const rowByKey = Object.fromEntries(b.reviewRows.value.map((r) => [r.key, r.value]));
		for (const k of Object.keys(rowByKey)) {
			expect(rowByKey[k]).toBe(payload[k]);
		}
		// Provenance rides the payload but is NOT shown on the card.
		expect(payload.source_company).toBe("Aerele");
		expect(payload.source_address).toBe("Aerele-Billing");
		expect(b.reviewRows.value.some((r) => r.key === "source_company")).toBe(false);
	});

	it("omits blank optional fields from both payload and card (country defaults to India)", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("contact", "+91 90000 00000");
		const payload = b.buildBilling();
		// Country is a required field defaulting to India, so it is always captured;
		// every other untouched field is still omitted.
		expect(payload).toEqual({ contact_number: "+91 90000 00000", country: "India" });
		expect(b.reviewRows.value).toEqual([
			{ key: "contact_number", label: "Contact", value: "+91 90000 00000" },
			{ key: "country", label: "Country", value: "India" },
		]);
	});

	it("defaults country to India so it is captured, not just displayed", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.fields.country.value).toBe("India");
		// still source 'empty' so an ERP company default / resumed snapshot can win
		expect(b.fields.country.source).toBe("empty");
	});
});

describe("Invoicing Details party (billing_company_name / billing_email)", () => {
	it("setInvoicing adds company_name + email to the payload; blanks are omitted", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("contact", "+91 90000 00000");
		b.setInvoicing("Acme Invoicing Pvt Ltd", "invoices@acme.test");
		const payload = b.buildBilling();
		expect(payload.company_name).toBe("Acme Invoicing Pvt Ltd");
		expect(payload.email).toBe("invoices@acme.test");
		// no invoicing entered -> omitted (admin falls back to the customer's own company)
		const b2 = useBillingDetails({ site: "s2", user: "u2" });
		b2.setUserValue("contact", "+91 90000 00000");
		expect(b2.buildBilling().company_name).toBeUndefined();
		expect(b2.buildBilling().email).toBeUndefined();
	});

	it("persists + restores the invoicing party across a reload", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setInvoicing("Acme Invoicing Pvt Ltd", "invoices@acme.test");
		const fresh = useBillingDetails({ site: "s1", user: "u1" });
		fresh.restore();
		expect(fresh.invoicing.company_name).toBe("Acme Invoicing Pvt Ltd");
		expect(fresh.invoicing.email).toBe("invoices@acme.test");
	});

	it("hydrates the invoicing party from the server summary", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.hydrateServerSnapshot({ company_name: "Server Co", email: "server@acme.test" });
		expect(b.invoicing.company_name).toBe("Server Co");
		expect(b.invoicing.email).toBe("server@acme.test");
	});

	it("shows the invoicing party on the Review card", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setInvoicing("Acme Invoicing Pvt Ltd", "invoices@acme.test");
		const rows = Object.fromEntries(b.reviewRows.value.map((r) => [r.key, r.value]));
		expect(rows.company_name).toBe("Acme Invoicing Pvt Ltd");
		expect(rows.email).toBe("invoices@acme.test");
	});

	it("defaults the invoicing party from the company + work email, follows them, until edited", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.syncInvoicingDefaults("Acme Pvt Ltd", "acct@acme.test");
		expect(b.invoicing.company_name).toBe("Acme Pvt Ltd");
		expect(b.invoicing.email).toBe("acct@acme.test");
		b.syncInvoicingDefaults("Acme Holdings", "acct2@acme.test"); // follows changes
		expect(b.invoicing.company_name).toBe("Acme Holdings");
		expect(b.invoicing.email).toBe("acct2@acme.test");
		// editing the invoicing company stops ITS tracking; the email keeps following
		b.setInvoicing("Custom Co", undefined);
		b.syncInvoicingDefaults("Acme Reborn", "acct3@acme.test");
		expect(b.invoicing.company_name).toBe("Custom Co");
		expect(b.invoicing.email).toBe("acct3@acme.test");
	});

	it("carries address line 2 + pincode in the payload", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("address2", "Level 4");
		b.setUserValue("pincode", "600001");
		const p = b.buildBilling();
		expect(p.address_line2).toBe("Level 4");
		expect(p.pincode).toBe("600001");
	});
});

describe("cross-device recovery — server snapshot wins (brief §6)", () => {
	it("hydrates from admin's normalized summary, overriding local, and rewrites the snapshot", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("city", "LocalCity"); // stale local value
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy();
		const applied = b.hydrateServerSnapshot({
			contact_number: "+91 88888 00000",
			address_line1: "Server Address",
			city: "ServerCity",
			gstin: "33ABCDE1234F1Z5",
			source_company: "Aerele",
		});
		expect(applied).toBe(true);
		expect(b.fields.city.value).toBe("ServerCity"); // server wins
		expect(b.fields.city.source).toBe("user"); // authoritative, non-overwritable
		expect(b.billingSaved.value).toBe(true); // stored ⇒ saved
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_SAVED);
		// The remnant is REWRITTEN with server truth rather than deleted: onboarding
		// is still in flight here, and the checkout round trip that follows would
		// otherwise leave nothing to restore from (see markBillingSaved).
		expect(JSON.parse(window.localStorage.getItem(b.storageKey)).city).toBe("ServerCity");
		// A later ERP default cannot clobber the recovered snapshot.
		fetchAndApply(b, "Aerele", DEF_A);
		expect(b.fields.city.value).toBe("ServerCity");
	});

	it("does nothing for an empty/absent summary", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.hydrateServerSnapshot(null)).toBe(false);
		expect(b.hydrateServerSnapshot({})).toBe(false);
		expect(b.billingSaved.value).toBe(false);
	});
});

describe("edit-after-intent uses the authenticated facade, never guest signup (P1-03)", () => {
	it("returns update_billing once an intent exists, no-op before", () => {
		expect(billingEditAction(false)).toBe("none");
		expect(billingEditAction(true)).toBe("update_billing");
		// The action vocabulary never includes a guest-signup verb.
		expect(billingEditAction(true)).not.toBe("signup");
	});
});

describe("the snapshot has a bounded lifetime (retention)", () => {
	// Moving the clear from the durable-write ack to the end of onboarding fixed
	// resuming after a checkout round trip, but on its own it meant a customer who
	// ABANDONED at the pay screen left billing PII in localStorage forever, since
	// they never reach the end. Retention is therefore bounded by TIME as well,
	// which holds however the customer leaves.
	it("restores a fresh snapshot", () => {
		const t = 1_000_000_000_000;
		const a = useBillingDetails({ site: "s1", user: "u1", now: () => t });
		a.setUserValue("city", "Chennai");
		const b = useBillingDetails({ site: "s1", user: "u1", now: () => t + 60_000 });
		expect(b.restore()).toBe(true);
		expect(b.fields.city.value).toBe("Chennai");
	});

	it("drops and DELETES a snapshot past its window, even if nothing finished", () => {
		const t = 1_000_000_000_000;
		const a = useBillingDetails({ site: "s1", user: "u1", now: () => t });
		a.setUserValue("gstin", "33ABCDE1234F1Z7");
		expect(window.localStorage.getItem(a.storageKey)).toBeTruthy();
		const later = t + BILLING_SNAPSHOT_TTL_MS + 1;
		const b = useBillingDetails({ site: "s1", user: "u1", now: () => later });
		expect(b.restore()).toBe(false);
		expect(b.fields.gstin.value).toBe("");
		// Collected on read: no timer, no lifecycle hook, no cooperation needed from
		// the session that abandoned.
		expect(window.localStorage.getItem(b.storageKey)).toBeNull();
	});

	it("drops an unstamped snapshot written by an older build", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		window.localStorage.setItem(b.storageKey, JSON.stringify({ city: "Chennai" }));
		expect(b.restore()).toBe(false);
		expect(window.localStorage.getItem(b.storageKey)).toBeNull();
	});
});

describe("state + country (India Compliance place of supply)", () => {
	it("round-trips state and country through buildBilling under the contract keys", () => {
		const b = useBillingDetails({ site: "sc1", user: "uc1" });
		b.setUserValue("city", "Bengaluru");
		b.setUserValue("state", "Karnataka");
		b.setUserValue("country", "India");
		const out = b.buildBilling();
		expect(out.city).toBe("Bengaluru");
		expect(out.state).toBe("Karnataka");
		expect(out.country).toBe("India");
	});

	it("persists and restores state + country for the same site/user", () => {
		const b1 = useBillingDetails({ site: "sc2", user: "uc2" });
		b1.setUserValue("state", "Tamil Nadu");
		b1.setUserValue("country", "India");
		const b2 = useBillingDetails({ site: "sc2", user: "uc2" });
		expect(b2.restore()).toBe(true);
		expect(b2.fields.state.value).toBe("Tamil Nadu");
		expect(b2.fields.country.value).toBe("India");
	});
});

// Review P2: both conversion helpers must hydrate the now-required postal code (and the
// optional second address line), or ERP defaults never prefill pincode and server recovery
// leaves it blank / stale.
describe("address2 + pincode hydrate from ERP defaults and from the server snapshot", () => {
	it("applyDefaults fills address_line2 and pincode", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const resp = {
			ok: true,
			data: {
				company: "Aerele",
				billing_address: {
					address_line1: "12 MG Road",
					address_line2: "Floor 3",
					city: "Chennai",
					pincode: "600001",
				},
			},
		};
		expect(fetchAndApply(b, "Aerele", resp)).toBe("applied");
		expect(b.fields.address2.value).toBe("Floor 3");
		expect(b.fields.pincode.value).toBe("600001");
	});

	it("hydrateServerSnapshot fills address_line2 and pincode", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.hydrateServerSnapshot({
			address_line1: "9 Ring Road",
			address_line2: "Unit 2",
			city: "Delhi",
			pincode: "110001",
		});
		expect(b.fields.address2.value).toBe("Unit 2");
		expect(b.fields.pincode.value).toBe("110001");
	});
});

// Review P2: syncInvoicingDefaults mutated memory only. setIdentity persists the snapshot
// BEFORE the view's watcher calls it, so a reload kept the new identity beside a stale
// invoicing default. It must persist the mirror too.
describe("mirrored invoicing default is persisted, not just held in memory", () => {
	it("persists the synced invoicing company + email so a reload keeps them", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setIdentity("team@example.com", "Aerele"); // persists identity (stale invoicing)
		b.syncInvoicingDefaults("Aerele", "team@example.com");
		const snap = JSON.parse(window.localStorage.getItem(b.storageKey));
		expect(snap.invoice_company_name).toBe("Aerele");
		expect(snap.invoice_email).toBe("team@example.com");
	});

	it("does not clobber a user-overridden invoicing value", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setInvoicing("Custom Billing Co", undefined); // user owns the company field
		b.syncInvoicingDefaults("Aerele", "team@example.com");
		expect(b.invoicing.company_name).toBe("Custom Billing Co"); // untouched
		expect(b.invoicing.email).toBe("team@example.com"); // email still auto-tracks
	});

	it("does not write when nothing changed (idempotent)", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.syncInvoicingDefaults("Aerele", "team@example.com"); // first sync writes
		window.localStorage.removeItem(b.storageKey); // prove a redundant call does not rewrite
		b.syncInvoicingDefaults("Aerele", "team@example.com"); // same values -> no change
		expect(window.localStorage.getItem(b.storageKey)).toBe(null);
	});
});

// Review P1: a country from a resumed snapshot / ERP default bypasses the select, so it is
// canonicalised on the way in and on the way out — never submitting a legacy/alias name.
describe("country is canonicalised from restore and from ERP defaults", () => {
	it("restore canonicalises a legacy country and buildBilling submits the canonical name", () => {
		const key = billingStorageKey("s1", "u1");
		window.localStorage.setItem(
			key,
			JSON.stringify({ country: "Turkey", saved_at: Date.now() })
		);
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.restore()).toBe(true);
		expect(b.fields.country.value).toBe("Türkiye");
		expect(b.buildBilling().country).toBe("Türkiye");
	});

	it("applyDefaults canonicalises a legacy ERP country", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		const resp = {
			ok: true,
			data: { company: "Acme", billing_address: { address_line1: "x", country: "Turkey" } },
		};
		expect(fetchAndApply(b, "Acme", resp)).toBe("applied");
		expect(b.fields.country.value).toBe("Türkiye");
	});
});

// Review P2: persisting the mirrored invoicing default (the earlier fix) must also persist its
// PROVENANCE, or restore() treats the auto-mirrored value as a user override and stops tracking.
describe("invoicing-default provenance survives a reload", () => {
	it("an auto-mirrored invoicing default keeps tracking a later company change after restore", () => {
		const seed = useBillingDetails({ site: "s1", user: "u1" });
		seed.setIdentity("team@example.com", "Acme"); // persist identity
		seed.syncInvoicingDefaults("Acme", "team@example.com"); // auto-mirror (not user-set) + persist
		expect(seed.invoicing.company_name).toBe("Acme");

		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.restore()).toBe(true);
		expect(b.invoicing.company_name).toBe("Acme");
		b.syncInvoicingDefaults("Globex", "team@example.com"); // provenance = auto → must re-mirror
		expect(b.invoicing.company_name).toBe("Globex");
	});

	it("a user-overridden invoicing value stays fixed after restore", () => {
		const seed = useBillingDetails({ site: "s1", user: "u1" });
		seed.setInvoicing("Custom Billing Co", undefined); // user override (user-set) + persist
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.restore()).toBe(true);
		expect(b.invoicing.company_name).toBe("Custom Billing Co");
		b.syncInvoicingDefaults("Globex", "team@example.com"); // must NOT clobber the override
		expect(b.invoicing.company_name).toBe("Custom Billing Co");
	});

	it("a legacy snapshot with no provenance flag is treated as user-set (back-compat)", () => {
		const key = billingStorageKey("s1", "u1");
		window.localStorage.setItem(
			key,
			JSON.stringify({ invoice_company_name: "Legacy Co", saved_at: Date.now() })
		);
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.restore()).toBe(true);
		expect(b.invoicing.company_name).toBe("Legacy Co");
		b.syncInvoicingDefaults("Globex", "team@example.com"); // legacy default = user-set → not clobbered
		expect(b.invoicing.company_name).toBe("Legacy Co");
	});
});
