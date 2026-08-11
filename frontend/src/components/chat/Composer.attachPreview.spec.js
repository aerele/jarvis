import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Composer from "@/components/chat/Composer.vue";

/**
 * Plan 2026-08-11-chat-attachment-preview, T2 (SPA composer):
 * a pending attachment the user can PREVIEW. The composer emits
 * `preview-attachment` when an uploaded image thumbnail or file chip is clicked,
 * but never for an in-flight/failed entry (no file_url), and the remove `×` must
 * remove WITHOUT also previewing (edge cases 2, 3, 4).
 */

const IMG = {
	key: "img",
	file_name: "a.png",
	preview_url: "/private/files/a.png",
	file_url: "/private/files/a.png",
	removable: true,
};
const PDF = {
	key: "pdf",
	file_name: "b.pdf",
	preview_url: "",
	file_url: "/private/files/b.pdf",
	removable: true,
};
const UPLOADING = { key: "up", uploading: true, file_name: "c.pdf" };
const FAILED = { key: "fail", failed: true, file_name: "d.pdf", error: "nope", removable: true };

function makeWrapper(attachments) {
	return mount(Composer, { props: { modelValue: "", attachments } });
}

describe("Composer pending-attachment preview", () => {
	it("emits preview-attachment with the image when its thumbnail is clicked", async () => {
		const w = makeWrapper([IMG]);
		await w.get('[title="Preview a.png"]').trigger("click");
		const ev = w.emitted("preview-attachment");
		expect(ev).toHaveLength(1);
		expect(ev[0][0]).toMatchObject({ file_url: "/private/files/a.png", file_name: "a.png" });
	});

	it("emits preview-attachment with the file when a file chip is clicked", async () => {
		const w = makeWrapper([PDF]);
		await w.get('[title="Preview b.pdf"]').trigger("click");
		const ev = w.emitted("preview-attachment");
		expect(ev).toHaveLength(1);
		expect(ev[0][0]).toMatchObject({ file_url: "/private/files/b.pdf", file_name: "b.pdf" });
	});

	it("removing a chip emits remove-attachment and NOT preview-attachment", async () => {
		const w = makeWrapper([PDF]);
		// The remove × is a sibling of the label button, not nested inside it.
		await w.get('[title="Remove"]').trigger("click");
		expect(w.emitted("remove-attachment")).toHaveLength(1);
		// index 0 = the only entry
		expect(w.emitted("remove-attachment")[0][0]).toBe(0);
		expect(w.emitted("preview-attachment")).toBeUndefined();
	});

	it("preview triggers are real <button>s so they are keyboard-focusable", () => {
		const w = makeWrapper([IMG, PDF]);
		// A native button is focusable and activates on Enter/Space with no extra
		// wiring - this is the accessibility contract (plan edge 15).
		expect(w.get('[title="Preview a.png"]').element.tagName).toBe("BUTTON");
		expect(w.get('[title="Preview b.pdf"]').element.tagName).toBe("BUTTON");
	});

	it("does not render a preview trigger for in-flight or failed entries", () => {
		const w = makeWrapper([UPLOADING, FAILED]);
		// Neither an uploading pill nor a failure chip carries a "Preview …" title,
		// so there is nothing clickable to open a preview (no file_url yet).
		expect(w.find('[title^="Preview "]').exists()).toBe(false);
	});
});
