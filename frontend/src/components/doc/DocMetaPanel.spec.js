import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

// #674: the attachment picker takes several files, and a failed file is named in its toast.
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
const upload = vi.hoisted(() => vi.fn());
vi.mock("frappe-ui", () => ({
	Avatar: { template: "<span/>" },
	Autocomplete: { template: "<div/>" },
	Button: { template: "<button><slot/></button>" },
	FeatherIcon: { template: "<i/>" },
	FileUploadHandler: class {
		upload(file, args) {
			return upload(file, args);
		}
	},
	Popover: { template: "<div/>" },
	Tooltip: { template: "<div><slot/></div>" },
	confirmDialog: vi.fn(),
	toast,
}));
vi.mock("@/api", () => ({ listShareableUsers: vi.fn(async () => []) }));

import DocMetaPanel from "./DocMetaPanel.vue";

describe("DocMetaPanel attachments", () => {
	it("names the failed file in its toast, and the next file still uploads", async () => {
		const docmeta = {
			doctype: "Purchase Invoice",
			name: "ACC-PINV-0001",
			meta: { attachments: [], assignees: [], shares: [] },
			afterUpload: vi.fn(),
		};
		upload.mockImplementation(async (file) => {
			if (file.name.endsWith("big.pdf")) {
				throw new Error("File size exceeded the maximum allowed size of 25 MB");
			}
			return { name: "F-1", file_name: file.name };
		});
		const w = mount(DocMetaPanel, { props: { docmeta, canWrite: true } });
		const input = w.find('input[type="file"]');
		expect(input.attributes("multiple")).toBeDefined();
		Object.defineProperty(input.element, "files", {
			value: [new File(["x"], "<b>big.pdf"), new File(["y"], "ok.pdf")],
		});
		await input.trigger("change");
		await flushPromises();
		// the toast renders HTML, so the name is escaped
		expect(toast.error).toHaveBeenCalledWith(
			"&lt;b&gt;big.pdf: File size exceeded the maximum allowed size of 25 MB"
		);
		expect(docmeta.afterUpload).toHaveBeenCalledWith({ name: "F-1", file_name: "ok.pdf" });
	});
});
