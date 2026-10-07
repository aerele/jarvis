import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

// #671: in edit mode each menu row has a six-dot grip, and only the grip starts a drag.
vi.mock("vue-router", () => ({
	useRoute: () => ({ path: "/", name: "Chat" }),
	useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Badge: { template: "<span/>" },
	FeatherIcon: { template: "<i/>" },
	KeyboardShortcut: { template: "<span/>" },
}));
vi.mock("@/stores/shell", async () => {
	const { reactive } = await import("vue");
	const store = reactive({
		mobile: false,
		sidebarCollapsed: false,
		sidebarWidth: 240,
		conversations: [],
		conversationsLoading: false,
		approvalsCount: 0,
		paletteOpen: false,
		requestNewChat: vi.fn(),
	});
	return { useShellStore: () => store };
});
vi.mock("@/api", () => ({
	getMySettings: vi.fn(async () => ({})),
	setSidebarOrder: vi.fn(async () => ({})),
}));
vi.mock("./UserMenu.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("./ConversationRow.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("./SidebarLink.vue", () => ({
	default: { props: ["label"], template: '<a class="link">{{ label }}</a>' },
}));

import * as api from "@/api";
import Sidebar from "./Sidebar.vue";

const rows = (w) => w.findAll("nav > div.relative");
const editButton = (w) => w.find('button[title="Edit sidebar order"]');

async function editing() {
	const w = mount(Sidebar);
	await flushPromises();
	await editButton(w).trigger("click");
	return w;
}

describe("Sidebar reorder grip", () => {
	beforeEach(() => vi.clearAllMocks());

	it("shows no grip outside edit mode", async () => {
		const w = mount(Sidebar);
		await flushPromises();
		expect(w.find(".lucide-grip-vertical").exists()).toBe(false);
	});

	it("in edit mode, each row has a six-dot grip and only the grip is draggable", async () => {
		const w = await editing();
		const row = rows(w)[0];
		const grip = row.find(".lucide-grip-vertical");
		expect(grip.attributes("draggable")).toBe("true");
		expect(grip.attributes("title")).toBe("Drag to move File Box");
		expect(row.attributes("draggable")).toBeUndefined();
		expect(w.find('[name="more-vertical"]').exists()).toBe(false);
	});

	it("a drag from the grip, dropped on another row, moves the row", async () => {
		const w = await editing();
		const setDragImage = vi.fn();
		const dataTransfer = { setData: vi.fn(), setDragImage, effectAllowed: "" };
		await rows(w)[0].find(".lucide-grip-vertical").trigger("dragstart", { dataTransfer });
		expect(setDragImage).toHaveBeenCalledWith(rows(w)[0].element, 16, 14);
		vi.useFakeTimers();
		try {
			await rows(w)[2].trigger("drop");
			expect(
				rows(w)
					.map((r) => r.find(".link").text())
					.slice(0, 3)
			).toEqual(["Approval Board", "File Box", "Dashboard"]);
			vi.advanceTimersByTime(400); // the order is saved after a short pause
		} finally {
			vi.useRealTimers();
		}
		expect(api.setSidebarOrder.mock.calls[0][0].top.slice(0, 3)).toEqual([
			"Approval Board",
			"File Box",
			"Dashboard",
		]);
	});

	it("a drag that starts on the row itself does nothing", async () => {
		const w = await editing();
		await rows(w)[0].trigger("dragstart", { dataTransfer: { setData: vi.fn() } });
		vi.useFakeTimers();
		try {
			await rows(w)[2].trigger("drop");
			vi.advanceTimersByTime(400);
		} finally {
			vi.useRealTimers();
		}
		expect(rows(w)[0].find(".link").text()).toBe("File Box");
		expect(api.setSidebarOrder).not.toHaveBeenCalled();
	});
});
