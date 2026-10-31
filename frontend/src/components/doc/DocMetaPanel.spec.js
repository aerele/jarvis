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

// #756: a review-scoped Chat question is tagged through its own endpoints, so the
// generic Assignees / Shared-with blocks are switched off for it. Every other screen
// leaves the props alone and keeps both.
describe("DocMetaPanel generic sharing and assignment", () => {
	const docmeta = (doctype) => ({
		doctype,
		name: "X-1",
		meta: { attachments: [], assignees: [], shares: [] },
	});
	const blocks = (w) => w.text();

	it("shows both blocks by default, as before", () => {
		const w = mount(DocMetaPanel, {
			props: { docmeta: docmeta("Jarvis Approval Request"), canWrite: true },
		});
		expect(blocks(w)).toContain("Assignees");
		expect(blocks(w)).toContain("Shared with");
	});

	it("hides Shared with when sharing is switched off, keeping the rest", () => {
		const w = mount(DocMetaPanel, {
			props: {
				docmeta: docmeta("Jarvis Approval Request"),
				canWrite: true,
				allowShares: false,
			},
		});
		expect(blocks(w)).not.toContain("Shared with");
		expect(blocks(w)).toContain("Assignees");
		expect(blocks(w)).toContain("Attachments");
	});

	it("hides Assignees when assignment is switched off, keeping the rest", () => {
		const w = mount(DocMetaPanel, {
			props: {
				docmeta: docmeta("Jarvis Approval Request"),
				canWrite: true,
				allowAssignees: false,
			},
		});
		expect(blocks(w)).not.toContain("Assignees");
		expect(blocks(w)).toContain("Shared with");
		expect(blocks(w)).toContain("Attachments");
	});

	it("does not fetch the people list for a panel that can share and assign nothing", async () => {
		const { listShareableUsers } = await import("@/api");
		listShareableUsers.mockClear();
		const w = mount(DocMetaPanel, {
			props: {
				docmeta: docmeta("Jarvis Approval Request"),
				canWrite: true,
				allowShares: false,
				allowAssignees: false,
			},
		});
		await flushPromises();
		expect(listShareableUsers).not.toHaveBeenCalled();
		expect(blocks(w)).toContain("Attachments");
	});

	it("still fetches it by default, and other doctypes keep their blocks", async () => {
		const { listShareableUsers } = await import("@/api");
		listShareableUsers.mockClear();
		const w = mount(DocMetaPanel, {
			props: { docmeta: docmeta("Jarvis Macro"), canWrite: true },
		});
		await flushPromises();
		expect(listShareableUsers).toHaveBeenCalledOnce();
		expect(blocks(w)).toContain("Shared with");
	});
});
