import fs from "node:fs";
import * as Vue from "vue";
import { describe, expect, it, vi } from "vitest";

// Run the actual reverifySite from the SFC at its API boundary (the same approach as the other
// ChatView specs); a browser run covers the mounted banner.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const body = declaration("reverifySite");

function deferred() {
	let resolve, reject;
	const promise = new Promise((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}

function harness({ instanceChanged = false } = {}) {
	const state = {
		canReverifySite: true,
		reverifying: Vue.ref(false),
		reverifyError: Vue.ref(""),
		copyNotice: Vue.ref({ instance_changed: instanceChanged }),
		forgetReady: vi.fn(),
		api: { reverifySite: vi.fn() },
	};
	const run = new Function(...Object.keys(state), `${body}\nreturn reverifySite;`)(
		...Object.values(state)
	);
	return { state, run };
}

describe("Re-verify this site banner action", () => {
	it("shows the loading state on the button for the whole request", () => {
		const banner = source.slice(
			source.indexOf('v-if="copyAlert"'),
			source.indexOf('v-else-if="replacedAlert"')
		);
		expect(banner).toMatch(/<Button[^>]*:loading="reverifying"/s);
		expect(banner).toContain("Re-verify this site");
	});

	it("is in flight until the answer arrives, then clears the banner on a proof", async () => {
		const { state, run } = harness();
		const pending = deferred();
		state.api.reverifySite.mockReturnValue(pending.promise);
		const done = run();
		expect(state.reverifying.value).toBe(true);
		await run();
		expect(state.api.reverifySite).toHaveBeenCalledTimes(1);
		pending.resolve({ proved: true, blocked: false });
		await done;
		expect(state.reverifying.value).toBe(false);
		expect(state.copyNotice.value).toBeNull();
		expect(state.forgetReady).toHaveBeenCalled();
	});

	it("keeps the existing outcome texts and stops loading on every failure", async () => {
		const outcomes = [
			[
				{ proved: true, blocked: false, pin_rebound: false },
				"This site is verified, but Jarvis could not reconnect it yet. Try again in a minute, or contact support if this keeps happening.",
			],
			[
				{ proved: false, error: "unreachable" },
				"Could not reach Jarvis to verify this site. Try again in a minute.",
			],
			[{ proved: false }, "Re-verify did not confirm this site."],
		];
		for (const [res, text] of outcomes) {
			const { state, run } = harness({ instanceChanged: true });
			state.api.reverifySite.mockResolvedValue(res);
			await run();
			expect(state.reverifyError.value).toBe(text);
			expect(state.reverifying.value).toBe(false);
		}
		const { state, run } = harness();
		state.api.reverifySite.mockRejectedValue(new Error("offline"));
		await run();
		expect(state.reverifyError.value).toBe(
			"Could not reach Jarvis to verify this site. Try again in a minute."
		);
		expect(state.reverifying.value).toBe(false);
	});
});
