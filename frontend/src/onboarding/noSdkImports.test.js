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
// (The shared libs themselves stay in the tree for BillingPage until WS8; this
// only forbids ONBOARDING from importing them.)

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const VIEWS = join(HERE, "..", "views");

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
