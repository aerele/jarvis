// T16 (MAJOR 1 fix, Amendment 2): AppShell renders OnboardingGate IN PLACE OF
// the sidebar + router-view for a disconnected bench (is_ready_for_chat
// returns "signup", same as a first-time workspace), so this gate is the only
// screen a reloaded disconnected bench actually lands on. It must name the
// emailed-code reconnect there instead of the generic first-time-setup
// poster - and a first-time bench must never be told it was disconnected.
//
// Mounted via @vue/test-utils and driven through the exposed <script setup>
// bindings (w.vm.*) rather than DOM clicks - same convention as
// GeneralPane.spec.js in the settings directory.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	benchConnectionState: vi.fn(),
}));
vi.mock("@/api", () => api);

const push = vi.hoisted(() => vi.fn());
vi.mock("vue-router", () => ({ useRouter: () => ({ push }) }));

vi.mock("frappe-ui", () => ({
	// label rendered as text (not a slot) so tests can assert on the CTA copy,
	// matching how OnboardingGate.vue actually invokes <Button :label="...">.
	Button: {
		name: "Button",
		props: ["label"],
		template: '<button @click="$emit(\'click\')">{{ label }}</button>',
	},
}));

import OnboardingGate from "./OnboardingGate.vue";

const STUBS = { JarvisMark: true };

async function mountGate() {
	const w = mount(OnboardingGate, { global: { stubs: STUBS } });
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	window.is_system_manager = false;
	window.is_jarvis_admin = true;
	api.benchConnectionState.mockResolvedValue({ disconnected: false, needs_company: false });
});

describe("not disconnected: unchanged generic first-time-setup copy", () => {
	it("renders the original poster untouched when bench_connection_state reports false", async () => {
		api.benchConnectionState.mockResolvedValue({ disconnected: false, needs_company: false });
		const w = await mountGate();

		expect(w.vm.disconnected).toBe(false);
		expect(w.text()).toContain("Finish setting up Jarvis");
		expect(w.text()).toContain(
			"This workspace isn't connected to an AI agent yet. Complete a short setup to start chatting with Jarvis about your ERPNext data."
		);
		expect(w.text()).toContain("Complete setup");
		expect(w.text()).not.toContain("disconnected");
		expect(w.text()).not.toContain("Reconnect");
	});
});

describe("endpoint throws: unchanged generic copy, never a false disconnected claim", () => {
	it("falls through to the same poster a genuine first-time bench sees", async () => {
		api.benchConnectionState.mockRejectedValue(new Error("network error"));
		const w = await mountGate();

		expect(w.vm.disconnected).toBe(false);
		expect(w.text()).toContain("Finish setting up Jarvis");
		expect(w.text()).toContain("This workspace isn't connected to an AI agent yet.");
		expect(w.text()).not.toContain("disconnected");
		expect(w.text()).not.toContain("Reconnect");
	});
});

describe("disconnected: true names the emailed-code reconnect (admin)", () => {
	it("shows the recovery route instead of the generic poster, with a working CTA", async () => {
		window.is_system_manager = true;
		api.benchConnectionState.mockResolvedValue({ disconnected: true, needs_company: false });
		const w = await mountGate();

		expect(w.vm.disconnected).toBe(true);
		expect(w.text()).toContain("Reconnect Jarvis");
		expect(w.text()).toContain("This workspace still exists");
		expect(w.text()).toContain(
			"Reconnect with the one-time code emailed to this workspace's registered address."
		);
		// The generic first-time copy must not also be present.
		expect(w.text()).not.toContain("isn't connected to an AI agent yet");
		// needs_company is false here, so no company clause.
		expect(w.text()).not.toContain("company name");

		expect(w.text()).toContain("Reconnect");
		// Same route the "Complete setup" CTA already used - the wizard owns
		// the reconnect flow (code entry, company disambiguation).
		await w.findComponent({ name: "Button" }).trigger("click");
		expect(push).toHaveBeenCalledWith({ name: "Onboarding" });
	});
});

describe("needs_company: true adds the company-name clause", () => {
	it("tells the admin they'll also need the company name", async () => {
		window.is_system_manager = true;
		api.benchConnectionState.mockResolvedValue({ disconnected: true, needs_company: true });
		const w = await mountGate();

		expect(w.vm.needsCompany).toBe(true);
		expect(w.text()).toContain(
			"That address is linked to more than one company, so you'll also need to give the company name."
		);
	});

	it("tells a non-admin teammate the same, phrased for relaying it", async () => {
		window.is_system_manager = false;
		window.is_jarvis_admin = false;
		api.benchConnectionState.mockResolvedValue({ disconnected: true, needs_company: true });
		const w = await mountGate();

		expect(w.text()).toContain("Ask your administrator (a System Manager) to reconnect it");
		expect(w.text()).toContain(
			"That address is linked to more than one company, so they'll also need to give the company name."
		);
		// Non-admins never get the CTA button (matches the pre-existing
		// "Complete setup" gating this branch reuses) - only the unconditional
		// "Switch to Desk" plain button remains.
		expect(w.findComponent({ name: "Button" }).exists()).toBe(false);
	});
});
