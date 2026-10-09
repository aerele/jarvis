import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// The phone's DecisionSheet and draft card (pwa/src/components), mounted here as
// PwaPendingCard.spec.js does: the PWA has no component runner of its own.
// PWA components import agentName from "@/branding". In this frontend runner "@" is
// frontend/src, whose branding has no agentName, so give them the PWA's values.
vi.mock("@/branding", () => ({ agentName: "Jarvis", brandLogoUrl: "" }));
vi.mock("../../../pwa/src/api", () => ({
	confirmTool: vi.fn(),
	approveAndRun: vi.fn(),
	dismissTool: vi.fn(),
	applyAction: vi.fn(),
	getDoctypeFormMeta: vi.fn(),
	loadDoc: vi.fn(),
}));

import * as api from "../../../pwa/src/api";
import DecisionSheet from "../../../pwa/src/components/DecisionSheet.vue";
import ActionCard from "../../../pwa/src/components/ActionCard.vue";

const parked = {
	token: "pa-31",
	conversation: "c1",
	tool: "submit_doc",
	summary: "Submit SO-1",
	preview: {},
};

describe("phone DecisionSheet: a failed confirmation shows its reference", () => {
	beforeEach(() => vi.clearAllMocks());

	it("shows the person's words and the reference", async () => {
		api.confirmTool.mockResolvedValue({
			ok: false,
			reason_code: "failed",
			pa_status: "Failed",
			outcome: "failed",
			error: {
				code: "InvalidArgumentError",
				message: "Credit limit crossed",
				reference: "pa-31",
			},
		});
		const w = mount(DecisionSheet, { props: { action: parked }, attachTo: document.body });
		await w
			.findAll("button")
			.find((b) => b.text() === "Approve")
			.trigger("click");
		await flushPromises();
		expect(document.body.textContent).toContain("Credit limit crossed");
		expect(document.body.querySelector(".jv-ref-id").textContent).toBe("pa-31");
		w.unmount();
	});

	it("no reference when the bench sent none", async () => {
		api.confirmTool.mockResolvedValue({ ok: false, error: { message: "Nope" } });
		const w = mount(DecisionSheet, { props: { action: parked }, attachTo: document.body });
		await w
			.findAll("button")
			.find((b) => b.text() === "Approve")
			.trigger("click");
		await flushPromises();
		expect(document.body.textContent).toContain("Nope");
		expect(document.body.querySelector(".jv-ref-id")).toBeNull();
		w.unmount();
	});
});

describe("phone draft card: a failed save (R2-9)", () => {
	const action = {
		verb: "create",
		doctype: "ToDo",
		fields: [{ label: "Description", value: "Call" }],
	};

	beforeEach(() => {
		vi.clearAllMocks();
		api.getDoctypeFormMeta.mockResolvedValue({
			ok: true,
			fields: [{ label: "Description", fieldname: "description" }],
		});
	});

	const mountCard = () =>
		mount(ActionCard, { props: { action, conversation: "c1", messageKey: "m1" } });
	const createButton = (w) => w.findAll("button").find((b) => b.text() === "Create");

	it("says it cannot be edited here, so the assistant is never told the user is fixing it", async () => {
		api.applyAction.mockResolvedValue({
			ok: false,
			error: { message: "Value missing", kind: "fixable" },
		});
		const w = mountCard();
		await createButton(w).trigger("click");
		await flushPromises();
		expect(api.applyAction.mock.calls[0][0].editable).toBe(0);
		expect(w.text()).toContain("Value missing Ask Jarvis to correct it.");
		expect(createButton(w).exists()).toBe(true);
		expect(w.emitted("failed")).toBeUndefined();
	});

	it("a closed failure hides Create and hands over to the chat", async () => {
		api.applyAction.mockResolvedValue({
			ok: false,
			closed: true,
			error: { message: "5 units needed in Stores.", kind: "not_fixable" },
		});
		const w = mountCard();
		await createButton(w).trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Couldn't save this: 5 units needed in Stores.");
		expect(createButton(w)).toBeUndefined();
		expect(w.emitted("failed")).toHaveLength(1);
	});

	it("a closed failure the assistant could not be told about says to ask in the chat", async () => {
		api.applyAction.mockResolvedValue({
			ok: false,
			closed: true,
			assistant_told: false,
			error: { message: "5 units needed in Stores.", kind: "not_fixable" },
		});
		const w = mountCard();
		await createButton(w).trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Jarvis could not be told about it, so ask in the chat.");
		expect(w.text()).not.toContain("explains what to do");
	});
});
