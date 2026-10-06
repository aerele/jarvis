import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import SendRecoveryCard from "../../../pwa/src/components/SendRecoveryCard.vue";
import { createSendRecovery } from "../../../pwa/src/lib/sendRecovery";

let wrapper;
beforeEach(() => {
	HTMLDialogElement.prototype.showModal = function () {
		this.setAttribute("open", "");
	};
	HTMLDialogElement.prototype.close = function () {
		this.removeAttribute("open");
	};
});
afterEach(() => {
	wrapper?.unmount();
	document.body.innerHTML = "";
});
function mountCard(state = "rejected") {
	const memory = { requests: [] };
	const recovery = createSendRecovery(memory, () => "request");
	const request = recovery.stage("A", "Read this invoice", [
		{ name: "invoice.pdf", file_url: "/private/files/invoice.pdf" },
	]);
	request.state = state;
	wrapper = mount(SendRecoveryCard, { attachTo: document.body, props: { request } });
	return request;
}
function button(label) {
	return wrapper.findAll("button").find((b) => b.text() === label);
}

describe("PWA recovery card", () => {
	it("keeps text and files visible and emits a retry without changing the request", async () => {
		const request = mountCard();
		expect(wrapper.text()).toContain("Read this invoice");
		expect(wrapper.get("a").attributes("href")).toBe("/private/files/invoice.pdf");
		await button("Retry").trigger("click");
		expect(wrapper.emitted("retry")).toHaveLength(1);
		expect(request.attachments).toHaveLength(1);
	});
	it("only offers a read-only delivery check for an uncertain request", async () => {
		mountCard("uncertain");
		expect(button("Retry")).toBeUndefined();
		expect(button("Edit")).toBeUndefined();
		expect(button("Discard")).toBeUndefined();
		await button("Check delivery").trigger("click");
		expect(wrapper.emitted("check")).toHaveLength(1);
		expect(wrapper.emitted("retry")).toBeUndefined();
	});
	it("opens a named separate editor with retained attachments and restores focus on cancel", async () => {
		mountCard();
		const edit = button("Edit");
		edit.element.focus();
		await edit.trigger("click");
		await nextTick();
		expect(wrapper.get("dialog").attributes("open")).toBeDefined();
		expect(wrapper.get("dialog").attributes("aria-label")).toBe("Edit unsent message");
		expect(document.activeElement).toBe(wrapper.get("textarea").element);
		expect(wrapper.get("dialog").text()).toContain("invoice.pdf");
		await wrapper.get("textarea").setValue("Revised request");
		await wrapper.get("dialog").trigger("cancel");
		expect(wrapper.emitted("edit")).toBeUndefined();
		expect(document.activeElement).toBe(edit.element);
	});
	it("allows attachment-only edited requests and requires confirmation to discard", async () => {
		mountCard();
		await button("Edit").trigger("click");
		await flushPromises();
		await wrapper.get("textarea").setValue("");
		await wrapper.get("form").trigger("submit");
		expect(wrapper.emitted("edit")[0]).toEqual([""]);
		await button("Discard").trigger("click");
		await flushPromises();
		expect(wrapper.emitted("discard")).toBeUndefined();
		await button("Discard request").trigger("click");
		expect(wrapper.emitted("discard")).toHaveLength(1);
	});
});

it("wraps Tab and Shift-Tab inside the editor", async () => {
	mountCard();
	await button("Edit").trigger("click");
	await flushPromises();
	const first = wrapper.get("textarea");
	const last = button("Save changes");
	last.element.focus();
	await last.trigger("keydown", { key: "Tab" });
	expect(document.activeElement).toBe(first.element);
	await first.trigger("keydown", { key: "Tab", shiftKey: true });
	expect(document.activeElement).toBe(last.element);
});

it("still permits editing preserved work while retry is disabled by maintenance", async () => {
	mountCard();
	await wrapper.setProps({ disabled: true });
	expect(button("Retry").attributes("disabled")).toBeDefined();
	await button("Edit").trigger("click");
	await flushPromises();
	expect(wrapper.get("dialog").attributes("open")).toBeDefined();
});
