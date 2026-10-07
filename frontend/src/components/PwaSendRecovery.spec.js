import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import SendRecoveryCard from "../../../pwa/src/components/SendRecoveryCard.vue";
import { createSendRecovery } from "../../../pwa/src/lib/sendRecovery";

let wrapper;
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
	it("offers explicit recovery choices without automatically resending an uncertain request", async () => {
		mountCard("uncertain");
		expect(button("Retry")).toBeUndefined();
		expect(button("Edit")).toBeUndefined();
		expect(button("Discard")).toBeDefined();
		expect(button("Edit as new message")).toBeDefined();
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
		expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
		expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe(
			"Edit unsent message"
		);
		expect(document.activeElement).toBe(wrapper.get("textarea").element);
		expect(wrapper.get('[role="dialog"]').text()).toContain("invoice.pdf");
		await wrapper.get("textarea").setValue("Revised request");
		await wrapper.get('[role="dialog"]').trigger("keydown", { key: "Escape" });
		await flushPromises();
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
	expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
});

it("warns before deliberately moving an unknown request to the composer", async () => {
	mountCard("uncertain");
	await button("Edit as new message").trigger("click");
	await flushPromises();
	expect(wrapper.get('[role="dialog"]').text()).toContain("could duplicate work");
	expect(wrapper.emitted("edit-new")).toBeUndefined();
	await button("Move to composer").trigger("click");
	expect(wrapper.emitted("edit-new")).toHaveLength(1);
	expect(wrapper.emitted("retry")).toBeUndefined();
});
it("unknown discard explains it cannot cancel previously executed work", async () => {
	mountCard("uncertain");
	await button("Discard").trigger("click");
	await flushPromises();
	expect(wrapper.get('[role="dialog"]').text()).toContain("does not cancel work");
	await button("Discard request").trigger("click");
	expect(wrapper.emitted("discard")).toHaveLength(1);
});
it("release-update rejection offers an explicit reload and backup instead of retry", async () => {
	const request = mountCard();
	const backup = vi.fn();
	await wrapper.setProps({
		request: { ...request, result: { reason: "release_update_required" } },
		backup,
	});
	expect(button("Retry")).toBeUndefined();
	await button("Reload").trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("including other conversations");
	expect(wrapper.emitted("reload")).toBeUndefined();
	await button("Download backup").trigger("click");
	expect(backup).toHaveBeenCalledOnce();
	await button("Reload now").trigger("click");
	expect(wrapper.emitted("reload")).toHaveLength(1);
	expect(wrapper.emitted("retry")).toBeUndefined();
});
