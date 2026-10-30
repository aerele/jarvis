// What the mini chat's welcome does on a holiday: the hero smiles with its item, and on a
// day or birthday the holiday's particles drift down behind it. No full scene: people open
// this panel mid-work. Text only (the art arrives through holiday.ensureArt); Panel.vue
// gives it the svg to draw into and a callback for the smile.
//
// One "welcome open" is one call to open(): the welcome block becoming visible while the
// panel is open. close() ends everything and leaves no timer behind. Every async step
// checks it still belongs to the current open, so a late reply draws nothing.
//
// One deadline (DEADLINE_MS) from the open covers the art. The smile and the particles start
// together, once the art is in and, on a day or birthday, the server has answered. Art that is
// not in by the deadline means no smile, now or later. A reply that misses the deadline still
// smiles; if it turns out to be `play: true` while this same welcome is open, the particles
// start then (the server has already spent the slot).
//
// A click on the hero replays smile and particles without asking the server (uncounted).
import { DEADLINE_MS } from "./holiday_celebration.mjs";

export const SMILE_MS = 2400;
const SMILE_KINDS = ["leadup", "day", "birthday"];
const CELEBRATE_KINDS = ["day", "birthday"];

export function createWelcomeFx({
  holiday, // the Desk holiday state (holiday_state.mjs)
  getFxEl, // () => the welcome's particle svg, or null
  setSmiling, // (bool) => void
  reduced = () => false,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  deadlineMs = DEADLINE_MS,
}) {
  let token = 0;
  let timer = null;
  let cancelWait = null; // gives up on the art wait without waiting for the deadline
  let particles = null;
  let smiling = false;

  function showSmile(on) {
    if (smiling === on) return;
    smiling = on;
    setSmiling(on);
  }

  const view = () => holiday.view.value;
  const wants = () => SMILE_KINDS.includes(view().kind) && view().worn === true;

  function stopVisuals() {
    if (cancelWait) cancelWait();
    if (timer !== null) clearTimer(timer);
    timer = null;
    if (particles) particles.cancel();
    particles = null;
    showSmile(false);
  }

  // Smile with the item; reduced motion holds the smiling frame until close().
  function smile() {
    if (timer !== null) clearTimer(timer);
    showSmile(true);
    timer = reduced()
      ? null
      : setTimer(() => {
          timer = null;
          showSmile(false);
        }, SMILE_MS);
  }

  function drop(art) {
    const el = getFxEl();
    if (particles) particles.cancel();
    particles =
      el && art && art.particles ? art.particles(el, view().design) : null;
  }

  // The art, or null: it failed, the deadline passed first, or this open was abandoned.
  function artWithinDeadline() {
    return new Promise((resolve) => {
      let waitTimer = setTimer(() => finish(null), deadlineMs);
      function finish(art) {
        if (waitTimer === null) return;
        clearTimer(waitTimer);
        waitTimer = null;
        cancelWait = null;
        resolve(art);
      }
      cancelWait = () => finish(null);
      Promise.resolve(holiday.ensureArt()).then(finish, () => finish(null));
    });
  }

  async function open() {
    const mine = ++token;
    stopVisuals();
    if (!wants()) return;
    const celebrating = CELEBRATE_KINDS.includes(view().kind)
      ? holiday.celebrate()
      : null;
    const [art, reply] = await Promise.all([artWithinDeadline(), celebrating]);
    if (mine !== token || !art) return;
    smile();
    if (reply && reply.play === true && !reduced()) drop(art);
    else if (reply && reply.late) playIfLate(reply.late, art, mine);
  }

  // The reply missed the deadline: if it says play and this welcome is still the open one, play.
  function playIfLate(late, art, mine) {
    late.then((outcome) => {
      if (mine === token && outcome && outcome.play === true && !reduced())
        drop(art);
    });
  }

  async function replay() {
    const mine = ++token;
    stopVisuals();
    if (!wants()) return;
    const art = await artWithinDeadline();
    if (mine !== token || !art) return;
    smile();
    if (CELEBRATE_KINDS.includes(view().kind) && !reduced()) drop(art);
  }

  function close() {
    token++;
    stopVisuals();
  }

  return { open, replay, close };
}
