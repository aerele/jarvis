// The mobile app and the Desk widget cannot import frontend/src, so the send-refusal sentences
// live twice: here (sendRejectionCopy.js) and in jarvis/public/js/shared/chat_failure_copy.mjs.
// This fails the moment the two say different things for the same refusal.
import test from "node:test";
import assert from "node:assert/strict";
import { sendRejectionCopy } from "./sendRejectionCopy.js";
import { refusalCopy } from "../../../jarvis/public/js/shared/chat_failure_copy.mjs";

test("desktop and mobile/widget give the same sentence for every shared refusal", () => {
	const cases = [
		["maintenance", {}],
		["usage_limit", {}],
		["usage_limit", { limit_period: "Daily" }],
		["usage_limit", { limit_period: "Weekly" }],
		["usage_limit", { limit_period: "Monthly" }],
		["llm_not_configured", {}],
		["workspace_resetting", {}],
		["subscription_suspended", {}],
	];
	for (const [reason, envelope] of cases) {
		const shared = refusalCopy(reason, "Jarvis", envelope);
		assert.ok(shared, `${reason}: missing from the shared table`);
		assert.equal(sendRejectionCopy(reason, "Jarvis", envelope).message, shared, reason);
	}
});
