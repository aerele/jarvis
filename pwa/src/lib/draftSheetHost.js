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
import { errorLineOf } from "./draftSheetView.js";
import { thrownActionError } from "../../../frontend/src/lib/chatErrorCopy.js";
import { applyAutoOpen } from "../../../frontend/src/lib/draftTables.js";
import { hasFlaggedRow } from "../../../frontend/src/lib/draftRowError.js";
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
//   loadChat(), loadPending(source, {quiet})   read the conversation; read the pending list, which
//                       resolves to the set of tokens the SERVER listed (null when the read did not
//                       answer cleanly). After a Save both are read, chat first (a final draft starts
//                       no agent turn, so nothing else will)
//   loadChatQuietly()   read the conversation without scrolling, loading state or banner; the poll
//                       uses it once, when the card being edited is missing from the server's list
//                       (the thread's row still says pending, and the list is merged from the rows)
//   refreshChats(), say(text) (the chat's banner), receiptShown(token), showQueued({run_id, position})
//                       (a continuation turn is waiting for a slot)
//   create(pa), discard(pa)   the card's Create and Cancel (runDraftDecision under their busy kind);
//                       discard resolves to runDraftDecision's string
export function makeDraftSheet(io) {
	const ui = io.ui;
	const panel = ref(null); // the edit form's model (a ref: the form edits it in place)
	const preview = ref(null); // { token, model }
	const building = ref(""); // the token whose form or preview is being built
	const confirming = ref(false); // the inline "Discard your changes?" is showing
	const confirmingDiscard = ref(false); // the inline "Discard this draft?" is showing
	const previewNote = ref(""); // why the form could not be built from the Preview (shown in it)
	let previewEdit = null; // { dismissed } while a form is being built from the Preview
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
			// a Preview the person dismissed while this form was being built from it: the form must not
			// open when the build lands
			if (previewEdit && previewEdit.dismissed) return;
			confirming.value = false;
			confirmingDiscard.value = false;
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
		// A draft that continues the chat can queue its turn: the waiting card, as after a send.
		showQueued: (r) => {
			if (r && r.queued)
				io.showQueued({ run_id: r.run_id, position: r.queued_position || null });
		},
		save: (token, conversation, values) => io.api.saveDraftCard(token, conversation, values),
		agent: () => io.agent(),
		// the shared sanitised text of a failed call (what the desktop's card says too): what the
		// server said on purpose, an expired session or the offline sentence, never the raw error
		thrown: (e, fallback) => thrownActionError(e, fallback),
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
	// Create in the Preview is off while a turn runs, the card is busy, or a form is being built from
	// this Preview (the Preview stays up meanwhile).
	const previewOff = computed(
		() =>
			previewPrimaryDisabled({
				streaming: io.streaming(),
				busy: !!(previewItem.value && ui.busyKind(previewItem.value.token)),
			}) || !!building.value
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
	// A Preview's note is about the Preview that is showing.
	watch(preview, () => {
		previewNote.value = "";
	});
	// The banner sits at the top of the form's scrolling body: when a message arrives that no field
	// mark takes the person to (a failed call, an offline phone, a card that died), scroll it into
	// view. A refusal that marks fields or names a row has its own scroll to the mark (the shared door).
	watch(
		() => {
			const p = panel.value;
			const line = p && errorLineOf(p.error);
			// a message that marks fields or names a row ("Row 3: ...") has its own scroll to the mark
			return line && !((p.error && p.error.fields) || []).length && !hasFlaggedRow(p)
				? line.message
				: "";
		},
		(message) => {
			if (message) d.afterMarks(null);
		}
	);
	watch(
		() => dead.value,
		(isDead) => {
			if (isDead) d.afterMarks(null);
		}
	);

	// ---- while a form is open: the pending list is read again ------------------------------
	// every few seconds, and when the app comes back to the foreground. A card that left the list by
	// another route (cancelled or replaced elsewhere, confirmed by "go ahead") then shows the dead
	// banner without waiting for the person. Each tick reads ONLY the server's pending list (and a
	// failed read is silent). The list the phone shows is that read merged with the thread's rows,
	// and a row still says "pending" until the thread is read again, so when the card being edited is
	// missing from the SERVER's list the thread is read once, quietly (no scroll, no loading state,
	// no banner), and the merge then drops the card: the banner shows.
	let timer = null;
	let refreshing = false;
	// At most ONE quiet thread read per "the card is missing from the server's list" episode: if that
	// read did not drop the card, reading the thread every tick would not either. The next episode starts
	// when the card is listed again (or another form opens). A read that did not land does not count,
	// but only three in a row are tried: then it waits for the card to be listed again or for the
	// app to come back to the foreground (a phone that is offline is not asked every five seconds).
	const QUIET_READ_TRIES = 3;
	let quietReadDone = false;
	let quietFails = 0;
	const tick = async () => {
		// a card already found dead stays dead: nothing more to read
		if (refreshing || !panel.value || dead.value) return;
		refreshing = true;
		const token = panel.value.token;
		try {
			const listed = await io.loadPending("auto", { quiet: true });
			if (listed && listed.has(token)) {
				quietReadDone = false;
				quietFails = 0;
			} else if (
				listed &&
				!quietReadDone &&
				quietFails < QUIET_READ_TRIES &&
				panel.value &&
				panel.value.token === token
			) {
				// a read that did not land (offline, overtaken by a loud load) does not count: the next
				// tick asks again, up to QUIET_READ_TRIES in a row
				quietReadDone = (await io.loadChatQuietly()) !== false;
				quietFails = quietReadDone ? 0 : quietFails + 1;
				await io.loadPending("auto", { quiet: true });
			}
		} catch {
			/* a failed read keeps the screen as it is */
		} finally {
			refreshing = false;
		}
	};
	const onVisible = () => {
		if (document.visibilityState !== "visible") return;
		quietFails = 0;
		tick();
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
			quietReadDone = false;
			quietFails = 0;
			confirming.value = false;
			confirmingDiscard.value = false;
			if (!token) return;
			timer = setInterval(tick, TOOL_FORM_POLL_MS);
			document.addEventListener("visibilitychange", onVisible);
		}
	);

	// ---- closing -----------------------------------------------------------------------------
	function closeNow() {
		link.resetDraftLink();
		confirming.value = false;
		confirmingDiscard.value = false;
		panel.value = null;
	}
	function closeLinkMenu() {
		link.draftLink.value = { ...link.draftLink.value, open: false };
	}
	// The close control, the scrim, Escape and the phone's back action all come here. Returns what
	// it did: "closed", "menu" (only the link menu closed), "asking" (the inline question is now
	// showing), "kept" (the question was answered "keep editing" by closing it again), "none".
	function requestClose() {
		if (preview.value && !panel.value) {
			if (previewEdit) previewEdit.dismissed = true;
			preview.value = null;
			return "closed";
		}
		const p = panel.value;
		if (!p) return "none";
		if (link.draftLink.value.open && link.draftLink.value.items.length) {
			closeLinkMenu();
			return "menu";
		}
		// "Discard this draft?" is showing: closing it again means keep the draft
		if (confirmingDiscard.value) {
			confirmingDiscard.value = false;
			return "kept";
		}
		if (confirming.value) {
			confirming.value = false;
			heldNavigation = null;
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

	async function buildEdit(pa) {
		ui.setError(pa.token, "");
		notes = [];
		const result = await openToolDraftPanel(pa, d);
		return { result, text: lastError() };
	}
	async function openEdit(pa) {
		if (!pa || !pa.token) return "ignored";
		const { result, text } = await buildEdit(pa);
		tellAbout(pa.token, text);
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
	//   closed-dead               the form stays with its dead banner, which already carries the words
	//                             (the shared door put them on the panel): nothing more to place
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
		else if (result === "closed" && !io.receiptShown(p.token)) io.say(text);
		return result;
	}

	// The footer's "Discard draft": the card's Cancel (not "Discard changes", which only closes).
	function discardDraft() {
		const p = panel.value;
		confirmingDiscard.value = false;
		return p ? discardFromForm(p, d) : Promise.resolve("ignored");
	}

	function createFromPreview() {
		const pa = previewItem.value;
		if (building.value) return undefined;
		preview.value = null;
		return pa ? io.create(pa) : undefined;
	}
	// Preview -> Edit in the SAME open sheet: the Preview stays until the form is built and set (the
	// template shows the form as soon as it exists), so the sheet is never closed and reopened
	// between the two (its fade, the focus handed back to the card and the scroll lock would all
	// flicker). When the form cannot be built the Preview stays and says why.
	async function editFromPreview() {
		const pa = previewItem.value;
		// a build is already running (a second tap): the first one decides, nothing is said
		if (!pa || building.value) return "ignored";
		previewNote.value = "";
		const mine = (previewEdit = { dismissed: false });
		let built;
		try {
			built = await buildEdit(pa);
		} finally {
			if (previewEdit === mine) previewEdit = null;
		}
		// the Preview was closed while the form was being built: nothing opens, nothing is said
		if (mine.dismissed) return "ignored";
		const { result, text } = built;
		if (result === "opened") {
			preview.value = null;
		} else if (result === "failed") {
			previewNote.value = text || "Could not load this draft. Try again.";
		}
		return result;
	}

	// The footer's "Discard draft" asks first (a server cancel is not undone); the question's
	// "Discard draft" is discardDraft(), its "Keep draft" closes the question.
	function askDiscardDraft() {
		if (!panel.value || footer.value.discardDisabled) return "ignored";
		confirming.value = false;
		confirmingDiscard.value = true;
		return "asking";
	}

	// ---- the router's guard ------------------------------------------------------------------
	// Called for every route change out of, or within, this chat (`to` and `from` are the router's).
	// The sheet holds one history entry while it is open (lib/sheetHistory.js), so:
	//   - the sheet's own push and pop of that entry pass;
	//   - a pop to the same chat is the phone's Back: a clean sheet closes and the pop stands, a
	//     dirty one asks inline and the guard refuses (the router puts the entry back);
	//   - a change to ANOTHER chat (a notification tap): a clean sheet closes and the navigation
	//     goes on; a dirty one asks inline, and "Discard changes" then continues that navigation.
	let heldNavigation = null;
	function guardLeave(to, from) {
		if (io.history && io.history.own()) return true;
		if (!open.value) return true;
		const sameChat = !to || !from || to.path === from.path;
		if (sameChat) {
			const result = requestClose();
			if (result !== "closed") return false;
			// a PUSH to this same chat (a notification tap for the chat already open) has nothing to add:
			// the sheet closed and its own release pops the marker, which lands on the same URL
			if (io.history && io.history.isPush(to)) return false;
			if (io.history) io.history.consumed();
			return true;
		}
		const p = panel.value;
		if (p && closeAsksFirst(p, { dead: dead.value })) {
			heldNavigation = to;
			confirming.value = true;
			return false;
		}
		// nothing to lose. With the marker entry on top the navigation is held back and continued as a
		// replace of that entry (no stale entry left behind); with none it goes on as it is, and the
		// chat's reset closes the sheet.
		if (io.history && io.history.hasEntry()) {
			io.history.leaveTo(to);
			return false;
		}
		if (io.history) io.history.consumed();
		return true;
	}
	// "Discard changes": close the sheet and keep going where the person was headed, if they were
	// (replacing the marker entry, as above).
	function discardChanges() {
		const to = heldNavigation;
		heldNavigation = null;
		if (to && io.history) io.history.leaveTo(to);
		closeNow();
	}
	function keepEditing() {
		heldNavigation = null;
		confirming.value = false;
	}

	// The marker entry follows the sheet: pushed when it opens, popped when it closes by itself.
	let resetting = false;
	watch(
		() => open.value,
		(now, was) => {
			if (!io.history || resetting) return;
			if (now && !was) io.history.mark();
			else if (was && !now) io.history.release();
		}
	);

	// A conversation switch or leaving the chat: nothing of this chat's forms may stay (the marker
	// entry, if any, stays in the history: it cannot be popped from another chat).
	function reset() {
		resetting = true;
		heldNavigation = null;
		if (io.history) io.history.consumed();
		preview.value = null;
		building.value = "";
		closeNow();
		stopWatching();
		// the watcher above runs after this tick: it must not see this close as the sheet's own
		Promise.resolve().then(() => {
			resetting = false;
		});
	}

	return {
		// state
		panel,
		preview,
		building,
		confirming,
		confirmingDiscard,
		previewNote,
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
		keepEditing,
		askDiscardDraft,
		keepDraft: () => {
			confirmingDiscard.value = false;
		},
		discardChanges,
		closeLinkMenu,
		guardLeave,
		reset,
	};
}
