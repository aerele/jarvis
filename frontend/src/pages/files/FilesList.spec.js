import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// A document whose approval sheet is being applied reads "Applying", links to the
// sheet on the board, and keeps the list polling until it moves on.
vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	const route = reactive({ name: "FilesList", params: {}, query: {} });
	return { useRoute: () => route, useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) };
});
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});
vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options"],
		template:
			'<div class="picker"><i v-for="o in options" :key="o.value" class="opt">{{ o.value }}</i></div>',
	},
	Badge: { props: ["label", "theme"], template: '<span class="badge">{{ label }}</span>' },
	Button: { props: ["label"], template: "<button>{{ label }}</button>" },
	Dropdown: { template: "<div/>" },
	FeatherIcon: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
	toast: {
		success: vi.fn(),
		error: vi.fn(),
		create: vi.fn(),
		promise: vi.fn(async (p, o) => o.success(await p)),
	},
	confirmDialog: vi.fn(),
}));
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		props: ["rows"],
		template: `<div><slot name="banner" /><div v-for="row in rows" :key="row.name" class="row"><slot name="cell-status" :row="row" /><slot name="cell-result" :row="row" /><slot name="cell-_rerun" :row="row" /></div></div>`,
	},
}));
vi.mock("@/components/FilePreview.vue", () => ({ default: { template: "<div/>" } }));
const shell = vi.hoisted(() => ({ refreshApprovalsCount: vi.fn() }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => shell }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "now", exactDate: () => "" }));
vi.mock("@/branding", () => ({ agentName: "Jarvis" }));
vi.mock("@/api", () => ({
	fileboxListPage: vi.fn(),
	listCustomSkills: vi.fn(async () => []),
	fileboxCheckSkill: vi.fn(),
	fileboxDrop: vi.fn(),
	fileboxRerun: vi.fn(),
	uploadFile: vi.fn(),
	getListFilterSchema: vi.fn(),
	getListFilterCapabilities: vi.fn(),
}));

import * as api from "@/api";
import { toast } from "frappe-ui";
import { escapeHtml } from "@/lib/errors";
import FilesList from "./FilesList.vue";

const applying = {
	name: "conv-1",
	title: "loreal.pdf",
	status: "applying",
	result: "Applying the approval sheet… 40/250",
	result_link: "/approvals?held=PA-S1",
};

async function mountList(rows) {
	api.fileboxListPage.mockResolvedValue({ rows, total: rows.length, has_more: false });
	const w = mount(FilesList, {
		global: {
			stubs: { "router-link": { props: ["to"], template: '<a :href="to"><slot /></a>' } },
		},
	});
	await flushPromises();
	return w;
}

describe("FilesList: a sheet being applied", () => {
	let w;
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
	});
	afterEach(() => {
		w && w.unmount();
		vi.useRealTimers();
	});

	it("reads Applying and links its progress line to the sheet", async () => {
		w = await mountList([applying]);
		expect(w.find(".badge").text()).toBe("Applying");
		const link = w.find(".row a");
		expect(link.attributes("href")).toBe("/approvals?held=PA-S1");
		expect(link.text()).toBe("Applying the approval sheet… 40/250");
	});

	it("keeps polling while it applies, and stops once nothing moves", async () => {
		w = await mountList([applying]);
		const calls = api.fileboxListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(5000);
		expect(api.fileboxListPage.mock.calls.length).toBe(calls + 1);
		w.unmount();
		w = await mountList([{ ...applying, status: "draft_created", result_link: "/app/x/1" }]);
		const settled = api.fileboxListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(15000);
		expect(api.fileboxListPage.mock.calls.length).toBe(settled);
	});
});

describe("FilesList: the File Box skill picker and a bulk drop", () => {
	let w;
	const SKILLS = [
		{ skill_name: "bills", mine: 1, enabled: 1, use_in_file_box: 1 },
		{ skill_name: "chat-only", mine: 1, enabled: 1, use_in_file_box: 0 },
		{ skill_name: "draft-skill", mine: 1, enabled: 0, use_in_file_box: 1 },
	];
	const files = () => [new File(["a"], "a.pdf"), new File(["b"], "b.pdf")];
	async function pin(slug) {
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", { value: slug });
		await flushPromises();
	}
	async function drop(list) {
		await w.trigger("drop", { dataTransfer: { files: list, types: ["Files"] } });
		await flushPromises();
	}

	beforeEach(async () => {
		vi.clearAllMocks();
		api.listCustomSkills.mockResolvedValue(SKILLS);
		api.uploadFile.mockImplementation(async (f) => ({
			file_url: `/private/files/${f.name}`,
			file_name: f.name,
			name: `F-${f.name}`,
		}));
		api.fileboxDrop.mockResolvedValue({ ok: true });
		w = await mountList([]);
	});
	afterEach(() => w && w.unmount());

	it("offers only enabled skills File Box may use", () => {
		expect(w.findAll(".picker .opt").map((o) => o.text())).toEqual(["", "bills"]);
	});

	it("checks the pin once, then drops every file with it", async () => {
		api.fileboxCheckSkill.mockResolvedValue({ ok: true, available: 1 });
		await pin("bills");
		await drop(files());
		expect(api.fileboxCheckSkill).toHaveBeenCalledTimes(1);
		expect(api.fileboxCheckSkill).toHaveBeenCalledWith("bills");
		expect(api.fileboxDrop.mock.calls.map((c) => [c[1], c[2]])).toEqual([
			["a.pdf", "bills"],
			["b.pdf", "bills"],
		]);
	});

	it("keeps every file un-dropped when the pin is gone, with one toast", async () => {
		api.fileboxCheckSkill.mockResolvedValue({ ok: true, available: 0 });
		await pin("bills");
		await drop(files());
		expect(api.fileboxCheckSkill).toHaveBeenCalledTimes(1);
		expect(api.uploadFile).not.toHaveBeenCalled();
		expect(api.fileboxDrop).not.toHaveBeenCalled();
		expect(toast.error).toHaveBeenCalledTimes(1);
		expect(toast.error.mock.calls[0][0]).toContain(
			"“bills” is no longer available for File Box"
		);
		expect(w.vm.pinnedSkill).toBe(""); // back to None, so the next drop is deliberate
	});

	it("drops anyway when the check itself fails: drop_file validates the pin", async () => {
		api.fileboxCheckSkill.mockRejectedValue(new Error("offline"));
		await pin("bills");
		await drop(files());
		expect(api.fileboxDrop.mock.calls.map((c) => c[2])).toEqual(["bills", "bills"]);
		expect(toast.error).not.toHaveBeenCalled();
	});

	it("drops without a check when no skill is tagged", async () => {
		await drop(files());
		expect(api.fileboxCheckSkill).not.toHaveBeenCalled();
		expect(api.fileboxDrop).toHaveBeenCalledTimes(2);
	});

	it("says when a dropped file waits for a choice", async () => {
		api.fileboxCheckSkill.mockResolvedValue({ ok: true, available: 1 });
		api.fileboxDrop.mockResolvedValueOnce({ ok: true, needs_approval: 1 });
		await pin("bills");
		await drop(files());
		const [, opts] = toast.promise.mock.calls[0];
		expect(opts.success(2)).toBe(
			"Added 2 files to File Box · tagged: bills · 1 waiting for your choice"
		);
		expect(shell.refreshApprovalsCount).toHaveBeenCalled();
	});
});

describe("FilesList: a re-run whose tagged skill is gone", () => {
	const WORDS = "Waiting for your choice: <the server's words>"; // never a client copy
	const failed = { name: "conv-2", title: "bill.pdf", status: "failed", is_owner: 1 };
	const rerun = (w) =>
		w
			.findAll("button")
			.find((b) => b.text() === "Re-run")
			.trigger("click");

	beforeEach(() => vi.clearAllMocks());

	it("says the server's words for the waiting row and refreshes the board count", async () => {
		api.fileboxRerun.mockResolvedValue({ ok: true, needs_approval: 1, result: WORDS });
		const w = await mountList([failed]);
		await rerun(w);
		await flushPromises();
		expect(toast.create).toHaveBeenCalledWith({ type: "info", message: escapeHtml(WORDS) });
		expect(toast.success).not.toHaveBeenCalled();
		expect(shell.refreshApprovalsCount).toHaveBeenCalledTimes(1);
		w.unmount();
	});

	it("a plain re-run just says it is re-running", async () => {
		api.fileboxRerun.mockResolvedValue({ ok: true });
		const w = await mountList([failed]);
		await rerun(w);
		await flushPromises();
		expect(toast.success).toHaveBeenCalledWith("Re-running…");
		expect(toast.create).not.toHaveBeenCalled();
		expect(shell.refreshApprovalsCount).not.toHaveBeenCalled();
		w.unmount();
	});
});
