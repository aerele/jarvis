import { afterEach, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import Sheet from "../../../pwa/src/components/Sheet.vue";
let wrappers = [];
afterEach(() => {
	wrappers.reverse().forEach((w) => w.unmount());
	wrappers = [];
	document.body.innerHTML = "";
	document.body.style.overflow = "";
});
async function open(label) {
	const w = mount(Sheet, {
		attachTo: document.body,
		props: { open: true, label },
		slots: {
			default: '<button>First</button><input aria-label="Input"><button>Last</button>',
		},
	});
	wrappers.push(w);
	await flushPromises();
	return w;
}
it("contains keyboard focus, closes on Escape and restores focus/scroll on close", async () => {
	const opener = document.createElement("button");
	document.body.append(opener);
	opener.focus();
	document.body.style.overflow = "auto";
	const w = await open("Actions");
	const buttons = w.findAll("button");
	expect(document.activeElement).toBe(buttons[0].element);
	expect(document.body.style.overflow).toBe("hidden");
	buttons[1].element.focus();
	await buttons[1].trigger("keydown", { key: "Tab" });
	expect(document.activeElement).toBe(buttons[0].element);
	await buttons[0].trigger("keydown", { key: "Tab", shiftKey: true });
	expect(document.activeElement).toBe(buttons[1].element);
	await buttons[1].trigger("keydown", { key: "Escape" });
	expect(w.emitted("close")).toHaveLength(1);
	await w.setProps({ open: false });
	await flushPromises();
	expect(document.activeElement).toBe(opener);
	expect(document.body.style.overflow).toBe("auto");
});
it("only the top nested sheet handles Escape and closing it retains the parent scroll lock", async () => {
	const parent = await open("Parent");
	parent.findAll("button")[1].element.focus();
	const child = await open("Child");
	await child.get("input").trigger("keydown", { key: "Escape" });
	expect(child.emitted("close")).toHaveLength(1);
	expect(parent.emitted("close")).toBeUndefined();
	await child.setProps({ open: false });
	await flushPromises();
	expect(document.activeElement).toBe(parent.findAll("button")[1].element);
	expect(document.body.style.overflow).toBe("hidden");
	parent.unmount();
	expect(document.body.style.overflow).toBe("");
});
it("a sheet without controls can receive focus and retain Tab", async () => {
	const w = mount(Sheet, { attachTo: document.body, props: { open: true, label: "Empty" } });
	wrappers.push(w);
	await flushPromises();
	expect(document.activeElement).toBe(w.get('[role="dialog"]').element);
	await w.get('[role="dialog"]').trigger("keydown", { key: "Tab" });
	expect(document.activeElement).toBe(w.get('[role="dialog"]').element);
});
