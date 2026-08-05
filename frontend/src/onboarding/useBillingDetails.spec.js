import { describe, it, expect, beforeEach } from "vitest";
import {
	useBillingDetails,
	billingEditAction,
	billingStorageKey,
	STORAGE_PROMISE_SAVED,
	STORAGE_PROMISE_LOCAL,
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

describe("ack-gated storage promise (P0-01)", () => {
	it("shows the honest local copy until billing_saved: true is observed", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_LOCAL);
		b.markBillingSaved(true);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_SAVED);
	});

	// Mutation guard for the ack gate: only a literal boolean true may flip it.
	// A truthy-but-not-true ack (older admin echoing 1, or a "true" string) must
	// NOT claim persistence.
	it("only a literal true ack flips the promise", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		expect(b.markBillingSaved(1)).toBe(false);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_LOCAL);
		expect(b.markBillingSaved("true")).toBe(false);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_LOCAL);
		expect(b.markBillingSaved(undefined)).toBe(false);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_LOCAL);
		expect(b.markBillingSaved(true)).toBe(true);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_SAVED);
	});

	// The durable ack used to ALSO clear the local snapshot. It no longer does, and
	// that is the fix rather than a regression: the ack arrives from start_signup,
	// which is the call made immediately before the customer is top-level-navigated
	// away to the checkout. Clearing there destroyed the only copy that could
	// survive the round trip, so a customer who came back from a failed payment and
	// pressed Back found an empty form. The promise is now kept at the END of
	// onboarding instead, via finish().
	it("keeps the local snapshot through a durable ack, so a checkout round trip can resume", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("city", "Chennai");
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy();
		b.markBillingSaved(false);
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy(); // kept
		b.markBillingSaved(true);
		expect(b.promiseCopy.value).toBe(STORAGE_PROMISE_SAVED); // the promise still flips
		expect(window.localStorage.getItem(b.storageKey)).toBeTruthy(); // but the copy stays
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

	it("omits blank optional fields from both payload and card", () => {
		const b = useBillingDetails({ site: "s1", user: "u1" });
		b.setUserValue("contact", "+91 90000 00000");
		const payload = b.buildBilling();
		expect(payload).toEqual({ contact_number: "+91 90000 00000" });
		expect(b.reviewRows.value).toEqual([
			{ key: "contact_number", label: "Contact", value: "+91 90000 00000" },
		]);
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
