import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, afterEach } from "vitest";
const mocks = vi.hoisted(() => ({
	initialize: vi.fn(),
	render: vi.fn(async () => ({
		svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>Purchase order</text><text>Goods received</text><foreignObject><script>bad()</script></foreignObject><a href="https://evil.invalid"><text>external</text></a></svg>',
	})),
}));
vi.mock("mermaid", () => ({ default: mocks }));
import SharedDiagram from "./SharedDiagram.vue";
afterEach(() => {
	vi.unstubAllGlobals();
	vi.clearAllMocks();
});
describe("safe shared diagrams", () => {
	it("keeps fallback diagram text available to assistive technology", async () => {
		mocks.render.mockRejectedValueOnce(new Error("malformed"));
		const w = mount(SharedDiagram, { props: { source: "Unreadable diagram source" } });
		await flushPromises();
		expect(w.get("pre").text()).toBe("Unreadable diagram source");
		expect(w.get("pre").element.closest('[role="img"]')).toBeNull();
		w.unmount();
	});
	it("requests SVG text labels so sanitizing HTML does not erase the diagram", async () => {
		const w = mount(SharedDiagram, {
			props: { source: "flowchart LR\n A[Purchase order] --> B[Goods received]" },
		});
		await flushPromises();
		expect(mocks.initialize).toHaveBeenCalledWith(
			expect.objectContaining({
				securityLevel: "strict",
				htmlLabels: false,
				flowchart: expect.objectContaining({ htmlLabels: false }),
			})
		);
		expect(w.text()).toContain("Purchase order");
		expect(w.find("script,foreignObject,a").exists()).toBe(false);
		w.unmount();
	});
	it("renders without secure-context crypto APIs on plain HTTP", async () => {
		vi.stubGlobal("crypto", {});
		const w = mount(SharedDiagram, { props: { source: "flowchart LR\n A --> B" } });
		await flushPromises();
		expect(mocks.render).toHaveBeenCalled();
		expect(w.find("svg").exists()).toBe(true);
		w.unmount();
	});
});
