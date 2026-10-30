// What the mini chat's welcome does on a holiday: the hero smiles with its item, and on a
// day or birthday the holiday's particles drift down behind it. No full scene: people open
// this panel mid-work. Text only (the art arrives through holiday.ensureArt); Panel.vue
// gives it the svg to draw into and a callback for the smile.
//
// One "welcome open" is one call to open(): the welcome block becoming visible while the
// panel is open. close() ends everything and leaves no timer behind. Every async step
// checks it still belongs to the current open, so a late reply draws nothing.
//
// A click on the hero replays smile and particles without asking the server (uncounted).
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
}) {
  let token = 0;
  let timer = null;
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

  async function open() {
    const mine = ++token;
    stopVisuals();
    if (!wants()) return;
    const celebrate = CELEBRATE_KINDS.includes(view().kind)
      ? holiday.celebrate()
      : null;
    const art = await holiday.ensureArt();
    if (mine !== token) return;
    if (art) smile();
    const reply = await celebrate;
    if (mine !== token || !reply || reply.play !== true || reduced()) return;
    drop(art);
  }

  async function replay() {
    const mine = ++token;
    stopVisuals();
    if (!wants()) return;
    const art = await holiday.ensureArt();
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
