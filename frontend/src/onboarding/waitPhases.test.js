import test from "node:test";
import assert from "node:assert/strict";

import {
	PHASE_STATE,
	provisioningPhase,
	readinessPhase,
	inFlightPhase,
	connectHeadline,
} from "./waitPhases.js";

// ---- provisioning wait ---------------------------------------------------

test("provisioning: an unanswered poll never claims a phase", () => {
	const p = provisioningPhase({ answered: false });
	assert.equal(p.observed, false);
	assert.equal(p.state, PHASE_STATE.UNKNOWN);
	assert.match(p.label, /couldn't reach your workspace/i);
	assert.doesNotMatch(p.label, /getting ready/i);
});

test("provisioning: an unanswered poll still restates the settled payment fact", () => {
	const p = provisioningPhase({ answered: false });
	assert.match(p.detail, /payment is complete/i);
});

test("provisioning: 'Jarvis is getting ready' requires admin to have answered", () => {
	const answered = provisioningPhase({ answered: true, tenantStatus: "pending" });
	assert.equal(answered.observed, true);
	assert.equal(answered.state, PHASE_STATE.ACTIVE);
	assert.match(answered.label, /getting ready for you/i);

	// The same line must be unreachable without an answer.
	const silent = provisioningPhase({ answered: false, tenantStatus: "pending" });
	assert.doesNotMatch(silent.label, /getting ready for you/i);
});

test("provisioning: running is a completed phase", () => {
	const p = provisioningPhase({ answered: true, tenantStatus: "running" });
	assert.equal(p.state, PHASE_STATE.DONE);
});

test("provisioning: status matching is case and whitespace tolerant", () => {
	assert.equal(
		provisioningPhase({ answered: true, tenantStatus: "  RUNNING " }).state,
		PHASE_STATE.DONE
	);
});

test("provisioning: an unrecognised answered status shares the prepared phase, invents nothing", () => {
	const p = provisioningPhase({ answered: true, tenantStatus: "allocating" });
	assert.equal(p.state, PHASE_STATE.ACTIVE);
	assert.doesNotMatch(p.label, /allocating/i);
});

test("provisioning: an answered poll with no status at all is still not 'running'", () => {
	const p = provisioningPhase({ answered: true, tenantStatus: "" });
	assert.equal(p.state, PHASE_STATE.ACTIVE);
});

// ---- readiness wait ------------------------------------------------------

test("readiness: an unanswered poll never claims a phase", () => {
	const p = readinessPhase({ answered: false });
	assert.equal(p.observed, false);
	assert.equal(p.state, PHASE_STATE.UNKNOWN);
	assert.equal(p.stop, false);
});

test("readiness: pool and direct provisioning both name the apply phase", () => {
	for (const reason of ["llm_pool_provisioning", "llm_provisioning"]) {
		const p = readinessPhase({ answered: true, reason });
		assert.equal(p.state, PHASE_STATE.ACTIVE);
		assert.match(p.label, /applying your AI configuration/i);
	}
});

test("readiness: container_provisioning names the container phase and quotes the detail", () => {
	const p = readinessPhase({
		answered: true,
		reason: "container_provisioning",
		detail: "applying your LLM configuration",
	});
	assert.equal(p.state, PHASE_STATE.ACTIVE);
	assert.match(p.label, /coming online/i);
	assert.equal(p.detail, "applying your LLM configuration");
});

test("readiness: readiness_unconfirmed is the absence of a verdict, never an active phase", () => {
	const p = readinessPhase({ answered: true, reason: "readiness_unconfirmed" });
	assert.equal(p.state, PHASE_STATE.UNKNOWN);
	assert.notEqual(p.state, PHASE_STATE.ACTIVE);
	assert.doesNotMatch(p.label, /coming online|applying/i);
});

test("readiness: authority_repair_required stops the wait, is paged, quotes admin verbatim", () => {
	const detail = "Your payment is safe. Please don't pay again while we sort this out.";
	const p = readinessPhase({ answered: true, reason: "authority_repair_required", detail });
	assert.equal(p.stop, true);
	assert.equal(p.paged, true);
	assert.equal(p.detail, detail);
	assert.notEqual(p.state, PHASE_STATE.ACTIVE);
});

// jarvis/account.py keeps subscription_suspended distinct from
// container_provisioning precisely "so a suspended customer isn't told to wait
// for a container that won't come back". site_replaced can never be resolved by
// waiting either - the account lives on another site now. Both used to fall
// through to the default and render as active progress.
test("readiness: reasons that waiting cannot fix never render as progress", () => {
	for (const reason of ["subscription_suspended", "site_replaced"]) {
		const p = readinessPhase({ answered: true, reason, detail: "why" });
		assert.equal(p.stop, true, reason);
		assert.notEqual(p.state, PHASE_STATE.ACTIVE, reason);
		assert.doesNotMatch(p.label, /coming online|keep (waiting|checking)/i, reason);
		// Nobody was paged for these: the CUSTOMER has to act.
		assert.equal(p.paged, false, reason);
		assert.ok(p.title, reason);
	}
});

test("readiness: only a paged repair claims support was already notified", () => {
	assert.equal(
		readinessPhase({ answered: true, reason: "authority_repair_required" }).paged,
		true
	);
	for (const reason of [
		"subscription_suspended",
		"site_replaced",
		"container_provisioning",
		"readiness_unconfirmed",
		"unmapped",
	]) {
		assert.notEqual(readinessPhase({ answered: true, reason }).paged, true, reason);
	}
});

// The row shown while a wait is genuinely running but nothing has reported yet.
// Seeding "last observation" as answered:false instead made the screen announce
// a FAILED check before one had been attempted.
test("inFlight: names the act of checking, and claims no observation", () => {
	const p = inFlightPhase("Checking on your workspace");
	assert.equal(p.observed, false);
	assert.equal(p.state, PHASE_STATE.ACTIVE);
	assert.equal(p.label, "Checking on your workspace");
	assert.equal(p.stop, false);
	assert.doesNotMatch(p.label, /couldn't reach|could not reach/i);
});

test("readiness: a resolvable wait never stops itself", () => {
	const reasons = [
		"llm_pool_provisioning",
		"llm_provisioning",
		"container_provisioning",
		"readiness_unconfirmed",
		"something_new",
		"",
	];
	for (const reason of reasons) {
		assert.equal(readinessPhase({ answered: true, reason }).stop, false, reason);
	}
	assert.equal(readinessPhase({ answered: false }).stop, false);
});

test("readiness: an unknown reason falls back and never echoes itself as a phase name", () => {
	const p = readinessPhase({ answered: true, reason: "brand_new_reason" });
	assert.equal(p.state, PHASE_STATE.ACTIVE);
	assert.doesNotMatch(p.label, /brand_new_reason/);
});

test("readiness: no branch ever claims setup is finishing on its own", () => {
	const reasons = [
		"llm_pool_provisioning",
		"container_provisioning",
		"readiness_unconfirmed",
		"authority_repair_required",
		"subscription_suspended",
		"site_replaced",
		"unmapped",
	];
	for (const reason of reasons) {
		for (const answered of [true, false]) {
			const p = readinessPhase({ answered, reason });
			assert.doesNotMatch(p.label + " " + p.detail, /finishing on its own/i);
		}
	}
});

// ---- headline ------------------------------------------------------------

test("headline: the readiness ceiling never says setup is still finishing", () => {
	const h = connectHeadline("retry", { fromReadinessCeiling: true });
	assert.match(h, /couldn't confirm/i);
	assert.doesNotMatch(h, /still finishing/i);
});

test("headline: an operation failure reads as a failure, not as an unknown", () => {
	assert.match(connectHeadline("retry", { fromReadinessCeiling: false }), /snag/i);
});

test("headline: superseded and support keep their own meanings", () => {
	assert.match(connectHeadline("superseded"), /workspace changed/i);
	assert.match(connectHeadline("support"), /couldn't finish/i);
});

test("headline: no branch renders the phrase jarvis#709 removed", () => {
	const cases = [
		["retry", { fromReadinessCeiling: true }],
		["retry", { fromReadinessCeiling: false }],
		["superseded", {}],
		["support", {}],
		["", {}],
	];
	for (const [phase, opts] of cases) {
		assert.doesNotMatch(connectHeadline(phase, opts), /still finishing setup/i);
	}
});
