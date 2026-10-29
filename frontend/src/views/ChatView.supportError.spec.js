import fs from "node:fs";
import { compile } from "@vue/compiler-dom";
import * as Vue from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import { supportErrorDraft } from "@/lib/supportErrorDraft";
import { turnErrorInfo } from "@/lib/errors";
// Run the actual SFC functions and compiled error-card markup. Chromium covers
// the full view. Router and Retry are observed boundaries, never real writes.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const start = source.lastIndexOf("<div", source.indexOf('v-if="m.error && errorInfo(m).code'));
const end = source.indexOf(
	"</template>",
	source.indexOf("goReconnectSubscription(errorInfo(m).upstream)", start)
);
const { code } = compile(source.slice(start, end), { mode: "function", prefixIdentifiers: true });
const render = new Function("Vue", code)(Vue);
function deferred() {
	let resolve, reject;
	const promise = new Promise((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}
function harness({
	enabled = true,
	error = "Request timed out",
	overrides = {},
	mountCard = true,
	index = 0,
	retryIndex = 0,
} = {}) {
	const m = Vue.reactive({
		name: "reply788fixture",
		role: "assistant",
		error,
		modified: "2026-10-29 12:01:07",
		...overrides,
	});
	const s = {
		supportOn: enabled,
		openSupportInFlight: Vue.ref(false),
		currentId: Vue.ref("conv788fixture"),
		_shownConvId: "conv788fixture",
		route: { params: { id: "conv788fixture" } },
		messages: Vue.ref([m]),
		errorInfo: (row) => turnErrorInfo(row.error),
		supportErrorDraft,
		router: { push: vi.fn(async () => undefined) },
		notify: vi.fn(),
		__: (x) => x,
		recentMessagesForSupport: () => ["You: existing generic context"],
		supportStore: { getCopyPref: vi.fn(async () => "No"), setCopyPref: vi.fn() },
		promptSupportCopy: vi.fn(async () => "cancel"),
		explainUnavailable: vi.fn(),
	};
	const actions = new Function(
		...Object.keys(s),
		["canReportError", "openErrorSupport", "openSupport"].map(declaration).join("\n") +
			"\nreturn {canReportError,openErrorSupport,openSupport};"
	)(...Object.values(s));
	const retry = vi.fn(),
		reconnect = vi.fn(),
		rawOpen = Vue.reactive({});
	const w = mountCard
		? mount({
				render,
				methods: { __: (x) => x },
				setup: () => ({
					openSupportInFlight: s.openSupportInFlight,
					errorInfo: s.errorInfo,
					...actions,
					m,
					mi: index,
					retryIdx: retryIndex,
					retrying: false,
					busy: false,
					retryText: "Retry",
					retry,
					rawOpen,
					toggleRaw: (id) => (rawOpen[id] = !rawOpen[id]),
					canConnectModel: true,
					goReconnectSubscription: reconnect,
				}),
		  })
		: null;
	return { s, m, ...actions, w, retry, reconnect };
}
const reportButton = (h) => h.w.findAll("button").find((b) => b.text() === "Report error");
describe("Report error in the real ChatView card", () => {
	it("keeps Report on an older failure without a Retry control", async () => {
		const h = harness({ index: 0, retryIndex: 2 });
		expect(reportButton(h)).toBeDefined();
		expect(h.w.findAll("button").some((b) => b.text() === "Retry")).toBe(false);
		await reportButton(h).trigger("click");
		await flushPromises();
		expect(h.s.router.push).toHaveBeenCalledOnce();
		h.w.unmount();
	});
	it("keeps reconnect on its own handler", async () => {
		const h = harness({ error: "Please run /login" });
		const b = h.w.findAll("button").find((b) => b.text().startsWith("Reconnect"));
		expect(b).toBeDefined();
		await b.trigger("click");
		expect(h.reconnect).toHaveBeenCalledWith("anthropic");
		expect(h.s.router.push).not.toHaveBeenCalled();
		h.w.unmount();
	});
	it("blocks Report while the existing header consent flow waits", async () => {
		const h = harness();
		const d = deferred();
		h.s.supportStore.getCopyPref.mockReturnValue(d.promise);
		const first = h.openSupport();
		await Vue.nextTick();
		expect(reportButton(h).attributes("disabled")).toBeDefined();
		expect(reportButton(h).element.style.opacity).toBe("0.6");
		expect(reportButton(h).element.style.cursor).toBe("default");
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).not.toHaveBeenCalled();
		d.resolve("");
		await first;
		expect(h.s.promptSupportCopy).toHaveBeenCalledOnce();
		expect(h.s.router.push).not.toHaveBeenCalled();
		expect(h.s.openSupportInFlight.value).toBe(false);
		h.w.unmount();
	});

	it("opens selected draft without retry or a backend call", async () => {
		const h = harness();
		await reportButton(h).trigger("click");
		await flushPromises();
		expect(h.s.router.push).toHaveBeenCalledOnce();
		expect(h.s.router.push.mock.calls[0][0]).toEqual({
			name: "SupportNew",
			query: supportErrorDraft({
				code: "timeout",
				conversation: "conv788fixture",
				message: "reply788fixture",
				at: h.m.modified,
			}),
		});
		expect(h.retry).not.toHaveBeenCalled();
		expect(h.s.supportStore.getCopyPref).not.toHaveBeenCalled();
		h.w.unmount();
	});
	it.each([
		{ enabled: false },
		{ error: "You cancelled this message" },
		{ overrides: { streaming: 1 } },
		{ overrides: { stopped: 1 } },
		{ overrides: { hidden: 1 } },
		{ overrides: { role: "user" } },
		{ overrides: { name: "" } },
	])("does not offer or invoke Report for %j", async (options) => {
		const h = harness(options);
		expect(reportButton(h)).toBeUndefined();
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).not.toHaveBeenCalled();
		h.w.unmount();
	});
	it("retains Report on nonretryable errors and retains Details", async () => {
		const h = harness({ error: "Please run /login" });
		expect(reportButton(h)).toBeDefined();
		const b = h.w.findAll("button").find((b) => b.text() === "Details");
		await b.trigger("click");
		expect(h.w.text()).toContain("Please run /login");
		await b.trigger("click");
		expect(b.text()).toBe("Details");
		h.w.unmount();
	});
	it("retains Retry as a separate action", async () => {
		const h = harness();
		await h.w
			.findAll("button")
			.find((b) => b.text() === "Retry")
			.trigger("click");
		expect(h.retry).toHaveBeenCalledWith(h.m.name);
		expect(h.s.router.push).not.toHaveBeenCalled();
		h.w.unmount();
	});
	it("ignores a stale card during chat switch", async () => {
		const h = harness({ mountCard: false });
		h.s.currentId.value = "other";
		h.s.route.params.id = "other";
		expect(h.canReportError(h.m)).toBe(false);
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).not.toHaveBeenCalled();
	});
	it("rejects absent messages and mismatched route", async () => {
		const h = harness({ mountCard: false });
		await h.openErrorSupport({ ...h.m, name: "foreign" });
		h.s.route.params.id = "other";
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).not.toHaveBeenCalled();
	});
	it("supports root restored chat and current row metadata", async () => {
		const h = harness({ mountCard: false });
		delete h.s.route.params.id;
		await h.openErrorSupport({ ...h.m, modified: "PRIVATE-OLD" });
		expect(h.s.router.push.mock.calls[0][0].query.body).toContain(h.m.modified);
	});
	it("shares one guard with repeated Report and header clicks", async () => {
		const h = harness();
		const d = deferred();
		h.s.router.push.mockReturnValue(d.promise);
		const first = h.openErrorSupport(h.m);
		await Vue.nextTick();
		expect(reportButton(h).attributes("disabled")).toBeDefined();
		expect(reportButton(h).element.style.opacity).toBe("0.6");
		expect(reportButton(h).element.style.cursor).toBe("default");
		await h.openErrorSupport(h.m);
		await h.openSupport();
		expect(h.s.router.push).toHaveBeenCalledOnce();
		d.resolve();
		await first;
		expect(h.s.openSupportInFlight.value).toBe(false);
		h.w.unmount();
	});
	it.each([true, false])("waits for header navigation (empty=%s)", async (empty) => {
		const h = harness({ mountCard: false });
		if (empty) h.s.recentMessagesForSupport = () => [];
		const header = new Function(
			...Object.keys(h.s),
			declaration("openSupport") + "\nreturn openSupport;"
		)(...Object.values(h.s));
		const d = deferred();
		h.s.router.push.mockReturnValue(d.promise);
		const first = header();
		await flushPromises();
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).toHaveBeenCalledOnce();
		expect(h.s.openSupportInFlight.value).toBe(true);
		d.resolve();
		await first;
		expect(h.s.openSupportInFlight.value).toBe(false);
	});
	it("clears guard on navigation cancellation without false error", async () => {
		const h = harness({ mountCard: false });
		h.s.router.push.mockResolvedValue({ type: 8 });
		await h.openErrorSupport(h.m);
		expect(h.s.openSupportInFlight.value).toBe(false);
		expect(h.s.notify).not.toHaveBeenCalled();
	});
	it("shows safe notice and allows retry after thrown navigation failure", async () => {
		const h = harness({ mountCard: false });
		h.s.router.push.mockRejectedValueOnce(new Error("Bearer PRIVATE-SECRET"));
		await h.openErrorSupport(h.m);
		expect(h.s.openSupportInFlight.value).toBe(false);
		expect(h.s.notify.mock.calls[0][0]).not.toContain("PRIVATE-SECRET");
		await h.openErrorSupport(h.m);
		expect(h.s.router.push).toHaveBeenCalledTimes(2);
	});
});
