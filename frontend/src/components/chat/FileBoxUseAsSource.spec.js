import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FileBoxUseAsSource from "./FileBoxUseAsSource.vue";

const api = vi.hoisted(() => ({ getFileBoxSourceState: vi.fn(), setFileBoxSource: vi.fn() }));
vi.mock("@/api/fileboxSource", () => api);
vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "disabled", "loading"],
		template: '<button :disabled="disabled || loading">{{ label }}</button>',
	},
}));

const original = {
	ok: true,
	status: "ok",
	version: "",
	source: {
		name: "original",
		file_url: "/private/files/original.docx",
		file_name: "original.docx",
	},
};
const selected = {
	ok: true,
	status: "ok",
	version: "pdf",
	source: {
		name: "pdf",
		file_url: "/private/files/replacement.pdf",
		file_name: "replacement.pdf",
	},
};
const props = {
	conversation: "A",
	message: "message-A",
	attachment: { name: "item-A", file_url: "/private/files/replacement.pdf" },
};
const Button = {
	props: ["label", "disabled", "loading"],
	template: '<button :disabled="disabled || loading">{{ label }}</button>',
};
const create = () => mount(FileBoxUseAsSource, { props, global: { stubs: { Button } } });
const button = (wrapper, text) => wrapper.findAll("button").find((b) => b.text() === text);
const deferred = () => {
	let resolve;
	const promise = new Promise((r) => {
		resolve = r;
	});
	return { promise, resolve };
};

describe("File Box attachment source", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.getFileBoxSourceState.mockResolvedValue(original);
		api.setFileBoxSource.mockResolvedValue(selected);
	});
	it("opening an attachment only reads; an explicit click selects the exact attachment", async () => {
		const w = create();
		await flushPromises();
		expect(api.setFileBoxSource).not.toHaveBeenCalled();
		await button(w, "Use as source").trigger("click");
		await flushPromises();
		expect(api.setFileBoxSource).toHaveBeenCalledWith({
			conversation: "A",
			message: "message-A",
			item: "item-A",
			file_url: props.attachment.file_url,
			expected: "",
		});
		expect(button(w, "Current source").attributes("disabled")).toBeDefined();
		w.unmount();
	});
	it("a reload reads the persistent selection without another write", async () => {
		api.getFileBoxSourceState.mockResolvedValue(selected);
		const w = create();
		await flushPromises();
		expect(button(w, "Current source")).toBeDefined();
		expect(api.setFileBoxSource).not.toHaveBeenCalled();
		w.unmount();
	});
	it("a lost acknowledgement is reconciled with a read, never a second selection", async () => {
		api.getFileBoxSourceState.mockResolvedValueOnce(original).mockResolvedValue(selected);
		api.setFileBoxSource.mockRejectedValueOnce(new Error("Connection lost"));
		const w = create();
		await flushPromises();
		await button(w, "Use as source").trigger("click");
		await flushPromises();
		expect(button(w, "Current source")).toBeDefined();
		expect(api.setFileBoxSource).toHaveBeenCalledTimes(1);
		expect(api.getFileBoxSourceState).toHaveBeenCalledTimes(2);
		w.unmount();
	});
	it("a stale write shows the current state and does not overwrite it", async () => {
		api.setFileBoxSource.mockResolvedValue({
			...original,
			ok: false,
			reason: "source_changed",
			version: "other-source",
		});
		const w = create();
		await flushPromises();
		await button(w, "Use as source").trigger("click");
		await flushPromises();
		expect(w.get('[role="alert"]').text()).toContain("changed");
		expect(api.setFileBoxSource).toHaveBeenCalledTimes(1);
		w.unmount();
	});
	it("a late response for chat A cannot change chat B", async () => {
		const late = deferred();
		api.setFileBoxSource.mockReturnValueOnce(late.promise);
		const w = create();
		await flushPromises();
		await button(w, "Use as source").trigger("click");
		await w.setProps({
			conversation: "B",
			message: "message-B",
			attachment: { name: "item-B", file_url: "/private/files/B.pdf" },
		});
		await flushPromises();
		late.resolve(selected);
		await flushPromises();
		expect(button(w, "Current source")).toBeUndefined();
		expect(button(w, "Use as source")).toBeDefined();
		w.unmount();
	});
	it("a failed source read offers retry without a write", async () => {
		api.getFileBoxSourceState.mockRejectedValueOnce(new Error("Connection lost"));
		const w = create();
		await flushPromises();
		await button(w, "Retry source check").trigger("click");
		await flushPromises();
		expect(button(w, "Use as source")).toBeDefined();
		expect(api.setFileBoxSource).not.toHaveBeenCalled();
		w.unmount();
	});
	it("an unsupported site explains the disabled action even when it has an original source", async () => {
		api.getFileBoxSourceState.mockResolvedValue({ ...original, supported: false });
		const w = create();
		await flushPromises();
		expect(button(w, "Use as source").attributes("disabled")).toBeDefined();
		expect(w.text()).toContain("Source selection is unavailable");
		expect(api.setFileBoxSource).not.toHaveBeenCalled();
		w.unmount();
	});
	it("a missing attachment disables selection and reports its own preview identity", async () => {
		api.getFileBoxSourceState.mockResolvedValue({
			...original,
			status: "lost",
			source: null,
			candidate_available: false,
			preview_available: false,
		});
		const w = create();
		await flushPromises();
		expect(button(w, "Use as source").attributes("disabled")).toBeDefined();
		expect(w.text()).toContain("This attachment is unavailable");
		expect(w.emitted("availability").at(-1)[0]).toEqual({
			conversation: "A",
			message: "message-A",
			item: "item-A",
			file_url: props.attachment.file_url,
			available: false,
		});
		expect(api.setFileBoxSource).not.toHaveBeenCalled();
		w.unmount();
	});
	it("a lost old source still permits a valid new replacement", async () => {
		api.getFileBoxSourceState.mockResolvedValue({
			...original,
			status: "lost",
			source: null,
			version: "gone",
			candidate_available: true,
		});
		const w = create();
		await flushPromises();
		expect(button(w, "Use as source").attributes("disabled")).toBeUndefined();
		await button(w, "Use as source").trigger("click");
		await flushPromises();
		expect(api.setFileBoxSource.mock.calls[0][0].expected).toBe("gone");
		expect(button(w, "Current source")).toBeDefined();
		w.unmount();
	});
});
