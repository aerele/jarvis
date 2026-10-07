import { describe, expect, it } from "vitest";
import { mount, RouterLinkStub } from "@vue/test-utils";

import FileBoxWaits from "./FileBoxWaits.vue";

const QUESTION = {
	src: "ar",
	name: "AR-1",
	title: "Which supplier?",
	question: "Which supplier sent this bill?",
	options: ["Acme", "Globex"],
	link: "/approvals/AR-1",
};
const HELD = {
	src: "pa",
	name: "PA-1",
	title: "Create Supplier Acme",
	link: "/approvals?held=PA-1",
};

function mountWith(items, busy = "") {
	return mount(FileBoxWaits, {
		props: { items, busy },
		global: { stubs: { RouterLink: RouterLinkStub } },
	});
}

describe("FileBoxWaits", () => {
	it("shows a File Box question with its options and a link to the board", () => {
		const w = mountWith([QUESTION]);
		expect(w.text()).toContain("Which supplier sent this bill?");
		expect(w.findAll(".jv-fbw-opt").map((b) => b.text())).toEqual(["Acme", "Globex"]);
		expect(w.findComponent(RouterLinkStub).props("to")).toBe("/approvals/AR-1");
	});

	it("answers with the picked option, as the board does", async () => {
		const w = mountWith([QUESTION]);
		const send = w.find(".jv-fbw-send");
		expect(send.attributes("disabled")).toBeDefined(); // nothing picked or typed yet
		await w.findAll(".jv-fbw-opt")[1].trigger("click");
		await send.trigger("click");
		expect(w.emitted("decide")[0]).toEqual([QUESTION, "Globex", 1]);
	});

	it("a question without options is answered with the typed text", async () => {
		const w = mountWith([{ ...QUESTION, options: [] }]);
		await w.find(".jv-fbw-field").setValue("  It is from Acme  ");
		await w.find(".jv-fbw-send").trigger("click");
		expect(w.emitted("decide")[0][1]).toBe("It is from Acme");
	});

	it("Reject sends the note, else 'Rejected'", async () => {
		const w = mountWith([QUESTION]);
		await w.find(".jv-fbw-reject").trigger("click");
		expect(w.emitted("decide")[0].slice(1)).toEqual(["Rejected", 0]);
	});

	it("a held record or sheet links to the board for review", () => {
		const w = mountWith([
			HELD,
			{
				...HELD,
				src: "sheet",
				name: "SH-1",
				title: "3 records",
				link: "/approvals?held=SH-1",
			},
		]);
		expect(w.text()).toContain("Waiting for approval: Create Supplier Acme");
		expect(w.text()).toContain("Approval sheet: 3 records");
		expect(w.findAllComponents(RouterLinkStub).map((l) => l.props("to"))).toEqual([
			"/approvals?held=PA-1",
			"/approvals?held=SH-1",
		]);
	});

	it("the item being decided is locked", () => {
		const w = mountWith([QUESTION], "AR-1");
		expect(w.find(".jv-fbw-reject").attributes("disabled")).toBeDefined();
		expect(w.find(".jv-fbw-opt").attributes("disabled")).toBeDefined();
	});
});
