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

it("gives each delivery state its own tone instead of one error colour", () => {
	const tone = (props) => {
		const w = mount(Message, { props: { text: "Invoice", ...props } });
		const classes = w.get(".jv-delivery").classes();
		w.unmount();
		return classes.find((c) => c.startsWith("jv-delivery--"));
	};
	expect(tone({ failed: true })).toBe("jv-delivery--error");
	expect(tone({ failed: true, deliveryState: "rejected" })).toBe("jv-delivery--error");
	expect(tone({ failed: true, deliveryState: "uncertain" })).toBe("jv-delivery--warn");
	expect(tone({ deliveryState: "checking" })).toBe("jv-delivery--busy");
	expect(tone({ deliveryState: "delivered" })).toBe("jv-delivery--ok");
});

it("does not repeat the title inside the generic note", () => {
	const w = mount(Message, {
		props: {
			text: "Invoice",
			failed: true,
			deliveryState: "uncertain",
			deliveryNote: "Delivery not confirmed. Check delivery before sending again.",
		},
	});
	expect(w.get(".jv-delivery-title").text()).toBe("Delivery not confirmed");
	expect(w.get(".jv-delivery-note").text()).toBe("Check delivery before sending again.");
	w.unmount();
});

it("links a confirmed delivery to its conversation and offers no retry", () => {
	const w = mount(Message, {
		props: {
			text: "Invoice",
			deliveryState: "delivered",
			deliveryNote: "Delivery confirmed. View the conversation for the current result.",
			deliveryConversation: "conv 1",
		},
	});
	expect(w.get(".jv-delivery-note").text()).toBe(
		"View the conversation for the current result."
	);
	expect(w.get("a").attributes("href")).toBe("/jarvis/c/conv%201");
	const labels = w.findAll("button").map((b) => b.text());
	expect(labels).not.toContain("Retry");
	expect(labels).toContain("Dismiss");
	w.unmount();
});
