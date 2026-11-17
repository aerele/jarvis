import { describe, it, expect, afterEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import FieldControl from "@/components/forms/FieldControl.vue";

/**
 * #760: a long-text field (Supplier Details on an Approval Board record) was a fixed
 * two-row box, so its text was cut off. It now grows with its content up to a cap,
 * then scrolls. jsdom does no layout, so the content height is stubbed.
 */
let contentHeight = 0;
const realScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");

function stubContent(px) {
	contentHeight = px;
	Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
		configurable: true,
		get: () => contentHeight,
	});
}

const textField = (value) => ({ control: "text", value, label: "Supplier Details" });

describe("FieldControl text field height", () => {
	afterEach(() => {
		delete HTMLTextAreaElement.prototype.scrollHeight;
		if (realScroll) Object.defineProperty(HTMLElement.prototype, "scrollHeight", realScroll);
		vi.restoreAllMocks();
	});

	it("opens tall enough for its text", async () => {
		stubContent(150);
		const w = mount(FieldControl, { props: { id: "f1", field: textField("a long note") } });
		await nextTick();
		expect(w.get("textarea").element.style.height).toBe("150px");
	});

	it("stops growing at the cap and scrolls from there", async () => {
		stubContent(900);
		const w = mount(FieldControl, { props: { id: "f2", field: textField("very long") } });
		await nextTick();
		const el = w.get("textarea").element;
		expect(el.style.height).toBe("320px");
		expect(el.style.maxHeight).toBe("320px");
	});

	it("refits as the user types and still emits the value", async () => {
		stubContent(60);
		const w = mount(FieldControl, { props: { id: "f3", field: textField("short") } });
		await nextTick();
		const ta = w.get("textarea");
		expect(ta.element.style.height).toBe("60px");
		stubContent(200);
		await ta.setValue("short\nand now\nmuch\nlonger");
		expect(w.emitted("update").at(-1)).toEqual(["short\nand now\nmuch\nlonger"]);
		expect(ta.element.style.height).toBe("200px");
	});

	it("refits when the value is replaced from outside", async () => {
		stubContent(60);
		const w = mount(FieldControl, { props: { id: "f4", field: textField("x") } });
		await nextTick();
		stubContent(180);
		await w.setProps({ field: textField("a much longer value set by the parent") });
		await nextTick();
		expect(w.get("textarea").element.style.height).toBe("180px");
	});

	it("leaves other controls alone", async () => {
		const w = mount(FieldControl, {
			props: { id: "f5", field: { control: "data", value: "v", label: "Name" } },
		});
		expect(w.find("textarea").exists()).toBe(false);
		expect(w.get("input").element.style.height).toBe("");
	});
});
