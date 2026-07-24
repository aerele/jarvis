import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// vi.mock is HOISTED; vi.doMock is not. Mocking the store from inside a helper
// would run AFTER the component's graph resolved, loading the real singleton
// and failing every assertion.
// `call` is stubbed too: @/api imports it at module scope, and vitest throws
// "No 'call' export is defined on the mock" for any imported name the factory
// omits — so leaving it out would break the import of supportDownloadUrl, which
// these tests deliberately exercise for real.
vi.mock("frappe-ui", () => ({
	Badge: { template: "<span/>" },
	Button: { template: "<button><slot/></button>" },
	FeatherIcon: { template: "<i/>" },
	LoadingIndicator: { template: "<i/>" },
	call: vi.fn(),
	toast: { success: vi.fn(), error: vi.fn() },
}));

// useRoute/useRouter are injection-based — global.mocks.$route does NOT feed
// them; the module has to be mocked.
vi.mock("vue-router", () => ({
	useRoute: () => ({ params: { ticket: "T1" } }),
	useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const storeDouble = {
	tickets: [],
	thread: { ticket: "T1", messages: [], attachments: [], loading: false, error: "" },
	ticketRow: () => storeDouble.tickets[0] || null,
	badgeFor: () => ({ label: "Open", tone: "open", theme: "blue" }),
	isClosed: () => false,
	isAwaiting: () => false,
	fingerprintOf: () => "x",
	loadTickets: vi.fn(),
	loadThread: vi.fn(),
	closeTicket: vi.fn(),
	reply: vi.fn(async () => true),
	uploadTo: vi.fn(async () => 1),
};
vi.mock("@/stores/support", () => ({ useSupportStore: () => storeDouble }));

import SupportThreadPage from "@/pages/support/SupportThreadPage.vue";

beforeEach(() => {
	vi.stubGlobal("matchMedia", () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {},
	}));
	// Assign onto URL; do NOT replace it with a spread — class statics are
	// non-enumerable, so `{ ...URL }` is `{}` and would destroy the constructor.
	URL.createObjectURL = () => "blob:x";
	URL.revokeObjectURL = () => {};
});

function mountWith(messages, ticket = { name: "T1", subject: "Broken", status: "Open" }) {
	storeDouble.tickets = [ticket];
	storeDouble.thread.messages = messages;
	return mount(SupportThreadPage, {
		global: {
			stubs: { SupportShell: { template: "<div><slot name='actions'/><slot/></div>" } },
		},
	});
}

describe("SupportThreadPage", () => {
	it("renders an agent message as a row and a customer message as a bubble", () => {
		// sent_or_received === "Sent" is the ONLY proven discriminator; if this
		// inverts, every message renders on the wrong side with no error.
		const w = mountWith([
			{ sent_or_received: "Sent", content: "<p>hi</p>", creation: "2026-07-24 10:00:00" },
			{
				sent_or_received: "Received",
				content: "<p>thanks</p>",
				creation: "2026-07-24 10:01:00",
			},
		]);
		const msgs = w.findAllComponents({ name: "Message" });
		expect(msgs[0].props("variant")).toBe("row");
		expect(msgs[1].props("variant")).toBe("bubble");
	});

	it("renders the customer bubble's HTML body, not flattened text", () => {
		// Message's bubble took `text` only before this task; passing email HTML
		// through textContent collapses <p>a</p><p>b</p> into "ab". The bubble has
		// to render the sanitized html, inside the bubble's own chrome.
		const w = mountWith([
			{ sent_or_received: "Received", content: "<p>one</p><p>two</p>", creation: "" },
		]);
		const bubble = w.find(".jv-ububble");
		expect(bubble.exists()).toBe(true);
		expect(bubble.findAll("p")).toHaveLength(2);
		expect(bubble.find(".jv-md-body").classes()).toContain("jv-html");
	});

	it("passes bodyClass=jv-html so email HTML gets the right style block", () => {
		const w = mountWith([{ sent_or_received: "Sent", content: "<p>hi</p>" }]);
		expect(w.findComponent({ name: "Message" }).props("bodyClass")).toBe("jv-html");
	});

	it("labels the agent 'Support' and never leaks the sender email", () => {
		// The payload carries `sender` as an email and no display name at all —
		// often the service account jarvis-support-bot@jarvis.internal. Rendering
		// it would leak plumbing and mislabel a human agent.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				sender: "jarvis-support-bot@jarvis.internal",
				content: "<p>hi</p>",
			},
		]);
		const m = w.findComponent({ name: "Message" });
		expect(m.props("sender")).toBe("Support");
		expect(w.text()).not.toContain("@jarvis.internal");
	});

	it("routes the body through the sanitizer — not raw content", () => {
		// THE mutation that matters: swapping renderSupportHtml(m.content) for
		// m.content leaves every other test in this file green while shipping an
		// XSS hole. supportHtml is unit-tested in isolation; this pins its USE.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: '<img src="/files/a.png" onerror="alert(1)"><script>alert(2)</script>',
			},
		]);
		const html = w.findComponent({ name: "Message" }).props("html");
		expect(html).not.toContain("onerror");
		expect(html).not.toContain("<script");
		expect(html).toContain("jarvis.support.media.download");
	});

	it("maps attachments into the shape Message actually renders", () => {
		// Message keys attachments on `name` and titles them `title`, and picks the
		// thumbnail vs. file-chip branch off `type`. The CP sends {file_url,
		// file_name} — pass those through untouched and every attachment silently
		// vanishes.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: "<p>see attached</p>",
				attachments: [
					{ file_url: "/files/shot.png", file_name: "shot.png" },
					{ file_url: "/files/log.txt", file_name: "log.txt" },
				],
			},
		]);
		const atts = w.findComponent({ name: "Message" }).props("attachments");
		expect(atts[0]).toMatchObject({
			type: "image",
			title: "shot.png",
			name: "/files/shot.png",
		});
		expect(atts[0].file_url).toContain("jarvis.support.media.download");
		// Non-images must still be present and classified — they are the primary
		// agent-reply payload (logs, PDFs), not an edge case.
		expect(atts[1]).toMatchObject({ type: "file", title: "log.txt" });
		// …and they must actually REACH the DOM: Message renders a file chip for
		// them, which is the amendment this page depends on.
		const chip = w.find(".jv-file-chip");
		expect(chip.exists()).toBe(true);
		expect(chip.text()).toContain("log.txt");
		expect(chip.attributes("href")).toContain("jarvis.support.media.download");
	});

	it("links ticket-level attachments through the authenticated proxy", () => {
		storeDouble.thread.attachments = [{ file_url: "/files/spec.pdf", file_name: "spec.pdf" }];
		const w = mountWith([{ sent_or_received: "Sent", content: "<p>hi</p>" }]);
		const link = w.find(".jv-sup-file");
		expect(link.exists()).toBe(true);
		expect(link.attributes("href")).toContain("jarvis.support.media.download");
		expect(link.attributes("href")).toContain("spec.pdf");
		storeDouble.thread.attachments = [];
	});

	it("keeps the composer enabled on a resolved ticket and says replying reopens it", () => {
		// There is no reopen endpoint — a reply is the ONLY way back. Disabling
		// the composer here would strand the user with no path forward.
		const w = mountWith([], { name: "T1", subject: "x", status: "Resolved" });
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toContain("reopens");
	});

	it("arms Send for an attachment-only reply", () => {
		const w = mountWith([]);
		w.findComponent({ name: "Composer" }).vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
		]);
		return w.vm.$nextTick().then(() => {
			expect(w.findComponent({ name: "Composer" }).props("canSend")).toBe(true);
		});
	});

	it("posts the body BEFORE uploading, since upload needs the ticket to exist", async () => {
		// media.upload attaches to an EXISTING ticket and posting the text is what
		// reopens a resolved one — so this order is a correctness constraint, not
		// a style preference. Reversed, an attachment-and-text reply to a resolved
		// ticket uploads into a ticket that is still closed.
		const order = [];
		const w = mountWith([]);
		storeDouble.reply = vi.fn(async () => {
			order.push("reply");
			return true;
		});
		storeDouble.uploadTo = vi.fn(async () => {
			order.push("upload");
			return 1;
		});

		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "here is the log");
		c.vm.$emit("files-added", [{ name: "log.txt", type: "text/plain" }]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(order).toEqual(["reply", "upload"]);
	});

	it("keeps the draft and the pending attachment when the reply fails", async () => {
		// The regression this pins: `draft.value = ""` moving above the
		// `if (!ok) return` guard. If that happens, modelValue below reverts to
		// "" even though nothing was ever posted, and the user's text is gone.
		storeDouble.reply = vi.fn(async () => false);
		// Fresh spy: a prior test's uploadTo call history must not leak in here —
		// nothing in this file resets mocks between tests.
		storeDouble.uploadTo = vi.fn(async () => 1);
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "please help");
		c.vm.$emit("files-added", [{ name: "log.txt", type: "text/plain" }]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("modelValue")).toBe("please help");
		expect(c.props("attachments")).toHaveLength(1);
		expect(storeDouble.uploadTo).not.toHaveBeenCalled();
	});

	it("drops canSend while a reply is in flight and restores it once settled", async () => {
		// This is the double-submit guard: Composer's `busy` prop is deliberately
		// unused (see the template comment), so `canSend` going false while
		// `sending` is true is the ONLY thing standing between the user and a
		// second concurrent submit. Resolving to `false` here (reply failed, so
		// the draft is kept per the fix above) gives an unambiguous "back to true"
		// afterward without needing to re-type anything.
		let resolveReply;
		storeDouble.reply = vi.fn(() => new Promise((r) => (resolveReply = r)));
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "hello");
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await w.vm.$nextTick();

		expect(c.props("canSend")).toBe(false);

		resolveReply(false);
		await flushPromises();

		expect(c.props("canSend")).toBe(true);
	});

	it("shows no disclaimer for an open (non-resolved, non-closed) ticket", () => {
		// Only the Resolved positive case was covered before; an unconditional
		// disclaimer (e.g. dropping the ternary's else branch) would pass that
		// test and still be wrong for the common case.
		const w = mountWith([], { name: "T1", subject: "x", status: "Open" });
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toBe("");
	});

	it("keeps staged files pending when uploadTo reports fewer successes than requested", async () => {
		// Proof of fix 1: uploadTo returns a COUNT, not per-file results. A
		// silent `files.value = []` here would discard attachments the user
		// still needs to retry after a transient upload failure.
		storeDouble.uploadTo = vi.fn(async () => 0);
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("attachments")).toHaveLength(2);
	});
});
