import { mount } from "@vue/test-utils";
import { beforeAll, afterAll, afterEach, describe, expect, it, vi } from "vitest";
import SharedDiagram from "./SharedDiagram.vue";

// Exercise the pinned renderer: a mock cannot reveal directives overriding
// initialize(). jsdom supplies no SVG geometry; browser checks cover layout.
vi.unmock("mermaid");
const geometry = new Map();
const mounted = [];
beforeAll(async () => {
	for (const [name, value] of Object.entries({
		getBBox: () => ({ x: 0, y: 0, width: 120, height: 24 }),
		getComputedTextLength() {
			return this.textContent.length * 8;
		},
	})) {
		geometry.set(name, Object.getOwnPropertyDescriptor(SVGElement.prototype, name));
		Object.defineProperty(SVGElement.prototype, name, { configurable: true, value });
	}
	await import("mermaid");
}, 20000);
afterEach(() => {
	for (const wrapper of mounted.splice(0)) wrapper.unmount();
});
afterAll(() => {
	for (const [name, descriptor] of geometry) {
		if (descriptor) Object.defineProperty(SVGElement.prototype, name, descriptor);
		else delete SVGElement.prototype[name];
	}
});
const graph = "flowchart LR\n A[Purchase order] --> B[Goods received]";
const remoteStyle = "& { background-image: url(https://review-egress.invalid/pixel.png); }";

async function diagram(source, dark = false) {
	const wrapper = mount(SharedDiagram, { props: { source, dark } });
	mounted.push(wrapper);
	await vi.waitFor(() => expect(wrapper.find("svg").exists()).toBe(true), {
		timeout: 10000,
	});
	return wrapper;
}
function expectLabels(wrapper) {
	const labels = wrapper
		.findAll("svg text")
		.map((node) => node.text())
		.join(" ");
	expect(labels).toContain("Purchase order");
	expect(labels).toContain("Goods received");
	expect(wrapper.find("foreignObject,script,a").exists()).toBe(false);
}
describe("shared diagrams with real Mermaid", () => {
	it.each([
		["Purchase\u00a0order", "Purchase order"],
		["Purchase#nbsp;order", "Purchase&nbsp;order"],
	])("keeps valid non-breaking-space labels: %s", async (label, expected) => {
		const wrapper = await diagram(`flowchart LR\n A["${label}"] --> B[Goods received]`);
		const labels = wrapper
			.findAll("svg text")
			.map((node) => node.text())
			.join(" ");
		expect(labels.replaceAll("\u00a0", " ")).toContain(expected);
		expect(labels).toContain("Goods received");
	});
	it.each([false, true])("keeps readable labels (dark=%s)", async (dark) => {
		expectLabels(await diagram(graph, dark));
	});
	it.each([
		`%%{init: ${JSON.stringify({ themeCSS: remoteStyle })}}%%\n${graph}`,
		`---\nconfig:\n  themeCSS: '${remoteStyle}'\n---\n${graph}`,
	])("ignores remote theme styles before rendering: %s", async (source) => {
		const wrapper = await diagram(source);
		expectLabels(wrapper);
		expect(wrapper.get("svg").html()).not.toContain("review-egress.invalid");
	});
	it.each([
		`%%{init: {"htmlLabels":true,"flowchart":{"htmlLabels":true}}}%%\n${graph}`,
		`---\nconfig:\n  htmlLabels: true\n  flowchart:\n    htmlLabels: true\n---\n${graph}`,
	])("keeps labels when content requests HTML labels: %s", async (source) => {
		expectLabels(await diagram(source));
	});
});
