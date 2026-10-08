import { it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Message from "./Message.vue";
it("offers a check rather than retry for an unknown delivery", async () => {
	const w = mount(Message, {
		props: { text: "Invoice", failed: true, deliveryState: "uncertain" },
	});
	expect(w.text()).toContain("Delivery not confirmed");
	expect(w.text()).not.toContain("Not sent");
	const check = w.findAll("button").find((b) => b.text() === "Check delivery");
	await check.trigger("click");
	expect(w.emitted("retry")).toHaveLength(1);
	w.unmount();
});
it("announces the pending read and disables repeated checks", () => {
	const w = mount(Message, { props: { text: "Invoice", deliveryState: "checking" } });
	expect(w.get('[role="status"]').text()).toBe("Checking delivery…");
	expect(
		w
			.findAll("button")
			.find((b) => b.text() === "Check delivery")
			.attributes("disabled")
	).toBeDefined();
	w.unmount();
});

it("renders interrupted-delivery guidance as text and retains safe recovery controls", async () => {
	const note =
		"Processing was interrupted after work may have started. Check action receipts. <script>unsafe</script>";
	const w = mount(Message, {
		props: { text: "Invoice", failed: true, deliveryState: "uncertain", deliveryNote: note },
	});
	expect(w.text()).toContain(note);
	expect(w.find("script").exists()).toBe(false);
	await w
		.findAll("button")
		.find((b) => b.text() === "Retry same request")
		.trigger("click");
	expect(w.emitted("retry-same")).toHaveLength(1);
	w.unmount();
});
