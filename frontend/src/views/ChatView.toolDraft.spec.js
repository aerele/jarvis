// How ChatView.vue wires a server draft card on the desktop: the routed DraftCard at the
// pending block, Create and Cancel through the existing handlers, one card always, the stale
// banner on a typed edit panel, and the twin row. The decisions are unit-tested in
// lib/toolDraftHost.spec.js; this file pins the wiring (source slices that ASSERT their
// markers were found) and runs the real glue code where a text pin would not be enough.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { computed, nextTick, ref, watch } from "vue";
import crypto from "crypto";
import {
	nextPanelStale,
	receiptReloadNeeded,
	toolDraftCtxMap,
	withBusyKind,
} from "../lib/toolDraftHost.js";
import { isToolDraftItem } from "../lib/toolDraft.js";
import { draftReceipt, isDraftReceiptRow } from "../lib/draftReceipt.js";

const src = fs.readFileSync(path.resolve(__dirname, "ChatView.vue"), "utf8");

// The text between two markers, failing loudly when either is missing.
function slice(from, to, fromAt = 0) {
	const a = src.indexOf(from, fromAt);
	expect(a, `start marker not found: ${from}`).toBeGreaterThan(-1);
	const b = src.indexOf(to, a + from.length);
	expect(b, `end marker not found: ${to}`).toBeGreaterThan(a);
	return src.slice(a, b);
}

const PENDING_BLOCK = slice(
	'<template v-for="(pa, pi) in visiblePendingActions"',
	"<!-- A typed yes/no that bound no card"
);
const DRAFT_BRANCH = slice(
	'<div v-if="isToolDraftItem(pa)"',
	'<div v-else class="jv-action jv-pending">'
);
const LEGACY_BRANCH = PENDING_BLOCK.slice(
	PENDING_BLOCK.indexOf('<div v-else class="jv-action jv-pending">')
);
const squash = (s) => s.replace(/\s+/g, " ");

describe("the pending block routes on the card", () => {
	// Break: the draft branch moves out of the v-for, loses its v-if, or a second chain appears
	// so a draft item would draw BOTH cards.
	it("is one v-if / v-else chain per item: DraftCard first, today's card as the v-else", () => {
		expect(PENDING_BLOCK.indexOf('v-if="isToolDraftItem(pa)"')).toBeGreaterThan(-1);
		expect(PENDING_BLOCK.indexOf('v-else class="jv-action jv-pending"')).toBeGreaterThan(
			PENDING_BLOCK.indexOf('v-if="isToolDraftItem(pa)"')
		);
		expect(PENDING_BLOCK.match(/v-if="isToolDraftItem\(pa\)"/g)).toHaveLength(1);
		expect(PENDING_BLOCK.match(/<DraftCard\b/g)).toHaveLength(1);
		expect(DRAFT_BRANCH).toContain("<DraftCard");
		expect(LEGACY_BRANCH).not.toContain("<DraftCard");
		// the template that carries the loop keeps the key the card list had
		expect(PENDING_BLOCK).toContain(':key="pa.token"');
	});

	// Break: flag-off parity. With no tool draft item the pending block must still be today's
	// card: its card, its Confirm and Discard buttons and its Approve & run, all on the v-else.
	it("keeps today's card, byte for byte in content, on the legacy branch", () => {
		const legacy = squash(LEGACY_BRANCH);
		for (const kept of [
			"Confirm before this runs",
			'<PendingCard v-if="pendingCardOf(pa)"',
			'@click="confirmPending(pa)"',
			'@click="discardPending(pa)"',
			'@click="approveAndRunPending(pa)"',
			'v-if="pi === visiblePendingActions.length - 1"',
			"{{ typedApprovalHint }}",
			"✓ Confirm",
		])
			expect(legacy, kept).toContain(kept);
		expect(legacy).toContain("Discard </button>");
	});

	// Break: flag-off parity. Anything the legacy confirmation card draws changes (a class, a
	// label, a handler, a condition), which the pins above can miss. With no server draft in the
	// chat, this inner markup, with every whitespace character removed (so prettier's re-wrapping
	// does not count), is what a pending item draws today. It was proved equal to the markup at
	// commit 8d0e95fed, before the draft branch existed (4005 characters).
	// TO CHANGE THE LEGACY CARD ON PURPOSE: edit it, run this spec, copy the new hash from the
	// failure message into LEGACY_CARD_SHA256 and the new length into LEGACY_CARD_LENGTH.
	const LEGACY_CARD_SHA256 = "21f60a5df94427facd9e2dabbb8f207cb94bdc96c3bbb94264381b113507c004";
	const LEGACY_CARD_LENGTH = 4005;
	it("the legacy card's inner markup is exactly what it was before drafts", () => {
		const open = '<div v-else class="jv-action jv-pending">';
		const inner = LEGACY_BRANCH.slice(open.length, LEGACY_BRANCH.lastIndexOf("</template>"));
		const stripped = inner.slice(0, inner.lastIndexOf("</div>")).replace(/\s+/g, "");
		expect([
			stripped.length,
			crypto.createHash("sha256").update(stripped).digest("hex"),
		]).toEqual([LEGACY_CARD_LENGTH, LEGACY_CARD_SHA256]);
	});

	// Break: the draft branch carries a second confirm path, or draws the legacy hint on top of
	// the card's own, or an old PendingCard.
	it("the draft branch has no legacy buttons, hint or PendingCard", () => {
		for (const gone of [
			"confirmPending(",
			"discardPending(",
			"approveAndRunPending(",
			"<PendingCard",
			"typedApprovalHint",
			"jv-action-typehint",
			"jv-action-primary",
		])
			expect(DRAFT_BRANCH, gone).not.toContain(gone);
	});

	// Break: the legacy hint is drawn twice, or is moved out of its branch (a draft card then
	// shows DraftCard's own hint AND this one).
	it("draws the legacy hint exactly once, on the legacy branch", () => {
		expect(src.match(/\{\{ typedApprovalHint \}\}/g)).toHaveLength(1);
		expect(LEGACY_BRANCH).toContain("{{ typedApprovalHint }}");
	});

	// Break: Preview or Edit points at the wrong handler (or at nothing), Create / Cancel point at
	// the wrong one, or the card is told the wrong state.
	it("hands DraftCard the card, the ctx, and its four events", () => {
		const mount = squash(slice("<DraftCard", "/>", src.indexOf('v-if="isToolDraftItem(pa)"')));
		expect(mount).toContain(':card="cardOfItem(pa)"');
		expect(mount).toContain(':ctx="draftCtxByToken[pa.token]"');
		expect(mount).toContain('@create="createToolDraft(pa)"');
		expect(mount).toContain('@cancel="cancelToolDraft(pa)"');
		expect(mount).toContain('@preview="previewToolDraft(pa)"');
		expect(mount).toContain('@edit="openToolDraft(pa)"');
	});

	// Break: a failed Create / Cancel shows nothing for a draft card (the error line is drawn
	// only by the legacy branch today).
	it("shows the item's error under the card", () => {
		expect(squash(DRAFT_BRANCH)).toContain('<div v-if="pa.error"');
		expect(DRAFT_BRANCH).toContain("<ActionError");
	});
});

describe("Create and Cancel use the existing handlers, with a busy kind", () => {
	const fn = (name) => slice(`function ${name}(pa)`, "\n}\n");

	// Break: Create calls something other than the confirm handler (a second confirm path), or
	// the busy kind is not "create"; the same for Cancel.
	it("Create is confirmPending under busyKind create, Cancel is discardPending under cancel", () => {
		expect(fn("createToolDraft")).toContain('withBusyKind(pa, "create", confirmPending)');
		// Cancel runs through the form-aware door, whose discard is the busy-kind wrapper
		expect(fn("cancelToolDraft")).toContain("cancelToolItem(pa, toolDeps)");
		expect(squash(slice("const toolDeps = {", "\n};\n"))).toContain(
			'discard: (pa) => withBusyKind(pa, "cancel", discardPending)'
		);
		expect(squash(slice("const toolDeps = {", "\n};\n"))).toContain(
			"create: (pa) => createToolDraft(pa)"
		);
	});

	// The real computed from ChatView, run with real refs.
	function ctxMap() {
		const code =
			slice("const draftCtxByToken = computed(", "\n);\n") + "\n);\nreturn draftCtxByToken;";
		const draft = { draft: { verb: "create", values: { a: 1 } } };
		const visiblePendingActions = ref([
			{ token: "t1", recent: true, preview: { card: draft } },
			{ token: "t2", recent: true, preview: { card: { kind: "verb" } } },
		]);
		const convStreaming = ref(false);
		const typedApprovalHint = ref("stack hint");
		const draftPanel = ref(null);
		const toolDraftBuilding = ref("");
		const map = new Function(
			"computed",
			"toolDraftCtxMap",
			"visiblePendingActions",
			"convStreaming",
			"typedApprovalHint",
			"draftPanel",
			"toolDraftBuilding",
			code
		)(
			computed,
			toolDraftCtxMap,
			visiblePendingActions,
			convStreaming,
			typedApprovalHint,
			draftPanel,
			toolDraftBuilding
		);
		return {
			map,
			visiblePendingActions,
			convStreaming,
			typedApprovalHint,
			draftPanel,
			toolDraftBuilding,
		};
	}

	// Break: the ctx is built per render (a new object for every streamed token), or one of its
	// inputs (the turn flag, the visible stack, the stack hint) is not what the view really has.
	it("builds the ctx once per change of its inputs, from the visible stack, the turn flag and the stack hint", () => {
		const { map, convStreaming, typedApprovalHint, visiblePendingActions } = ctxMap();
		const first = map.value.t1;
		expect(Object.keys(map.value)).toEqual(["t1"]);
		expect(map.value.t1).toBe(first);
		expect([first.position, first.count, first.stackHint, first.streaming]).toEqual([
			1,
			2,
			"stack hint",
			false,
		]);
		convStreaming.value = true;
		expect(map.value.t1.streaming).toBe(true);
		typedApprovalHint.value = "other hint";
		expect(map.value.t1.stackHint).toBe("other hint");
		visiblePendingActions.value[0].busy = true;
		visiblePendingActions.value[0].busyKind = "create";
		expect(map.value.t1.busy).toBe("create");
	});

	// Break: the card does not read "Editing" while its form is open (and a typed form is taken for
	// it), or a card whose form is being built can be pressed again.
	it("reads the open form's token (a tool form only) and the token being built", () => {
		const { map, draftPanel, toolDraftBuilding } = ctxMap();
		expect(map.value.t1.editing).toBe(false);
		draftPanel.value = { tool: true, token: "t1" };
		expect(map.value.t1.editing).toBe(true);
		draftPanel.value = { token: "t1" }; // a typed panel never "edits" a card
		expect(map.value.t1.editing).toBe(false);
		toolDraftBuilding.value = "t1";
		expect(map.value.t1.busy).toBe("busy");
	});
});

describe("Create reloads the chat when the card is spent without a receipt (S10)", () => {
	// createToolDraft, run with the real busy wrapper and the real decision, and a fake confirm.
	// `confirm` plays what confirmPending does: take the card off the list (and, on success, load
	// the decided row, which is what a normal Create's own reload does).
	async function run(confirm, { switchTo = "" } = {}) {
		const code =
			slice("async function createToolDraft(pa) {", "\n}\n") +
			"\n}\nreturn createToolDraft;";
		const pendingActions = ref([{ token: "t1", conversation: "c1" }]);
		const messages = ref([{ role: "tool", tool_call_id: "t1", action_outcome: "" }]);
		const currentId = ref("c1");
		const loads = [];
		const confirmPending = async () => {
			await confirm({ pendingActions, messages });
			if (switchTo) currentId.value = switchTo;
		};
		const createToolDraft = new Function(
			"withBusyKind",
			"confirmPending",
			"receiptReloadNeeded",
			"currentId",
			"pendingActions",
			"messages",
			"loadConversation",
			code
		)(
			withBusyKind,
			confirmPending,
			receiptReloadNeeded,
			currentId,
			pendingActions,
			messages,
			async (id) => loads.push(id)
		);
		await createToolDraft(pendingActions.value[0]);
		return loads;
	}
	const drop = ({ pendingActions }) => (pendingActions.value = []);

	// Break: the stale-record Create removes the card and shows no "Not saved ..." receipt.
	it("reloads once when the card left the list and no receipt is loaded", async () => {
		expect(await run(drop)).toEqual(["c1"]);
	});

	// Break: every normal Create reloads twice.
	it("does not reload again when the confirm already loaded the receipt", async () => {
		const loads = await run(({ pendingActions, messages }) => {
			drop({ pendingActions });
			messages.value = [{ role: "tool", tool_call_id: "t1", action_outcome: "confirmed" }];
		});
		expect(loads).toEqual([]);
	});

	// Break: a refused Create that keeps the card reloads the chat for nothing.
	it("does not reload while the card is still pending", async () => {
		expect(await run(() => {})).toEqual([]);
	});

	// Break: the reload lands on another chat that was opened during the confirm.
	it("does not reload a chat the person has left", async () => {
		expect(await run(drop, { switchTo: "c2" })).toEqual([]);
	});
});

describe("the typed Preview closes when a server draft card arrives", () => {
	// Break: the typed Preview overlay stays up over the new card; its Confirm saves nothing.
	it("clears previewOpen when a draft becomes parked, and only then", async () => {
		const code = slice("watch(hasParkedDraft, (parked) => {", "\n});\n") + "\n});";
		const hasParkedDraft = ref(false);
		const previewOpen = ref(true);
		new Function("watch", "hasParkedDraft", "previewOpen", code)(
			watch,
			hasParkedDraft,
			previewOpen
		);
		hasParkedDraft.value = true;
		await nextTick();
		expect(previewOpen.value).toBe(false);
		previewOpen.value = true;
		hasParkedDraft.value = false;
		await nextTick();
		expect(previewOpen.value).toBe(true);
	});
});

describe("one card always", () => {
	// Break: a typed draft is picked for this chat while a server draft card is on screen.
	// The funnel is the only caller of liveDraftMessage; every consumer (the tail rule, the
	// card message, "show it", the typed card, the panel) derives from it.
	it("the typed draft funnel yields nothing while a draft card is parked", () => {
		const funnel = slice("const _liveDraft = computed(() => {", "\n});\n");
		const gate = funnel.indexOf("if (hasParkedDraft.value) return null;");
		expect(gate).toBeGreaterThan(-1);
		expect(gate).toBeLessThan(funnel.indexOf("return live &&"));
		expect(funnel).toContain("liveDraftMessage(messages.value, _lastAssistant.value)");
	});

	// Break: a new place picks the typed draft without the gate. The calls of the two pickers
	// are exactly these: the funnel (gated above) and the post-cancel hide, which only decides
	// whether a draft that was just cancelled stays hidden (so it cannot revive the card).
	it("no other caller picks a typed draft", () => {
		expect(src.match(/\bliveDraftMessage\(/g)).toHaveLength(1);
		expect(src.match(/\bliveDraftRow\(/g)).toHaveLength(1);
		expect(slice("async function cancelDraftFor(key)", "\n}\n")).toContain(
			"liveDraftRow(messages.value)?.name === key"
		);
	});

	// Break: hasParkedDraft is declared after _liveDraft. The first watcher at setup evaluates
	// the funnel, so a later declaration throws a ReferenceError and the chat never opens.
	it("hasParkedDraft is built from the visible pending list and declared before the funnel", () => {
		const decl = src.indexOf("const hasParkedDraft = computed(");
		expect(decl).toBeGreaterThan(-1);
		expect(src.indexOf("const visiblePendingActions = computed(")).toBeLessThan(decl);
		expect(src.indexOf("const pendingActions = ref([]);")).toBeLessThan(decl);
		expect(decl).toBeLessThan(src.indexOf("const _liveDraft = computed("));
		expect(squash(src.slice(decl, decl + 140))).toContain(
			"itemsHoldParkedDraft(visiblePendingActions.value)"
		);
	});
});

describe("the typed edit panel when a server draft card arrives", () => {
	const PANEL = slice('ref="draftPanelEl"', "</aside>");

	// Break: the banner is missing, sits below the form, uses another style or other words.
	it("shows the banner above the form body, in the panel's error style", () => {
		const banner = PANEL.indexOf('v-if="draftPanelStale"');
		expect(banner).toBeGreaterThan(-1);
		expect(banner).toBeGreaterThan(PANEL.indexOf('class="jv-artifact-head"'));
		expect(banner).toBeLessThan(PANEL.indexOf("<DraftEditForm"));
		expect(squash(PANEL.slice(banner, banner + 200))).toContain(
			'<Banner type="error" :message="DRAFT_STALE_TEXT" />'
		);
	});

	// Break: a stale panel can still save (nothing happens, silently), or Discard / X change.
	it("disables both save buttons while stale, and leaves Discard and the X alone", () => {
		const second = slice(
			'class="jv-action-2nd"',
			'@click="applyDraft(1)"',
			src.indexOf('ref="draftPanelEl"')
		);
		const primary = slice(
			'class="jv-action-primary"',
			'@click="applyDraft(0)"',
			src.indexOf('@click="applyDraft(1)"')
		);
		expect(second).toContain("|| draftPanelStale");
		expect(primary).toContain("|| draftPanelStale");
		expect(PANEL).toContain('@click="closeDraftPanel"');
		const discard = slice(
			'class="jv-action-discard"',
			'@click="discardDraft"',
			src.indexOf('@click="applyDraft(0)"')
		);
		expect(discard).not.toContain("draftPanelStale");
	});

	// The real watcher from ChatView, run with real refs.
	function stale() {
		const code =
			slice("const draftPanelStale = ref(false);", "\n});\n") +
			"\n});\nreturn draftPanelStale;";
		const hasParkedDraft = ref(false);
		const draftPanel = ref(null);
		const flag = new Function(
			"ref",
			"watch",
			"nextPanelStale",
			"hasParkedDraft",
			"draftPanel",
			code
		)(ref, watch, nextPanelStale, hasParkedDraft, draftPanel);
		return { flag, hasParkedDraft, draftPanel };
	}

	// Break: the flag is wired to the wrong sources (a closed panel stale, a new panel stale
	// from the old one, the flag cleared when the server draft is decided, never set).
	it("opens on arrival, survives the card being decided, clears on close and on a new panel", async () => {
		const { flag, hasParkedDraft, draftPanel } = stale();
		const seen = [];
		const step = async (name, act) => {
			act();
			await nextTick();
			seen.push([name, flag.value]);
		};
		await step("panel opens", () => (draftPanel.value = { id: 1 }));
		await step("server draft arrives", () => (hasParkedDraft.value = true));
		await step("server draft decided", () => (hasParkedDraft.value = false));
		await step(
			"swapped for a new panel, nothing parked",
			() => (draftPanel.value = { id: 9 })
		);
		await step("panel closes", () => (draftPanel.value = null));
		await step("another panel opens", () => (draftPanel.value = { id: 2 }));
		await step("arrival again", () => (hasParkedDraft.value = true));
		await step("swapped for a new panel while parked", () => (draftPanel.value = { id: 3 }));
		await step("closed", () => (draftPanel.value = null));
		await step("arrival with no panel", () => (hasParkedDraft.value = false));
		await step("arrival with no panel (parked)", () => (hasParkedDraft.value = true));
		expect(seen).toEqual([
			["panel opens", false],
			["server draft arrives", true],
			["server draft decided", true],
			["swapped for a new panel, nothing parked", false],
			["panel closes", false],
			["another panel opens", false],
			["arrival again", true],
			["swapped for a new panel while parked", true],
			["closed", false],
			["arrival with no panel", false],
			["arrival with no panel (parked)", false],
		]);
	});
});

describe("a server draft card's edit form in the panel", () => {
	const PANEL = slice('ref="draftPanelEl"', "</aside>");
	const TYPED_FOOT = slice(
		'<div v-if="!draftPanel.tool" class="jv-draft-foot">',
		"<!-- A server draft:"
	);
	const TOOL_FOOT = slice(
		'<div v-else class="jv-draft-foot">',
		"</aside>",
		src.indexOf('<div v-else class="jv-draft-foot">')
	);

	// Break: the tool form is judged by the typed rule and opens with "This draft changed" at once.
	it("is never stale just because its own draft is parked", async () => {
		const flagOf = (() => {
			const code =
				slice("const draftPanelStale = ref(false);", "\n});\n") +
				"\n});\nreturn draftPanelStale;";
			const hasParkedDraft = ref(true);
			const draftPanel = ref(null);
			const flag = new Function(
				"ref",
				"watch",
				"nextPanelStale",
				"hasParkedDraft",
				"draftPanel",
				code
			)(ref, watch, nextPanelStale, hasParkedDraft, draftPanel);
			return { flag, draftPanel };
		})();
		flagOf.draftPanel.value = { tool: true, token: "t1" };
		await nextTick();
		expect(flagOf.flag.value).toBe(false);
		flagOf.draftPanel.value = { id: "typed" };
		await nextTick();
		expect(flagOf.flag.value).toBe(true);
		flagOf.draftPanel.value = { tool: true, token: "t1" };
		await nextTick();
		expect(flagOf.flag.value).toBe(false);
	});

	// Break: the footer of a server draft reaches the typed save or the typed discard, offers
	// Create & Submit, or says other words than the card's.
	it("has its own footer: the tool save, the card's Cancel, no Create & Submit, no typed handler", () => {
		expect(PANEL.indexOf('v-if="!draftPanel.tool"')).toBeLessThan(PANEL.indexOf("v-else"));
		const foot = squash(TOOL_FOOT);
		expect(foot).toContain('@click="saveToolDraft()"');
		expect(foot).toContain('@click="discardToolPanel()"');
		// the words and the disabled rules are lib/toolDraftSheet.toolFooterState (a table and the
		// template reads in toolDraftSheet.spec.js)
		expect(foot).toContain("{{ toolFoot.primaryLabel }}");
		for (const gone of [
			"applyDraft",
			"discardDraft",
			"Create &amp; Submit",
			"draftCta",
			"cancelDraft",
		])
			expect(foot, gone).not.toContain(gone);
		// the typed footer keeps its buttons and handlers (only a v-if was added to its div)
		const typed = squash(TYPED_FOOT);
		for (const kept of [
			'@click="applyDraft(1)"',
			'@click="applyDraft(0)"',
			'@click="discardDraft"',
			"draftCta",
		])
			expect(typed, kept).toContain(kept);
	});

	// Break: the notices (dead card, blocked save, unloaded record) are not drawn, or below the form.
	it("draws the notices above the form, one banner each", () => {
		const at = PANEL.indexOf('v-for="n in toolNotices"');
		expect(at).toBeGreaterThan(PANEL.indexOf('v-if="draftPanelStale"'));
		expect(at).toBeLessThan(PANEL.indexOf("<DraftEditForm"));
		expect(squash(PANEL.slice(at, at + 260))).toContain(
			'<Banner :type="n.type" :message="n.text" />'
		);
	});

	// Break: the panel header says "Draft - not saved" for a server UPDATE draft (the phone and the
	// Preview say "Pending change"), or the typed panel's text changes.
	it("the header badge reads Pending change for a tool update only; the typed text stays", () => {
		const badge = squash(PANEL.slice(PANEL.indexOf('<span class="jv-draft-badge">')));
		expect(badge).toContain(
			'draftPanel.tool && draftPanel.verb === "update" ? draftBadgeText(true) : "Draft - not saved"'
		);
	});

	// Break: a dead card is not judged against the list the screen draws, or the banner is not
	// derived (a one-shot watcher can miss it).
	it("the dead-card flag and the notices are computed from the panel and the visible list", () => {
		expect(squash(slice("const draftPanelDead = computed(", ");", 0))).toContain(
			"toolPanelDead(draftPanel.value, visiblePendingActions.value)"
		);
		expect(squash(slice("const toolNotices = computed(", ";", 0))).toContain(
			"toolPanelNotices(draftPanel.value, draftPanelDead.value)"
		);
	});

	// Break: a typed draft that comes back after the card died replaces the form and drops its edits.
	it("the typed watcher leaves a server draft's form alone", () => {
		const w = slice("watch(actionFor, () => {", "\n});\n");
		expect(w).toContain("if (draftPanel.value && !draftPanel.value.tool) {");
	});

	// Break: the tool path reaches the typed draft's functions, which need activeAction and would
	// either do nothing or save the wrong thing.
	it("no door of the tool path calls the typed draft's functions", () => {
		const doors = slice("const toolDeps = {", "function onToolPreviewEdit()", 0);
		for (const typed of [
			"applyDraft(",
			"openDraftPanel(",
			"draftModelViewIsCurrent(",
			"cancelDraftFor(",
			"confirmSummary(",
		])
			expect(doors, typed).not.toContain(typed);
		const lib = fs.readFileSync(path.resolve(__dirname, "../lib/toolDraftHost.js"), "utf8");
		for (const typed of ["applyAction", "applyDraft", "openDraftPanel", "dismissDraft"])
			expect(lib, typed).not.toContain(typed);
	});

	// Break: the second preview replaces the typed one (pinned elsewhere) or confirms something
	// other than the card's Create, or says other words.
	it("has its own preview mount; its Confirm is the card's Create and its Edit opens the form", () => {
		expect(src).toContain(':model="cardModel"');
		const mount = squash(slice("<DraftPreview", "/>", src.indexOf('v-if="toolPreview"') - 20));
		for (const want of [
			':model="toolPreview.model"',
			':confirm-label="toolPrimaryLabel(toolPreview.model)"',
			'@confirm="onToolPreviewConfirm"',
			'@edit="onToolPreviewEdit"',
			'@close="toolPreview = null"',
		])
			expect(mount, want).toContain(want);
		expect(slice("function onToolPreviewConfirm()", "\n}\n")).toContain("createToolDraft(pa)");
		expect(slice("function onToolPreviewEdit()", "\n}\n")).toContain("openToolDraft(pa)");
	});
});

describe("the twin row of a Claude-route draft", () => {
	// A tool row with a call id starting `mcp-`, completed, with no outcome is the Claude route's
	// echo of the call. The decided draft row (an outcome) is the only receipt.
	const twin = {
		name: "m-twin",
		role: "tool",
		tool_name: "create_doc",
		tool_call_id: "mcp-7f3a",
		tool_status: "completed",
		action_outcome: "",
		seq: 11,
	};
	const decided = {
		name: "m-decided",
		role: "tool",
		tool_name: "create_doc",
		tool_call_id: "tok-1",
		tool_status: "",
		action_outcome: "confirmed",
		pending_card: null,
		seq: 10,
	};
	const pendingRow = {
		name: "m-pending",
		role: "tool",
		tool_name: "create_doc",
		tool_call_id: "tok-2",
		tool_status: "pending",
		pending_card: { kind: "create", draft: { verb: "create", values: { a: 1 } } },
		pending_card_safe: true,
		seq: 12,
	};
	const assistant = { name: "m-a", role: "assistant", content: "Done", seq: 9 };

	it("is not drawn as a receipt chip next to the decided row", () => {
		const code = slice(
			"const orphanToolFailures = computed(() => {",
			"// Group role=tool messages"
		);
		const messages = ref([assistant, decided, twin]);
		const visible = new Function(
			"computed",
			"messages",
			"toolFailureCopy",
			"currentRunId",
			"currentMsgId",
			"recovering",
			code + "\nreturn visibleMessages;"
		)(computed, messages, () => "failed", ref(null), ref(null), ref(null));
		// Break: the twin gains an outcome-free path into the thread. The decided row stays.
		expect(visible.value.map((m) => m.name)).toEqual(["m-a", "m-decided"]);
	});

	it("is not seeded as a pending card (even a twin that carries a card), while a pending draft row is", () => {
		const code = slice(
			"function seedPendingFromRows(msgs, convId) {",
			"// Tokens with a Confirm/Discard RPC"
		);
		const queued = [];
		new Function(
			"enqueuePending",
			"pendingActionFromRow",
			"rows",
			code + "\nseedPendingFromRows(rows, 'c');"
		)(
			(x) => queued.push(x),
			(m) => ({ token: m.tool_call_id }),
			[
				twin,
				{ ...twin, pending_card: pendingRow.pending_card, pending_card_safe: true },
				decided,
				pendingRow,
			]
		);
		expect(queued).toEqual([{ token: "tok-2" }]);
	});
});

describe("the receipt of a decided server draft card", () => {
	// Break: the chip is told a draft wording for a row the server did not stamp (a flag-off site, a
	// sensitive card, another tool), or loses it for a stamped row, or is not handed the words at all.
	it("hands ReceiptChip the draft wording only for a stamped draft row", () => {
		expect(squash(slice("<ReceiptChip", "/>"))).toContain(
			':message="m" :draft-receipt="draftReceiptFor(m)"'
		);
		const code = [
			slice("function draftReceiptFor(m) {", "// The stack's hint, drawn under"),
			"return draftReceiptFor;",
		].join("\n");
		const draftReceiptFor = new Function("isDraftReceiptRow", "draftReceipt", code)(
			isDraftReceiptRow,
			draftReceipt
		);
		const discarded = (over) => ({
			role: "tool",
			tool_name: "create_doc",
			tool_call_id: "tok-1",
			action_outcome: "discarded",
			...over,
		});
		expect(draftReceiptFor(discarded({}))).toBeNull();
		expect(draftReceiptFor(discarded({ draft_card: false }))).toBeNull();
		expect(draftReceiptFor(discarded({ draft_card: false, draft_receipt: true }))).toEqual({
			text: "Draft cancelled",
			tone: "ok",
		});
	});
});

// Break: a card or note in the thread starts under the avatar (the owner's "starts from more
// left"), or the indent drifts from the assistant row (avatar size or gap changed).
describe("every agent-side card and note starts where the agent's replies do", () => {
	const message = fs.readFileSync(
		path.resolve(__dirname, "../components/chat/Message.vue"),
		"utf8"
	);
	const indent = Number(/--jv-reply-indent: (\d+)px;/.exec(src)?.[1]);
	const rule = (selector) => {
		const at = src.indexOf(selector + " {");
		expect(at, `rule not found: ${selector}`).toBeGreaterThan(-1);
		return src.slice(at, src.indexOf("}", at));
	};

	it("the indent is the reply avatar plus the row's gap", () => {
		const avatar = Number(
			/<HolidayMark\s+:size="(\d+)"/.exec(slice("<!-- assistant -->", "#above-body"))?.[1]
		);
		const gap = Number(
			/class="jv-amsg" style="display: flex; gap: (\d+)px"/.exec(message)?.[1]
		);
		expect(indent).toBe(avatar + gap);
	});

	it("receipts, tiles and pending cards use the indent, and user bubbles and banners do not", () => {
		const listed = rule(
			".jv-thread-inner > :is(.jv-receipt, .jv-toolfail, .jv-action, .jv-tool-draft, .jv-fbw, .jv-rrc)"
		);
		expect(listed).toContain("margin-left: var(--jv-reply-indent)");
		expect(rule(".jv-thread-inner > :is(.jv-stack-hint, .jv-pending-older-note)")).toContain(
			"calc(var(--jv-reply-indent) + 14px)"
		);
		for (const left of [".jv-umsg", ".jv-ububble", ".jv-daydivider", ".jv-macrobar"])
			expect(listed, left).not.toContain(left);
	});
});
