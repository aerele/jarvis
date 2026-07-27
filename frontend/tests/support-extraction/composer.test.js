// Task 4 of the PR1 extraction: the Composer generic core. Locks the rendered
// shape (snapshot) plus the four behaviours the host relies on — Send arming,
// the Send↔Stop swap, the "Uploading…" pill, and Enter vs Shift+Enter.
import { expect, test } from "vitest";
import { mount } from "@vue/test-utils";
import { h, ref } from "vue";
import Composer from "../../src/components/chat/Composer.vue";
import { mountWithPalette } from "./fixtures.js";

const findComposer = (w) => w.findComponent(Composer);

// `mountWithPalette` bakes props into a wrapper's render fn, so `setProps` on
// it can't reach the Composer. These tests drive props directly instead (the
// palette vars are irrelevant to height math and to the expose contract).
const mountBare = (props = {}) => mount(Composer, { props });

// jsdom never lays anything out, so scrollHeight is a hard 0 — stub it, or
// auto-grow measures nothing and every assertion below passes vacuously.
const stubScrollHeight = (el, value) =>
	Object.defineProperty(el, "scrollHeight", { value, configurable: true });

test("composer renders identically (light)", () => {
	const w = mountWithPalette(Composer, {
		modelValue: "hi",
		placeholder: "Ask Jarvis…",
		disclaimer: "Jarvis can make mistakes.",
	});
	expect(w.html()).toMatchSnapshot();
});

test("Send is armed once there is text", () => {
	const w = mountWithPalette(Composer, { modelValue: "hi" });
	const send = w.find(".jv-sendbtn");
	expect(send.exists()).toBe(true);
	expect(send.attributes("disabled")).toBeUndefined();
	expect(send.classes()).toContain("ready");
});

test("Send is disabled with an empty box and no attachments", () => {
	const w = mountWithPalette(Composer, { modelValue: "   " });
	const send = w.find(".jv-sendbtn");
	expect(send.attributes("disabled")).toBeDefined();
	expect(send.classes()).not.toContain("ready");
});

test("an attachment alone arms Send", () => {
	const w = mountWithPalette(Composer, {
		modelValue: "",
		attachments: [{ key: 0, file_name: "a.pdf", removable: true }],
	});
	expect(w.find(".jv-sendbtn").attributes("disabled")).toBeUndefined();
});

test("the canSend prop overrides the derived value", () => {
	const w = mountWithPalette(Composer, { modelValue: "hi", canSend: false });
	expect(w.find(".jv-sendbtn").attributes("disabled")).toBeDefined();
});

test("busy swaps Send for Stop and flips the hint", () => {
	const w = mountWithPalette(Composer, { modelValue: "hi", busy: true });
	expect(w.find(".jv-sendbtn").exists()).toBe(false);
	const stop = w.find('button[title="Stop generating"]');
	expect(stop.exists()).toBe(true);
	expect(w.text()).toContain("Stop");
	expect(w.text()).not.toContain("Enter ↵");
});

test("Stop emits stop", async () => {
	const w = mountWithPalette(Composer, { modelValue: "hi", busy: true });
	await w.find('button[title="Stop generating"]').trigger("click");
	expect(findComposer(w).emitted("stop")).toHaveLength(1);
});

test("an uploading attachment renders the Uploading… pill", () => {
	const w = mountWithPalette(Composer, {
		attachments: [{ key: "uploading", uploading: true }],
	});
	expect(w.text()).toContain("Uploading…");
});

test("attachments render an image thumbnail, a file chip, and remove buttons", async () => {
	const w = mountWithPalette(Composer, {
		attachments: [
			{ key: 0, file_name: "shot.png", preview_url: "/files/shot.png", removable: true },
			{ key: 1, file_name: "report.pdf", removable: false },
		],
	});
	const img = w.find("img");
	expect(img.attributes("src")).toBe("/files/shot.png");
	expect(w.text()).toContain("📎 report.pdf");
	// only the removable one gets an ×
	const removes = w.findAll("button").filter((b) => b.text() === "×");
	expect(removes).toHaveLength(1);
	await removes[0].trigger("click");
	expect(findComposer(w).emitted("remove-attachment")[0]).toEqual([0]);
});

// The index alignment that chat's adapter depends on. `pendingFiles` goes over
// the wire verbatim, so ChatView projects it through a `composerAttachments`
// computed and appends the "Uploading…" pill LAST; `removeFile(i)` splices
// `pendingFiles` by that same index. If the pill were ever prepended (or made
// removable) every × would splice the wrong file. The existing thumbnail/chip
// test has no pill, so this is the only case that can break.
test("remove-attachment emits the right index while an Uploading… pill is present", async () => {
	const w = mountWithPalette(Composer, {
		attachments: [
			{ key: 0, file_name: "a.pdf", removable: true },
			{ key: 1, file_name: "b.pdf", removable: true },
			{ key: "uploading", uploading: true },
		],
	});
	const c = findComposer(w);
	expect(w.text()).toContain("Uploading…");
	// the pill is NOT removable: exactly two × buttons for two real chips
	const removes = w.findAll("button").filter((b) => b.text() === "×");
	expect(removes).toHaveLength(2);
	await removes[1].trigger("click");
	await removes[0].trigger("click");
	expect(c.emitted("remove-attachment")).toEqual([[1], [0]]);
});

test("Enter submits, Shift+Enter does not", async () => {
	const w = mountWithPalette(Composer, { modelValue: "hi" });
	const c = findComposer(w);
	const ta = w.find("textarea");
	await ta.trigger("keydown", { key: "Enter", shiftKey: true });
	expect(c.emitted("submit")).toBeUndefined();
	await ta.trigger("keydown", { key: "Enter" });
	expect(c.emitted("submit")).toHaveLength(1);
});

// The contract chat depends on: the raw keydown reaches the host BEFORE the
// built-in acts, so a host that claims the key (chat's mention navigation,
// prompt history and its own Enter-to-send all preventDefault) never gets a
// second, duplicate submit.
test("the raw keydown is emitted first, and a host preventDefault suppresses submit", async () => {
	const seen = [];
	const w = mountWithPalette(Composer, {
		modelValue: "hi",
		onKeydown: (e) => {
			seen.push(e.key);
			e.preventDefault();
		},
	});
	const c = findComposer(w);
	await w.find("textarea").trigger("keydown", { key: "Enter" });
	expect(seen).toEqual(["Enter"]);
	expect(c.emitted("submit")).toBeUndefined();
});

test("a host that does NOT preventDefault still gets the built-in submit", async () => {
	const w = mountWithPalette(Composer, { modelValue: "hi", onKeydown: () => {} });
	await w.find("textarea").trigger("keydown", { key: "Enter" });
	expect(findComposer(w).emitted("submit")).toHaveLength(1);
});

test("a host preventDefault on paste suppresses the built-in image extraction", async () => {
	const file = new File(["x"], "a.png", { type: "image/png" });
	const w = mountWithPalette(Composer, {
		modelValue: "",
		onPaste: (e) => e.preventDefault(),
	});
	await w.find("textarea").trigger("paste", { clipboardData: { files: [file], items: [] } });
	expect(findComposer(w).emitted("files-added")).toBeUndefined();
});

test("an unclaimed image paste emits files-added", async () => {
	const file = new File(["x"], "a.png", { type: "image/png" });
	const w = mountWithPalette(Composer, { modelValue: "" });
	await w.find("textarea").trigger("paste", { clipboardData: { files: [file], items: [] } });
	expect(findComposer(w).emitted("files-added")[0][0]).toEqual([file]);
});

// ORDER is the contract, not just "both fired": v-model's own listener is
// registered by the directive before @input is patched on, so the host sees the
// raw `input` event with the new value ALREADY committed upstream. Chat's
// onInput reads `input.value` to parse @/ mentions off the caret; flip the
// order and it parses the previous keystroke's text.
test("typing emits update:modelValue BEFORE the raw input event", async () => {
	const order = [];
	const w = mountWithPalette(Composer, {
		modelValue: "",
		"onUpdate:modelValue": (v) => order.push(`update:modelValue(${v})`),
		onInput: () => order.push("input"),
	});
	const c = findComposer(w);
	await w.find("textarea").setValue("ab");
	expect(order).toEqual(["update:modelValue(ab)", "input"]);
	expect(c.emitted("update:modelValue")[0]).toEqual(["ab"]);
	expect(c.emitted("input")).toHaveLength(1);
});

test("Send click emits submit", async () => {
	const w = mountWithPalette(Composer, { modelValue: "hi" });
	await w.find(".jv-sendbtn").trigger("click");
	expect(findComposer(w).emitted("submit")).toHaveLength(1);
});

test("drop emits files-added and never uploads", async () => {
	const w = mountWithPalette(Composer, { modelValue: "" });
	const c = findComposer(w);
	const file = new File(["x"], "a.png", { type: "image/png" });
	await w.find(".jv-composer").trigger("drop", { dataTransfer: { files: [file] } });
	expect(c.emitted("files-added")[0][0]).toEqual([file]);
});

test("the default #left-toolbar is an attach button; a host slot replaces it", () => {
	const plain = mountWithPalette(Composer, {});
	expect(plain.find('button[title="Attach file"]').exists()).toBe(true);
	// Chat takes the slot over (mic + its own 📎 + wiki) and drives the file
	// input through the `pickFiles` slot prop, so the default must yield.
	let slotProps = null;
	const slotted = mountWithPalette(
		Composer,
		{},
		{
			slots: {
				"left-toolbar": (p) => {
					slotProps = p;
					return h("button", { class: "host-mic" }, "mic");
				},
			},
		}
	);
	expect(slotted.find(".host-mic").exists()).toBe(true);
	expect(slotted.find('button[title="Attach file"]').exists()).toBe(false);
	expect(typeof slotProps.pickFiles).toBe("function");
});

test("the #above / #overlay / #footer slots render at their positions", () => {
	const w = mountWithPalette(
		Composer,
		{ disclaimer: "fine print" },
		{
			slots: {
				above: () => h("div", { class: "s-above" }, "nudge"),
				overlay: () => h("div", { class: "s-overlay" }, "mentions"),
				footer: () => h("div", { class: "s-footer" }, "foot"),
			},
		}
	);
	const html = w.html();
	expect(html.indexOf("s-above")).toBeLessThan(html.indexOf("jv-composer"));
	// the overlay sits inside the box, above the textarea
	expect(html.indexOf("s-overlay")).toBeLessThan(html.indexOf("<textarea"));
	expect(html.indexOf("fine print")).toBeLessThan(html.indexOf("s-footer"));
});

// ---- auto-grow ----------------------------------------------------------
// Ten explicit autoGrow() calls in ChatView collapsed into ONE
// watch(modelValue, autoGrow, {flush:"post"}) plus the inline call in the input
// handler. Every programmatic set — restored draft, dictation transcript,
// prompt-history recall, the clear after send — now reaches the textarea only
// through that watcher, so a dropped watcher or the wrong flush timing (which
// would measure the pre-update DOM) is a silent regression.
test("a programmatic modelValue change grows the box, clamped to the default maxHeight", async () => {
	const w = mountBare({ modelValue: "" });
	const ta = w.find("textarea");
	stubScrollHeight(ta.element, 400);
	await w.setProps({ modelValue: "x\ny\nz" });
	// 140 is Composer's declared default for the `maxHeight` prop.
	expect(ta.element.style.height).toBe("140px");
});

test("a shorter value shrinks the box back (height is recomputed, not only raised)", async () => {
	const w = mountBare({ modelValue: "" });
	const ta = w.find("textarea");
	stubScrollHeight(ta.element, 400);
	await w.setProps({ modelValue: "x\ny\nz" });
	expect(ta.element.style.height).toBe("140px");
	stubScrollHeight(ta.element, 30);
	await w.setProps({ modelValue: "x" });
	expect(ta.element.style.height).toBe("30px");
});

// Guards flush:"post" specifically. scrollHeight here is derived from the
// element's LIVE value, so a pre-flush watcher measures the textarea before Vue
// has written the new text into it and lands one value behind.
test("the watcher measures the textarea AFTER the DOM update (flush: post)", async () => {
	const w = mountBare({ modelValue: "" });
	const ta = w.find("textarea");
	Object.defineProperty(ta.element, "scrollHeight", {
		get: () => 20 * String(ta.element.value || "").split("\n").length,
		configurable: true,
	});
	await w.setProps({ modelValue: "a\nb\nc" });
	expect(ta.element.style.height).toBe("60px");
});

test("the maxHeight prop moves both the auto-grow clamp and the inline max-height", async () => {
	const w = mountBare({ modelValue: "", maxHeight: 300 });
	const ta = w.find("textarea");
	expect(ta.element.style.maxHeight).toBe("300px");
	stubScrollHeight(ta.element, 400);
	await w.setProps({ modelValue: "x\ny\nz" });
	expect(ta.element.style.height).toBe("300px");
});

// ---- the defineExpose contract ChatView reaches through ------------------
// 8 × composerRef.value?.focusInput() and 3 × composerRef.value?.el (caret math
// for mention insertion and edit-and-resend). Both are optional-chained at
// every call site, so renaming either one fails SILENTLY. Mounted through a
// template ref, i.e. exactly how ChatView holds it.
test("expose gives the host the raw textarea as `el` plus focusInput()", () => {
	const composerRef = ref(null);
	// attachTo: jsdom only moves activeElement for a node that is in the document.
	const w = mount(
		{
			render() {
				return h(Composer, { ref: composerRef, modelValue: "hi" });
			},
		},
		{ attachTo: document.body }
	);
	const exposed = composerRef.value;
	expect(exposed).toBeTruthy();
	// unwrapped by Vue's expose proxy — a live DOM node, not a Ref
	expect(exposed.el).toBe(w.find("textarea").element);
	expect(exposed.el.tagName).toBe("TEXTAREA");
	expect(typeof exposed.focusInput).toBe("function");
	exposed.focusInput();
	expect(document.activeElement).toBe(exposed.el);
	w.unmount();
});

test("dark render", () => {
	const w = mountWithPalette(Composer, { modelValue: "hi" }, { dark: true });
	expect(w.html()).toMatchSnapshot();
});
