import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Message from "@/components/chat/Message.vue";

/**
 * Plan 2026-08-11-chat-attachment-preview, T3 (SPA sent message):
 * a non-image attachment on a sent user bubble is a file chip that PREVIEWS in
 * the artifact panel on a left-click (emits `open-attachment`) while KEEPING its
 * href, so right-click-save / middle-click-new-tab still work (edge 7).
 */

const PDF = { name: "1", type: "pdf", file_url: "/private/files/b.pdf", title: "b.pdf" };
const IMG = { name: "2", type: "image", file_url: "/private/files/a.png", title: "a.png" };

describe("Message bubble attachment chip", () => {
	it("left-click on a file chip emits open-attachment and keeps the href", async () => {
		const w = mount(Message, {
			props: { variant: "bubble", text: "see this", attachments: [PDF] },
		});
		const chip = w.get('a[title="Preview b.pdf"]');
		// href preserved for right/middle-click open/download
		expect(chip.attributes("href")).toBe("/private/files/b.pdf");
		await chip.trigger("click");
		const ev = w.emitted("open-attachment");
		expect(ev).toHaveLength(1);
		expect(ev[0][0]).toMatchObject({ type: "pdf", file_url: "/private/files/b.pdf" });
	});

	it("an image attachment stays an inline thumbnail button (not the file chip)", async () => {
		const w = mount(Message, { props: { variant: "bubble", text: "", attachments: [IMG] } });
		// image → clickable thumbnail button that emits open-attachment
		expect(w.find("button.jv-img-artifact").exists()).toBe(true);
		await w.get("button.jv-img-artifact").trigger("click");
		expect(w.emitted("open-attachment")[0][0]).toMatchObject({ type: "image" });
	});
});
