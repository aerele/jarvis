import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";

// Real @/lib/supportSla (pure). toLocalMs is mocked to a plain parser so no
// frappe-ui/timezone setup is needed; JarvisMark is stubbed (it pulls @/branding).
vi.mock("@/utils/datetime", () => ({
	toLocalMs: (s) => (s == null || s === "" ? null : typeof s === "number" ? s : Date.parse(s)),
}));
vi.mock("frappe-ui", () => ({
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: "<span class='badge' :data-theme='theme'>{{ label }}</span>",
	},
	Button: {
		name: "Button",
		props: ["label"],
		// emits:["click"] so the parent's @click binds to the emitted event only,
		// not ALSO as a native fallthrough listener (which double-fires in jsdom).
		emits: ["click"],
		template: "<button @click=\"$emit('click')\">{{ label }}</button>",
	},
}));

const storeDouble = {
	thread: reactive({ meta: null }),
	badgeFor: (s) => (s ? { label: s, theme: "blue" } : { label: "Open", theme: "blue" }),
	isClosed: (s) => s === "Closed",
};
vi.mock("@/stores/support", () => ({ useSupportStore: () => storeDouble }));

import SupportTicketPanel from "@/components/support/SupportTicketPanel.vue";

const opts = { global: { stubs: { JarvisMark: { template: "<span class='mark'/>" } } } };
const mountPanel = () => mount(SupportTicketPanel, opts);
const badges = (w) => w.findAll(".badge");

// Deterministic SLA (all past dates), so the badges don't depend on wall-clock:
// first_responded_on (10:05) < response_by (11:00) => "Fulfilled in 5m"; resolution
// past + agreement Failed => "Failed".
const FULL_META = {
	name: "TCK-1",
	status: "Open",
	subject: "Login broken",
	agent_group: "Billing",
	priority: "High",
	creation: "2020-01-01 10:00:00",
	first_responded_on: "2020-01-01 10:05:00",
	response_by: "2020-01-01 11:00:00",
	resolution_by: "2020-01-01 12:00:00",
	resolution_date: null,
	agreement_status: "Failed",
};

beforeEach(() => {
	storeDouble.thread.meta = null;
});

describe("SupportTicketPanel — populated", () => {
	beforeEach(() => {
		storeDouble.thread.meta = { ...FULL_META };
	});

	it("shows the 'Aerele Support' counterparty (not the requester's own identity)", () => {
		const w = mountPanel();
		expect(w.text()).toContain("Ticket details");
		expect(w.text()).toContain("Aerele Support");
		expect(w.find(".mark").exists()).toBe(true);
	});

	it("renders Ticket ID / Subject / Team / Priority from meta", () => {
		const w = mountPanel();
		expect(w.text()).toContain("TCK-1");
		expect(w.text()).toContain("Login broken");
		expect(w.text()).toContain("Billing");
		expect(w.text()).toContain("High");
	});

	it("renders Status + both SLA badges (wiring meta -> supportSla)", () => {
		const w = mountPanel();
		const labels = badges(w).map((b) => b.text());
		expect(labels).toContain("Open"); // status
		expect(labels).toContain("Fulfilled in 5m"); // first response
		expect(labels).toContain("Failed"); // resolution
		const fr = badges(w).find((b) => b.text() === "Fulfilled in 5m");
		expect(fr.attributes("data-theme")).toBe("green");
		const res = badges(w).find((b) => b.text() === "Failed");
		expect(res.attributes("data-theme")).toBe("red");
	});

	it("emits open when Reply is clicked (ticket is not closed)", async () => {
		const w = mountPanel();
		const reply = w.findAll("button").find((b) => b.text() === "Reply");
		expect(reply).toBeTruthy();
		await reply.trigger("click");
		expect(w.emitted("open")).toHaveLength(1);
	});

	it("hides Reply on a closed ticket", () => {
		storeDouble.thread.meta = { ...FULL_META, status: "Closed" };
		const w = mountPanel();
		expect(w.findAll("button").some((b) => b.text() === "Reply")).toBe(false);
	});

	it("wires agreement_status through: a Paused ticket reads On hold, never Failed", () => {
		// Pins that the panel forwards agreement_status to BOTH SLA badges (first
		// response gained it in the High-2 fix), so an awaiting-customer ticket is
		// never blamed with a red Failed.
		storeDouble.thread.meta = {
			...FULL_META,
			agreement_status: "Paused",
			first_responded_on: null,
			resolution_date: null,
		};
		const w = mountPanel();
		const labels = badges(w).map((b) => b.text());
		expect(labels).toContain("On hold");
		expect(labels).not.toContain("Failed");
	});

	it("clears the 60s SLA refresh interval on unmount", () => {
		const spy = vi.spyOn(global, "clearInterval");
		mountPanel().unmount();
		expect(spy).toHaveBeenCalled();
		spy.mockRestore();
	});
});

describe("SupportTicketPanel — no meta yet", () => {
	it("falls back to - for every field and shows no Reply / no SLA badge", () => {
		storeDouble.thread.meta = null;
		const w = mountPanel();
		expect(w.text()).toContain("Ticket details");
		expect(w.text()).toContain("-");
		expect(w.findAll("button").some((b) => b.text() === "Reply")).toBe(false);
		expect(badges(w).length).toBe(0); // no status/SLA badges without data
	});

	it("shows - for a missing individual field (e.g. Team) while others render", () => {
		storeDouble.thread.meta = { ...FULL_META, agent_group: null };
		const w = mountPanel();
		expect(w.text()).toContain("High"); // priority still there
		// Team row falls back to -
		const teamRow = w.findAll(".jv-suptp-row").find((r) => r.text().includes("Team"));
		expect(teamRow.text()).toContain("-");
	});
});
