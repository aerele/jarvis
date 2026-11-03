// What the Desk art bundle (jarvis_holiday_touch.bundle.js) puts on the page. The widget
// loads that bundle by its own <script> tag and reads this global; a version it does not
// know is treated as a failed load (holiday_art_loader.mjs). Bump the version here and
// there together.
import { LOOK_AT, installWornCss, wornMarkup } from "./worn.mjs";
import { playParticles } from "./mini-fx.mjs";

export const ART_VERSION = 1;

export function installHolidayArt(win) {
  win.__jarvisHolidayArt = {
    v: ART_VERSION,
    wornMarkup,
    installWornCss,
    LOOK_AT,
    particles: playParticles,
  };
}
