// The phone's host of the edit form and the preview of a server draft card. The decisions are the
// shared ones (frontend/src/lib: toolDraftHost.js opens, saves and discards; toolDraftSheet.js
// says what the footer, the close and the poll do; draftModel.js builds the form); this file only
// supplies what is the phone's own: where the state lives, the pending list and the chat it
// reloads, where a message is shown, the poll's timer, the back action.
//
// Call `makeDraftSheet` once, synchronously, in the chat view's setup (it starts a link search
// that must belong to the component). It imports toolDraftHost.js, whose import chain `node --test`
// cannot resolve, so it is tested by vitest (frontend/src/components/PwaDraftSheet.spec.js); the
// pure parts are in draftSheetView.js and draftCardUi.js. Relative imports only.
import { computed, nextTick, ref, watch } from "vue";
import { makeDraftModelBuilder } from "../../../frontend/src/lib/draftModel.js";
import { applyAutoOpen } from "../../../frontend/src/lib/draftTables.js";
import {
	discardFromForm,
	openToolDraftPanel,
	openToolPreview,
	saveToolDraft,
	toolPanelDead,
	toolPanelNotices,
} from "../../../frontend/src/lib/toolDraftHost.js";
import { focusFirstMark } from "../../../frontend/src/lib/toolDraftPanel.js";
import {
	TOOL_FORM_POLL_MS,
	closeAsksFirst,
	previewPrimaryDisabled,
	toolFooterState,
	watchCalculatedCells,
} from "../../../frontend/src/lib/toolDraftSheet.js";
import { useDraftLink } from "../../../frontend/src/lib/useDraftLink.js";

// io = what the chat view gives it:
//   ui                  the token-keyed store (draftCardUi.makeDraftUi)
//   api                 { saveDraftCard, searchLink, getDoctypeFormMeta, loadDocForEdit, ruleValues }
//   items()             the pending list as drawn (reactive), conversation(), generation(),
//                       isCurrent(cid, generation)
//   streaming()         a turn is running; agent() the assistant's name
//   removePending(token)  take the card off the list and settle it (a stale read cannot bring it back)
//   loadChat(), loadPending(source)   read the conversation; read the pending list. After a Save both
//                       are read, chat first (a final draft starts no agent turn, so nothing else
//                       will); the poll and the foreground read do the same, because a card decided
//                       elsewhere still reads "pending" in the thread's rows until the thread is
//                       read again, and the list is merged from those rows
//   refreshChats(), say(text) (the chat's banner), receiptShown(token)
//   create(pa), discard(pa)   the card's Create and Cancel (runDraftDecision under their busy kind);
//                       discard resolves to runDraftDecision's string
export function makeDraftSheet(io) {
	const ui = io.ui;
	const panel = ref(null); // the edit form's model (a ref: the form edits it in place)
	const preview = ref(null); // { token, model }
	const building = ref(""); // the token whose form or preview is being built
	const confirming = ref(false); // the inline "Discard your changes?" is showing
	const bodyEl = ref(null); // the form's wrapper, where the first mark is found

	const builder = makeDraftModelBuilder({
		getDoctypeFormMeta: (doctype) => io.api.getDoctypeFormMeta(doctype),
		loadDocForEdit: (doctype, name) => io.api.loadDocForEdit(doctype, name),
		ruleValues: (doctype, table, links) => io.api.ruleValues(doctype, table, links),
	});
	const link = useDraftLink({
		draftPanel: panel,
		searchLink: (...args) => io.api.searchLink(...args),
		fillRuleValues: builder.fillRuleValues,
	});

	// What the shared doors tell the person (`d.notify`) is held while one door runs and shown
	// after it, where the door's RESULT says it belongs (notesAfter).
	let notes = [];
	const lastError = () => {
		const e = notes.filter((n) => n.type === "error").pop() || notes.pop();
		notes = [];
		return e ? e.text : "";
	};

	const view = (token) => ({
		conversation: io.conversation(),
		generation: io.generation(),
		token,
	});
	const d = {
		find: (token, conversation) =>
			io.items().find((x) => x.token === token && x.conversation === conversation),
		capture: (token) => view(token),
		chatCurrent: (v) => io.isCurrent(v.conversation, v.generation),
		pendingCurrent: (v) =>
			io.isCurrent(v.conversation, v.generation) &&
			io.items().some((x) => x.token === v.token),
		panel: () => panel.value,
		setPanel: (model) => {
			confirming.value = false;
			panel.value = model;
		},
		closePanel: () => closeNow(),
		build: (action) => builder.buildDraftModel(action),
		autoOpen: (model) => applyAutoOpen(model, null),
		building: () => building.value,
		setBuilding: (token) => {
			building.value = token;
		},
		setPreview: (p) => {
			preview.value = p;
		},
		notify: (text, opts) => notes.push({ text, type: (opts && opts.type) || "info" }),
		warn: (...args) => console.warn(...args),
		streaming: () => io.streaming(),
		inflight: new Set(),
		removePending: (token) => {
			io.removePending(token);
			ui.forget(token);
		},
		reload: async () => {
			// the write already happened: a failed read must not turn it into an error
			const cid = io.conversation();
			const generation = io.generation();
			try {
				await io.loadChat();
				if (io.isCurrent(cid, generation)) await io.loadPending();
			} catch {
				/* the next poll or resync shows it */
			}
			return io.isCurrent(cid, generation);
		},
		refreshChats: () => io.refreshChats(),
		// A draft that continues the chat queues its turn; the phone has no chip for it (the legacy
		// sheet has none either), the reply arrives as a normal turn.
		showQueued: () => {},
		save: (token, conversation, values) => io.api.saveDraftCard(token, conversation, values),
		agent: () => io.agent(),
		// as the card's own Create and Cancel read a thrown call (draftCardDecision.thrownOutcome):
		// the error's own words, else the fallback. The desktop's thrownActionError cannot be shared:
		// its import chain leaves src/ (lib/errors.js), which the phone build does not resolve.
		thrown: (e, fallback) => ({ message: (e && e.message) || fallback }),
		metaFields: (doctype) => (builder.formMetaCache[doctype] || {}).fields,
		afterMarks: (mark) => nextTick(() => focusFirstMark(bodyEl.value, mark)),
		where: ui.where,
		create: (pa) => io.create(pa),
		discard: async (pa) => (await io.discard(pa)) !== "ignored",
	};

	// ---- state the sheet and the card read ------------------------------------------------
	const open = computed(() => !!panel.value || !!preview.value);
	const editingToken = computed(() => (panel.value ? panel.value.token : ""));
	const dead = computed(() => toolPanelDead(panel.value, io.items()));
	const notices = computed(() => toolPanelNotices(panel.value, dead.value));
	const footer = computed(() =>
		toolFooterState(panel.value, { dead: dead.value, streaming: io.streaming() })
	);
	const previewItem = computed(() =>
		preview.value ? io.items().find((x) => x.token === preview.value.token) : undefined
	);
	const previewOff = computed(() =>
		previewPrimaryDisabled({
			streaming: io.streaming(),
			busy: !!(previewItem.value && ui.busyKind(previewItem.value.token)),
		})
	);

	// A preview holds no edits: it closes when its card goes (confirmed, discarded, replaced, chat
	// switched), as the desktop's does.
	watch(
		() => !!preview.value && !previewItem.value,
		(gone) => {
			if (gone) preview.value = null;
		}
	);
	// The filled calculated cells follow the person's edits: the shared watcher.
	watchCalculatedCells(watch, () => panel.value);

	// ---- while a form is open: the pending list is read again ------------------------------
	// every few seconds, and when the app comes back to the foreground. A card that left the list by
	// another route (cancelled or replaced elsewhere, confirmed by "go ahead") then shows the dead
	// banner without waiting for the person.
	let timer = null;
	let refreshing = false;
	const tick = async () => {
		if (refreshing || !panel.value) return;
		refreshing = true;
		try {
			await io.loadChat();
			await io.loadPending("auto");
		} catch {
			/* a failed read keeps the screen as it is */
		} finally {
			refreshing = false;
		}
	};
	const onVisible = () => {
		if (document.visibilityState === "visible") tick();
	};
	function stopWatching() {
		if (timer) clearInterval(timer);
		timer = null;
		document.removeEventListener("visibilitychange", onVisible);
	}
	watch(
		() => (panel.value ? panel.value.token : ""),
		(token) => {
			stopWatching();
			confirming.value = false;
			if (!token) return;
			timer = setInterval(tick, TOOL_FORM_POLL_MS);
			document.addEventListener("visibilitychange", onVisible);
		}
	);

	// ---- closing -----------------------------------------------------------------------------
	function closeNow() {
		link.resetDraftLink();
		confirming.value = false;
		panel.value = null;
	}
	function closeLinkMenu() {
		link.draftLink.value = { ...link.draftLink.value, open: false };
	}
	// The close control, the scrim, Escape and the phone's back action all come here. Returns what
	// it did: "closed", "menu" (only the link menu closed), "asking" (the inline question is now
	// showing), "kept" (the question was answered "keep editing" by closing it again), "none".
	function requestClose() {
		if (preview.value) {
			preview.value = null;
			return "closed";
		}
		const p = panel.value;
		if (!p) return "none";
		if (link.draftLink.value.open && link.draftLink.value.items.length) {
			closeLinkMenu();
			return "menu";
		}
		if (confirming.value) {
			confirming.value = false;
			return "kept";
		}
		if (closeAsksFirst(p, { dead: dead.value })) {
			confirming.value = true;
			return "asking";
		}
		closeNow();
		return "closed";
	}

	// ---- the doors -----------------------------------------------------------------------------
	// A message about a card that is still on the list goes under the card; a card that is gone has
	// no line to carry it, so it goes to the chat's banner.
	function tellAbout(token, text) {
		if (!text) return;
		if (io.items().some((x) => x.token === token)) ui.setError(token, text);
		else io.say(text);
	}

	async function openEdit(pa) {
		if (!pa || !pa.token) return "ignored";
		ui.setError(pa.token, "");
		notes = [];
		const result = await openToolDraftPanel(pa, d);
		tellAbout(pa.token, lastError());
		return result;
	}

	async function openPreview(pa) {
		if (!pa || !pa.token) return "ignored";
		ui.setError(pa.token, "");
		notes = [];
		const result = await openToolPreview(pa, d);
		tellAbout(pa.token, lastError());
		return result;
	}

	// Save in the form. What each reply does is the shared door's (saveToolDraft / settleSave); the
	// words it wanted to say are placed by its result:
	//   closed-open, keep-closed  the card is still waiting: its error line
	//   closed-dead               the form stays with the dead banner: the words go in its banner
	//   closed                    the card is gone: the chat banner, unless the reloaded thread
	//                             already holds the receipt that says it
	async function save() {
		const p = panel.value;
		if (!p) return "ignored";
		notes = [];
		const result = await saveToolDraft(p, d);
		const text = lastError();
		if (!text) return result;
		if (result === "closed-open" || result === "keep-closed") tellAbout(p.token, text);
		else if (result === "closed-dead") {
			if (panel.value === p) p.error = { message: text };
		} else if (result === "closed" && !io.receiptShown(p.token)) io.say(text);
		return result;
	}

	// The footer's "Discard draft": the card's Cancel (not "Discard changes", which only closes).
	function discardDraft() {
		const p = panel.value;
		return p ? discardFromForm(p, d) : Promise.resolve("ignored");
	}

	function createFromPreview() {
		const pa = previewItem.value;
		preview.value = null;
		return pa ? io.create(pa) : undefined;
	}
	function editFromPreview() {
		const pa = previewItem.value;
		preview.value = null;
		return pa ? openEdit(pa) : undefined;
	}

	// The router asks before leaving the chat (the phone's back button): while a sheet is open,
	// back closes (or asks about) the sheet and the chat stays.
	function guardLeave() {
		if (!open.value) return true;
		requestClose();
		return false;
	}

	// A conversation switch or leaving the chat: nothing of this chat's forms may stay.
	function reset() {
		preview.value = null;
		building.value = "";
		closeNow();
		stopWatching();
	}

	return {
		// state
		panel,
		preview,
		building,
		confirming,
		open,
		editingToken,
		dead,
		notices,
		footer,
		previewOff,
		link,
		bodyEl,
		setBody: (el) => {
			bodyEl.value = el;
		},
		// doors
		openEdit,
		openPreview,
		save,
		discardDraft,
		createFromPreview,
		editFromPreview,
		requestClose,
		keepEditing: () => {
			confirming.value = false;
		},
		discardChanges: closeNow,
		closeLinkMenu,
		guardLeave,
		reset,
	};
}
