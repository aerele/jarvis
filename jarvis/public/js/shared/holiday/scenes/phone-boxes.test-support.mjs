// The boxes a phone welcome screen reports, for the tests and for any script that renders the
// scenes at phone sizes (no test runner imports, so a browser bundle can take it). Sizes follow the PWA welcome: a 56 px
// mark, a 21 px greeting (20ch wide, up to two lines), a 14 px sub-line, the starter cards
// in a band above the composer, which the art layer stops short of. With no starters (a screen
// 640 px tall or less hides them) the band is what is left between the hero and the composer.
//
// The sizes the page's CSS sets are exported in PAGE, and pwa/src/lib/holidayBoxes.test.js
// checks them against pwa/src/views/NewChatView.vue. The rest depend on content and are
// measured on the real page (Chrome, 390 x 844), so they are kept here by hand: the bar,
// the composer with its safe area, the hero and the starter band.
export const PAGE = { MARK_PX: 56, HERO_GAP_PX: 14, GREETING_PX: 21 };
const BAR_PX = 56; // the app bar the art layer runs under
const COMPOSER_PX = 118; // composer and safe area: the layer ends above them
const HERO_H = 150;
const HERO_W = 260;
const STARTER_BAND_PX = { roomy: 200, short: 150 };
const GAP_PX = 8;

/**
 * Boxes for a phone `w` x `h` px. `starters` true puts the starter cards band at the bottom
 * of the layer; false leaves the band as the space under the hero. `markY` overrides where
 * the hero is centred in the space above the band. `lines` is the greeting's line count (a
 * long day greeting wraps to two and makes the hero 26 px taller).
 */
export function phoneBoxes({
  w = 390,
  h = 844,
  starters = true,
  markY,
  lines = 1,
} = {}) {
  const heroH = HERO_H + (lines - 1) * 26;
  const hostH = h - COMPOSER_PX;
  const bandH = h >= 800 ? STARTER_BAND_PX.roomy : STARTER_BAND_PX.short;
  const bandTop = starters ? hostH - GAP_PX - bandH : hostH - GAP_PX;
  const y =
    markY ?? Math.round(BAR_PX + (bandTop - GAP_PX - BAR_PX - heroH) / 2);
  const hero = { x: (w - HERO_W) / 2, y, w: HERO_W, h: heroH };
  const cards = starters
    ? { x: 14, y: bandTop, w: w - 28, h: bandH }
    : {
        x: 14,
        y: y + heroH + 16,
        w: w - 28,
        h: hostH - GAP_PX - (y + heroH + 16),
      };
  return {
    host: { w, h: hostH },
    mark: { x: (w - PAGE.MARK_PX) / 2, y, w: PAGE.MARK_PX, h: PAGE.MARK_PX },
    heading: {
      x: (w - 230) / 2,
      y: y + PAGE.MARK_PX + PAGE.HERO_GAP_PX,
      w: 230,
      h: 52,
    },
    hero,
    cards,
  };
}
