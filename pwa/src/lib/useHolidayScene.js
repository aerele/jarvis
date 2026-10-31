// Keep in step with frontend/src/lib/useHolidayScene.js (the desktop original). The phone
// differs in: the layout it asks for ("phone"), `fits` (is there room for the scene on this
// screen, asked before any art or showing slot is used), no hero glide (the phone's hero never
// moves), and the tap that ended a scene not also being a click on what is under it.
//
// The play logic of the holiday welcome scene: when it may play, the one overall deadline,
// what ends it, and what the page shows meanwhile. No DOM of its own: the page gives it a
// host for the art, the boxes of the mark, heading and card band, and the beats it should
// act on (scene contract: jarvis/public/js/shared/holiday/scenes/scene-runtime.mjs).
//
// Phases: pending (the scene may still play: cards hidden, normal greeting) -> playing ->
// rest. Or pending -> rest at once when the answer is no, there is no room, the deadline
// passes or the art will not load. A scene never restarts on its own. The replay on the mark runs
// as "replaying" and hides the cards like the first play (the floor row stands in their band, so
// they would be under the art), then brings them back at rest. A replay tapped while the last
// scene is still fading out waits for it (one tap is kept).
//
// A tab opened hidden does not begin until it is shown, so a day's showing is not spent on
// a scene nobody sees. `reason` says why the scene is or is not playing, for support.
import { computed, reactive, ref } from "vue";
import { mayPlayLocal, recordLocal } from "@jsshared/holiday/cadence.mjs";
import { report as reportError } from "@shared/lib/errorReporter";
import { recordHolidayShown } from "../api";
import { _resetReportedForTests, reportSceneFailure } from "./holidayReport";
import { hasScene, loadScene } from "./holidayScenes";

export const DEADLINE_MS = 2500; // import() then record_shown, from mount
const SMILE_MS = 1500;
const FADE_MS = { end: 600, early: 400, instant: 0 }; // how the cards come back

// The answer a reply (or a give-up) was already seen for. A second new chat in the same
// tab reads the same answer, so it must not hide the cards again; the next get_today
// brings a new answer object and with it a new chance.
let spentFor = null;

/** Test-only: forget what was spent, so each spec starts from a fresh page. */
export function _resetSpentForTests() {
	spentFor = null;
	_resetReportedForTests();
}

function safeStorage() {
	try {
		return window.localStorage;
	} catch {
		return null;
	}
}

export function createHolidayScene({
	answer, // () => today's holiday answer
	host, // () => the element the art is drawn into, or null
	getBoxes, // () => {host, hero, mark, heading, cards}
	fits = () => true, // () => is there room for the scene on this screen right now
	composerEmpty = () => true,
	reduced = () => false,
	idPrefix = "jvs",
	recordShown = recordHolidayShown,
	load = loadScene,
	now = () => new Date(),
	storage = safeStorage(),
	report = reportError,
	deadlineMs = DEADLINE_MS,
	fontsReady = () => Promise.resolve(), // () => a promise: the page's fonts are loaded
	doc = typeof document === "undefined" ? null : document, // for the tab's visibility
}) {
	const phase = ref("idle");
	// What the mark shows: the face, the item, the smile, and where the eyes look.
	const mark = reactive({ face: false, item: false, happy: false, look: null });
	// The greeting: swapped to the day heading, and whether that swap is instant.
	const text = reactive({ swapped: false, instant: false });
	const fadeMs = ref(0);
	// Why the scene is or is not playing: loaded | load-failed | deadline | declined | no-room |
	// threw | hidden (null until something is known).
	const reason = ref(null);
	const cardsHidden = computed(() => phase.value !== "rest" && phase.value !== "idle");

	let scene = null;
	// Every scene started, kept after it is done: one that settled early still has the smile's
	// closing timer, and only a last instant settle on it clears that.
	const started = [];
	let deadline = null;
	let smileTimer = null;
	let run = 0; // bumped whenever a pending decision is abandoned
	let swallowClick = false; // the tap that ended a scene must not also act on what is under it
	let queuedReplay = false; // a replay was tapped before the last scene was over
	let waitingToShow = false; // begin() is waiting for the tab to be shown

	function mayPlay() {
		const a = answer();
		if (!a || (a.kind !== "day" && a.kind !== "birthday")) return false;
		if (a.scene !== true || a.may_play === false) return false;
		if (!hasScene(a.design) || spentFor === a || reduced() || !composerEmpty()) return false;
		return storage ? mayPlayLocal(storage, now(), a.tz) : true;
	}

	function rest() {
		Object.assign(mark, { face: true, item: true, happy: false, look: null });
		text.swapped = true;
		text.instant = true;
	}

	// Decided before the first paint, so the cards are never shown and then hidden.
	if (mayPlay()) {
		phase.value = "pending";
	} else {
		rest();
		reason.value = "declined";
		phase.value = "rest";
	}

	function onBeat(name, ...args) {
		if (name === "peek") Object.assign(mark, { face: true, happy: false, look: null });
		else if (name === "look") mark.look = args[0] || args[1] ? [args[0], args[1]] : null;
		else if (name === "deliver") mark.item = true;
		else if (name === "happy") Object.assign(mark, { happy: true, look: null });
		else if (name === "swap") {
			text.swapped = true;
			text.instant = !!(args[0] && args[0].instant);
		} else if (name === "settle") onSettle(args[0]);
	}

	function onSettle(mode) {
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		fadeMs.value = FADE_MS[mode] ?? 0;
		phase.value = "rest";
	}

	// ---- pending: the import, then record_shown, both inside one deadline ------------
	// The server slot is asked for only once there is art to play, so a slow or failed
	// import (or a settle while it is still loading) never uses a slot without a play.
	function begin() {
		if (phase.value !== "pending" || deadline !== null || waitingToShow) return;
		if (doc && doc.visibilityState !== "visible") {
			// Opened in a background tab: nothing to show yet, and no deadline running.
			reason.value = "hidden";
			waitingToShow = true;
			doc.addEventListener("visibilitychange", onShown);
			return;
		}
		// No room (a short screen, landscape): rest before any art is loaded or slot used.
		if (!fits()) return giveUp(run, "early", "no-room");
		const a = answer();
		const mine = ++run;
		deadline = setTimeout(() => giveUp(mine, "early", "deadline"), deadlineMs);
		// The scene waits for the fonts too: a late font swap resizes the heading and the
		// art would be drawn for the wrong layout. Inside the same deadline, so fonts that
		// never arrive mean no scene, like a slow import. A fonts promise that rejects (or a
		// fontsReady that throws) must not stop the scene: the layout is then what it is.
		const fonts = Promise.resolve()
			.then(fontsReady)
			.catch(() => null);
		Promise.all([load(a.design), fonts]).then(([mod]) => {
			if (mine !== run || phase.value !== "pending") return;
			if (!mod) return giveUp(mine, "early", "load-failed");
			// The page may have moved while the art loaded (starters arriving, a rotation): ask
			// once more, before the showing is used.
			if (!fits()) return giveUp(mine, "early", "no-room");
			Promise.resolve()
				.then(recordShown)
				.then(
					(r) => ({ reply: r }),
					(error) => ({ error })
				)
				.then((result) => {
					spentFor = a; // any reply, or a failure, uses this answer up
					decide(mine, a, mod, result);
				});
		});
	}

	function onShown() {
		if (!doc || doc.visibilityState !== "visible") return;
		stopWaiting();
		begin();
	}

	function stopWaiting() {
		if (!waitingToShow) return;
		waitingToShow = false;
		doc.removeEventListener("visibilitychange", onShown);
	}

	function decide(mine, a, mod, result) {
		if (mine !== run || phase.value !== "pending") return;
		let go = !!result.reply && result.reply.play === true;
		if (result.error) {
			// A fast network error: play, and remember it here instead.
			go = storage ? mayPlayLocal(storage, now(), a.tz) : true;
			if (go && storage) recordLocal(storage, now(), a.tz);
		}
		if (go) startScene(mod, a, false);
		else giveUp(mine, "early", "declined");
	}

	// `why` is the reason for support (see reason); the person's own settle counts as declined.
	function giveUp(mine, mode = "early", why = "declined") {
		if (mine !== run || phase.value !== "pending") return;
		run++;
		clearTimeout(deadline);
		deadline = null;
		stopWaiting();
		// No room is a fact about this screen, not an answer: the next new chat may have room.
		if (why !== "no-room") spentFor = answer();
		fadeMs.value = 0;
		reason.value = why;
		rest();
		phase.value = "rest";
	}

	// ---- playing ---------------------------------------------------------------------
	function startScene(mod, a, replay) {
		clearTimeout(deadline);
		deadline = null;
		const el = host();
		if (!el) return giveUp(run, "early", "declined");
		// The scenes before this one are over (a replay waits for that); this closes their
		// last timer (the smile's) and drops them.
		started.splice(0).forEach((old) => old.settle("instant"));
		phase.value = replay ? "replaying" : "playing";
		reason.value = "loaded";
		try {
			const s = mod.play(el, {
				layout: "phone",
				getBoxes,
				year: a.year,
				idPrefix,
				reduced: false,
				onBeat,
				onError: (error) => failed(a.design, error),
				replay,
			});
			scene = s;
			started.push(s);
			s.done.then(() => {
				if (scene === s) finish();
			});
		} catch (error) {
			failed(a.design, error);
			scene = null;
			rest();
			phase.value = "rest";
		}
	}

	function finish() {
		scene = null;
		if (phase.value === "playing" || phase.value === "replaying") {
			rest();
			phase.value = "rest";
		}
		drain();
	}

	function failed(design, error) {
		reason.value = "threw";
		reportSceneFailure("play", design, error, report);
	}

	// ---- ending it -------------------------------------------------------------------
	/** End the scene: "early" (input, resize, ...) or "instant". Does nothing at rest. */
	function settle(mode = "early") {
		if (phase.value === "pending") return giveUp(run, mode);
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		const was = phase.value;
		if (scene) scene.settle(mode);
		if (phase.value === was) {
			// A scene that did not announce its ending still ends here.
			fadeMs.value = FADE_MS[mode] ?? 0;
			phase.value = "rest";
		}
	}

	/** The page layout changed under a playing scene: let it read the boxes again. */
	function relayout(boxes) {
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		if (scene && typeof scene.relayout === "function") scene.relayout(boxes);
	}

	/** The first pointerdown anywhere: ends the scene, and the click it starts must not act. */
	function onPointerDown() {
		swallowClick = phase.value !== "rest" && phase.value !== "idle";
		queuedReplay = false; // a new gesture; an older waiting click is forgotten
		settle("early");
	}

	/**
	 * A key anywhere ends the scene. Enter or Space on the mark (`startsMarkClick`) is also the
	 * start of the click that would replay it, so that click is swallowed like a tap's.
	 */
	function onKeyDown(startsMarkClick = false) {
		if (startsMarkClick) swallowClick = phase.value !== "rest" && phase.value !== "idle";
		settle("early");
	}

	/**
	 * The page asks this of a click on something a scene's end must not trigger (the starter
	 * cards): true once after a pointerdown that ended or hit a scene.
	 */
	function takeEndingTap() {
		const was = swallowClick;
		swallowClick = false;
		return was;
	}

	/** Tapping the mark at rest replays it with the cards showing; mid-scene it only settled. */
	async function replay() {
		if (takeEndingTap()) return;
		if (phase.value !== "rest") return;
		if (reduced() || !fits()) return smile();
		// The last scene is still fading out: start after it.
		if (scene) {
			queuedReplay = true;
			return;
		}
		await playReplay();
	}

	/** Runs the replay that was tapped too early, once nothing of the last scene is left. */
	function drain() {
		if (!queuedReplay || scene) return;
		queuedReplay = false;
		void playReplay();
	}

	async function playReplay() {
		const a = answer();
		const mod = await load(a.design);
		if (!mod || phase.value !== "rest" || stopped || scene) return;
		mark.item = false;
		startScene(mod, a, true);
	}

	function smile() {
		clearTimeout(smileTimer);
		mark.happy = true;
		smileTimer = setTimeout(() => {
			smileTimer = null;
			mark.happy = false;
		}, SMILE_MS);
	}

	let stopped = false;
	/** The page is leaving: end everything now and leave no timer behind. */
	function stop() {
		stopped = true;
		run++;
		clearTimeout(deadline);
		clearTimeout(smileTimer);
		deadline = null;
		smileTimer = null;
		queuedReplay = false;
		stopWaiting();
		started.forEach((s) => s.settle("instant"));
		scene = null;
		if (phase.value !== "rest") phase.value = "rest";
	}

	return {
		phase,
		mark,
		text,
		fadeMs,
		reason,
		cardsHidden,
		begin,
		settle,
		relayout,
		onPointerDown,
		onKeyDown,
		takeEndingTap,
		replay,
		stop,
	};
}
