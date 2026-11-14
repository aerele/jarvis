import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

vi.mock("frappe-ui", () => ({
	TabButtons: { props: ["buttons", "modelValue"], template: '<div class="tabs"/>' },
}));
import FileBoxSwitch from "./FileBoxSwitch.vue";

describe("FileBoxSwitch", () => {
	it("carries the wrapper class the contrast rule hangs on", () => {
		const w = mount(FileBoxSwitch);
		expect(w.find("[data-filebox-switch]").classes()).toContain("filebox-switch");
	});

	it("darkens the inactive option to ink-gray-7 for this usage only (scoped :deep rule)", () => {
		const src = readFileSync(resolve(__dirname, "FileBoxSwitch.vue"), "utf8");
		expect(src).toMatch(/<style scoped>/);
		expect(src).toMatch(
			/\.filebox-switch :deep\(\[role="radio"\]\[aria-checked="false"\]\)[^}]*var\(--ink-gray-7\)/
		);
	});
});
