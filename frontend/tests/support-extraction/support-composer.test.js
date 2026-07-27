import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

// SupportComposer is the shared rich-text reply/description editor used by BOTH
// support submit pages. TextEditor is TipTap (too heavy for jsdom) so it's
// stubbed; Button/FeatherIcon/toast/editor-style.css are stubbed too. withinSize
// (the real size cap from useStagedFiles) stays REAL — it toasts via the mocked
// frappe-ui toast, so the mock needs `error` as well as `info`.
vi.mock("frappe-ui/editor-style.css", () => ({}));
vi.mock("frappe-ui", () => ({
	TextEditor: {
		name: "TextEditor",
		props: ["content", "fixedMenu", "uploadFunction", "placeholder", "editorClass"],
		template: "<div class='editor' />",
	},
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading"],
		template: "<button :disabled='disabled' @click=\"$emit('click')\">{{ label }}</button>",
	},
	FeatherIcon: { props: ["name"], template: "<i />" },
	toast: { info: vi.fn(), error: vi.fn() },
}));

import { toast } from "frappe-ui";
import SupportComposer from "@/components/support/SupportComposer.vue";

// A real <input type=file>'s `.files` is a FileList (array-LIKE, not an Array).
// Reproduce that shape so the component's `[...e.target.files]` spread works.
function fileList(arr) {
	const fl = {
		length: arr.length,
		item: (i) => arr[i] ?? null,
		[Symbol.iterator]: function* () {
			yield* arr;
		},
	};
	arr.forEach((f, i) => (fl[i] = f));
	return fl;
}

function mountComposer(props = {}) {
	return mount(SupportComposer, {
		props: { modelValue: "", canSubmit: true, ...props },
	});
}

const editor = (w) => w.findComponent({ name: "TextEditor" });
const submitBtn = (w) => w.findComponent({ name: "Button" });
const card = (w) => w.find(".rounded-lg");

beforeEach(() => {
	vi.clearAllMocks();
});

describe("SupportComposer — editor wiring", () => {
	it("feeds modelValue into the editor and offers the shared toolbar + upload guard", () => {
		const w = mountComposer({ modelValue: "<p>seed</p>", placeholder: "Type here" });
		const e = editor(w);
		expect(e.props("content")).toBe("<p>seed</p>");
		expect(e.props("placeholder")).toBe("Type here");
		// The toolbar is the rich set; a bare/empty menu would be a silent regression.
		expect(Array.isArray(e.props("fixedMenu"))).toBe(true);
		expect(e.props("fixedMenu")).toContain("Bold");
		// Inline uploads must be intercepted (routed to attachments), never uploaded.
		expect(typeof e.props("uploadFunction")).toBe("function");
	});

	it("propagates editor changes up via update:modelValue (v-model)", () => {
		const w = mountComposer();
		editor(w).vm.$emit("change", "<p>typed</p>");
		expect(w.emitted("update:modelValue")[0]).toEqual(["<p>typed</p>"]);
	});
});

describe("SupportComposer — submit gesture", () => {
	it("submits on Ctrl+Enter and Cmd+Enter when armed", async () => {
		const w = mountComposer({ canSubmit: true });
		await card(w).trigger("keydown", { key: "Enter", ctrlKey: true });
		await card(w).trigger("keydown", { key: "Enter", metaKey: true });
		expect(w.emitted("submit")).toHaveLength(2);
	});

	it("does NOT submit on Ctrl+Enter when disarmed (the gate)", async () => {
		// THE mutation that matters: dropping the `if (props.canSubmit)` guard would
		// let a disarmed editor (empty subject / empty reply) fire a submit the host
		// then has to bounce. Pin the guard here.
		const w = mountComposer({ canSubmit: false });
		await card(w).trigger("keydown", { key: "Enter", ctrlKey: true });
		expect(w.emitted("submit")).toBeUndefined();
	});

	it("does NOT submit on a bare Enter (Enter is a newline in a formatting editor)", async () => {
		const w = mountComposer({ canSubmit: true });
		await card(w).trigger("keydown", { key: "Enter" });
		expect(w.emitted("submit")).toBeUndefined();
	});

	it("submits when the (armed) Submit button is clicked, and disables it when disarmed", async () => {
		const armed = mountComposer({ canSubmit: true, submitLabel: "Send" });
		expect(submitBtn(armed).props("disabled")).toBe(false);
		expect(submitBtn(armed).props("label")).toBe("Send");
		// Emit the component's own `click` (not a native DOM click) so this asserts
		// exactly "Button click -> submit", without the stub's native/fallthrough
		// double-fire that the real frappe-ui Button doesn't have.
		submitBtn(armed).vm.$emit("click");
		expect(armed.emitted("submit")).toHaveLength(1);

		const disarmed = mountComposer({ canSubmit: false });
		expect(submitBtn(disarmed).props("disabled")).toBe(true);
	});

	it("relays the loading flag to the Submit button's spinner", () => {
		const w = mountComposer({ loading: true });
		expect(submitBtn(w).props("loading")).toBe(true);
	});
});

describe("SupportComposer — attachments", () => {
	async function setFiles(w, files) {
		const fi = w.find('input[type="file"]');
		Object.defineProperty(fi.element, "files", { value: fileList(files), configurable: true });
		await fi.trigger("change");
	}

	it("emits size-checked files-added from the picker (silently — the chip is the feedback)", async () => {
		const w = mountComposer();
		await setFiles(w, [{ name: "a.png", type: "image/png" }]);
		expect(w.emitted("files-added")[0][0].map((f) => f.name)).toEqual(["a.png"]);
		// picker path does not announce
		expect(toast.info).not.toHaveBeenCalled();
	});

	it("drops an oversize file at the picker and toasts the rejection (withinSize)", async () => {
		const w = mountComposer();
		await setFiles(w, [
			{ name: "huge.png", type: "image/png", size: 26 * 1024 * 1024 },
			{ name: "ok.png", type: "image/png", size: 1000 },
		]);
		expect(w.emitted("files-added")[0][0].map((f) => f.name)).toEqual(["ok.png"]);
		expect(toast.error).toHaveBeenCalledTimes(1);
	});

	it("opens the hidden file input when Attach is clicked", async () => {
		const w = mountComposer();
		const input = w.find('input[type="file"]').element;
		const clickSpy = vi.spyOn(input, "click");
		await w.find('button[type="button"]').trigger("click"); // Attach is the first type=button
		expect(clickSpy).toHaveBeenCalled();
	});

	it("routes pasted/dropped files into attachments (announced) and leaves plain text alone", async () => {
		const w = mountComposer();
		await card(w).trigger("paste", {
			clipboardData: { files: fileList([{ name: "shot.png", type: "image/png" }]) },
		});
		expect(w.emitted("files-added")[0][0].map((f) => f.name)).toEqual(["shot.png"]);
		expect(toast.info).toHaveBeenCalledTimes(1);

		// a files-less paste (plain text) passes through untouched
		await card(w).trigger("paste", { clipboardData: { files: fileList([]) } });
		expect(w.emitted("files-added")).toHaveLength(1); // unchanged
		expect(toast.info).toHaveBeenCalledTimes(1); // unchanged

		await card(w).trigger("drop", {
			dataTransfer: { files: fileList([{ name: "log.txt", type: "text/plain" }]) },
		});
		expect(w.emitted("files-added")[1][0].map((f) => f.name)).toEqual(["log.txt"]);
		expect(toast.info).toHaveBeenCalledTimes(2);
	});

	it("stages a slash-command inline upload as an attachment and REJECTS the inline embed", async () => {
		const w = mountComposer();
		const uploadFn = editor(w).props("uploadFunction");
		await expect(uploadFn({ name: "pic.png", type: "image/png" })).rejects.toThrow();
		expect(w.emitted("files-added")[0][0].map((f) => f.name)).toEqual(["pic.png"]);
		expect(toast.info).toHaveBeenCalledTimes(1);
	});

	it("renders a chip per pending file and emits remove-attachment on its X", async () => {
		const w = mountComposer({
			pending: [
				{ key: "a", file_name: "one.png" },
				{ key: "b", file_name: "two.png" },
			],
		});
		const chips = w.findAll(".jv-supc-chip");
		expect(chips.map((c) => c.text())).toEqual(["one.png", "two.png"]);
		await chips[1].find("button").trigger("click");
		expect(w.emitted("remove-attachment")[0]).toEqual([1]);
	});
});

describe("SupportComposer — disclaimer", () => {
	it("renders the disclaimer line only when provided", () => {
		expect(mountComposer({ disclaimer: "" }).find(".jv-supc-disclaimer").exists()).toBe(false);
		const w = mountComposer({ disclaimer: "Replying reopens this ticket." });
		expect(w.find(".jv-supc-disclaimer").text()).toBe("Replying reopens this ticket.");
	});
});
