// plan-09 WS7, the strongest form of the NO-FALLBACK guarantee: the onboarding
// payment path imports NO gateway-SDK module at all. A raw-handles-without-token
// answer therefore cannot reach an SDK import BY CONSTRUCTION - there is no import
// to reach - so the mutation "make the raw-handles branch open a sheet" is
// impossible to write against these files: the opener simply is not in scope.
//
// This is a static, repo-level import check over the onboarding source paths. If a
// future edit re-introduces `useRazorpay` / `useCashfree` / `billingCheckout` /
// the retired `onboardingCheckout` dispatcher into any onboarding module, this
// fails - which is exactly the regression the owner's no-fallback decision forbids.
//
// plan-09 WS8: BILLING is now cut over too. The three shared gateway-SDK loaders
// (`useRazorpay` / `useCashfree` / `billingCheckout`) are DELETED, BillingPage
// top-level-navigates to the admin-hosted pay page, and the checks below extend
// the same by-construction guarantee to the billing source paths AND assert the
// loaders no longer exist anywhere in the tree. A repo-wide scan also forbids any
// module from loading a gateway SDK by URL. Together these are the no-tenant-origin
// -sheet guarantee for the WHOLE app (onboarding + billing).

import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const VIEWS = join(HERE, "..", "views");
const SRC = join(HERE, "..");
const BILLING = join(SRC, "pages", "billing");

// Every SDK-opener module onboarding must never pull in again.
const FORBIDDEN = [
	"useRazorpay",
	"useCashfree",
	"billingCheckout",
	"onboardingCheckout", // the retired onboarding-only SDK dispatcher (deleted)
];

// The onboarding payment code paths: every module under src/onboarding, plus the
// one view that hosts the pay step.
function onboardingFiles() {
	const files = readdirSync(HERE)
		.filter((f) => f.endsWith(".js") && !f.endsWith(".test.js") && !f.endsWith(".spec.js"))
		.map((f) => join(HERE, f));
	files.push(join(VIEWS, "OnboardingView.vue"));
	return files;
}

// Match a real ES import of a forbidden module, in any of the forms the repo uses:
//   import ... from "@/lib/useRazorpay"        (aliased)
//   import ... from "./onboardingCheckout"      (relative)
//   import("@/lib/useCashfree")                 (dynamic)
// Deliberately NOT a bare substring test: the WORD "useRazorpay" appears in
// explanatory comments (e.g. paymentMachine's history), and a comment is not an
// import. We only flag it inside a from-"…"/import("…") specifier.
function importsForbidden(source) {
	const hits = [];
	const specifiers = [];
	const reFrom = /\bfrom\s*["']([^"']+)["']/g;
	const reDyn = /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;
	let m;
	while ((m = reFrom.exec(source))) specifiers.push(m[1]);
	while ((m = reDyn.exec(source))) specifiers.push(m[1]);
	for (const spec of specifiers) {
		for (const bad of FORBIDDEN) {
			// Match the module basename in the specifier path (…/useRazorpay,
			// …/useRazorpay.js), not an incidental substring of an unrelated path.
			const re = new RegExp(`(^|/)${bad}(\\.js)?$`);
			if (re.test(spec)) hits.push(`${spec} (${bad})`);
		}
	}
	return hits;
}

test("no onboarding payment module imports a gateway SDK opener (no-fallback, by construction)", () => {
	for (const file of onboardingFiles()) {
		const src = readFileSync(file, "utf8");
		const hits = importsForbidden(src);
		assert.equal(hits.length, 0, `${file} imports a forbidden SDK opener: ${hits.join(", ")}`);
	}
});

test("the retired onboardingCheckout dispatcher is gone", () => {
	assert.throws(
		() => readFileSync(join(HERE, "onboardingCheckout.js"), "utf8"),
		/ENOENT/,
		"onboardingCheckout.js must be deleted, not left dormant"
	);
});

// A sanity check on the detector itself: it must FLAG a real forbidden import and
// must NOT flag the same word appearing only in a comment. Without this the test
// above could pass by silently matching nothing.
test("the import detector flags a real import but not a comment mention", () => {
	assert.deepEqual(importsForbidden(`import { openCheckout } from "@/lib/useRazorpay";`), [
		"@/lib/useRazorpay (useRazorpay)",
	]);
	assert.deepEqual(importsForbidden(`// useRazorpay used to open the sheet here`), []);
	assert.deepEqual(importsForbidden(`import { foo } from "@/onboarding/paymentMachine";`), []);
});

// --------------------------------------------------------------------------- //
// plan-09 WS8: the same guarantee, extended to BILLING.
// --------------------------------------------------------------------------- //

// Every .vue/.js under src/pages/billing - BillingPage and its components.
function billingFiles() {
	return readdirSync(BILLING)
		.filter(
			(f) =>
				(f.endsWith(".vue") || f.endsWith(".js")) &&
				!f.endsWith(".test.js") &&
				!f.endsWith(".spec.js")
		)
		.map((f) => join(BILLING, f));
}

test("no billing module imports a gateway SDK opener (no-fallback, by construction)", () => {
	for (const file of billingFiles()) {
		const src = readFileSync(file, "utf8");
		const hits = importsForbidden(src);
		assert.equal(hits.length, 0, `${file} imports a forbidden SDK opener: ${hits.join(", ")}`);
	}
});

test("the tenant-origin gateway SDK loaders are deleted, not left dormant", () => {
	for (const bad of ["useRazorpay", "useCashfree", "billingCheckout"]) {
		const p = join(SRC, "lib", `${bad}.js`);
		assert.equal(
			existsSync(p),
			false,
			`${p} must be deleted (WS8): the tenant-origin sheet has no remaining code to open`
		);
	}
});

// The strongest form for the WHOLE app: no source module loads a gateway SDK by
// URL either (a dynamic <script> injection would bypass the import check above).
const SDK_URL_MARKERS = ["checkout.razorpay.com", "sdk.cashfree.com", "cashfree.js"];

function allSourceFiles() {
	// Node 24: recursive readdir. Scan every .vue/.js under src, excluding tests.
	return readdirSync(SRC, { recursive: true })
		.map((f) => String(f))
		.filter(
			(f) =>
				(f.endsWith(".vue") || f.endsWith(".js")) &&
				!f.endsWith(".test.js") &&
				!f.endsWith(".spec.js")
		)
		.map((f) => join(SRC, f));
}

test("no source module loads a gateway SDK by URL", () => {
	for (const file of allSourceFiles()) {
		const src = readFileSync(file, "utf8");
		for (const marker of SDK_URL_MARKERS) {
			assert.equal(
				src.includes(marker),
				false,
				`${file} references a gateway SDK URL (${marker}); no tenant-origin SDK may load`
			);
		}
	}
});
